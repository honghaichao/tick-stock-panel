"""分钟 K 扫描 schema 异构修复回归。

背景: 旧 symbol= 分区迁移到 date= 分区时, _migrate_symbol_to_date_partition 保留了旧
分区里的 `date` 列, 但 _write_minute_partition 写的新分区没有该列。混扫 glob 时 polars
抛 "extra column in file outside of expected schema: date", 撮合轮询
(repo.get_minute_batch) 整个 try/except 兜底返回空表, 委托永远停在 new。
修复: scan_parquet(..., extra_columns="ignore")。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.tickflow.repository import DataStore, KlineRepository


def _mk_store_with_hetero_schema(root: Path) -> DataStore:
    """手工造一个数据目录: 一个旧分区(带 date 列) + 一个新分区(无 date 列)。"""
    (root / "kline_minute" / "date=2026-09-27" / "symbol=600519.SH").mkdir(parents=True)
    (root / "kline_minute" / "date=2026-09-28").mkdir(parents=True)

    old = pl.DataFrame({
        "symbol": ["600519.SH"] * 3,
        "datetime": [
            datetime(2026, 9, 27, 9, 30),
            datetime(2026, 9, 27, 9, 31),
            datetime(2026, 9, 27, 9, 32),
        ],
        "open": [1.0, 1.0, 1.0],
        "high": [1.0, 1.0, 1.0],
        "low": [1.0, 1.0, 1.0],
        "close": [1.0, 1.0, 1.0],
        "volume": [0.0, 0.0, 0.0],
        "amount": [0.0, 0.0, 0.0],
        "date": [date(2026, 9, 27), date(2026, 9, 27), date(2026, 9, 27)],
    })
    old.write_parquet(root / "kline_minute" / "date=2026-09-27" / "symbol=600519.SH" / "part.parquet")

    new = pl.DataFrame({
        "symbol": ["600519.SH"] * 2,
        "datetime": [
            datetime(2026, 9, 28, 9, 30),
            datetime(2026, 9, 28, 9, 31),
        ],
        "open": [2.0, 2.0],
        "high": [2.0, 2.0],
        "low": [2.0, 2.0],
        "close": [2.0, 2.0],
        "volume": [0.0, 0.0],
        "amount": [0.0, 0.0],
    })
    new.write_parquet(root / "kline_minute" / "date=2026-09-28" / "part.parquet")

    return DataStore(root)


def test_get_minute_batch_tolerates_hetero_schema(tmp_path):
    """撮合调用 get_minute_batch 时, 即使新旧分区 schema 不一致也不能抛空。"""
    store = _mk_store_with_hetero_schema(tmp_path)
    repo = KlineRepository(store)
    df = repo.get_minute_batch(["600519.SH"], date(2026, 9, 28))
    assert df.height == 2, f"应读到新分区 2 行, 实际 {df.height}"
    assert df["datetime"].dt.date().unique().to_list() == [date(2026, 9, 28)]


def test_get_minute_tolerates_hetero_schema(tmp_path):
    """单股 minute (用于 fetcher) 也要能跨异构分区读。"""
    store = _mk_store_with_hetero_schema(tmp_path)
    repo = KlineRepository(store)
    df = repo.get_minute("600519.SH", date(2026, 9, 27))
    assert df.height == 3
    df = repo.get_minute("600519.SH", date(2026, 9, 28))
    assert df.height == 2


def test_get_minute_range_tolerates_hetero_schema(tmp_path):
    """get_minute_range (分钟 K 精确回测用) 同样要走 extra_columns=ignore。"""
    store = _mk_store_with_hetero_schema(tmp_path)
    repo = KlineRepository(store)
    df = repo.get_minute_range(["600519.SH"], date(2026, 9, 27), date(2026, 9, 28))
    # 不该被 schema 异构熔断 — 至少读到旧分区的 3 行
    assert df.height >= 3
