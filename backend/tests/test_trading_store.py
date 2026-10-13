"""store.py 测试 — JSONL 持久化 + 内存索引 + 落盘 / 重启加载一致性。"""
from __future__ import annotations

from datetime import date

from app.services.trading import store
from app.services.trading.models import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TradeLogEntry,
    TradeLogKind,
)


def _order(oid: str = "o1", status: OrderStatus = OrderStatus.NEW) -> Order:
    return Order(
        id=oid, symbol="600519.SH", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, qty=100, limit_price=10.0,
        status=status, created_at="2026-01-15T09:30:00+08:00",
    )


def _fill(fid: str = "f1") -> Fill:
    return Fill(
        id=fid, order_id="o1", symbol="600519.SH", side=OrderSide.BUY,
        qty=100, price=10.0, fee=5.0, tax=0.0, amount=1000.0,
        filled_at="2026-01-15T10:00:00+08:00", trade_date=date(2026, 1, 15),
    )


def test_append_and_list_orders(tmp_path):
    store.reset_for_tests()
    o1 = _order("o1")
    o2 = _order("o2")
    o2.created_at = "2026-01-15T11:00:00+08:00"
    store.append_order(tmp_path, o1)
    store.append_order(tmp_path, o2)
    items = store.list_orders(tmp_path, limit=None)
    assert len(items) == 2
    # 倒序: o2 (11:00) 在前
    assert items[0].id == "o2"


def test_update_order_overrides_status(tmp_path):
    """update_order 应当把内存态和 jsonl 末尾状态都更新."""
    store.reset_for_tests()
    o = _order("o1")
    store.append_order(tmp_path, o)
    o.status = OrderStatus.FILLED
    o.filled_qty = 100
    store.update_order(tmp_path, o)
    got = store.get_order(tmp_path, "o1")
    assert got.status == OrderStatus.FILLED
    assert got.filled_qty == 100


def test_reload_after_reset_preserves_latest(tmp_path):
    """reset_for_tests 后重新加载, 应该读到末次状态 (update 后的)."""
    store.reset_for_tests()
    o = _order("o1", OrderStatus.NEW)
    store.append_order(tmp_path, o)
    o.status = OrderStatus.FILLED
    store.update_order(tmp_path, o)
    # 模拟进程重启
    store.reset_for_tests()
    got = store.get_order(tmp_path, "o1")
    assert got.status == OrderStatus.FILLED


def test_pending_orders_filters_status(tmp_path):
    store.reset_for_tests()
    store.append_order(tmp_path, _order("o1", OrderStatus.NEW))
    store.append_order(tmp_path, _order("o2", OrderStatus.PARTIAL))
    store.append_order(tmp_path, _order("o3", OrderStatus.FILLED))
    store.append_order(tmp_path, _order("o4", OrderStatus.CANCELED))
    pend = store.pending_orders(tmp_path)
    ids = sorted([p.id for p in pend])
    assert ids == ["o1", "o2"]


def test_append_fill_and_list_fills(tmp_path):
    store.reset_for_tests()
    f1 = _fill("f1")
    f2 = _fill("f2")
    f2.filled_at = "2026-01-15T11:00:00+08:00"
    store.append_fill(tmp_path, f1)
    store.append_fill(tmp_path, f2)
    items = store.list_fills(tmp_path, limit=None)
    assert len(items) == 2
    # 倒序
    assert items[0].id == "f2"


def test_logs_prune_when_exceeds_cap(tmp_path, monkeypatch):
    """填满 LOG_MAX_RECORDS 后再 append, 应触发 prune."""
    monkeypatch.setattr(store, "LOG_MAX_RECORDS", 5)
    monkeypatch.setattr(store, "LOG_PRUNE_EVERY", 2)  # 每 2 次写盘就 prune, 让测试能命中
    store.reset_for_tests()
    for i in range(10):
        e = TradeLogEntry(
            id=f"lg{i}", kind=TradeLogKind.MATCHER_TICK,
            ts=f"2026-01-15T10:{i:02d}:00+08:00",
            symbol=None, order_id=None, fill_id=None,
            message=f"tick {i}", details=None,
        )
        store.append_log(tmp_path, e)
    items = store.list_logs(tmp_path, limit=None)
    # 触发 prune 后保留最新 5 条; list_logs 返回时按时间倒序 (最新优先).
    assert len(items) == 5
    assert items[0].id == "lg9"   # 最新
    assert items[-1].id == "lg5"  # 最旧


def test_account_initializes_when_missing(tmp_path):
    store.reset_for_tests()
    account = store.load_account(tmp_path, initial_cash=500_000.0)
    assert account.initial_cash == 500_000.0
    assert account.cash == 500_000.0
    # 落盘后再读
    store.reset_for_tests()
    again = store.load_account(tmp_path, initial_cash=500_000.0)
    assert again.initial_cash == 500_000.0


def test_save_account_updates_persisted(tmp_path):
    store.reset_for_tests()
    acc = store.load_account(tmp_path, initial_cash=100.0)
    acc.cash = 50.0
    store.save_account(tmp_path, acc)
    store.reset_for_tests()
    again = store.load_account(tmp_path, initial_cash=100.0)
    assert again.cash == 50.0


def test_update_order_atomic_rewrite_does_not_grow_file(tmp_path):
    """update_order 原子覆写, 不再 append: 同一 id 多次更新后, 文件行数 = id 数,
    重启加载能拿到每 id 最新状态. 防 orders.jsonl 无限增长回归.
    """
    store.reset_for_tests()
    for i in range(3):
        order = Order(
            id=f"ord_x{i}", symbol="600519.SH", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, qty=100, limit_price=10.0,
            status=OrderStatus.NEW,
            created_at=f"2026-01-{15 + i}T09:30:00+08:00",
        )
        store.append_order(tmp_path, order)

    orders_path = tmp_path / "user_data" / "trading" / "orders.jsonl"
    assert orders_path.exists()
    initial_lines = sum(1 for _ in orders_path.open(encoding="utf-8") if _.strip())
    assert initial_lines == 3

    # 多次 update 同 id, 走 NEW → PARTIAL → FILLED → CANCELED 状态机
    target = store.get_order(tmp_path, "ord_x0")
    target.status = OrderStatus.PARTIAL
    store.update_order(tmp_path, target)
    target.status = OrderStatus.FILLED
    store.update_order(tmp_path, target)
    target.status = OrderStatus.CANCELED
    store.update_order(tmp_path, target)

    # 文件应仍只有 3 行 (一个 id 一行)
    after_lines = sum(1 for _ in orders_path.open(encoding="utf-8") if _.strip())
    assert after_lines == 3

    # 重启加载应拿到 ord_x0 的最新态 (CANCELED), 不是中间任何状态
    store.reset_for_tests()
    again = store.get_order(tmp_path, "ord_x0")
    assert again is not None
    assert again.status == OrderStatus.CANCELED
