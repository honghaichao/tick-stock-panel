"""extend_history 的 DuckDB view 刷新测试。

回归 Bug: extend_history._refresh_single_view 字面量里引用了一个未定义的
``_extend_history_km_globs``,任何 extend_history 任务 (日 K 写完后刷视图)
都会在构造 paths 字典时抛 NameError,任务必败。

正确实现应复用 ``repository._kline_minute_glob_list``,与
``repository.refresh_views`` 走同一份 glob 构造器,避免 kline_minute
视图永远只能由 /api/pipeline/run 端点创建/刷新。

测试覆盖:
  - consolidated 布局 (date=*/part.parquet): 刷 kline_minute view 不抛 NameError
    且 view 真的能从 DuckDB 里 SELECT 出数据。
  - 刷其他 view (kline_daily) 不受 kline_minute glob 构造影响 — 验证回归点
    不仅限于 kline_minute 名,修复后任意 view 都能刷。
"""
from __future__ import annotations

from datetime import datetime as _dt

import polars as pl
import pytest

from app.services import extend_history
from app.tickflow.repository import DataStore, KlineRepository


def _minute_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600519.SH"],
        "datetime": [_dt(2026, 9, 30, 9, 30)],
        "open": [10.0], "high": [10.5], "low": [9.5], "close": [10.2],
        "volume": [100.0], "amount": [1020.0],
    })


def _seed_consolidated_minute(tmp_path) -> None:
    """放置一个 consolidation 后的 date=2026-09-30/part.parquet。"""
    out = tmp_path / "kline_minute" / "date=2026-09-30" / "part.parquet"
    out.parent.mkdir(parents=True)
    _minute_frame().write_parquet(out)


def _seed_consolidated_daily(tmp_path) -> None:
    """放置一个 date=2026-09-30/part.parquet 给 kline_daily 视图用。"""
    out = tmp_path / "kline_daily" / "date=2026-09-30" / "part.parquet"
    out.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600519.SH"], "date": [_dt(2026, 9, 30).date()],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1],
    }).write_parquet(out)


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def test_refresh_kline_minute_view_consolidated_layout(repo):
    _seed_consolidated_minute(repo.store.data_dir)

    # 关键回归点: 修复前会抛 NameError,修复后必须不抛。
    extend_history._refresh_single_view(repo, "kline_minute")

    rows = repo.db.execute("SELECT symbol FROM kline_minute").fetchall()
    assert rows == [("600519.SH",)]


def test_refresh_other_view_does_not_require_kline_minute_path(repo):
    """修复前的 NameError 在构造 paths 字典时就抛,任意 view 都会挂。

    这里给一个最小的 kline_daily 数据,验证修复后刷 kline_daily 视图能成功,
    也间接说明 kline_minute 的 glob 路径不再影响其他 view 的刷新。
    """
    _seed_consolidated_daily(repo.store.data_dir)

    extend_history._refresh_single_view(repo, "kline_daily")

    rows = repo.db.execute("SELECT symbol FROM kline_daily").fetchall()
    assert rows == [("600519.SH",)]
