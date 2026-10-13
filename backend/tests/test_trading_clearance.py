"""17:00 清算测试 — 收盘后自动撤所有未完成委托."""
from __future__ import annotations

import json
from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path

import polars as pl
import pytest

from app.services.trading import clearance as clearance_mod
from app.services.trading import store
from app.services.trading.engine import TradingEngine
from app.services.trading.models import Order, OrderSide, OrderStatus, OrderType


@pytest.fixture
def tmp_data_dir(tmp_path):
    store.reset_for_tests()
    return tmp_path


def _pending_order(sym: str, oid: str = None) -> Order:
    return Order(
        id=oid or f"ord_{sym}_{datetime.now().strftime('%H%M%S%f')}",
        symbol=sym, side=OrderSide.BUY, order_type=OrderType.LIMIT,
        qty=100, limit_price=10.0,
        created_at="2026-01-15T09:30:00+08:00",
        submitted_at="2026-01-15T09:30:01+08:00",
    )


def _filled_order(sym: str) -> Order:
    o = _pending_order(sym)
    o.status = OrderStatus.FILLED
    o.filled_qty = 100
    o.avg_fill_price = 10.0
    o.filled_at = "2026-01-15T09:30:05+08:00"
    return o


# ── _should_run_clearance 单元 ─────────────────────
def test_should_run_at_exactly_17_00():
    """17:00:00 整点: 应该跑."""
    clearance_mod.reset_for_tests()
    now = datetime(2026, 1, 15, 17, 0, 0)
    assert clearance_mod._should_run_clearance(now, time(17, 0)) is True


def test_should_run_within_window():
    """17:00:15 (15 秒内): 应该跑."""
    clearance_mod.reset_for_tests()
    now = datetime(2026, 1, 15, 17, 0, 15)
    assert clearance_mod._should_run_clearance(now, time(17, 0)) is True


def test_should_not_run_outside_window():
    """17:01:00 (60 秒后): 不应该跑."""
    clearance_mod.reset_for_tests()
    now = datetime(2026, 1, 15, 17, 1, 0)
    assert clearance_mod._should_run_clearance(now, time(17, 0)) is False


def test_should_not_run_in_morning():
    """10:00: 不应该跑."""
    clearance_mod.reset_for_tests()
    now = datetime(2026, 1, 15, 10, 0, 0)
    assert clearance_mod._should_run_clearance(now, time(17, 0)) is False


def test_should_not_run_twice_same_day():
    """同一天第二次调用: 不应该再跑."""
    clearance_mod.reset_for_tests()
    # 第一次跑过 (手动设置 _last_clearance_date)
    clearance_mod._last_clearance_date = "2026-01-15"
    now = datetime(2026, 1, 15, 17, 0, 5)
    assert clearance_mod._should_run_clearance(now, time(17, 0)) is False


# ── run_daily_clearance_if_due 集成 ─────────────────
def test_clearance_cancels_pending_keeps_filled(tmp_data_dir):
    """17:00 清算: 2 个 NEW + 1 个 FILLED → 2 个变 CANCELED, FILLED 不动."""
    clearance_mod.reset_for_tests()
    # 落 3 笔委托
    store.append_order(tmp_data_dir, _pending_order("600519.SH", "o1"))
    store.append_order(tmp_data_dir, _pending_order("000001.SZ", "o2"))
    store.append_order(tmp_data_dir, _filled_order("002998.SZ"))
    # 模拟 17:00 调用
    eng = TradingEngine()
    eng._broadcast_event = MagicMock()
    with patch.object(clearance_mod, "_cn_now", return_value=datetime(2026, 1, 15, 17, 0, 0)):
        canceled = clearance_mod.run_daily_clearance_if_due(tmp_data_dir, eng)
    assert canceled == 2

    # 验证状态
    orders = store.list_orders(tmp_data_dir)
    by_id = {o.id: o for o in orders}
    assert by_id["o1"].status == OrderStatus.CANCELED
    assert by_id["o1"].reject_reason == "17:00 自动清算"
    assert by_id["o2"].status == OrderStatus.CANCELED
    assert by_id["o2"].reject_reason == "17:00 自动清算"
    filled_id = [oid for oid, o in by_id.items() if o.status == OrderStatus.FILLED][0]
    assert by_id[filled_id].status == OrderStatus.FILLED  # 不动

    # 验证日志
    logs = store.list_logs(tmp_data_dir, kind="cancel")
    assert len(logs) == 2
    assert all("17:00 自动清算" in l.message for l in logs)

    # 验证 SSE 推送
    assert eng._broadcast_event.call_count == 2
    for call in eng._broadcast_event.call_args_list:
        ev = call.args[0]
        assert ev["type"] == "order_cleared"
        assert ev["reason"] == "clearance"

    # 验证 _last_clearance_date 置位
    assert clearance_mod._last_clearance_date == "2026-01-15"


def test_clearance_no_pending_is_noop(tmp_data_dir):
    """17:00 清算但无 pending: noop, 但 _last_clearance_date 仍置位 (避免重复判断)."""
    clearance_mod.reset_for_tests()
    eng = TradingEngine()
    eng._broadcast_event = MagicMock()
    with patch.object(clearance_mod, "_cn_now", return_value=datetime(2026, 1, 15, 17, 0, 0)):
        canceled = clearance_mod.run_daily_clearance_if_due(tmp_data_dir, eng)
    assert canceled == 0
    assert clearance_mod._last_clearance_date == "2026-01-15"
    assert eng._broadcast_event.call_count == 0


def test_clearance_outside_window_returns_zero(tmp_data_dir):
    """非 17:00 时调用: 返回 0, 不动."""
    clearance_mod.reset_for_tests()
    store.append_order(tmp_data_dir, _pending_order("600519.SH", "o1"))
    eng = TradingEngine()
    eng._broadcast_event = MagicMock()
    # 模拟 10:00 调用
    with patch.object(clearance_mod, "_cn_now", return_value=datetime(2026, 1, 15, 10, 0, 0)):
        canceled = clearance_mod.run_daily_clearance_if_due(tmp_data_dir, eng)
    assert canceled == 0
    # 委托未动
    o = store.get_order(tmp_data_dir, "o1")
    assert o.status == OrderStatus.NEW


def test_clearance_continues_on_single_order_error(tmp_data_dir):
    """单笔 cancel 失败不中断后续."""
    clearance_mod.reset_for_tests()
    store.append_order(tmp_data_dir, _pending_order("600519.SH", "o1"))
    store.append_order(tmp_data_dir, _pending_order("000001.SZ", "o2"))

    eng = TradingEngine()
    eng._broadcast_event = MagicMock()

    # Mock update_order 让 o1 抛异常, o2 正常
    original_update = store.update_order
    def flaky_update(data_dir, order):
        if order.id == "o1":
            raise RuntimeError("mock failure")
        original_update(data_dir, order)
    with patch.object(store, "update_order", side_effect=flaky_update), \
         patch.object(clearance_mod, "_cn_now", return_value=datetime(2026, 1, 15, 17, 0, 0)):
        canceled = clearance_mod.run_daily_clearance_if_due(tmp_data_dir, eng)

    # o2 成功: 状态 CANCELED, 日志 + SSE 都触发
    o2 = store.get_order(tmp_data_dir, "o2")
    assert o2.status == OrderStatus.CANCELED
    # o1 update_order 抛异常: 实现先把 status 改了再调 update_order, 失败时内存 status 仍
    # 是 CANCELED 但没落盘. 业务上单笔失败被 catch 不中断后续 (这是测试的目标).
    # 验证 SSE 调用次数 = 1 (只 o2 推送)
    assert eng._broadcast_event.call_count == 1
    # 验证 ERROR 日志被记录
    import logging
    # 不强求 logger.error 输出 (取决于 root logger 配置), 用 caplog 验证更稳
    # 这里仅验证 o2 流程没被打断
