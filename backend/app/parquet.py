"""Parquet 读 / 写适配层。

集中管理日K / enriched parquet 物理 schema + 跨文件 schema 漂移兼容。

历史 issue (2026-09-23 17:20 / 18:43):
  老 ``kline_daily/*.parquet`` 写盘时 ``date`` 物理类型是 String,
  新版 schema 声明 Date; ``pl.scan_parquet(glob)`` 在 collect 时抛
  ``SchemaError: data type mismatch for column date: incoming: String
  != target: Date``, 整条 enriched 流水线失败。

修复策略:
  1. 不用 ``schema=`` 强声明列类型; 让物理 schema 自适配。
  2. 跨文件 glob 显式 per-file scan + ``pl.concat(how="diagonal_relaxed")``,
     这样 polars 不会在扫描阶段就要求所有文件 schema 严格一致。
  3. 每文件 collect_schema() 后, 如 ``date`` 不是 ``pl.Date``, 注入
     ``pl.col("date").cast(pl.Date, strict=False)`` 归一化 (Polars 1.x
     Deprecated 路径但仍可用, 升级到 2.x 前先静默 warning)。
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Iterable, Union

import polars as pl


DAILY_STORAGE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "raw_close": pl.Float64,
    "raw_high": pl.Float64,
    "raw_low": pl.Float64,
    "turnover_rate": pl.Float64,
    "consecutive_limit_ups": pl.UInt32,
    "consecutive_limit_downs": pl.UInt32,
    "quote_ts": pl.Int64,
}


ENRICHED_STORAGE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "raw_close": pl.Float64,
    "raw_high": pl.Float64,
    "raw_low": pl.Float64,
    "turnover_rate": pl.Float64,
    "consecutive_limit_ups": pl.UInt32,
    "consecutive_limit_downs": pl.UInt32,
    "quote_ts": pl.Int64,
}


# 兼容老入口: tests 仍引用
def _coerce_date_schema(schema: dict[str, pl.DataType]) -> dict[str, pl.DataType]:
    return dict(schema)


def _is_glob_pattern(source: Any) -> bool:
    """判断 source 是否是 glob 字符串 (含 * / ? / [...])。"""
    if not isinstance(source, (str, bytes, os.PathLike)):
        return False
    s = str(source)
    return any(c in s for c in ("*", "?", "["))


def _explode_to_files(source: Union[str, Path, Iterable[Any]]) -> list[Path]:
    """glob / 目录 / 单文件 / 文件列表 → 扁平 Path 列表, 按字典序排。

    支持 ``**`` (递归) 与 ``*`` (单层) 混合 glob, 例如
    ``/data/kline_daily/**/*.parquet``。
    """
    if isinstance(source, (str, bytes, os.PathLike)):
        s = str(source)
        p = Path(s)
        if not _is_glob_pattern(s):
            # 纯路径: 目录 / 文件
            if p.is_dir():
                return sorted(p.rglob("*.parquet"))
            if p.is_file():
                return [p]
            return []
        if "**" in s:
            # 含 ``**`` 的递归 glob, 把 ``**`` 之前的真实目录抠出来再用 rglob
            base_dir = s.split("**", 1)[0].rstrip("/")
            base_p = Path(base_dir)
            if not base_p.exists():
                return []
            return sorted(f for f in base_p.rglob("*.parquet") if f.is_file())
        # 单层 glob: 父目录需存在
        if p.parent.exists():
            return sorted(f for f in p.parent.glob(p.name) if f.is_file())
        return []
    if isinstance(source, Iterable):
        return list(source)
    return []


def _normalize_date_column(lf: pl.LazyFrame) -> pl.LazyFrame:
    """在 LazyFrame 层把 ``date`` 列规整成 ``pl.Date``。

    兼容 Date / String / Datetime / Timestamp 多版本物理 schema。
    ``cast(pl.Date, strict=False)`` 在 1.x 仍可用且同时接受 Date(no-op) 与
    String 解析; 2.0 起移除后改 ``.str.to_date`` (届时再适配)。
    """
    try:
        schema = lf.collect_schema()
    except Exception:
        return lf
    if "date" not in schema:
        return lf
    if schema["date"] is pl.Date:
        return lf
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return lf.with_columns(pl.col("date").cast(pl.Date, strict=False))


def _scan_with_date_coercion(source: Any) -> pl.LazyFrame:
    """核心: 把扫描入口兼容跨文件 schema 漂移。

    - glob 字符串/目录 → per-file scan + 归一化 + diagonal_relaxed
    - 单文件 / Path 列表 → 直接 pl.scan_parquet + 归一化
    """
    files = _explode_to_files(source)
    if not files:
        # 退化为 polars 原生 scan (会抛错, 但至少报错信息更清楚)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            lf = pl.scan_parquet(source)
        return _normalize_date_column(lf)

    if len(files) == 1:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            lf = pl.scan_parquet(str(files[0]))
        return _normalize_date_column(lf)

    # 多个文件: per-file scan + 归一化 + diagonal_relaxed
    # 这样 polars 不会在 scan 阶段要求所有文件 schema 严格一致。
    parts: list[pl.LazyFrame] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for fp in files:
            try:
                lf = pl.scan_parquet(str(fp))
                parts.append(_normalize_date_column(lf))
            except Exception as e:
                # 单文件坏掉不应阻断其他文件, 仅记到 stderr 调试
                import sys
                print(f"[parquet] 跳过损坏文件 {fp}: {e}", file=sys.stderr)
    if not parts:
        return pl.LazyFrame()  # 空
    return pl.concat(parts, how="diagonal_relaxed")


def scan_parquet_compat(source: Any, **kwargs: Any) -> pl.LazyFrame:
    """跨文件 schema 漂移兼容入口。

    任意 source (单文件/目录/glob/文件列表) + 任意 kwargs
    (cast_options / extra_columns / ...) 都走 per-file scan:
    ``pl.scan_parquet(fp, **kwargs)`` 逐文件, ``diagonal_relaxed`` 拼合,
    polars 不再以首个文件 schema 强制要求所有文件一致,新写入文件多出列
    (如 ``quote_ts``) 也能与历史 8 列文件共存。

    不再区分 kwargs 是否为空 — 历史文件本来就靠 per-file 兼容拼合。
    保留 ``_normalize_date_column`` 把 ``String``/``Date`` 归一为 ``Date``。
    """
    files = _explode_to_files(source)
    if not files:
        return pl.LazyFrame()
    parts: list[pl.LazyFrame] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for fp in files:
            try:
                lf = pl.scan_parquet(str(fp), **kwargs)
                parts.append(_normalize_date_column(lf))
            except Exception as e:  # noqa: BLE001
                # 单文件坏掉不应阻断其他文件, 仅记到 stderr 调试
                import sys
                print(f"[parquet] 跳过损坏文件 {fp}: {e}", file=sys.stderr)
    if not parts:
        return pl.LazyFrame()
    return pl.concat(parts, how="diagonal_relaxed")


def scan_daily_parquet(source: Any, **kwargs: Any) -> pl.LazyFrame:
    return scan_parquet_compat(source, **kwargs)


def scan_enriched_parquet(source: Any, **kwargs: Any) -> pl.LazyFrame:
    return scan_parquet_compat(source, **kwargs)


import os  # 必须在 _is_glob_pattern / _explode_to_files 用到
