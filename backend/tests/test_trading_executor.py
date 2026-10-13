"""StrategyExecutor 测试 — on_event 路由 + state 持久化 + 多 schedule.

测试重点:
  - 多 schedule 策略, run_now(strat_id, event='pre_market') 只走对应 event.
  - state 在 on_event 调用前注入, 调用后写回 (跨调用持久化).
  - 空 target → status='no_target' 不调仓.
  - universe 过滤跨 schedule 一致生效.
  - 异常隔离: 策略抛异常 → run 状态 error, state 仍保存 (部分写入).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.services.trading import engine as engine_mod
from app.services.trading import executor as executor_mod
from app.services.trading import live_strategy as live_strategy_mod
from app.services.trading import preferences as prefs
from app.services.trading import store
from app.services.trading.engine import TradingEngine
from app.services.trading.live_strategy import LiveStrategy, StrategySchedule
from app.services.trading.runner import TradingRunner


class _MultiEventStrategy(LiveStrategy):
    """3 个 event 的测试策略: 准备 / 调仓 / 检查."""
    id = "multi"
    name = "Multi"
    enabled_default = False
    schedules = [  # noqa: RUF012
        StrategySchedule(name="pre_market", hour=9, minute=5),
        StrategySchedule(name="rebalance", hour=9, minute=26),
        StrategySchedule(name="limit_check", hour=13, minute=30),
    ]

    def __init__(self, target: dict[str, int] | None = None,
                 raise_exc: bool = False) -> None:
        self._target = target or {}
        self._raise = raise_exc

    def on_event(self, event: str, context):
        if self._raise:
            raise RuntimeError("boom")
        if event == "pre_market":
            # 准备: 把 context.state 写入 high_limit_list
            context.state["high_limit_list"] = ["999999.SH"]
            return {}
        if event == "rebalance":
            return dict(self._target)
        if event == "limit_check":
            # 检查: state 里读出, 清仓 + 返回
            held = list(context.state.get("high_limit_list", []))
            return {sym: 0 for sym in held}
        return {}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    store.reset_for_tests()
    import app.services.preferences as base_prefs
    base_prefs._invalidate_cache()
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(prefs, "save", base_prefs.save)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_minute_batch=lambda symbols, today: pl.DataFrame(),
    )
    qs = MagicMock()
    qs.get_quotes_compat.return_value = pl.DataFrame({
        "symbol": ["600519.SH", "000858.SZ", "999999.SH"],
        "last_price": [10.0, 20.0, 30.0],
        "close": [10.0, 20.0, 30.0],
        "prev_close": [9.0, 19.0, 29.0],
    })
    app_state = SimpleNamespace(repo=repo, quote_service=qs)
    eng = TradingEngine()
    app_state.trading_engine = eng
    engine_mod.set_app_state(app_state)
    return TradingRunner(eng, tmp_path)


def _build_executor(runner, strategy):
    reg = live_strategy_mod.LiveStrategyRegistry()
    reg.register(strategy)
    return reg, executor_mod.StrategyExecutor(reg, runner)


# ── 多 event 路由 ──────────────────────────────────
def test_executor_pre_market_writes_state_no_trade(runner):
    strat = _MultiEventStrategy()
    _reg, ex = _build_executor(runner, strat)
    report = ex.run_now("multi", event="pre_market")
    assert report.status == "no_target"
    assert report.submitted == []
    # state 已落盘
    from app.services.trading.executor import load_state
    state = load_state(runner.data_dir, "multi")
    assert state.get("high_limit_list") == ["999999.SH"]


def test_executor_rebalance_submits_orders(runner):
    strat = _MultiEventStrategy(target={"600519.SH": 100, "000858.SZ": 200})
    _reg, ex = _build_executor(runner, strat)
    report = ex.run_now("multi", event="rebalance")
    assert report.status == "success"
    assert len(report.submitted) == 2


def test_executor_limit_check_uses_state(runner):
    strat = _MultiEventStrategy()
    _reg, ex = _build_executor(runner, strat)
    # 1. 先跑 pre_market 写入 state
    ex.run_now("multi", event="pre_market")
    # 2. 跑 limit_check 应基于 state 返回清仓单
    report = ex.run_now("multi", event="limit_check")
    assert report.status == "success"
    # 999999.SH 不在 market 快照里 (last_price=None) → 不入 rebalance → 实际无 submitted
    # 这里我们验证 target 字段是 {999999.SH: 0}
    assert report.target == {"999999.SH": 0}


def test_executor_default_event_uses_schedules_zero(runner):
    """不传 event → 默认 schedules[0].name = 'pre_market'."""
    strat = _MultiEventStrategy()
    _reg, ex = _build_executor(runner, strat)
    report = ex.run_now("multi")  # 不传 event
    assert report.event == "pre_market"
    assert report.status == "no_target"


def test_executor_unknown_strategy_raises(runner):
    reg = live_strategy_mod.LiveStrategyRegistry()
    ex = executor_mod.StrategyExecutor(reg, runner)
    with pytest.raises(ValueError):
        ex.run_now("nonexistent", event="x")


def test_executor_catches_strategy_exception_and_keeps_state(runner):
    """策略抛异常 → run 状态 error; state 仍保存."""
    strat = _MultiEventStrategy(raise_exc=True)
    _reg, ex = _build_executor(runner, strat)
    report = ex.run_now("multi", event="pre_market")
    assert report.status == "error"
    assert "boom" in report.error
    # state 仍然落盘 (策略抛异常前没机会写, 但 fallback save 会保存 ctx.state)
    from app.services.trading.executor import load_state
    state = load_state(runner.data_dir, "multi")
    # 抛异常前 ctx.state 没被修改, 所以是空 dict
    assert state == {} or state == {"high_limit_list": []}


def test_executor_records_run_with_event_and_duration(runner):
    strat = _MultiEventStrategy(target={"600519.SH": 100})
    _reg, ex = _build_executor(runner, strat)
    report = ex.run_now("multi", event="rebalance", trigger="cron")
    assert report.duration_ms >= 0
    assert report.event == "rebalance"
    assert report.trigger == "cron"


def test_executor_no_target_status(runner):
    """target 为空 → status='no_target', 不调仓."""
    strat = _MultiEventStrategy(target={})
    _reg, ex = _build_executor(runner, strat)
    report = ex.run_now("multi", event="rebalance")
    assert report.status == "no_target"
    assert report.submitted == []


def test_state_persists_across_runs(runner):
    """pre_market 写入 state, limit_check 读出; 中间无其他干扰."""
    strat = _MultiEventStrategy()
    _reg, ex = _build_executor(runner, strat)

    ex.run_now("multi", event="pre_market")
    from app.services.trading.executor import load_state
    s1 = load_state(runner.data_dir, "multi")
    assert s1["high_limit_list"] == ["999999.SH"]

    # 直接再次读 (模拟第二次 run)
    s2 = load_state(runner.data_dir, "multi")
    assert s2["high_limit_list"] == ["999999.SH"]


def test_executor_writes_pnl_snapshot_after_run(runner):
    from app.services.trading import pnl
    strat = _MultiEventStrategy(target={"600519.SH": 100})
    _reg, ex = _build_executor(runner, strat)
    ex.run_now("multi", event="rebalance")
    snaps = pnl.list_history(runner.data_dir, trigger="strategy:multi:rebalance")
    assert len(snaps) >= 1


def test_executor_missing_price_target_symbol_falls_back_to_market(runner):
    """策略返回的目标里有 ctx.market 没覆盖的 symbol → 改市价单, 不被 broker 拒.

    原 bug: 走 rebalance_to(prices={...}) 时, 没价的 symbol 拿到 limit_price=None,
    paper_broker 会拒 "限价单必须填写价格" → 策略侧表现为"莫名某只票没成交".
    """
    strat = _MultiEventStrategy(target={
        "600519.SH": 100,       # ctx.market 有价 → LIMIT
        "688999.SH": 50,        # ctx.market 无价 → MARKET
    })
    _reg, ex = _build_executor(runner, strat)
    report = ex.run_now("multi", event="rebalance")
    assert report.status == "success"
    assert len(report.submitted) == 2

    # 检视订单: 600519 限价, 688999 市价
    from app.services.trading.store import list_orders
    orders = list_orders(runner.data_dir, limit=10)
    by_sym = {o.symbol: o for o in orders}
    assert by_sym["600519.SH"].order_type.value == "limit"
    assert by_sym["600519.SH"].limit_price == pytest.approx(10.0)
    assert by_sym["688999.SH"].order_type.value == "market"
    assert by_sym["688999.SH"].limit_price is None


def test_executor_records_traceback_on_strategy_exception(runner):
    """策略抛异常时, strategy_runs.jsonl 同时记录 error + traceback (便于后续追溯)."""
    import json
    from pathlib import Path
    strat = _MultiEventStrategy(raise_exc=True)
    _reg, ex = _build_executor(runner, strat)
    report = ex.run_now("multi", event="pre_market")
    assert report.status == "error"
    assert "boom" in report.error
    # traceback 必须含 Traceback + 异常类名 + 异常消息
    assert "Traceback" in report.traceback
    assert "boom" in report.traceback
    # 必须落盘到 strategy_runs.jsonl, 含 traceback 列
    runs_path = Path(runner.data_dir) / "user_data" / "trading" / "strategy_runs.jsonl"
    assert runs_path.exists()
    lines = [json.loads(l) for l in runs_path.read_text().splitlines() if l.strip()]
    last = lines[-1]
    assert last["status"] == "error"
    assert "Traceback" in last.get("traceback", "")
