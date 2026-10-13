"""默认 PaperBroker — 按分钟 K 线撮合, 模拟 A 股规则。

撮合规则 (paper-trading 约定, 与真实成交存在差异, 前端需在 UI 注明):

  1. 限价买单 (LIMIT BUY):
     - bar.low ≤ limit_price: 触发; 成交价 = min(bar.open, limit_price)
     - bar.low >  limit_price: 不成交
     - 涨停日 bar.low == bar.high == 涨停价 (一字板): 涨停价 = upper_limit;
       由于 upper_limit > limit_price (否则本就不是涨停价), 因此 bar.low
       不会 ≤ limit_price → 不会成交, 与真实市场一致。
  2. 限价卖单 (LIMIT SELL): 对称
     - bar.high ≥ limit_price: 触发; 成交价 = max(bar.open, limit_price)
     - bar.high < limit_price: 不成交
     - 跌停日不会成交, 与真实一致。
  3. 市价单 (MARKET): bar.open 成交 (open = 0 视为停牌, 不成交)。
  4. T+1: 卖单需校验 "可卖数量 = 持仓数量 - 今日买入数量"; 不足即拒单。
  5. 涨跌停: 调用方在委派前用 `app.price_limits` 自行校验; 撮合本身用
     high/low range 已经隐含拒绝, 这里再做一层兜底 (upper < bar.low 时拒绝买入)。

撮合不会修改任何持久化数据, 仅返回 MatchDecision 列表。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

import polars as pl

from app.services.trading.broker import BrokerAdapter, OrderAck, limit_crossed
from app.services.trading.models import (
    BrokerKind,
    MatchDecision,
    Order,
    OrderDraft,
    OrderSide,
    OrderStatus,
)
from app.services.trading.slippage import SlippageConfig, SlippageKind

logger = logging.getLogger(__name__)


def _bar_open(bar: pl.DataFrame) -> float | None:
    if bar.is_empty():
        return None
    row = bar.row(0, named=True)
    op = row.get("open")
    try:
        v = float(op) if op is not None else 0.0
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _bar_close(bar: pl.DataFrame) -> float | None:
    if bar.is_empty():
        return None
    row = bar.row(0, named=True)
    cl = row.get("close")
    try:
        v = float(cl) if cl is not None else 0.0
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _bar_volume(bar: pl.DataFrame) -> float | None:
    if bar.is_empty():
        return None
    row = bar.row(0, named=True)
    vol = row.get("volume") or row.get("vol")
    try:
        v = float(vol) if vol is not None else 0.0
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _bar_high_low(bar: pl.DataFrame) -> tuple[float | None, float | None]:
    if bar.is_empty():
        return None, None
    row = bar.row(0, named=True)
    try:
        hi = float(row.get("high") or 0.0)
        lo = float(row.get("low") or 0.0)
    except (TypeError, ValueError):
        return None, None
    if hi <= 0 or lo <= 0:
        return None, None
    return hi, lo


class PaperBroker(BrokerAdapter):
    """默认模拟撮合。kind=paper, name=固定 'paper'."""

    kind = BrokerKind.PAPER
    name = "paper"

    def __init__(self) -> None:
        self._cancelled: set[str] = set()

    # ── 委托受理 ──────────────────────────────────────
    def submit(self, draft: OrderDraft, order: Order) -> OrderAck:
        # 基础合法性已由 Order / OrderDraft 校验过 (qty > 0, limit_price > 0)
        # 这里只做撮合视角的额外校验: 限价单必须给 limit_price, 市价单不接收限价。
        #
        # 防御性降级: 限价单 limit_price 缺失时, 静默降级为市价单 (与
        # runner.rebalance_to:185-187 同款行为), 避免上游漏掉 price 传递就直接拒单。
        # 历史 case: 2026-09-30 09:26:42 ord_2f932170aaba 002998.SZ 因这个路径 reject
        # 共损失 1 / 49 = 2% 通过率. 上游 executor 的 limit/market 分单已避免大多数情况,
        # 此处再兜底一道.
        if draft.order_type == "limit" and (draft.limit_price is None or draft.limit_price <= 0):
            logger.warning(
                "paper_broker: 限价单缺价自动降级为市价单 (symbol=%s qty=%s)",
                draft.symbol, draft.qty,
            )
            draft = draft.model_copy(update={
                "order_type": "market",
                "limit_price": None,
            })
        if draft.order_type == "market" and draft.limit_price is not None:
            return OrderAck(accepted=False, reject_reason="市价单不能附带限价")
        return OrderAck(accepted=True, order=order)

    def cancel(self, order: Order) -> tuple[bool, str | None]:
        # 仅 NEW / PARTIAL 可撤; 其他状态拒收。
        if order.status not in (OrderStatus.NEW, OrderStatus.PARTIAL):
            return False, f"订单状态为 {order.status.value}, 不可撤单"
        self._cancelled.add(order.id)
        return True, None

    # ── 撮合 ──────────────────────────────────────────
    def match_tick(
        self,
        pending: Iterable[Order],
        bar_by_symbol: dict[str, pl.DataFrame],
        *,
        slippage: SlippageConfig | None = None,
        order_volume_ratio: float = 1.0,
        first_bar_of_day: dict[str, bool] | None = None,
    ) -> list[MatchDecision]:
        decisions: list[MatchDecision] = []
        slippage = slippage or SlippageConfig()
        for order in pending:
            if order.id in self._cancelled:
                continue
            bar = bar_by_symbol.get(order.symbol)
            is_first = (first_bar_of_day or {}).get(order.symbol, False)
            decision = self._match_one(
                order, bar,
                slippage=slippage,
                order_volume_ratio=order_volume_ratio,
                is_first_bar=is_first,
            )
            decisions.append(decision)
        return decisions

    def _match_one(
        self,
        order: Order,
        bar: pl.DataFrame | None,
        *,
        slippage: SlippageConfig,
        order_volume_ratio: float,
        is_first_bar: bool,
    ) -> MatchDecision:
        # 无 K 线 → 视作停牌或拉取失败, 不成交, 继续挂单
        if bar is None or bar.is_empty():
            return MatchDecision(
                order_id=order.id,
                matched=False,
                reason="无分钟 K 数据, 继续等待",
            )

        hi, lo = _bar_high_low(bar)
        op = _bar_open(bar)
        if hi is None or lo is None or op is None:
            return MatchDecision(
                order_id=order.id,
                matched=False,
                reason="分钟 K 字段无效, 继续等待",
            )

        # ── 限价单 ─────────────────────────────────
        if order.order_type.value == "limit":
            lp = order.limit_price
            if lp is None:
                # OrderDraft 已校验 limit_price > 0, 这里再兜一次防止 -O 下 assert 被剥离.
                return MatchDecision(
                    order_id=order.id, matched=False,
                    reason="限价单缺少价格, 继续等待",
                )
            if not limit_crossed(order.side.value, lp, bar):
                # 区间未触及委托价 → 不成交; 区分涨跌停说明便于前端展示
                if order.side == OrderSide.BUY and lo > lp:
                    return MatchDecision(
                        order_id=order.id, matched=False,
                        reason=f"买单未触发: 区间最低 {lo:.3f} > 限价 {lp:.3f}",
                    )
                if order.side == OrderSide.SELL and hi < lp:
                    return MatchDecision(
                        order_id=order.id, matched=False,
                        reason=f"卖单未触发: 区间最高 {hi:.3f} < 限价 {lp:.3f}",
                    )
                return MatchDecision(order_id=order.id, matched=False, reason="区间未触及")

            # 触及: 成交价 = min(open, limit) for buy / max(open, limit) for sell.
            # 若 open 已穿越限价 (低开), 优先 open (实际无法在低于 limit 的价格成交);
            # 若 open 反向穿越 (高开时 buy), 用 limit.
            fill_price = min(op, lp) if order.side == OrderSide.BUY else max(op, lp)
            return MatchDecision(
                order_id=order.id,
                matched=True,
                fill_price=fill_price,
                fill_qty=order.qty - order.filled_qty,
                reason=f"按 {order.order_type.value} 单撮合 @ {fill_price:.3f}",
            )

        # ── 市价单 (聚宽语义) ─────────────────────
        # 基准价 = 9:30 第一根 K 用 bar.open, 其他用 bar.close (上一根已收盘 K).
        # 撮合价 = apply_slippage(base, side). 量限 = min(qty, ratio * bar.volume).
        base_price = op if is_first_bar else _bar_close(bar)
        if base_price is None or base_price <= 0:
            return MatchDecision(
                order_id=order.id, matched=False,
                reason="市价单基准价无效 (K 线 close/open 缺失)",
            )
        fill_price = slippage.apply(base_price, side=order.side.value)
        qty_remaining = order.qty - order.filled_qty
        bar_volume = _bar_volume(bar)
        qty_capped = False
        if order_volume_ratio < 1.0 and bar_volume is not None and bar_volume > 0:
            max_qty = int(bar_volume * order_volume_ratio)
            fill_qty = min(qty_remaining, max_qty)
            qty_capped = fill_qty < qty_remaining
        else:
            fill_qty = qty_remaining
        suffix_parts = []
        if is_first_bar:
            suffix_parts.append("第一根K(open)")
        else:
            suffix_parts.append("上一根K(close)")
        if slippage.kind != SlippageKind.NONE:
            suffix_parts.append(f"滑点={slippage.kind.value}:{slippage.value}")
        if qty_capped:
            suffix_parts.append(f"量限={order_volume_ratio}")
        suffix = ", ".join(suffix_parts)
        reason = f"按市价撮合 @ {fill_price:.3f} (基准 {base_price:.3f}, {suffix})"
        return MatchDecision(
            order_id=order.id,
            matched=True,
            fill_price=fill_price,
            fill_qty=fill_qty,
            reason=reason,
        )


__all__ = ["PaperBroker"]
