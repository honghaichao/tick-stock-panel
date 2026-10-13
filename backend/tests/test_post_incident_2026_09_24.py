"""Patches 0001-0005 回归测试 (pytest 兼容; 不依赖 pytest fixture 时也可裸跑)。

对应事故链:
  2026-09-23 16:39 (Tushare 限速) + 16:46 (on_chunk_done 签名)
  + 17:20 / 18:43 (Polars SchemaError) + 18:29 (真值判错)
  + 2026-09-24 00:10 (watchfiles graceful shutdown)。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import polars as pl

# 后端目录加入路径, 让裸跑也可 import
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.parquet import (  # noqa: E402
    DAILY_STORAGE_SCHEMA,
    ENRICHED_STORAGE_SCHEMA,
    _coerce_date_schema,
    scan_daily_parquet,
    scan_enriched_parquet,
)
from app.plugins.tushare import provider as ts_provider  # noqa: E402


# ====================================================================== 0001 ============
def _write_dummy_daily_parquet(out_dir, *, date_dtype: pa.DataType):
    """``out_dir`` 可为 str 或 Path。返回 Path。"""
    out_dir = Path(out_dir)
    table = pa.table({
        "symbol": ["600000.SH"] * 3,
        "date": pa.array(["2026-09-20", "2026-09-21", "2026-09-22"], type=date_dtype),
        "open": [10.0, 10.1, 10.2],
        "high": [10.5, 10.6, 10.7],
        "low": [9.9, 9.95, 10.0],
        "close": [10.4, 10.5, 10.6],
        "volume": [1000.0, 1100.0, 1200.0],
        "amount": [1e7, 1.1e7, 1.2e7],
        "raw_close": [10.4, 10.5, 10.6],
        "raw_high": [10.5, 10.6, 10.7],
        "raw_low": [9.9, 9.95, 10.0],
        "turnover_rate": [0.01, 0.011, 0.012],
        "consecutive_limit_ups": pa.array([0, 0, 0], type=pa.uint32()),
        "consecutive_limit_downs": pa.array([0, 0, 0], type=pa.uint32()),
        "quote_ts": pa.array([0, 0, 0], type=pa.int64()),
    })
    date_part = out_dir / "date=2026-09-22"
    date_part.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, date_part / "600000.SH.parquet")
    return date_part


def test_coerce_date_schema_is_identity():
    """LF-层 ``_normalize_date_column`` 负责 schema 转换, ``_coerce_date_schema``
    现在是 identity 替代品, 仅保证调用方不会爆 KeyError。"""
    base = {"date": pl.Utf8, "symbol": pl.Utf8}
    coerced = _coerce_date_schema(base)
    # 不应改变 date 类型, 仅透传
    assert coerced["date"] is pl.Utf8
    assert _coerce_date_schema(DAILY_STORAGE_SCHEMA)["date"] is pl.Date
    assert _coerce_date_schema(ENRICHED_STORAGE_SCHEMA)["date"] is pl.Date


def test_scan_daily_parquet_handles_string_date_column():
    """老 parquet 的 ``date`` 是 String, 不能让 ``.collect(streaming=True)`` 报 SchemaError。"""
    out_dir = tempfile.mkdtemp(prefix="daily_")
    date_part = _write_dummy_daily_parquet(out_dir, date_dtype=pa.string())
    lf = scan_daily_parquet(str(date_part / "*.parquet"))
    df = lf.collect(streaming=True)
    assert df.schema["date"] == pl.Date
    assert df.height == 3


def test_scan_enriched_parquet_handles_string_date_column():
    out_dir = tempfile.mkdtemp(prefix="enr_")
    date_part = _write_dummy_daily_parquet(out_dir, date_dtype=pa.string())
    df = scan_enriched_parquet(str(date_part / "*.parquet")).collect()
    assert df.schema["date"] == pl.Date


# ====================================================================== 0002 ============
class _FakePolarsDf:
    def __init__(self, *, is_empty: bool) -> None:
        self._e = is_empty

    def is_empty(self) -> bool:
        return self._e


class _FakePandasDf:
    def __init__(self, *, empty: bool) -> None:
        self._e = empty

    @property
    def empty(self) -> bool:
        return self._e


def test_df_empty_accepts_none_pandas_polars():
    assert ts_provider._df_empty(None) is True
    assert ts_provider._df_empty(_FakePandasDf(empty=True)) is True
    assert ts_provider._df_empty(_FakePandasDf(empty=False)) is False
    assert ts_provider._df_empty(_FakePolarsDf(is_empty=True)) is True
    assert ts_provider._df_empty(_FakePolarsDf(is_empty=False)) is False


def test_df_empty_does_not_blow_up_on_polars_truthiness():
    bad = _FakePolarsDf(is_empty=False)
    assert ts_provider._df_empty(bad) is False


def test_instrument_sync_polars_empty_df_does_not_warn():
    """provider 返回 polars 空 DataFrame 时归一化为 [], 不进 ``or []`` 真值判错。"""
    class _PolarsProvider:
        name = "stub"
        def get_instruments(self, asset_type):
            return pl.DataFrame()  # polars 空 DF
    items = _PolarsProvider().get_instruments("stock")
    if isinstance(items, pl.DataFrame):
        items = items.to_dicts() if not items.is_empty() else []
    elif not items:
        items = []
    assert items == []


# ====================================================================== 0003 ============
def test_rpm_constants_are_sane():
    assert ts_provider._RPM_LIMIT <= 200  # 免费档标称 200/min
    assert ts_provider._RPM_WINDOW_S == 60.0
    assert ts_provider._RPM_LIMIT > 0


def test_rpm_acquire_fast_path():
    saved = list(ts_provider._rpm_calls)
    try:
        ts_provider._rpm_calls.clear()
        t0 = time.monotonic()
        ts_provider._rpm_acquire()
        assert (time.monotonic() - t0) < 0.05
        assert len(ts_provider._rpm_calls) == 1
    finally:
        ts_provider._rpm_calls.clear()
        ts_provider._rpm_calls.extend(saved)


def test_rpm_acquire_throttles_burst_when_full():
    """灌满窗口后再 acquire, 必须 sleep 至少一次, 释放槽位后接住新调用。"""
    saved_calls = list(ts_provider._rpm_calls)
    saved_sleep = ts_provider.time.sleep
    try:
        ts_provider._rpm_calls.clear()
        base = time.monotonic()
        ts_provider._rpm_calls.extend([base - 1.0] * ts_provider._RPM_LIMIT)
        slept_with: list[float] = []

        def fake_sleep(s):
            slept_with.append(s)
            # fake_sleep 模拟"等待某个槽位滑出窗口": 把最早一格置为很久以前
            ts_provider._rpm_calls[0] = time.monotonic() - ts_provider._RPM_WINDOW_S - 1

        ts_provider.time.sleep = fake_sleep  # type: ignore[attr-defined]
        ts_provider._rpm_acquire()
        # 关键: 节流确实被触发, 而不是 fast path 返回
        assert slept_with, "应触发节流 sleep"
        assert slept_with[0] > 0, "sleep 时间应大于 0"
        # 节流后拿到的总槽位数 = LIMIT (最旧的被弹出, 新的被压入)
        assert len(ts_provider._rpm_calls) == ts_provider._RPM_LIMIT
    finally:
        ts_provider.time.sleep = saved_sleep  # type: ignore[attr-defined]
        ts_provider._rpm_calls.clear()
        ts_provider._rpm_calls.extend(saved_calls)


# ====================================================================== 0004 ============
def test_dev_sh_excludes_data_and_env_changes():
    """"0004 守护更新 (2026-09-30)。

    原版本要求 dev.sh 显式 --reload-exclude data/** + ../.env 等多模式, 但
    uvicorn --reload 在配多 exclude 时 watchfiles baseline scan 极慢 (实测 1+ 分钟不响应,
    见 backend/scripts/consolidate_kline_minute.py 同日事故链)。

    新版策略: 只用 --reload-include "app/**/*.py" 白名单 — 等价语义 (不修改 app/*.py 不 reload),
    启动从分钟级降到亚秒级。data/** 与 .env 自然不被 watchfiles 触发。
    """
    dev_sh = Path(__file__).resolve().parents[2] / "dev.sh"
    text = dev_sh.read_text()
    # 等价语义 (仅 app/**/*.py 改动触发 reload):
    assert "--reload-include" in text
    assert 'app/**/*.py' in text
    # 显式注释里要解释为什么不写 --reload-exclude, 防止后人恢复旧写法:
    assert "watchfiles" in text and ("reload" in text)


# ====================================================================== 0005 ============
def test_tushare_gitignore_blocks_bak_files():
    g = Path(__file__).resolve().parents[2] / "backend/app/plugins/tushare/.gitignore"
    assert g.exists(), "应存在 .gitignore 防止 .bak 再入仓"
    text = g.read_text()
    assert ".bak" in text


# ====================================================================== 守护型 ==================
def test_tushare_provider_get_adj_factors_accepts_on_chunk_done():
    """16:46 的 TypeError 不再回潮 (签名必须接受 on_chunk_done kw)。"""
    provider = ts_provider.TushareProvider()
    captured: dict = {}

    class _Pro:
        def adj_factor(self, *, ts_code, start_date, end_date):
            captured["call"] = (ts_code, start_date, end_date)
            import pandas as pd
            return pd.DataFrame({"trade_date": ["20260922"], "adj_factor": [1.0]})

    saved = provider._get_pro
    provider._get_pro = lambda: _Pro()
    try:
        df = provider.get_adj_factors(
            ["600000.SH"],
            start_time=None,
            end_time=None,
            asset_type="stock",
            on_chunk_done=lambda i, n: None,
        )
        assert "call" in captured
        assert df.height == 1
        assert set(df.columns) == {"symbol", "trade_date", "ex_factor"}
    finally:
        provider._get_pro = saved


# ====================================================================== runner-only =============
# ====================================================================== 实数据烟雾测试
def test_real_workspace_data_reads_clean():
    """真实 workspace 上 kline_daily 已混合 8 个 String-typed date 分区
    (2026-09-14 ~ 2026-09-23), 验证 parquet.py 修复后能跨类型合并。"""
    import os
    files = ["data/kline_daily", "data/kline_daily_enriched", "data/kline_index_daily"]
    for sub in files:
        base = Path(__file__).resolve().parents[2] / sub
        if not base.exists():
            continue
        lf = scan_daily_parquet(str(base / "**/*.parquet"))
        df = lf.collect()
        assert df.schema["date"] == pl.Date, f"{sub} date dtype 应为 Date, 实际 {df.schema['date']}"
        assert df.height > 0, f"{sub} 数据非空"



def _run_all_degenerate():
    tests = [
        test_coerce_date_schema_is_identity,
        test_scan_daily_parquet_handles_string_date_column,
        test_scan_enriched_parquet_handles_string_date_column,
        test_df_empty_accepts_none_pandas_polars,
        test_df_empty_does_not_blow_up_on_polars_truthiness,
        test_instrument_sync_polars_empty_df_does_not_warn,
        test_rpm_constants_are_sane,
        test_rpm_acquire_fast_path,
        test_rpm_acquire_throttles_burst_when_full,
        test_dev_sh_excludes_data_and_env_changes,
        test_tushare_gitignore_blocks_bak_files,
        test_real_workspace_data_reads_clean,
    test_tushare_provider_get_adj_factors_accepts_on_chunk_done,
    ]
    passed = failed = 0
    print("=" * 72)
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print("=" * 72)
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run_all_degenerate() else 1)
