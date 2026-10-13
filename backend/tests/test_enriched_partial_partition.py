"""#223 回归: 盘后管道必须识别并重算覆盖不全的 enriched 分区。

自选实时路径 (merge_live_enriched_asset) 会在全市场 enriched 生成前提前
创建当日分区 (只有几只自选); 仅按日期目录计数比较会把它误判为完整分区
而跳过计算, 造成日K连续缺失与均线错误。_prune_partial_enriched_partitions
按过滤停牌后的标的集合检查覆盖, 避免正确过滤的停牌记录导致反复删除重算。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.indicators import pipeline
from app.jobs import daily_pipeline
from app.jobs.daily_pipeline import _prune_partial_enriched_partitions
from app.services import data_integrity, preferences
from app.tickflow.capabilities import CapabilitySet


def _write_partition(base: Path, day: str, symbols: list[str]) -> None:
    part = base / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": symbols,
        "open": [1.0] * len(symbols),
        "high": [1.0] * len(symbols),
        "close": [1.0] * len(symbols),
        "volume": [100.0] * len(symbols),
        "amount": [100.0] * len(symbols),
    }).write_parquet(
        part / "part.parquet"
    )


def test_partial_partition_is_pruned(tmp_path) -> None:
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    # 2026-08-28: daily 全市场 100 只, enriched 只有实时写入的 5 只 (issue 实测形态)
    _write_partition(daily, "2026-08-28", [f"s{i:06d}" for i in range(100)])
    _write_partition(enriched, "2026-08-28", [f"s{i:06d}" for i in range(5)])
    # 前一日两边都是完整的 → 不动
    _write_partition(daily, "2026-08-27", ["a", "b"])
    _write_partition(enriched, "2026-08-27", ["a", "b"])

    pruned = _prune_partial_enriched_partitions(daily, enriched)

    assert pruned == ["2026-08-28"]
    assert not (enriched / "date=2026-08-28").exists()
    assert (enriched / "date=2026-08-27").exists()  # 完整分区保留


def test_complete_partitions_untouched(tmp_path) -> None:
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    syms = [f"s{i:06d}" for i in range(50)]
    _write_partition(daily, "2026-09-01", syms)
    _write_partition(enriched, "2026-09-01", syms)

    assert _prune_partial_enriched_partitions(daily, enriched) == []
    assert (enriched / "date=2026-09-01" / "part.parquet").exists()


def test_enriched_date_without_daily_is_left_alone(tmp_path) -> None:
    # 今日日K尚未同步时, 实时创建的当日分区留给当日正常流程处理
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    day = "2026-09-07"
    _write_partition(enriched, day, ["only_watchlist"])

    assert _prune_partial_enriched_partitions(daily, enriched) == []
    assert (enriched / f"date={day}").exists()


@pytest.mark.parametrize("legacy_halt", [False, True])
def test_computed_partition_with_halt_is_preserved_on_repeated_checks(tmp_path, monkeypatch, legacy_halt):
    monkeypatch.setattr(pipeline, "_custom_signal_exprs", {})
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    day = "2026-09-07"
    _write_partition(daily, day, ["600001.SH", "600002.SH"])
    raw_path = daily / f"date={day}" / "part.parquet"
    raw = pl.read_parquet(raw_path).with_columns(
        pl.lit(date.fromisoformat(day)).alias("date"),
        pl.lit(1.0).alias("low"),
        *[
            pl.when(pl.col("symbol") == "600002.SH").then(0.0).otherwise(pl.col(c)).alias(c)
            for c in (["volume", "amount"] if legacy_halt else ["open", "high", "volume", "amount"])
        ],
    )
    raw.write_parquet(raw_path)
    computed = pipeline._select_storage_cols(pipeline.compute_enriched(raw))
    assert computed["symbol"].to_list() == ["600001.SH"]
    target = enriched / f"date={day}" / "part.parquet"
    target.parent.mkdir(parents=True)
    computed.write_parquet(target)
    original = target.read_bytes()

    for _ in range(2):
        assert _prune_partial_enriched_partitions(daily, enriched) == []
        assert target.read_bytes() == original


@pytest.mark.parametrize("actual", [["a"], ["a", "a"], ["a", "extra"], ["a", "extra", "other"]])
def test_missing_active_symbol_is_not_hidden_by_counts(tmp_path, actual):
    daily = tmp_path / "daily"
    enriched = tmp_path / "enriched"
    _write_partition(daily, "2026-09-07", ["a", "b"])
    _write_partition(enriched, "2026-09-07", actual)

    assert _prune_partial_enriched_partitions(daily, enriched) == ["2026-09-07"]


def test_duplicate_daily_rows_do_not_require_duplicate_enriched_rows(tmp_path):
    daily = tmp_path / "daily"
    enriched = tmp_path / "enriched"
    _write_partition(daily, "2026-09-07", ["a", "a", "b"])
    _write_partition(enriched, "2026-09-07", ["a", "b"])

    assert _prune_partial_enriched_partitions(daily, enriched) == []


@pytest.mark.parametrize("missing", ["symbol", "open", "high"])
def test_missing_daily_required_column_does_not_delete(tmp_path, missing):
    daily = tmp_path / "daily"
    enriched = tmp_path / "enriched"
    _write_partition(daily, "2026-09-07", ["a", "b"])
    _write_partition(enriched, "2026-09-07", ["a"])
    raw_path = daily / "date=2026-09-07" / "part.parquet"
    pl.read_parquet(raw_path).drop(missing).write_parquet(raw_path)

    assert _prune_partial_enriched_partitions(daily, enriched) == []
    assert (enriched / "date=2026-09-07" / "part.parquet").exists()


@pytest.mark.parametrize("broken_kind", ["daily", "enriched"])
def test_unreadable_partition_does_not_delete(tmp_path, broken_kind):
    daily = tmp_path / "daily"
    enriched = tmp_path / "enriched"
    _write_partition(daily, "2026-09-07", ["a", "b"])
    _write_partition(enriched, "2026-09-07", ["a"])
    (tmp_path / broken_kind / "date=2026-09-07" / "broken.parquet").write_bytes(b"broken")

    assert _prune_partial_enriched_partitions(daily, enriched) == []
    assert (enriched / "date=2026-09-07" / "part.parquet").exists()


def test_partition_files_are_checked_together(tmp_path):
    daily = tmp_path / "daily"
    enriched = tmp_path / "enriched"
    _write_partition(daily, "2026-09-07", ["a", "b"])
    _write_partition(enriched, "2026-09-07", ["a"])
    target = enriched / "date=2026-09-07" / "second.parquet"
    pl.DataFrame({"symbol": ["b"]}).write_parquet(target)

    assert _prune_partial_enriched_partitions(daily, enriched) == []


@pytest.mark.parametrize("missing", ["volume", "amount"])
def test_legacy_daily_without_optional_halt_column(tmp_path, missing):
    daily = tmp_path / "daily"
    enriched = tmp_path / "enriched"
    _write_partition(daily, "2026-09-07", ["a", "b"])
    _write_partition(enriched, "2026-09-07", ["a", "b"])
    path = daily / "date=2026-09-07" / "part.parquet"
    pl.read_parquet(path).drop(missing).write_parquet(path)
    assert _prune_partial_enriched_partitions(daily, enriched) == []


def test_all_halted_partition_does_not_require_active_rows(tmp_path):
    daily = tmp_path / "daily"
    enriched = tmp_path / "enriched"
    _write_partition(daily, "2026-09-07", ["a", "b"])
    _write_partition(enriched, "2026-09-07", ["a"])
    path = daily / "date=2026-09-07" / "part.parquet"
    pl.read_parquet(path).with_columns(pl.lit(0.0).alias("open"), pl.lit(0.0).alias("high")).write_parquet(path)
    assert _prune_partial_enriched_partitions(daily, enriched) == []


def test_pruned_interior_date_is_rebuilt_without_new_daily_or_factors(tmp_path, monkeypatch):
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    dates = ["2026-09-01", "2026-09-02", "2026-09-03"]
    for day in dates:
        _write_partition(daily, day, ["a", "b"])
        _write_partition(enriched, day, ["a"] if day == dates[1] else ["a", "b"])

    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(preferences, "load", lambda: {
        "pipeline_pull_a_share": False, "pipeline_regime_enabled": False,
        "minute_sync_enabled": False, "adj_factor_provider": "tickflow",
    })
    monkeypatch.setattr(daily_pipeline.instrument_sync, "sync_instruments", lambda *_: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda *_: [])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda *_: None)
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *_, **__: [])
    calls = []

    def rebuild(**kwargs):
        calls.append(kwargs)
        assert kwargs["new_dates_only"] is True
        assert kwargs["symbols"] is None
        _write_partition(enriched, dates[1], ["a", "b"])
        return 2

    monkeypatch.setattr(daily_pipeline, "run_pipeline", rebuild)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           latest_daily_date=lambda: date.fromisoformat(dates[-1]))
    daily_pipeline.run_now(repo, CapabilitySet(set()))
    assert len(calls) == 1
    assert pl.read_parquet(enriched / f"date={dates[1]}" / "part.parquet")["symbol"].to_list() == ["a", "b"]
    daily_pipeline.run_now(repo, CapabilitySet(set()))
    assert len(calls) == 1



def test_apply_adj_factor_ratio_guards_against_infinite_cum_factor():
    """adj_factor 数据源异常 (Tushare ex_factor 累积 → inf): ratio 兜底 1.0, OHLC 写非 null.

    历史 issue (2026-10-12): 300006.SZ 等多次送转股的股票, adj_factor.all.parquet 的
    cum_factor 总累计非有限值 (1e17+, 1e30+). 修复前的 `fill_null(1.0) / fill_null(1.0)`
    对 inf 算不出, ratio=nan, open/high/low/close * NaN 写入 null → 下游 K 线图缺柱.
    修复后: cum/total 任一是 inf/nan 时回退 1.0, OHLC 保持 raw 值, 不前复权.
    """
    from app.indicators.pipeline import _apply_adj_factor

    # 模拟 300006.SZ 的疯狂 cum_factor: 总累计极大, 单日 cum 也是极大
    raw = pl.DataFrame({
        "symbol": ["300006.SZ"],
        "date": [date(2026, 4, 13)],
        "open": [5.73], "high": [6.07], "low": [5.58], "close": [5.99],
        "raw_close": [5.99],
    })
    factors = pl.DataFrame({
        "symbol": ["300006.SZ"],
        "trade_date": [date(2026, 4, 13)],
        "ex_factor": [1.0],  # 单日 ex_factor 看似正常
    })
    # 模拟 cum_factor 与 total_factor 都被上游算成 inf 的情形
    factors_inf = pl.DataFrame({
        "symbol": ["300006.SZ"],
        "trade_date": [date(2026, 4, 13)],
        "ex_factor": [float("inf")],
    })
    out = _apply_adj_factor(raw, factors_inf)
    row = out.row(0, named=True)
    # ratio 应被兜底为 1.0, OHLC 保持原值不变成 null
    assert row["open"] == 5.73
    assert row["high"] == 6.07
    assert row["low"] == 5.58
    assert row["close"] == 5.99
    assert row["raw_close"] == 5.99
    # sanity: 正常 factors 行为不变
    out2 = _apply_adj_factor(raw, factors)
    assert out2.row(0, named=True)["open"] == 5.73


def test_apply_adj_factor_ratio_guards_against_nan_cum_factor():
    """cum_factor = NaN 也走兜底 (上游 ex_factor 含 0 / - 异常值 → cum_prod NaN)."""
    from app.indicators.pipeline import _apply_adj_factor

    raw = pl.DataFrame({
        "symbol": ["X.SH"],
        "date": [date(2026, 4, 13)],
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
        "raw_close": [10.5],
    })
    factors = pl.DataFrame({
        "symbol": ["X.SH"],
        "trade_date": [date(2026, 4, 13)],
        "ex_factor": [float("nan")],
    })
    out = _apply_adj_factor(raw, factors)
    row = out.row(0, named=True)
    assert row["open"] == 10.0
    assert row["close"] == 10.5
    assert row["raw_close"] == 10.5


def test_apply_adj_factor_ratio_normal_case_unchanged():
    """正常 cumulative ex_factor 的前复权公式不被改坏.

    ex_factor 语义 (2026-10-12 之后): 自 IPO 起的累积复权因子, 每次除权事件后单调递增;
    per-event 噪声行 (ex_factor < 2.0) 被过滤,避免把比率当累积值传给 ratio 公式。
    ratio = ex_factor(D) / ex_factor(today), 全 finite 时启用, 否则 fallback 1.0。
    """
    from app.indicators.pipeline import _apply_adj_factor

    raw = pl.DataFrame({
        "symbol": ["000001.SZ"] * 3,
        "date": [date(2026, 1, 2), date(2026, 6, 1), date(2026, 12, 31)],
        "open": [10.0, 9.0, 8.0],
        "high": [10.5, 9.5, 8.5],
        "low": [9.5, 8.5, 7.5],
        "close": [10.0, 9.0, 8.0],
        "raw_close": [10.0, 9.0, 8.0],
    })
    # cumulative ex_factor: 6-1 送转事件 (2.0 → 3.0); 12-31 再送转 (3.0 → 4.0)
    # ratio = ex_factor(D) / ex_factor(today) = ex_factor(D) / 4.0
    #   1-2: 10 * 2.0 / 4.0 = 5.0 (历史价格被向下除权调降)
    #   6-1: 9 * 3.0 / 4.0 = 6.75
    #   12-31: 8 * 4.0 / 4.0 = 8.0 (今日不变)
    factors = pl.DataFrame({
        "symbol": ["000001.SZ"] * 3,
        "trade_date": [date(2026, 1, 2), date(2026, 6, 1), date(2026, 12, 31)],
        "ex_factor": [2.0, 3.0, 4.0],
    })
    out = _apply_adj_factor(raw, factors)
    # raw_close 不参与 ratio, 应保持原值
    raws = out["raw_close"].to_list()
    assert raws == [10.0, 9.0, 8.0]
    # 前复权 open 应等于 5.0 / 6.75 / 8.0
    out_sorted = out.sort("date")
    rows = [out_sorted.row(i, named=True) for i in range(3)]
    assert rows[0]["open"] == 5.0
    assert rows[0]["close"] == 5.0
    assert rows[1]["open"] == 6.75
    assert rows[1]["close"] == 6.75
    assert rows[2]["open"] == 8.0
    assert rows[2]["close"] == 8.0
    # ex_rights 仅在除权日 (factors 中 trade_date 与 K 线 date 重合) 为 True
    assert rows[0]["ex_rights"] is True
    assert rows[1]["ex_rights"] is True
    assert rows[2]["ex_rights"] is True


def test_apply_adj_factor_filters_per_event_rows_below_threshold():
    """Tushare/TickFlow 混合格式: per-event 行 (ex_factor < 2.0) 被过滤掉,
    只用累积行算 ratio, 避免把事件比率当累积值用导致 ratio 偏差。
    """
    from app.indicators.pipeline import _apply_adj_factor

    raw = pl.DataFrame({
        "symbol": ["X.SH", "X.SH", "X.SH"],
        "date": [date(2026, 1, 2), date(2026, 6, 1), date(2026, 12, 31)],
        "open": [10.0, 9.0, 8.0],
        "high": [10.5, 9.5, 8.5],
        "low": [9.5, 8.5, 7.5],
        "close": [10.0, 9.0, 8.0],
        "raw_close": [10.0, 9.0, 9.0],  # raw_close 列也补齐避免后续断言误判
    })
    # 1-2 cum=2.0; 6-1 per-event=1.02 (噪声, 被过滤); 6-2 起累积因子跳到 4.0;
    # 6-5 又一个 per-event=1.05 (噪声, 被过滤); 12-31 累积因子跳到 5.0
    factors = pl.DataFrame({
        "symbol": ["X.SH"] * 5,
        "trade_date": [date(2026, 1, 2), date(2026, 6, 1), date(2026, 6, 2),
                       date(2026, 6, 5), date(2026, 12, 31)],
        "ex_factor": [2.0, 1.02, 4.0, 1.05, 5.0],
    })
    out = _apply_adj_factor(raw, factors)
    # total_factor = 5.0 (过滤掉 1.02/1.05 后, 最后累积行 = 5.0)
    # join_asof 用累积行: 1-2 → 2.0; 6-1 → 2.0 (无新累积行); 12-31 → 5.0
    # ratio = 2.0/5.0 = 0.4, 2.0/5.0 = 0.4, 5.0/5.0 = 1.0
    #   1-2: 10 * 0.4 = 4.0
    #   6-1: 9 * 0.4 = 3.6
    #   12-31: 8 * 1.0 = 8.0
    out_sorted = out.sort("date")
    rows = [out_sorted.row(i, named=True) for i in range(3)]
    assert rows[0]["close"] == 4.0
    assert rows[1]["close"] == 3.6
    assert rows[2]["close"] == 8.0


def test_apply_adj_factor_uses_only_cumulative_rows_for_ratio():
    """正常混合数据: per-event 行被过滤, 累积行主导 ratio 计算.

    模拟 000001.SZ 风格: 1-2 cum=2.0, 6-1 per-event=1.02, 6-2 cum=4.0, 12-31 cum=5.0
    total = 5.0 (最后累积行), 12-31 ratio = 5.0/5.0 = 1.0
    """
    from app.indicators.pipeline import _apply_adj_factor

    raw = pl.DataFrame({
        "symbol": ["Y.SH"],
        "date": [date(2026, 12, 31)],
        "open": [8.0], "high": [8.5], "low": [7.5], "close": [8.0],
        "raw_close": [8.0],
    })
    factors = pl.DataFrame({
        "symbol": ["Y.SH", "Y.SH", "Y.SH", "Y.SH"],
        "trade_date": [date(2026, 1, 2), date(2026, 6, 1),
                       date(2026, 6, 2), date(2026, 12, 31)],
        "ex_factor": [2.0, 1.02, 4.0, 5.0],  # 1.02 是 per-event
    })
    out = _apply_adj_factor(raw, factors)
    row = out.row(0, named=True)
    # 12-31 是当日, cum=5.0, total=5.0 (filtered 后), ratio=1.0
    assert row["close"] == 8.0
    assert row["ex_rights"] is True


def test_apply_adj_factor_per_symbol_filter_not_global():
    """回归测试: per-symbol 而非全局决定是否过滤 per-event 行.

    背景: 修复前的实现用全局 filter `cum_only.is_empty() else cum_only`,
    导致 300750.SZ 这种所有 ex_factor < 2 的 symbol 被高 cum symbol (cum_only 非空) 一起丢弃,
    join_asof 匹配不到任何因子, ratio fallback 1.0, 整支股票前复权丢失。

    验证: 全局 factors 表中混入高 cum symbol (600519.SH cum~8), 仍能为
    低 cum symbol (300750.SZ 所有 ex_factor < 2) 算出 forward-adjusted close。
    """
    from app.indicators.pipeline import _apply_adj_factor

    # 全局 factors 表: 600519.SH (高 cum) + 300750.SZ (全 < 2)
    factors = pl.DataFrame({
        "symbol": (["600519.SH"] * 3) + (["300750.SZ"] * 3),
        "trade_date": [
            date(2020, 1, 2), date(2026, 6, 26), date(2026, 9, 23),  # 600519
            date(2020, 1, 2), date(2023, 4, 27), date(2026, 8, 11),  # 300750
        ],
        "ex_factor": [7.319, 1.024, 8.6463,  # 600519: cum + per-event + cum
                      1.002, 1.822, 1.9566],  # 300750: 全 < 2
    })

    raw = pl.DataFrame({
        "symbol": ["300750.SZ"],
        "date": [date(2020, 1, 2)],
        "open": [107.21], "high": [108.85], "low": [105.7], "close": [107.52],
        "raw_close": [107.52], "raw_high": [108.85], "raw_low": [105.7],
    })
    out = _apply_adj_factor(raw, factors)
    row = out.row(0, named=True)
    # 300750.SZ 走 fallback sub (因为此 symbol 无 cum>=2 行), ex_factor(D=2020-01-02)=1.002,
    # total=1.9566, ratio=0.512, close=107.52*0.512≈55.06
    expected = 107.52 * (1.002 / 1.9566)
    assert abs(row["close"] - expected) < 0.01, (
        f"close {row['close']:.4f} != expected {expected:.4f}; "
        "per-symbol filter 未生效, 整支股票被全局 filter 排除"
    )
