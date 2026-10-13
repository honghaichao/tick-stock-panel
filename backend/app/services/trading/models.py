"""实盘领域模型 — Order / Fill / Position / Account / TradeLogEntry。

所有金额单位: 元。价格单位: 元/股。数量单位: 股 (A 股 1 手 = 100 股,
委托提交/返回统一按股, 前端展示再换算为手)。

数据口径 (CONTRIBUTING §3):
  - 价格口径: 成交价、现价、持仓成本均使用原始价 (不复权) — 撮合必须基于
    当时的实际可成交价, 复权会影响历史对比。当前价由 PaperBroker 用
    enriched.last_price (等同 raw close) 计算。
  - 涨跌停口径: `app.price_limits` 现有实现 (按板块、上市初期无限制窗口)。
  - 百分比口径: 累计盈亏%、当时盈亏% 一律为小数制 (0.0366 = 3.66%);
    持仓 pnl_pct 字段语义同上。
"""
from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """委托类型。

    - LIMIT: 限价 (默认)。撮合等待行情穿越 `limit_price`。
    - MARKET: 市价 (本模拟撮合按下一根 K 线开盘价成交, 标记说明在前端展示)。
    """

    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(StrEnum):
    """委托状态机: NEW → (PARTIAL →) FILLED | CANCELED | REJECTED.

    模拟撮合只走单次撮合 (一笔委托一次性成交或不成交), 不存在多次部分成交流程,
    但状态机保留 PARTIAL 状态以兼容未来真实券商适配。
    """

    NEW = "new"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class BrokerKind(StrEnum):
    PAPER = "paper"
    LIVE = "live"  # 二开接入真实券商时使用, 主线不内置任何 live broker


# ── 委托 ──────────────────────────────────────────────
class Order(BaseModel):
    id: str
    symbol: str
    side: OrderSide
    order_type: OrderType = OrderType.LIMIT
    qty: int                          # 股 (A 股最小 100, 这里不做硬限)
    limit_price: float | None = None  # 限价; 市价单为 None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: int = 0
    avg_fill_price: float | None = None
    created_at: str                    # ISO8601 北京时区
    submitted_at: str | None = None    # 进入撮合队列时间
    filled_at: str | None = None
    canceled_at: str | None = None
    reject_reason: str | None = None   # 拒单原因 (中文, 供前端直接展示)
    note: str | None = None            # 用户备注
    strategy_id: str | None = None       # 第一性归属字段 (per-strategy 过滤用);
                                          # 不再依赖 note 字符串解析 (历史格式 "strategy:<sid>:<event>")

    @field_validator("qty")
    @classmethod
    def _qty_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("qty 必须 > 0")
        return v

    @field_validator("limit_price")
    @classmethod
    def _limit_positive(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if v <= 0:
            raise ValueError("limit_price 必须 > 0")
        return v


class OrderDraft(BaseModel):
    """手动下单请求体 (POST /api/trading/orders 入参)。

    id / created_at 由后端生成; 入参只携带业务字段。
    """

    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["limit", "market"] = "limit"
    qty: int = Field(gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    note: str | None = None
    strategy_id: str | None = None
    """可选归属策略 id (D4 — 给"脚本级手工带策略"留路径)。"""


# ── 成交 ──────────────────────────────────────────────
class Fill(BaseModel):
    """单次成交记录 (= 一笔 fill)。一笔委托可能产生多笔 fill。

    模拟撮合为单次成交, 但模型保留多笔填充的可能性 (适配真实券商)。
    """

    id: str
    order_id: str
    symbol: str
    side: OrderSide
    qty: int
    price: float                       # 成交价 (元/股)
    fee: float = 0.0                   # 手续费 (元)
    tax: float = 0.0                   # 印花税 (元, 仅卖出)
    amount: float                      # qty * price (元), 不含费税
    filled_at: str                     # ISO8601 北京时区
    trade_date: date                   # 归属交易日 (CONTRIBUTING §3.3)
    strategy_id: str | None = None       # 撮合时从关联 Order.strategy_id 继承


# ── 持仓 ──────────────────────────────────────────────
class Position(BaseModel):
    """当前持仓聚合视图 — 由 fills 实时重算, 不持久化.

    成本口径: 移动加权平均成本 (买入加权, 卖出不动成本)。  # noqa: RUF002
    累计盈亏: (现价 - 平均成本) x 持仓数量 — 不含已实现盈亏。  # noqa: RUF002
    当时盈亏: (现价 - 昨收) x 持仓数量 — 与累计盈亏口径不同, 前端必须并列展示。  # noqa: RUF002
    """

    symbol: str
    qty: int                          # 当前持仓数量 (股)
    avg_cost: float                   # 加权平均成本 (元/股, 不含费税)
    last_price: float | None = None   # 实时最新价
    prev_close: float | None = None   # 昨收 (当时盈亏口径)
    market_value: float | None = None # qty * last_price
    total_pnl: float | None = None    # (last_price - prev_close) * qty — 当时盈亏
    total_pnl_pct: float | None = None
    accumulated_pnl: float | None = None    # (last - avg_cost) * qty — 累计盈亏
    accumulated_pnl_pct: float | None = None
    first_buy_date: date | None = None     # 持仓起始日 (首次买入成交日)
    last_buy_date: date | None = None      # 最近一次买入成交日
    holding_days: int | None = None        # 持仓天数 (自然日, last_buy_date → 今天)


# ── 账户 ──────────────────────────────────────────────
class Account(BaseModel):
    """模拟账户状态 (内存视图)。

    现金 = 初始资金 - 累计买入金额 + 累计卖出金额 - 累计费税
    总市值 = 现金 + Σ(qty * last_price)
    """

    initial_cash: float
    cash: float
    market_value: float
    total_equity: float               # 现金 + 持仓市值
    realized_pnl: float = 0.0         # 已实现盈亏 (卖出 - 成本)
    unrealized_pnl: float = 0.0       # 未实现盈亏 = Σ(累计盈亏)
    total_pnl: float = 0.0            # realized + unrealized


# ── 交易日志 ─────────────────────────────────────────
class TradeLogKind(StrEnum):
    SUBMIT = "submit"      # 委托提交
    ACCEPT = "accept"      # 进入撮合队列
    FILL = "fill"          # 成交
    CANCEL = "cancel"      # 撤单
    REJECT = "reject"      # 拒单
    MATCHER_TICK = "matcher_tick" # 撮合轮询后返回"


class TradeLogEntry(BaseModel):
    """交易日志 — 一行一条, append-only JSONL。

    与 Fill 的区别: Fill 是会计事实 (仓位/资金变化), Log 是审计流水
    (提交/撤单/异常等"事件")。两者不互替。
    """

    id: str
    kind: TradeLogKind
    ts: str                          # ISO8601 北京时区
    symbol: str | None = None
    order_id: str | None = None
    fill_id: str | None = None
    message: str                     # 中文, 前端直接展示
    details: dict | None = None      # 额外结构化信息 (限价/成交量/原因码 等)
    strategy_id: str | None = None     # 记录日志时从关联 order 继承

# ── 撮合结果 (内部传输) ──────────────────────────────
class MatchDecision(BaseModel):
    """PaperBroker 单根 K 线撮合结果 — 引擎直接落库 + 推送 SSE。

    matched=False 表示本根 K 线没有满足成交条件; 委托继续挂单。
    """

    order_id: str
    matched: bool
    fill_price: float | None = None
    fill_qty: int | None = None
    reason: str | None = None        # 未成交原因 (中文) 或成交说明


# ── 引擎状态 (前端一次性 GET /api/trading/status) ───
class EngineStatus(BaseModel):
    enabled: bool                              # preferences.trading_enabled
    running: bool                              # 后台撮合线程是否运行
    broker: str = "paper"                      # 当前生效 broker name
    initial_cash: float
    pending_order_count: int = 0
    filled_order_count: int = 0
    matcher_last_ts: str | None = None         # 最近一次撮合扫描时间
    matcher_interval_seconds: int = 5          # 当前扫描间隔
    pending: list[Order] = Field(default_factory=list)


__all__ = [
    "Account",
    "BrokerKind",
    "EngineStatus",
    "Fill",
    "MatchDecision",
    "Order",
    "OrderDraft",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Position",
    "TradeLogEntry",
    "TradeLogKind",
]
