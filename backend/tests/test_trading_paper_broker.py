"""PaperBroker 撮合逻辑测试 — 限价穿越 / 市价 / T+1 / 涨跌停 / 状态机。"""
from __future__ import annotations

import secrets
from datetime import datetime

import polars as pl

from app.services.trading.broker import limit_crossed
from app.services.trading.models import (
    Order,
    OrderDraft,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.services.trading.paper_broker import PaperBroker
from app.services.trading.slippage import SlippageConfig, SlippageKind


def _bar(open_: float, high: float, low: float, close: float = 0.0) -> pl.DataFrame:
    """构造一根分钟 K; 默认 close 字段 (撮合只读 open/high/low)."""
    return pl.DataFrame({
        "symbol": ["600519.SH"],
        "datetime": [datetime(2026, 1, 15, 10, 0)],
        "open": [open_],
        "high": [high],
        "low": [low],
        "close": [close or low],
        "volume": [1000.0],
    })


def _order(side: str, qty: int, limit_price: float | None = None, *, oid: str | None = None) -> Order:
    return Order(
        id=oid or f"ord_{side}_{qty}_{limit_price}_{secrets.token_hex(3)}",
        symbol="600519.SH",
        side=OrderSide(side),
        order_type=OrderType.LIMIT if limit_price is not None else OrderType.MARKET,
        qty=qty,
        limit_price=limit_price,
        created_at="2026-01-15T09:30:00+08:00",
    )


# ── limit_crossed 单元 ──────────────────────────────
def test_limit_crossed_buy_in_range():
    bar = _bar(10.0, 11.0, 9.5)
    assert limit_crossed("buy", 10.0, bar) is True
    assert limit_crossed("buy", 9.5, bar) is True
    assert limit_crossed("buy", 9.4, bar) is False    # low > max → no fill
    assert limit_crossed("buy", 11.0, bar) is True    # high==11 OK


def test_limit_crossed_sell_in_range():
    bar = _bar(10.0, 11.0, 9.5)
    assert limit_crossed("sell", 10.0, bar) is True
    assert limit_crossed("sell", 11.0, bar) is True
    assert limit_crossed("sell", 11.1, bar) is False   # high < min → no fill


def test_limit_crossed_empty_bar():
    assert limit_crossed("buy", 10.0, pl.DataFrame()) is False


# ── PaperBroker.submit 委托受理 ─────────────────────
def test_paper_broker_submit_limit_without_price_downgrades_to_market():
    """限价单 limit_price=None 时, paper_broker 静默降级为市价单 (不放拒单).

    历史 case: 2026-09-30 09:26:42 ord_2f932170aaba 002998.SZ 因这个路径直接 reject,
    浪费 1 笔通过的撮合. 上游 executor 已经按有价/无价分单, 这里再兜底一道.
    行为: 接受 (accepted=True), order.order_type 改为 market, limit_price 置 None.
    """
    pb = PaperBroker()
    draft = OrderDraft(symbol="600519.SH", side="buy", order_type="limit", qty=100, limit_price=None)
    order = _order("buy", 100, None)
    ack = pb.submit(draft, order)
    assert ack.accepted is True
    assert ack.order.order_type.value == "market"
    assert ack.order.limit_price is None


def test_paper_broker_submit_market_rejects_price():
    pb = PaperBroker()
    draft = OrderDraft(symbol="600519.SH", side="buy", order_type="market", qty=100, limit_price=10.0)
    order = _order("buy", 100, 10.0)
    ack = pb.submit(draft, order)
    assert ack.accepted is False


def test_paper_broker_submit_accepts_limit_and_market():
    pb = PaperBroker()
    draft = OrderDraft(symbol="600519.SH", side="buy", order_type="limit", qty=100, limit_price=10.0)
    order = _order("buy", 100, 10.0)
    ack = pb.submit(draft, order)
    assert ack.accepted is True

    draft2 = OrderDraft(symbol="600519.SH", side="buy", order_type="market", qty=100)
    order2 = _order("buy", 100, None)
    ack2 = pb.submit(draft2, order2)
    assert ack2.accepted is True


# ── PaperBroker.cancel ─────────────────────────────
def test_paper_broker_cancel_new_order_ok():
    pb = PaperBroker()
    order = _order("buy", 100, 10.0)
    order.status = OrderStatus.NEW
    ok, reason = pb.cancel(order)
    assert ok is True
    assert reason is None


def test_paper_broker_cancel_filled_order_refused():
    pb = PaperBroker()
    order = _order("buy", 100, 10.0)
    order.status = OrderStatus.FILLED
    ok, reason = pb.cancel(order)
    assert ok is False
    assert "filled" in (reason or "").lower() or "已成交" in (reason or "")


# ── PaperBroker.match_tick 限价买单 ───────────────
def test_match_limit_buy_gap_down_fills_at_open():
    """低开: open(9.0) < limit(10.0), 成交价 = open (实际无法在低于 limit 成交)。"""
    pb = PaperBroker()
    bar = _bar(9.0, 9.5, 8.5)
    order = _order("buy", 100, 10.0)
    decisions = pb.match_tick([order], {"600519.SH": bar})
    assert len(decisions) == 1
    d = decisions[0]
    assert d.matched is True
    assert d.fill_price == 9.0
    assert d.fill_qty == 100


def test_match_limit_buy_normal_fill_at_limit():
    """正常穿越: bar.low == limit, 成交价 = limit."""
    pb = PaperBroker()
    bar = _bar(10.5, 10.8, 10.0)
    order = _order("buy", 100, 10.0)
    decisions = pb.match_tick([order], {"600519.SH": bar})
    d = decisions[0]
    assert d.matched is True
    assert d.fill_price == 10.0


def test_match_limit_buy_no_fill_when_range_above_limit():
    """区间都在 limit 之上: 不成交."""
    pb = PaperBroker()
    bar = _bar(11.0, 11.5, 10.5)
    order = _order("buy", 100, 10.0)
    decisions = pb.match_tick([order], {"600519.SH": bar})
    d = decisions[0]
    assert d.matched is False
    assert "买单未触发" in (d.reason or "")


def test_match_limit_buy_limit_up_blocks_fill():
    """一字涨停 (open == high == low == upper_limit > limit): 不成交."""
    pb = PaperBroker()
    bar = _bar(11.0, 11.0, 11.0)
    order = _order("buy", 100, 10.0)
    decisions = pb.match_tick([order], {"600519.SH": bar})
    d = decisions[0]
    assert d.matched is False


# ── PaperBroker.match_tick 限价卖单 ───────────────
def test_match_limit_sell_gap_up_fills_at_open():
    pb = PaperBroker()
    bar = _bar(11.0, 11.5, 10.5)
    order = _order("sell", 100, 10.0)
    decisions = pb.match_tick([order], {"600519.SH": bar})
    d = decisions[0]
    assert d.matched is True
    assert d.fill_price == 11.0    # 高开 → 成交价 = open


def test_match_limit_sell_normal_fill_at_limit():
    pb = PaperBroker()
    bar = _bar(9.5, 10.0, 9.0)
    order = _order("sell", 100, 10.0)
    decisions = pb.match_tick([order], {"600519.SH": bar})
    d = decisions[0]
    assert d.matched is True
    assert d.fill_price == 10.0


def test_match_limit_sell_limit_down_blocks_fill():
    """一字跌停 (open == high == low == lower_limit < limit): 不成交."""
    pb = PaperBroker()
    bar = _bar(9.0, 9.0, 9.0)
    order = _order("sell", 100, 10.0)
    decisions = pb.match_tick([order], {"600519.SH": bar})
    d = decisions[0]
    assert d.matched is False


# ── PaperBroker.match_tick 市价单 ─────────────────
def test_match_market_order_fills_at_close():
    """聚宽语义: 非第一根 K, 市价单按 bar.close 撮合 (不是 open)."""
    pb = PaperBroker()
    bar = _bar(open_=10.0, high=10.5, low=9.5, close=9.8)
    order = _order("buy", 100, None)
    decisions = pb.match_tick([order], {"600519.SH": bar})
    d = decisions[0]
    assert d.matched is True
    assert d.fill_price == 9.8    # close, 不是 open
    assert d.fill_qty == 100


def test_match_market_order_first_bar_of_day_uses_open():
    """聚宽语义: 9:30 第一根 K, 市价单按 bar.open 撮合."""
    pb = PaperBroker()
    bar = _bar(open_=10.0, high=10.5, low=9.5, close=9.8)
    order = _order("buy", 100, None)
    decisions = pb.match_tick(
        [order], {"600519.SH": bar},
        first_bar_of_day={"600519.SH": True},
    )
    d = decisions[0]
    assert d.matched is True
    assert d.fill_price == 10.0   # 第一根 K 用 open


def test_match_no_bar_skips():
    """无 bar 数据 (停牌/拉取失败): 不成交, 继续等待."""
    pb = PaperBroker()
    order = _order("buy", 100, 10.0)
    decisions = pb.match_tick([order], {})
    d = decisions[0]
    assert d.matched is False
    assert "无分钟 K" in (d.reason or "")


def test_match_open_zero_skips():
    """停牌: open = 0, 视为无数据."""
    pb = PaperBroker()
    bar = _bar(0.0, 0.0, 0.0)
    order = _order("buy", 100, 10.0)
    decisions = pb.match_tick([order], {"600519.SH": bar})
    d = decisions[0]
    assert d.matched is False
    assert "字段无效" in (d.reason or "")


# ── 多委托独立判定 ─────────────────────────────────
def test_match_multiple_orders_each_with_one():
    pb = PaperBroker()
    # 两个委托各自有自己的 symbol, bar 也要分别对应.
    bar_buy = _bar(9.0, 9.5, 8.5)
    bar_sell = _bar(11.0, 11.5, 10.5)
    o_buy = Order(id="o1", symbol="600519.SH", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                  qty=100, limit_price=10.0, created_at="2026-01-15T09:30:00+08:00")
    o_sell = Order(id="o2", symbol="000001.SZ", side=OrderSide.SELL, order_type=OrderType.LIMIT,
                   qty=100, limit_price=11.0, created_at="2026-01-15T09:30:00+08:00")
    decisions = pb.match_tick([o_buy, o_sell], {
        "600519.SH": bar_buy,
        "000001.SZ": bar_sell,
    })
    assert len(decisions) == 2
    assert decisions[0].matched is True
    assert decisions[1].matched is True


def test_match_skips_cancelled():
    """已被撤单的委托 (broker 内部 _cancelled 集合) 不进入撮合决策列表."""
    pb = PaperBroker()
    bar = _bar(9.0, 9.5, 8.5)
    o1 = _order("buy", 100, 10.0, oid="o1")
    o2 = _order("buy", 100, 10.0, oid="o2")
    pb.cancel(o1)
    decisions = pb.match_tick([o1, o2], {"600519.SH": bar})
    # o1 不进, o2 进: 决策列表只有 1 项 (o2)
    assert len(decisions) == 1
    assert decisions[0].matched is True
    assert decisions[0].order_id == "o2"


def test_limit_order_without_price_returns_explicit_no_match():
    """限价单 limit_price=None 时显式返回 not-matched reason (不再依赖 assert).

    防止 python -O 启动下原 assert 被剥离 → TypeError 的回归。
    """
    pb = PaperBroker()
    bar = _bar(10.0, 10.5, 9.5)
    o = Order(
        id="o_no_price", symbol="600519.SH",
        side=OrderSide.BUY, order_type=OrderType.LIMIT,
        qty=100, limit_price=None,
        created_at="2026-01-15T09:30:00+08:00",
    )
    d = pb._match_one(o, bar, slippage=SlippageConfig(), order_volume_ratio=1.0, is_first_bar=False)
    assert d.matched is False
    assert "缺" in d.reason
    assert d.fill_price is None


# ── 聚宽式撮合新语义 ───────────────────────────────
def test_match_market_with_price_related_slippage_buy():
    """PriceRelatedSlippage(0.001) 买: 撮合价 = close * (1 + 0.001)."""
    pb = PaperBroker()
    bar = _bar(open_=10.0, high=10.5, low=9.5, close=10.0)
    order = _order("buy", 100, None)
    decisions = pb.match_tick(
        [order], {"600519.SH": bar},
        slippage=SlippageConfig(kind=SlippageKind.PRICE_RELATED, value=0.001),
    )
    d = decisions[0]
    assert d.matched is True
    assert abs(d.fill_price - 10.01) < 0.0001    # 10 * 1.001


def test_match_market_with_price_related_slippage_sell():
    """PriceRelatedSlippage(0.001) 卖: 撮合价 = close * (1 - 0.001)."""
    pb = PaperBroker()
    bar = _bar(open_=10.0, high=10.5, low=9.5, close=10.0)
    order = _order("sell", 100, None)
    decisions = pb.match_tick(
        [order], {"600519.SH": bar},
        slippage=SlippageConfig(kind=SlippageKind.PRICE_RELATED, value=0.001),
    )
    d = decisions[0]
    assert d.matched is True
    assert abs(d.fill_price - 9.99) < 0.0001     # 10 * 0.999


def test_match_market_with_tick_related_slippage():
    """TickRelatedSlippage(2) 买: 撮合价 = close + 2 * 0.01 = 10.02 (tick_size=0.01)."""
    pb = PaperBroker()
    bar = _bar(open_=10.0, high=10.5, low=9.5, close=10.0)
    order = _order("buy", 100, None)
    decisions = pb.match_tick(
        [order], {"600519.SH": bar},
        slippage=SlippageConfig(kind=SlippageKind.TICK_RELATED, value=2),
    )
    d = decisions[0]
    assert d.matched is True
    assert abs(d.fill_price - 10.02) < 0.0001


def test_match_market_with_fixed_slippage():
    """FixedSlippage(0.05) 卖: 撮合价 = close - 0.05 = 9.95."""
    pb = PaperBroker()
    bar = _bar(open_=10.0, high=10.5, low=9.5, close=10.0)
    order = _order("sell", 100, None)
    decisions = pb.match_tick(
        [order], {"600519.SH": bar},
        slippage=SlippageConfig(kind=SlippageKind.FIXED, value=0.05),
    )
    d = decisions[0]
    assert d.matched is True
    assert abs(d.fill_price - 9.95) < 0.0001


def test_match_market_with_slippage_none_no_effect():
    """NONE slippage 默认不滑, 撮合价 = close."""
    pb = PaperBroker()
    bar = _bar(open_=10.0, high=10.5, low=9.5, close=9.8)
    order = _order("buy", 100, None)
    decisions = pb.match_tick([order], {"600519.SH": bar}, slippage=SlippageConfig())
    d = decisions[0]
    assert d.fill_price == 9.8


def test_match_market_volume_ratio_caps_qty():
    """order_volume_ratio=0.5, bar.volume=10000, qty=20000: 撮合量=5000."""
    pb = PaperBroker()
    bar = pl.DataFrame({
        "symbol": ["600519.SH"],
        "datetime": [datetime(2026, 1, 15, 10, 0)],
        "open": [10.0], "high": [10.5], "low": [9.5], "close": [9.8],
        "volume": [10000.0],
    })
    order = _order("buy", 20000, None)
    decisions = pb.match_tick(
        [order], {"600519.SH": bar},
        order_volume_ratio=0.5,
    )
    d = decisions[0]
    assert d.matched is True
    assert d.fill_qty == 5000    # 10000 * 0.5


def test_match_market_volume_ratio_one_no_cap():
    """order_volume_ratio=1.0 (默认) 不限量."""
    pb = PaperBroker()
    bar = _bar(open_=10.0, high=10.5, low=9.5, close=9.8)
    order = _order("buy", 50000, None)
    decisions = pb.match_tick([order], {"600519.SH": bar}, order_volume_ratio=1.0)
    d = decisions[0]
    assert d.fill_qty == 50000


def test_match_market_no_close_skips():
    """市价单 bar.close=0 (K 线无效): 不撮合."""
    pb = PaperBroker()
    bar = pl.DataFrame({
        "symbol": ["600519.SH"],
        "datetime": [datetime(2026, 1, 15, 10, 0)],
        "open": [0.0], "high": [0.0], "low": [0.0], "close": [0.0], "volume": [0.0],
    })
    order = _order("buy", 100, None)
    decisions = pb.match_tick([order], {"600519.SH": bar})
    d = decisions[0]
    assert d.matched is False
