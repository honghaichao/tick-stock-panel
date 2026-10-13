"""StrategyVerifier 单元测试 — read-only 回测验证.

测试重点:
  - 真实环境跑: 用项目 instruments.parquet + repo 历史 close 拉数据,
    不依赖 mock, 但 mock 一个返回 2 个交易日的 repo.
  - 跑策略每个 event, 记录 target / status / error.
  - 模拟成交: SimAccount 跟踪 现金/持仓/已实现, PnL 计算正确.
  - 报告含 per-day events + summary + 可选 PnL.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.trading import verifier as verifier_mod
from app.services.trading.live_strategy import (
    LiveStrategy,
    LiveStrategyContext,
    LiveStrategyRegistry,
    StrategySchedule,
)


class _FixedStrategy(LiveStrategy):
    id = "test_strat"
    name = "TestStrat"
    enabled_default = False
    schedules = [
        StrategySchedule(name="pre_market", hour=9, minute=5),
        StrategySchedule(name="rebalance", hour=9, minute=26),
    ]

    def __init__(self, target_by_event: dict[str, dict[str, int]] | None = None) -> None:
        self._target = target_by_event or {}

    def on_event(self, event, context):
        return dict(self._target.get(event, {}))


@pytest.fixture
def fake_repo(tmp_path):
    """最小可用的 repo: 给定 [day1, day2] 两个交易日, 每个有 3 个 symbol."""
    days = [date(2026, 1, 15), date(2026, 1, 16)]
    close_data = {
        date(2026, 1, 15): {"A": 10.0, "B": 20.0, "C": 5.0},
        date(2026, 1, 16): {"A": 11.0, "B": 19.0, "C": 5.5},
    }

    class _Repo:
        def get_enriched_latest(self):
            # verifier 内部先用此取 cache_date; 我们直接返回空 cache,
            # 但 verifier 仍会调 get_enriched_range 取实际日期.
            return (pl.DataFrame(), date(2026, 1, 17))

        def get_instruments(self):
            return pl.DataFrame({
                "symbol": ["A", "B", "C"],
                "name": ["Apple", "Bank", "Corp"],
                "limit_up": [11.0, 22.0, 5.5],
                "limit_down": [9.0, 18.0, 4.5],
                "total_shares": [1000.0, 2000.0, 500.0],
                "type": ["stock"] * 3,
                "exchange": ["SH", "SH", "SZ"],
            })

        def get_enriched_range(self, start, end, symbols=None, columns=None):
            return pl.DataFrame({"date": days})

        def get_daily_batch(self, symbols, start, end, columns=None):
            rows = []
            for d in days:
                if start <= d <= end:
                    for sym in symbols:
                        if sym in close_data.get(d, {}):
                            rows.append({
                                "symbol": sym,
                                "date": d,
                                "close": close_data[d][sym],
                                "raw_close": close_data[d][sym],
                            })
            return pl.DataFrame(rows) if rows else pl.DataFrame()

    repo = _Repo()
    repo.store = SimpleNamespace(data_dir=tmp_path)
    return repo


def _build_registry(strategy):
    reg = LiveStrategyRegistry()
    reg.register(strategy)
    return reg


# ── happy path ──────────────────────────────────────
def test_verifier_basic_happy_path(tmp_path, fake_repo):
    """固定策略 → 2 天 × 2 events → 报告含 events + summary."""
    strat = _FixedStrategy(target_by_event={
        "pre_market": {},
        "rebalance": {"A": 100, "B": 200},
    })
    reg = _build_registry(strat)
    v = verifier_mod.StrategyVerifier(reg, fake_repo)

    report = v.verify(
        "test_strat",
        start_date=date(2026, 1, 15),
        end_date=date(2026, 1, 16),
        simulate_fills=True,
        initial_cash=1_000_000.0,
    )

    assert report.run_id
    assert report.strategy_id == "test_strat"
    assert report.start_date == "2026-01-15"
    assert report.end_date == "2026-01-16"
    assert report.trading_days == 2
    # 4 events (2 days x 2 events)
    assert len(report.events) == 4
    # day 1 pre_market 是空 target → no_target; day 1 rebalance → success with target
    by_date_event = {(e.date, e.event): e for e in report.events}
    e_pre = by_date_event[("2026-01-15", "pre_market")]
    assert e_pre.status == "no_target"
    e_reb = by_date_event[("2026-01-15", "rebalance")]
    assert e_reb.status == "success"
    assert e_reb.target == {"A": 100, "B": 200}


def test_verifier_simulate_fills_produces_pnl(tmp_path, fake_repo):
    """模拟成交: day1 买 A @ 10 → day2 卖 A @ 11 → realized = 100."""
    strat = _FixedStrategy(target_by_event={
        "pre_market": {},
        "rebalance": {"A": 100},
    })
    reg = _build_registry(strat)
    v = verifier_mod.StrategyVerifier(reg, fake_repo)

    report = v.verify(
        "test_strat",
        start_date=date(2026, 1, 15),
        end_date=date(2026, 1, 16),
        simulate_fills=True,
        initial_cash=1_000_000.0,
    )

    assert report.pnl is not None
    pnl = report.pnl
    # day1: buy 100 A @ 10 → cash -= 1000, holdings={A:100}
    # day2: buy 100 A @ 11 → cash -= 1100, holdings={A:200}
    # 终态: cash ≈ 1000000 - 1000 - 1100 = 997900; mv = 200 * 11 = 2200; total = 1000100
    # auto-flatten 把 day1 的 100 股在 day2 close @11 卖出 → cash += 1100.
    assert pnl.final_cash == pytest.approx(1_000_000.0 - 1000 + 1100)
    assert pnl.final_market_value == pytest.approx(0)  # 自动平仓已全部卖出
    assert pnl.total_pnl == pytest.approx(100.0)  # (11 - 10) * 100, 来自 auto-flatten
    assert pnl.total_trades == 1  # 1 笔完整的 round trip (买→自动平仓卖)
    assert pnl.winning_trades == 1
    assert pnl.total_pnl_pct > 0


def test_verifier_no_simulate_fills_skips_pnl(tmp_path, fake_repo):
    """simulate_fills=False → report.pnl is None."""
    strat = _FixedStrategy()
    reg = _build_registry(strat)
    v = verifier_mod.StrategyVerifier(reg, fake_repo)
    report = v.verify(
        "test_strat",
        start_date=date(2026, 1, 15),
        end_date=date(2026, 1, 16),
        simulate_fills=False,
    )
    assert report.pnl is None
    # 仍然记录 events (策略行为画像, 不依赖 fill)
    assert len(report.events) >= 1


def test_verifier_strategy_exception_recorded_per_event(tmp_path, fake_repo):
    """策略某个 event 抛异常 → 该 event status='error', 不污染后续."""
    class _ExplodingStrategy(LiveStrategy):
        id = "boom"
        name = "Boom"
        enabled_default = False
        schedules = [StrategySchedule(name="rebalance", hour=9, minute=26)]

        def on_event(self, event, context):
            raise RuntimeError("intentional")

    reg = _build_registry(_ExplodingStrategy())
    v = verifier_mod.StrategyVerifier(reg, fake_repo)

    report = v.verify("boom", start_date=date(2026, 1, 15), end_date=date(2026, 1, 16))
    assert len(report.warnings) >= 1
    assert "intentional" in report.warnings[0]
    # 所有 event status='error'
    assert all(e.status == "error" for e in report.events)
    assert report.summary["errors_count"] == 2


def test_verifier_empty_trading_days(tmp_path, fake_repo):
    """区间内无交易日 (repo 返回空): events=[], 但报告骨架正常."""
    class _NoDaysRepo(fake_repo.__class__):
        def get_enriched_range(self, start, end, symbols=None, columns=None):
            return pl.DataFrame()  # 空

    strat = _FixedStrategy()
    reg = _build_registry(strat)
    repo = _NoDaysRepo()
    repo.store = fake_repo.store
    v = verifier_mod.StrategyVerifier(reg, repo)

    report = v.verify("test_strat", start_date=date(2026, 1, 15), end_date=date(2026, 1, 16))
    assert report.trading_days == 0
    assert report.events == []


def test_verifier_summary_includes_target_distribution(tmp_path, fake_repo):
    """summary.target_distribution 反映出现过的 symbol 次数."""
    strat = _FixedStrategy(target_by_event={
        "pre_market": {},
        "rebalance": {"A": 100, "B": 200},  # day1: A+B
    })
    reg = _build_registry(strat)
    v = verifier_mod.StrategyVerifier(reg, fake_repo)
    report = v.verify("test_strat", start_date=date(2026, 1, 15), end_date=date(2026, 1, 16))
    # 2 天都 rebalance 到 {A,B}; A B 各出现 2 次
    assert report.summary["target_distribution"]["A"] == 2
    assert report.summary["target_distribution"]["B"] == 2


def test_verifier_events_subset_filter(tmp_path, fake_repo):
    """events=['pre_market'] 只跑 pre_market event."""
    strat = _FixedStrategy(target_by_event={
        "pre_market": {},
        "rebalance": {"A": 100},
    })
    reg = _build_registry(strat)
    v = verifier_mod.StrategyVerifier(reg, fake_repo)
    report = v.verify(
        "test_strat",
        start_date=date(2026, 1, 15),
        end_date=date(2026, 1, 16),
        events=["pre_market"],
    )
    # 只有 pre_market, 没 rebalance
    assert all(e.event == "pre_market" for e in report.events)
    assert len(report.events) == 2  # 2 天 x 1 event


def test_verifier_unknown_strategy_raises(tmp_path, fake_repo):
    reg = LiveStrategyRegistry()
    v = verifier_mod.StrategyVerifier(reg, fake_repo)
    with pytest.raises(ValueError):
        v.verify("nonexistent", start_date=date(2026, 1, 15), end_date=date(2026, 1, 16))


def test_verifier_max_drawdown_calculation(tmp_path, fake_repo):
    """先涨后跌 → max_drawdown 反映下跌幅度."""
    # 临时把 close 数据换成先涨后跌; 必须按日期过滤, 否则 _historical_close_map
    # 拼出的 dict 永远覆盖到最后一行的 close, 三天 equity 平的 → drawdown = 0.
    _all_rows = [
        {"symbol": "A", "date": date(2026, 1, 15), "close": 10.0, "raw_close": 10.0},
        {"symbol": "A", "date": date(2026, 1, 16), "close": 12.0, "raw_close": 12.0},
        {"symbol": "A", "date": date(2026, 1, 17), "close": 6.0,  "raw_close": 6.0},
    ]

    def _patched_get_daily_batch(symbols, start, end, columns=None):
        rows = [r for r in _all_rows if start <= r["date"] <= end]
        return pl.DataFrame(rows) if rows else pl.DataFrame()

    fake_repo.get_daily_batch = _patched_get_daily_batch
    fake_repo.get_enriched_range = lambda s, e, symbols=None, columns=None: pl.DataFrame({
        "date": [date(2026, 1, 15), date(2026, 1, 16), date(2026, 1, 17)]
    })

    strat = _FixedStrategy(target_by_event={
        "rebalance": {"A": 100},  # 每天买 A
    })
    reg = _build_registry(strat)
    v = verifier_mod.StrategyVerifier(reg, fake_repo)

    report = v.verify(
        "test_strat",
        start_date=date(2026, 1, 15),
        end_date=date(2026, 1, 17),
        simulate_fills=True,
        initial_cash=1_000_000.0,
    )
    # day1: 100 A @ 10 → mv=1000
    # day2: +100 A @ 12 → 持仓 200, mv = 200 * 12 = 2400
    # day3: +100 A @ 6 → 持仓 300, mv = 300 * 6 = 1800
    # equity 峰值 day2 = 1000000 - 1000 - 1200 + 2400 = 1000200
    # equity day3 = 1000000 - 1000 - 1200 - 600 + 1800 = 999000
    # drawdown = (1000200 - 999000) / 1000200 ≈ 0.012
    assert report.pnl is not None
    assert report.pnl.max_drawdown > 0
    assert report.pnl.max_drawdown_date == "2026-01-17"


def test_verifier_save_and_load(tmp_path, fake_repo):
    """save=True → 文件落盘; list_verifications 能读到."""
    strat = _FixedStrategy()
    reg = _build_registry(strat)
    v = verifier_mod.StrategyVerifier(reg, fake_repo)

    report = v.verify("test_strat", start_date=date(2026, 1, 15), end_date=date(2026, 1, 16))
    path = verifier_mod.save_verification(tmp_path, report)
    assert path.exists()

    items = verifier_mod.list_verifications(tmp_path, "test_strat")
    assert len(items) == 1
    assert items[0]["run_id"] == report.run_id
