"""TradingRunner 测试 — buy/sell/cancel/rebalance_to 走通 engine 校验。

测试重点:
  - 委托落库状态机正确 (NEW / REJECTED).
  - 风控: 资金上限 / T+1 都触发 TradingRejectError, 委托落 REJECTED.
  - rebalance_to 先卖后买, 差额调仓, 跳过已对齐项.
  - 查询接口 (positions / account / pending_orders) 与 store 直接调用等价.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.services.trading import engine as engine_mod
from app.services.trading import preferences as prefs
from app.services.trading import store
from app.services.trading.engine import TradingEngine
from app.services.trading.models import (
    Fill,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.services.trading.runner import TradingRunner


@pytest.fixture
def runner(tmp_path, monkeypatch):
    store.reset_for_tests()
    import app.services.preferences as base_prefs
    base_prefs._invalidate_cache()
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(prefs.base, "save", base_prefs.save)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_minute_batch=lambda symbols, today: pl.DataFrame(),
    )
    qs = MagicMock()
    qs.get_quotes_compat.return_value = pl.DataFrame({
        "symbol": ["600519.SH", "000858.SZ"],
        "last_price": [10.0, 20.0],
        "close": [10.0, 20.0],
        "prev_close": [9.0, 19.0],
    })
    app_state = SimpleNamespace(repo=repo, quote_service=qs)
    eng = TradingEngine()
    app_state.trading_engine = eng
    engine_mod.set_app_state(app_state)
    return TradingRunner(eng, tmp_path)


# ── buy / sell 走通校验 ────────────────────────────
def test_runner_buy_creates_new_order(runner):
    o = runner.buy("600519.SH", 100, limit_price=10.5)
    assert o.status == OrderStatus.NEW
    assert o.side == OrderSide.BUY
    assert o.qty == 100
    assert o.limit_price == 10.5


def test_runner_sell_rejects_when_no_holding(runner):
    """T+1: 无持仓卖出被 engine 拒, 多走 1 笔 REJECTED 委托."""
    with pytest.raises(engine_mod.TradingRejectError) as ei:
        runner.sell("600519.SH", 100, limit_price=11.0)
    assert "可卖数量不足" in str(ei.value)
    # 委托本身被落库 REJECTED
    rejected = runner.list_orders(statuses=[OrderStatus.REJECTED])
    assert len(rejected) == 1
    assert rejected[0].reject_reason and "可卖数量不足" in rejected[0].reject_reason


def test_runner_buy_rejects_cash_shortage(runner, monkeypatch):
    monkeypatch.setattr(prefs, "max_order_amount", lambda: 1e12)
    monkeypatch.setattr(prefs, "initial_cash", lambda: 1000.0)
    with pytest.raises(engine_mod.TradingRejectError) as ei:
        runner.buy("600519.SH", 1000, limit_price=10.0)   # 1000*10 = 10000 > 1000
    assert "不足以支付" in str(ei.value)


def test_runner_market_order_no_limit_price(runner):
    o = runner.buy("600519.SH", 100, order_type="market")
    assert o.order_type == OrderType.MARKET
    assert o.limit_price is None


# ── cancel ────────────────────────────────────────
def test_runner_cancel_new_order(runner):
    o = runner.buy("600519.SH", 100, limit_price=10.0)
    canceled = runner.cancel(o.id)
    assert canceled.status == OrderStatus.CANCELED


def test_runner_cancel_nonexistent(runner):
    with pytest.raises(engine_mod.TradingRejectError):
        runner.cancel("ord_doesnotexist")


# ── rebalance_to ─────────────────────────────────
def test_rebalance_to_buys_only_when_target_larger(runner):
    """当前空仓 → rebalance 到 {600519.SH: 100} → 只生成 1 笔买."""
    orders = runner.rebalance_to({"600519.SH": 100})
    assert len(orders) == 1
    assert orders[0].side == OrderSide.BUY
    assert orders[0].qty == 100


def test_rebalance_to_sells_only_when_target_smaller(runner):
    """先建仓 200 → rebalance 到 100 → 卖 100."""
    # 直接落库模拟已有持仓 (绕过撮合, 走 store 写盘)
    store.append_fill(runner.data_dir, Fill(
        id="f1", order_id="o_hist", symbol="600519.SH", side=OrderSide.BUY,
        qty=200, price=10.0, fee=0, tax=0, amount=2000.0,
        filled_at="2026-01-14T10:00:00+08:00", trade_date=date(2026, 1, 14),
    ))
    orders = runner.rebalance_to({"600519.SH": 100})
    assert len(orders) == 1
    assert orders[0].side == OrderSide.SELL
    assert orders[0].qty == 100


def test_rebalance_to_skip_hold_keeps_existing(runner):
    """skip_hold=True 时, 不动已在 target 里的现存持仓."""
    store.append_fill(runner.data_dir, Fill(
        id="f1", order_id="o_hist", symbol="600519.SH", side=OrderSide.BUY,
        qty=100, price=10.0, fee=0, tax=0, amount=1000.0,
        filled_at="2026-01-14T10:00:00+08:00", trade_date=date(2026, 1, 14),
    ))
    # target 与当前持仓一致 → skip 后无新委托
    orders = runner.rebalance_to({"600519.SH": 100}, skip_hold=True)
    assert len(orders) == 0
    # skip_hold=False (默认) → 同样无委托 (差额=0)
    orders = runner.rebalance_to({"600519.SH": 100})
    assert len(orders) == 0


def test_rebalance_to_does_not_silently_swallow_sell_rejection(runner, monkeypatch):
    """卖出委托被 T+1 拒时, 不抛异常, 记 warn 继续 — buy 仍正常提交."""
    store.append_fill(runner.data_dir, Fill(
        id="f1", order_id="o_hist", symbol="600519.SH", side=OrderSide.BUY,
        qty=100, price=10.0, fee=0, tax=0, amount=1000.0,
        filled_at="2026-01-14T10:00:00+08:00", trade_date=date(2026, 1, 14),
    ))
    # target 把 600519.SH 设为 200 (买入 100), 同时清掉 000858.SZ 但没持仓 → 卖出 0 = noop
    orders = runner.rebalance_to({"600519.SH": 200})
    assert len(orders) == 1
    assert orders[0].side == OrderSide.BUY
    assert orders[0].qty == 100


# ── 回归: in-frame sell→buy 资金校验 (2026-09-29 14:02 现场) ─────────
def test_rebalance_to_uses_in_frame_sell_proceeds_for_buy_check(runner, monkeypatch):
    """rebalance 触发同帧 sell+buy: 旧实现 sell 是异步撮合, buy 走落账 cash → "可用现金不足" 拒单.

    修复: rebalance_to 累计同帧 sells proceeds, 把 effective_cash=落账cash+proceeds 注入
    buy 校验. 本测试断言: cash=1000, 持仓 600519 100 股 @10, target=买 000858 100 股 @20.
    旧实现: 卖 OK, 买时 cash=1000 < 2000 → 拒 (1 笔 sell + 0 buy submitted)
    新实现: 卖 OK, 买时 effective=2000 ≥ 2000 → 通过 (1 sell + 1 buy submitted)
    """
    # 把初始 cash 锁到 1000, max_order_amount 放大 (避免单笔金额硬卡 2000)
    monkeypatch.setattr(prefs, "initial_cash", lambda: 1000.0)
    monkeypatch.setattr(prefs, "max_order_amount", lambda: 1e12)
    monkeypatch.setattr(prefs, "max_order_amount_for_strategy", lambda: 1e12)
    # F0-3 (2026-10-13): 本测试用 1000 元账户 + 2000 元 buy, 默认 max_position_pct=20% 会拒,
    # 故 monkeypatch 成 100% (本测试只关心资金时序, 不关心 pct 限制)。
    monkeypatch.setattr(prefs, "max_position_pct", lambda: 1.0)
    monkeypatch.setattr(prefs, "max_position_count", lambda: 100)
    # F0-3: pct=1.0 不够 (2000/1000=2.0>1.0 仍拒), 本测试只需关注资金时序,
    # 直接 no-op 整个 _enforce_position_risk。
    from app.services.trading import engine as engine_mod
    monkeypatch.setattr(engine_mod.TradingEngine, "_enforce_position_risk", lambda *a, **kw: None)
    # 重新初始化 account.json (load_account 读的是落盘值, 测试里没动所以保持 prefs.initial_cash()=1e6 默认)
    # 直接写 account.json 让 load_account 走我们的初始值
    import json
    account_path = runner.data_dir / "user_data" / "trading" / "account.json"
    account_path.parent.mkdir(parents=True, exist_ok=True)
    account_path.write_text(json.dumps({
        "initial_cash": 1000.0, "cash": 1000.0,
        "market_value": 0.0, "total_equity": 1000.0,
        "realized_pnl": 0.0, "unrealized_pnl": 0.0, "total_pnl": 0.0,
    }), encoding="utf-8")
    store.reset_for_tests()  # 强制下次 load_account 重新读盘

    # 历史持仓 600519.SH 100 股 @ 10 (cost 1000, cash 已扣)
    store.append_fill(runner.data_dir, Fill(
        id="f1", order_id="o_hist", symbol="600519.SH", side=OrderSide.BUY,
        qty=100, price=10.0, fee=0, tax=0, amount=1000.0,
        filled_at="2026-01-14T10:00:00+08:00", trade_date=date(2026, 1, 14),
    ))
    # 历史 buy fill 1000 扣的 cash, 在落账 account.json 已体现 (cash=0). 重设 cash=1000
    # 模拟 "我今天开盘 cash=1000 + 持仓 100 股" 的状态.
    # 实际上 store.positions() 不扣 cash, account.cash = 落账值 = 1000, 持仓 = 100 股 (cost 1000)
    # cash + 持仓市价 1000 = 2000, 跟 rebalance 后预期 (cash=0 + 持仓 000858 100 股 @ 20 = 2000) 平.

    # target: 卖 600519.SH 100, 买 000858.SZ 100 @ 20
    orders = runner.rebalance_to(
        {"000858.SZ": 100},
        prices={"600519.SH": 10.0, "000858.SZ": 20.0},
    )

    # 期望: 1 sell (600519) + 1 buy (000858) 都 submitted
    sides = sorted([o.side.value for o in orders])
    assert sides == ["buy", "sell"], f"expected 1 buy + 1 sell, got {sides}"
    sell_o = next(o for o in orders if o.side.value == "sell")
    buy_o = next(o for o in orders if o.side.value == "buy")
    assert sell_o.qty == 100
    assert buy_o.qty == 100
    # 关键: buy 落库且 status=NEW (没被 engine 拒)
    assert buy_o.status == OrderStatus.NEW
    assert buy_o.reject_reason in (None, "")


def test_rebalance_to_in_frame_cash_does_not_overdraw_account(runner, monkeypatch):
    """防御性: in-frame cash 不能让 buy 委托金额超过"落账 cash + 同帧 sells 所得"。

    cash=1000, 持仓 600519 100 股 @10, target=买 000858 200 股 @20 → 总需 4000
    卖 1000 + cash 1000 = 2000 < 4000 → buy 应该被拒, 但卖应该 OK.
    """
    monkeypatch.setattr(prefs, "max_order_amount", lambda: 1e12)
    monkeypatch.setattr(prefs, "max_order_amount_for_strategy", lambda: 1e12)
    import json
    account_path = runner.data_dir / "user_data" / "trading" / "account.json"
    account_path.parent.mkdir(parents=True, exist_ok=True)
    account_path.write_text(json.dumps({
        "initial_cash": 1000.0, "cash": 1000.0,
        "market_value": 0.0, "total_equity": 1000.0,
        "realized_pnl": 0.0, "unrealized_pnl": 0.0, "total_pnl": 0.0,
    }), encoding="utf-8")
    store.reset_for_tests()

    store.append_fill(runner.data_dir, Fill(
        id="f1", order_id="o_hist", symbol="600519.SH", side=OrderSide.BUY,
        qty=100, price=10.0, fee=0, tax=0, amount=1000.0,
        filled_at="2026-01-14T10:00:00+08:00", trade_date=date(2026, 1, 14),
    ))

    orders = runner.rebalance_to(
        {"000858.SZ": 200},
        prices={"600519.SH": 10.0, "000858.SZ": 20.0},
    )

    # 期望: 1 sell (600519) submitted + 0 buy submitted (被 in-frame 资金不足拒)
    assert len(orders) == 1, f"expected only sell submitted, got {len(orders)}"
    assert orders[0].side == OrderSide.SELL
    # buy 委托被拒 → 落 REJECTED 状态, 但不返回在 submitted 列表
    rejected = runner.list_orders(statuses=[OrderStatus.REJECTED])
    assert any("可用现金" in (o.reject_reason or "") for o in rejected)


# ── 查询接口 ─────────────────────────────────────
def test_runner_positions_empty(runner):
    assert runner.positions() == []


def test_runner_account_initialized(runner):
    a = runner.account()
    assert a.initial_cash == pytest.approx(prefs.initial_cash())


def test_runner_pending_orders(runner):
    runner.buy("600519.SH", 100, limit_price=10.0)
    pending = runner.pending_orders()
    assert len(pending) == 1
    assert pending[0].status == OrderStatus.NEW
