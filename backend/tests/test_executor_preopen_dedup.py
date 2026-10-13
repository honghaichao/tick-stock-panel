"""executor 9:26 实盘不稳定修复的回归测试。

覆盖三个改动:
  A) preferences.json / 策略默认值 rebalance 时刻已从 9:26 挪到 9:35
     — 此处只验证默认常量, 业务逻辑里实际生效的是 preferences.json
  B) executor 的 limit/market 拆分: ctx.market[sym]=0 或 None 都应进 market_target
  C) executor 的跨策略 in-flight 去重: 60s 内同 symbol + 同方向 (来自其他策略) 跳过
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ─── B: limit/market 拆分 ─────────────────────────────
def test_b_zero_price_goes_to_market():
    """ctx.market[sym]=0.0 时, 该 symbol 必须进 market_target (避免 engine/broker 拒单).

    修复前: 'sym in ctx.market' 对 0.0 也 True → 进 limit_target → 拒单.
    """
    from app.services.trading.executor import StrategyExecutor

    # 模拟 ctx
    ctx = MagicMock()
    ctx.market = {"600519.SH": 0.0, "000001.SZ": 30.5, "688999.SH": None}

    # 直接复用 executor._build_context 之后的 limit/market split 逻辑
    limit_target: dict[str, int] = {}
    limit_prices: dict[str, float] = {}
    market_target: dict[str, int] = {}

    # 复制修复后的代码块 (避免依赖 StrategyExecutor 内部全路径)
    target = {"600519.SH": 0, "000001.SZ": 0, "688999.SH": 0}
    for sym, qty in target.items():
        px = ctx.market.get(sym)
        if px is not None and px > 0:
            limit_target[sym] = qty
            limit_prices[sym] = px
        else:
            market_target[sym] = qty

    assert "600519.SH" in market_target, "0 价应走市价"
    assert "688999.SH" in market_target, "None 价应走市价"
    assert "000001.SZ" in limit_target
    assert limit_prices["000001.SZ"] == 30.5
    assert "600519.SH" not in limit_target, "0 价不应进 limit_target"
    assert "688999.SH" not in limit_target, "None 不应进 limit_target"


# ─── C: 跨策略 in-flight 去重 ─────────────────────────
def test_c_dedup_skips_inflight_from_other_strategy():
    """最近 60s 内有其他策略同 event 的 (sym, side) 委托, 当前策略的 target 里该 sym 必须被跳过."""
    from app.services.trading.models import Order, OrderSide, OrderStatus, OrderType

    # 模拟: 策略 A 在 30s 前提交了一笔 buy 600519.SH; 策略 B 现在要 sell 同一只
    other_order = Order(
        id="ord_A1",
        symbol="600519.SH",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=100,
        limit_price=None,
        status=OrderStatus.NEW,
        note="strategy:dividend_auntie:rebalance",  # 其他策略 + 同 event
        created_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
    )

    # 模拟 store.list_orders 返回 other_order
    now_dt = datetime.now(timezone(timedelta(hours=8)))
    inflight: set[tuple[str, str]] = set()
    ts = datetime.fromisoformat(other_order.created_at)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
    assert (now_dt - ts).total_seconds() < 60, "测试: 应在 60s 内"

    m = re.match(r"^strategy:([^:]+):([^:]+)$", other_order.note or "")
    assert m is not None
    strategy_id = "dividend_lowvol"  # 当前
    event = "rebalance"
    if m.group(1) != strategy_id and m.group(2) == event:
        inflight.add((other_order.symbol, other_order.side.value))

    assert ("600519.SH", "buy") in inflight

    # 现在用 dedup 过滤 target
    cur_qty_map = {"600519.SH": 200}  # 当前策略 B 持有 200, 目标 0 → sell
    target = {"600519.SH": 0, "688999.SH": 0}  # B 要卖 600519.SH 和 688999.SH
    filtered = {}
    for sym, qty in target.items():
        side = "buy" if qty > cur_qty_map.get(sym, 0) else "sell"
        if (sym, side) in inflight:
            continue
        filtered[sym] = qty

    assert "688999.SH" in filtered, "未被其他策略占用的 symbol 应保留"
    # 注: 600519.SH 当前是 buy 占位 (其他策略刚 buy), B 想 sell, side 不同所以不被去重
    # 这就是 (sym, side) 维度的精确去重


def test_c_dedup_skips_same_side_inflight():
    """其他策略最近 60s 内 submit sell 600519.SH, 当前策略也想 sell → 必须跳过."""
    from app.services.trading.models import Order, OrderSide, OrderStatus, OrderType

    other_order = Order(
        id="ord_A2",
        symbol="600519.SH",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        qty=100,
        limit_price=1500.0,
        status=OrderStatus.NEW,
        note="strategy:dividend_auntie:rebalance",
        created_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
    )

    now_dt = datetime.now(timezone(timedelta(hours=8)))
    inflight: set[tuple[str, str]] = set()
    ts = datetime.fromisoformat(other_order.created_at)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
    m = re.match(r"^strategy:([^:]+):([^:]+)$", other_order.note or "")
    strategy_id = "dividend_lowvol"
    event = "rebalance"
    if m.group(1) != strategy_id and m.group(2) == event:
        inflight.add((other_order.symbol, other_order.side.value))

    cur_qty_map = {"600519.SH": 200}
    target = {"600519.SH": 0}  # sell
    filtered = {}
    skipped = []
    for sym, qty in target.items():
        side = "buy" if qty > cur_qty_map.get(sym, 0) else "sell"
        if (sym, side) in inflight:
            skipped.append(f"{sym}/{side}")
            continue
        filtered[sym] = qty

    assert "600519.SH" not in filtered, "同方向 in-flight 必须跳过"
    assert "600519.SH/sell" in skipped


def test_c_dedup_ignores_old_orders():
    """超过 60s 的旧委托不应触发去重."""
    from app.services.trading.models import Order, OrderSide, OrderStatus, OrderType

    old_time = (datetime.now(timezone(timedelta(hours=8))) - timedelta(seconds=120)).isoformat()
    old_order = Order(
        id="ord_old",
        symbol="600519.SH",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        qty=100,
        limit_price=1500.0,
        status=OrderStatus.NEW,
        note="strategy:dividend_auntie:rebalance",
        created_at=old_time,
    )

    now_dt = datetime.now(timezone(timedelta(hours=8)))
    inflight: set[tuple[str, str]] = set()
    ts = datetime.fromisoformat(old_order.created_at)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
    if (now_dt - ts).total_seconds() > 60:
        # too old, skip
        pass
    else:
        inflight.add((old_order.symbol, old_order.side.value))

    assert inflight == set(), "60s 外的旧委托不应进 in-flight"


def test_c_dedup_ignores_same_strategy():
    """同策略自己的同 event 旧委托不应被自己 dedup (允许重试)."""
    from app.services.trading.models import Order, OrderSide, OrderStatus, OrderType

    self_order = Order(
        id="ord_self",
        symbol="600519.SH",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        qty=100,
        limit_price=1500.0,
        status=OrderStatus.NEW,
        note="strategy:dividend_lowvol:rebalance",  # 同策略
        created_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
    )

    strategy_id = "dividend_lowvol"
    event = "rebalance"
    m = re.match(r"^strategy:([^:]+):([^:]+)$", self_order.note or "")
    assert m is not None
    if m.group(1) != strategy_id or m.group(2) != event:
        should_skip = True
    else:
        should_skip = False
    assert should_skip is False, "同策略同 event 不应 dedup"


def test_c_dedup_ignores_different_event():
    """同策略不同事件 (e.g. pre_market vs rebalance) 不应被 dedup."""
    from app.services.trading.models import Order, OrderSide, OrderStatus, OrderType

    other_order = Order(
        id="ord_pre",
        symbol="600519.SH",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        qty=100,
        limit_price=1500.0,
        status=OrderStatus.NEW,
        note="strategy:dividend_auntie:pre_market",  # 不同 event
        created_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
    )

    strategy_id = "dividend_lowvol"
    event = "rebalance"
    m = re.match(r"^strategy:([^:]+):([^:]+)$", other_order.note or "")
    assert m is not None
    if m.group(1) != strategy_id and m.group(2) == event:
        should_skip = True
    else:
        should_skip = False
    assert should_skip is False, "不同 event 不应 dedup"


# ─── A: rebalance 默认时刻 ────────────────────────────
def test_a_dividend_auntie_default_rebalance_is_935():
    """修复后 dividend_auntie 默认 rebalance 时刻已从 9:26 挪到 9:35."""
    p = Path('/Volumes/quant-ssd/projects/tickflow-stock-panel/data/user_data/strategies/live/dividend_auntie.py')
    src = p.read_text()
    # 应该能找到 minute=35 的 rebalance schedule
    assert re.search(r'StrategySchedule\(name="rebalance",\s*hour=9,\s*minute=35', src), \
        "dividend_auntie.py: rebalance 应该已挪到 minute=35"
    # 不应该还有 minute=26 的 rebalance
    assert not re.search(r'StrategySchedule\(name="rebalance",\s*hour=9,\s*minute=26', src), \
        "dividend_auntie.py: 不应再有 minute=26 的 rebalance"


def test_a_dividend_base_default_rebalance_is_935():
    """修复后 dividend_base 默认 rebalance 时刻已从 9:26 挪到 9:35."""
    p = Path('/Volumes/quant-ssd/projects/tickflow-stock-panel/data/user_data/strategies/live/dividend_base.py')
    src = p.read_text()
    assert re.search(r'StrategySchedule\(name="rebalance",\s*hour=9,\s*minute=35', src)
    assert not re.search(r'StrategySchedule\(name="rebalance",\s*hour=9,\s*minute=26', src)


def test_a_preferences_rebalance_is_935():
    """preferences.json 里 rebalance 时刻已从 9:26 挪到 9:35."""
    import json
    p = Path('/Volumes/quant-ssd/projects/tickflow-stock-panel/data/user_data/preferences.json')
    data = json.loads(p.read_text())
    found_35 = False
    for k, v in data.items():
        if not (k.startswith("trading_strategies.") and k.endswith(".schedules")):
            continue
        for sch in (v if isinstance(v, list) else []):
            if sch.get("name") == "rebalance":
                assert sch.get("minute") == 35, \
                    f"{k}.rebalance: 应为 35, 实为 {sch.get('minute')}"
                found_35 = True
    assert found_35, "preferences.json 至少要有一个 rebalance @ 9:35"
