"""Slippage 配置 — 聚宽式撮合滑点模型 (与 set_slippage 对齐).

3 种模式 + 1 种 NONE (默认, 向后兼容):
  - PriceRelatedSlippage(pct)   撮合价 = price * (1 ± pct)
  - TickRelatedSlippage(value)  撮合价 = price ± value * tick_size
  - FixedSlippage(value)        撮合价 = price ± value
  - NONE                        不滑点 (默认)

使用:
  cfg = SlippageConfig(kind=SlippageKind.PRICE_RELATED, value=0.001)
  buy_price = cfg.apply(base_price, side=OrderSide.BUY)   # price * 1.001
  sell_price = cfg.apply(base_price, side=OrderSide.SELL)  # price * 0.999
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# A 股最小价格变动单位 (用户决定, 写死不分支)
TICK_SIZE = 0.01


class SlippageKind(str, Enum):
    NONE = "none"
    PRICE_RELATED = "price_related"
    TICK_RELATED = "tick_related"
    FIXED = "fixed"


@dataclass(frozen=True)
class SlippageConfig:
    """撮合滑点配置.

    Attributes:
        kind: 滑点类型; 默认 NONE (与旧实现行为一致, 撮合价 = base_price).
        value:
          - PRICE_RELATED: 比例 (0.001 = 0.1%)
          - TICK_RELATED: tick 倍数 (1 = 0.01 元)
          - FIXED: 固定价差 (元)
    """

    kind: SlippageKind = SlippageKind.NONE
    value: float = 0.0

    def apply(self, base_price: float, *, side: str) -> float:
        """对 base_price 应用滑点, 返回新价.

        Args:
            base_price: 撮合基准价 (市价单: 上一根 K close; 限价单: bar.open/limit 取的撮合价).
            side: 'buy' (加价, 买贵) 或 'sell' (减价, 卖便宜).

        Returns:
            应用滑点后的撮合价. 负数 / 异常输入原样返回.
        """
        if base_price is None or base_price <= 0 or self.kind == SlippageKind.NONE or self.value == 0:
            return base_price
        sign = 1.0 if side == "buy" else -1.0
        if self.kind == SlippageKind.PRICE_RELATED:
            return base_price * (1.0 + sign * self.value)
        if self.kind == SlippageKind.TICK_RELATED:
            return base_price + sign * self.value * TICK_SIZE
        if self.kind == SlippageKind.FIXED:
            return base_price + sign * self.value
        return base_price

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_dict(cls, d: dict | None) -> SlippageConfig:
        if not d:
            return cls()
        try:
            kind = SlippageKind(d.get("kind", "none"))
        except ValueError:
            kind = SlippageKind.NONE
        try:
            value = float(d.get("value", 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        return cls(kind=kind, value=value)


__all__ = ["TICK_SIZE", "SlippageConfig", "SlippageKind"]
