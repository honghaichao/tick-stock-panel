"""PnL 模块测试 — 快照追加 / 读取 / 指标计算 (今日 / 总收益 / 最大回撤 / 胜率)."""
from __future__ import annotations

from datetime import date, timedelta, timezone

import pytest

from app.services.trading import pnl as pnl_mod
from app.services.trading import store
from app.services.trading.models import Fill, OrderSide


@pytest.fixture(autouse=True)
def _clean_store():
    store.reset_for_tests()
    yield


CN = timezone(timedelta(hours=8))


def _ts(s: str) -> str:
    return s


def test_append_and_list_history(tmp_path):
    pnl_mod.append_snapshot(tmp_path, pnl_mod.PnLSnapshot(
        ts="2026-01-15T10:00:00+08:00",
        trade_date="2026-01-15",
        total_equity=1_010_000.0, cash=500_000.0, market_value=510_000.0,
        realized_pnl=0.0, unrealized_pnl=10_000.0, total_pnl=10_000.0,
        trigger="fill",
    ))
    pnl_mod.append_snapshot(tmp_path, pnl_mod.PnLSnapshot(
        ts="2026-01-15T10:01:00+08:00",
        trade_date="2026-01-15",
        total_equity=1_020_000.0, cash=500_000.0, market_value=520_000.0,
        realized_pnl=0.0, unrealized_pnl=20_000.0, total_pnl=20_000.0,
        trigger="fill",
    ))
    items = pnl_mod.list_history(tmp_path)
    assert len(items) == 2
    assert items[-1]["total_equity"] == 1_020_000.0


def test_list_history_filters(tmp_path):
    for date_, equity, trigger in [
        ("2026-01-15", 1_000_000.0, "fill"),
        ("2026-01-15", 1_005_000.0, "tick"),
        ("2026-01-16", 1_010_000.0, "fill"),
    ]:
        pnl_mod.append_snapshot(tmp_path, pnl_mod.PnLSnapshot(
            ts=f"{date_}T10:00:00+08:00", trade_date=date_,
            total_equity=equity, cash=equity, market_value=0.0,
            realized_pnl=0.0, unrealized_pnl=0.0, total_pnl=0.0,
            trigger=trigger,
        ))
    items = pnl_mod.list_history(tmp_path, start="2026-01-16")
    assert len(items) == 1
    assert items[0]["trade_date"] == "2026-01-16"


def test_compute_metrics_no_data(tmp_path):
    """无快照 / 无 fill: 返回全 0 + default initial_cash (1_000_000)."""
    m = pnl_mod.compute_metrics(tmp_path)
    assert m.total_pnl == 0.0
    assert m.current_equity == pytest.approx(1_000_000.0)
    assert m.initial_cash == pytest.approx(1_000_000.0)
    assert m.total_trades == 0
    assert m.win_rate == 0.0


def test_compute_metrics_with_fills_and_snapshots(tmp_path):
    """构造一笔买 + 一笔卖 (盈利) → 胜率=100%, total_pnl > 0."""
    # 写入一笔历史 fill: 100 股 @ 10 元, 然后 100 股 @ 12 元 (卖出, 盈利 200)
    store.append_fill(tmp_path, Fill(
        id="f1", order_id="o1", symbol="600519.SH", side=OrderSide.BUY,
        qty=100, price=10.0, fee=5.0, tax=0.0, amount=1000.0,
        filled_at="2026-01-14T10:00:00+08:00", trade_date=date(2026, 1, 14),
    ))
    store.append_fill(tmp_path, Fill(
        id="f2", order_id="o2", symbol="600519.SH", side=OrderSide.SELL,
        qty=100, price=12.0, fee=5.0, tax=12.0, amount=1200.0,
        filled_at="2026-01-15T10:00:00+08:00", trade_date=date(2026, 1, 15),
    ))
    # 模拟当前账户态
    account = store.load_account(tmp_path, initial_cash=100_000.0)
    account.realized_pnl = 200.0
    account.total_pnl = 200.0
    store.save_account(tmp_path, account)
    # 写两个快照 (峰值 + 当前)
    pnl_mod.append_snapshot(tmp_path, pnl_mod.PnLSnapshot(
        ts="2026-01-14T10:00:00+08:00", trade_date="2026-01-14",
        total_equity=100_000.0, cash=99_995.0, market_value=0.0,
        realized_pnl=0.0, unrealized_pnl=0.0, total_pnl=0.0,
        trigger="fill",
    ))
    pnl_mod.append_snapshot(tmp_path, pnl_mod.PnLSnapshot(
        ts="2026-01-15T10:00:00+08:00", trade_date="2026-01-15",
        total_equity=100_178.0, cash=100_178.0, market_value=0.0,
        realized_pnl=200.0, unrealized_pnl=0.0, total_pnl=200.0,
        trigger="fill",
    ))

    m = pnl_mod.compute_metrics(tmp_path)
    assert m.total_pnl == pytest.approx(200.0)
    assert m.total_trades == 1
    assert m.winning_trades == 1
    assert m.losing_trades == 0
    assert m.win_rate == pytest.approx(1.0)


def test_compute_metrics_max_drawdown(tmp_path):
    """构造峰值 → 谷值 → 当前: max_drawdown 反映最大跌幅."""
    # 峰值 110 万, 谷值 90 万, 当前 95 万 → max_dd = (110-90)/110 = 0.1818
    for date_, eq in [
        ("2026-01-10", 1_000_000.0),
        ("2026-01-11", 1_100_000.0),    # 峰值
        ("2026-01-12", 900_000.0),      # 谷值
        ("2026-01-13", 950_000.0),      # 当前
    ]:
        pnl_mod.append_snapshot(tmp_path, pnl_mod.PnLSnapshot(
            ts=f"{date_}T10:00:00+08:00", trade_date=date_,
            total_equity=eq, cash=eq, market_value=0.0,
            realized_pnl=0.0, unrealized_pnl=0.0, total_pnl=0.0,
            trigger="fill",
        ))
    # 同步账户到当前
    acc = store.load_account(tmp_path, initial_cash=1_000_000.0)
    acc.cash = 950_000.0
    acc.realized_pnl = -50_000.0
    acc.unrealized_pnl = 0.0
    acc.total_pnl = -50_000.0
    store.save_account(tmp_path, acc)
    m = pnl_mod.compute_metrics(tmp_path)
    expected_dd = (1_100_000.0 - 900_000.0) / 1_100_000.0
    assert m.max_drawdown == pytest.approx(expected_dd, rel=1e-3)
    assert m.max_drawdown_peak == pytest.approx(1_100_000.0)
    assert m.max_drawdown_trough == pytest.approx(900_000.0)
    assert m.max_drawdown_date == "2026-01-11"


def test_compute_metrics_losing_trade(tmp_path):
    """卖在亏损位 → 胜率 0%, total_trades = 1."""
    store.append_fill(tmp_path, Fill(
        id="f1", order_id="o1", symbol="600519.SH", side=OrderSide.BUY,
        qty=100, price=10.0, fee=5.0, tax=0.0, amount=1000.0,
        filled_at="2026-01-14T10:00:00+08:00", trade_date=date(2026, 1, 14),
    ))
    store.append_fill(tmp_path, Fill(
        id="f2", order_id="o2", symbol="600519.SH", side=OrderSide.SELL,
        qty=100, price=9.0, fee=5.0, tax=9.0, amount=900.0,
        filled_at="2026-01-15T10:00:00+08:00", trade_date=date(2026, 1, 15),
    ))
    acc = store.load_account(tmp_path, initial_cash=100_000.0)
    acc.cash = 99881.0  # 100000 - 1000 - 5 + 900 - 5 - 9 = 99881
    acc.realized_pnl = -119.0   # 卖出 100*9 - 买入 100*10 - 费税 14 = -114 + 买入手续费 5 = -119
    acc.total_pnl = -119.0
    store.save_account(tmp_path, acc)
    m = pnl_mod.compute_metrics(tmp_path)
    assert m.total_trades == 1
    assert m.winning_trades == 0
    assert m.losing_trades == 1
    assert m.win_rate == 0.0
    assert m.total_pnl < 0
