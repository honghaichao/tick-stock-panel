"""Broker 抽象层 — 二开接入真实券商的入口契约。

设计要点 (docs/secondary-development.md §1):
  - 主线默认实现为 PaperBroker, 真实券商适配通过本 Protocol 二开接入,
    不在主线默认启用 (避免硬绑特定券商 + 凭证泄露风险)。
  - broker 负责「委托受理 + 撮合决策 + 成交回报」三件事; 资金/订单状态持久化
    在 store.py, 撮合器在 engine.py — broker 只产出事件。

调用约定:
  - submit() 同步返回 OrderAck (受理回执, 立刻给引擎), 不等待成交。
  - match_tick() 由 sync 心跳逻辑每 5s 调用一次, 传入上一根 K 线 OHLCV;
    broker 内部维护 pending orders 队列, 返回本轮的成交决策列表。
  - 撮合是 broker 的纯函数: 给定 (pending, current_date_bar) → decisions,
    broker 内部不阻塞等待网络 (PaperBroker 是这样; 真实券商适配必须
    在 worker 线程做, 不能在 quote 轮询线程做)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import polars as pl

from app.services.trading.models import (
    BrokerKind,
    MatchDecision,
    Order,
    OrderDraft,
)


@dataclass(frozen=True)
class OrderAck:
    """broker.submit 的同步返回 — 受理回执 (非成交)。

    - accepted=True, reject_reason=None: 进入 pending 队列, 等待撮合。
    - accepted=False, reject_reason=<中文>: 立即拒单, 不入队列。
    """

    accepted: bool
    reject_reason: str | None = None
    order: Order | None = None  # accepted 时为 broker 生成的 Order


class BrokerAdapter(ABC):
    """券商适配器抽象 — 二开接入真实券商时实现本类。

    实现要求:
      - name / kind: 静态标识, 用于 settings/UI。
      - submit() / cancel() 同步返回, 不阻塞外部网络; 真实券商的鉴权/下单
        请求必须在 worker 线程内 (由 engine.submit() 调度)。
      - match_tick() 是纯计算: 给定 (pending orders, 当前分钟 K) → 成交决策;
        不写盘、不修改 store。Engine 拿到决策后再持锁落库。
      - cancel() 也只产出决策 (CancelAck), 由 engine 落账。
    """

    name: str = "broker"
    kind: BrokerKind = BrokerKind.PAPER

    @abstractmethod
    def submit(self, draft: OrderDraft, order: Order) -> OrderAck:
        """受理委托。"""

    @abstractmethod
    def cancel(self, order: Order) -> tuple[bool, str | None]:
        """撤单。返回 (success, reject_reason)。"""

    @abstractmethod
    def match_tick(
        self,
        pending: list[Order],
        bar_by_symbol: dict[str, pl.DataFrame],
    ) -> list[MatchDecision]:
        """撮合一拍 (调用方传入当前分钟 K 数据)。

        Args:
            pending: 当前未完成委托 (NEW + PARTIAL)
            bar_by_symbol: {symbol: 单根分钟 K DataFrame}; 缺失的 symbol 不撮合.

        Returns:
            每个 pending 订单一个 MatchDecision (matched=False 表示继续挂单).
        """


# ── 撮合规则的可复用片段 ──────────────────────────────
def limit_crossed(
    side: Literal["buy", "sell"],
    limit_price: float,
    bar: pl.DataFrame,
) -> bool:
    """限价穿越判定: 当前 bar 的 high/low 触达委托价即视为可成交。

    规则:
      - 买入: bar.low  <=  limit_price  (有低价机会, 可买入)
      - 卖出: bar.high >=  limit_price  (有高价机会, 可卖出)
    一字板判定: bar.open == bar.high == bar.low (极小价差), 仍按上述规则。
    """
    if bar.is_empty():
        return False
    row = bar.row(0, named=True)
    lo = float(row.get("low") or 0.0)
    hi = float(row.get("high") or 0.0)
    if side == "buy":
        return lo <= limit_price
    return hi >= limit_price


__all__ = ["BrokerAdapter", "OrderAck", "limit_crossed"]
