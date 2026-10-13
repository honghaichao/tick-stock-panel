"""面向策略 / 脚本的实盘下单 API — 包装 TradingEngine 的细颗粒方法。

设计动机:
  - 直接调 `engine.submit(data_dir, OrderDraft(...))` 需要 6~8 行样板, 对策略脚本
    不友好; 这里提供 `buy / sell / cancel_order / rebalance_to` 等语义化方法。
  - 所有路径都穿透到引擎层, 享受同样的: 风控 (金额/资金/T+1)、broker 受理、
    JSONL 落盘、SSE 推送、撮合线程接管; 本模块**不**绕过任何校验。
  - 不维护独立状态; 状态全部由 store / engine 提供, runner 只是便利层。

典型用法 (策略脚本 / Jupyter):

    from pathlib import Path
    from app.services.trading import runner

    r = runner.make_default()    # 自动拿 app.state.repo.store.data_dir

    # 单笔
    order = r.buy("600519.SH", qty=100, limit_price=10.5, note="突破追单")

    # 调仓到目标持仓 (等权分配可用现金)
    r.rebalance_to({"600519.SH": 100, "000858.SZ": 200}, total_cash=100_000)

    # 查
    for p in r.positions():
        print(p.symbol, p.qty, p.avg_cost)

不在本模块:
  - 任何行情读取 / 撮合逻辑 (属于 engine / paper_broker)。
  - 时间调度 (cron / run_daily), 由调用方决定何时调这些方法。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from app.services.trading import engine as engine_mod
from app.services.trading.engine import TradingEngine
from app.services.trading.models import (
    Order,
    OrderDraft,
    Position,
)
from app.services.trading.store import Account, Fill

logger = logging.getLogger(__name__)


class TradingRunner:
    """策略 / 脚本可用的实盘下单门面。

    实例化方式:
      - 默认 `TradingRunner.make_default()`: 从 app.state 拿 data_dir + engine。
      - 自定义 `TradingRunner(engine, data_dir)`: 测试用, 绕过 app.state。
    """

    def __init__(self, engine: TradingEngine, data_dir: Path) -> None:
        self._engine = engine
        self._data_dir = data_dir

    # ── 工厂 ──────────────────────────────────────────
    @classmethod
    def make_default(cls) -> TradingRunner:
        """从全局 app.state 取 engine + data_dir. 主进程 / 测试启动后可用。

        与 engine.set_app_state() 配合: main.py 在 startup 时已经注入, 这里只读。
        """
        from app.services.trading import engine as engine_mod

        eng = engine_mod._APP_STATE  # type: ignore[attr-defined]
        if eng is None or not hasattr(eng, "repo"):
            raise RuntimeError(
                "未找到 app.state; 请确认后端进程已启动, "
                "或在脚本里手动调用 TradingRunner(engine, data_dir)。"
            )
        engine = getattr(eng, "trading_engine", None)
        if engine is None:
            raise RuntimeError(
                "app.state.trading_engine 未初始化; "
                "确认 main.py 启动钩子已运行, trading 路由已挂载。"
            )
        data_dir = eng.repo.store.data_dir
        return cls(engine, data_dir)

    @property
    def engine(self) -> TradingEngine:
        return self._engine

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    # ── 下单 (手动 / 策略均可调用) ──────────────────
    def buy(
        self,
        symbol: str,
        qty: int,
        *,
        limit_price: float | None = None,
        order_type: str = "limit",
        note: str | None = None,
        max_order_amount: float | None = None,
        effective_cash: float | None = None,
        strategy_id: str | None = None,
    ) -> Order:
        """买入委托。`order_type='market'` 时 `limit_price` 必须为 None.

        Returns:
            落库后的 Order (status=NEW / REJECTED); 拒单抛 `TradingRejectError`。
        max_order_amount: 单笔金额上限 override (None=用 prefs.max_order_amount). 策略 rebalance_to 传
            prefs.max_order_amount_for_strategy() (默认 1000万), 跟手动下单路径 (5万) 分开.
        effective_cash: 可用现金 override (None=用落账 cash). 由 rebalance_to 内部累计同帧 sells
            proceeds 传入, 解决同帧 sell→buy 异步撮合导致的 buy 资金校验误拒. 手动调用一般不传.
        strategy_id: 第一性归属字段 (D4). 策略 rebalance_to 显式传 strategy.id, 让 per-strategy
            过滤用第一性字段而非 note 字符串解析. None = manual 单 (不属于任何策略).
        """
        draft = OrderDraft(
            symbol=symbol,
            side="buy",
            order_type=order_type,
            qty=qty,
            limit_price=limit_price,
            note=note,
            strategy_id=strategy_id,
        )
        return self._engine.submit(
            self._data_dir, draft,
            max_order_amount_override=max_order_amount,
            effective_cash=effective_cash,
        )

    def sell(
        self,
        symbol: str,
        qty: int,
        *,
        limit_price: float | None = None,
        order_type: str = "limit",
        note: str | None = None,
        max_order_amount: float | None = None,
        strategy_id: str | None = None,
    ) -> Order:
        """卖出委托。会走 T+1 校验 (持仓不足即拒)。

        max_order_amount: 单笔金额上限 override (None=用 prefs.max_order_amount).
        strategy_id: 同 buy() 的说明 — 第一性字段. 策略 rebalance_to 显式传.
        """
        draft = OrderDraft(
            symbol=symbol,
            side="sell",
            order_type=order_type,
            qty=qty,
            limit_price=limit_price,
            note=note,
            strategy_id=strategy_id,
        )
        return self._engine.submit(
            self._data_dir, draft, max_order_amount_override=max_order_amount,
        )

    def cancel(self, order_id: str) -> Order:
        """撤单。仅 NEW / PARTIAL 状态可撤, 其他状态抛 TradingRejectError."""
        return self._engine.cancel(self._data_dir, order_id)

    # ── 调仓 ─────────────────────────────────────────
    def rebalance_to(
        self,
        target: Mapping[str, int],
        *,
        prices: Mapping[str, float] | None = None,
        order_type: str = "limit",
        note: str | None = None,
        skip_hold: bool = False,
        max_order_amount: float | None = None,
        strategy_id: str | None = None,
    ) -> list[Order]:
        """调仓到目标持仓 (symbol → qty).

        Args:
            target: {symbol: 目标 qty (股)}. qty=0 表示清仓, 缺失表示卖出.
            prices: {symbol: 单价}. 用于限价单; 不传则按 order_type 提交 (market 默认).
            order_type: 'limit' | 'market'. 默认 'limit'. 当 prices=None 且
                order_type='limit' 时, 自动降级为 'market' (策略脚本简化调用).
            note: 写入每笔委托的备注.
            skip_hold: True 时不调整当前持仓 = target 的部分 (只做差额调仓);
                    但 qty=0 的目标仍然清仓。False (默认) 严格按 target。
        max_order_amount: 单笔委托金额上限 (元). None=用 prefs.max_order_amount() (手动默认 5万);
            策略路径传 prefs.max_order_amount_for_strategy() (默认 1000万), 跟 JoinQuant 原版等权
            分仓对齐 (菜场大妈 5 票 × 1M cash = 每票 ~200k 才能跑).

        Returns:
            本轮新提交的委托列表 (不含已存在或被 skip 的).

        行为:
          - 单笔委托被风控拒 (TradingRejectError) 时**记 WARN 继续**, 不抛异常;
            调用方按返回的 orders 列表自行核对成功 / 失败情况。
        """
        # 没传 prices 且 order_type=limit → 自动降级为 market (避免 broker 拒 limit 无价).
        if prices is None and order_type == "limit":
            order_type = "market"

        def _price_for(sym: str) -> float | None:
            if prices is None:
                return None
            return prices.get(sym)

        submitted: list[Order] = []
        current: dict[str, int] = {p.symbol: p.qty for p in self.positions()}
        target_dict = dict(target)

        # ── 同帧资金池 ─────────────────────────────
        # 历史 bug (2026-09-29 14:02 现场): rebalance 触发 sell 4 只 + buy 4 只在同一帧内
        # 提交, sell 异步撮合 (撮合线程 5s 轮询), buy 走 engine.submit 资金校验时 cash 还是
        # 落账旧值 (5 只 buy 全部 "可用现金不足" 被拒). 修复: 在 rebalance_to 入口读一次
        # 落账 cash, sell 提交时累计 proceeds, buy 校验时把 "落账 cash + 同帧 sells proceeds"
        # 作为 effective_cash 注入, 跟 JoinQuant 原版 order_target→order_value 同帧生效的
        # 语义对齐. 注意: 这是 in-frame 估值, 撮合后落账时仍以 fill 为准.
        starting_cash = self.account().cash
        frame_sell_proceeds = 0.0   # 同帧已提交 sells 的预计所得
        frame_buy_used = 0.0        # 同帧已提交 buys 的预计占用 (防御性扣减, 让多笔 buy 不会互相透支)

        def _est_px(sym: str) -> float:
            """订单价格估算: 优先用 prices, 否则 quote_service 缓存的 last_price, 否则 0.

            0 时 buy 资金校验 fallback 到 0, engine.submit 已对 limit 单有 ref_price<=0
            的兜底, 这里仅用于 in-frame proceeds 估值, 撮合价以 fill 为准.
            """
            p = _price_for(sym)
            if p is not None and p > 0:
                return p
            mp = engine_mod._estimate_market_price(self._data_dir, sym)
            return mp or 0.0

        # 先卖 (释放资金 + 避开 T+1 受影响的 symbol)
        for sym in current.keys() | target_dict.keys():
            cur_qty = current.get(sym, 0)
            tgt_qty = target_dict.get(sym, 0)
            if cur_qty == tgt_qty:
                continue
            if cur_qty > tgt_qty:
                sell_qty = cur_qty - tgt_qty
                if skip_hold and tgt_qty > 0:
                    continue
                try:
                    o = self.sell(sym, sell_qty,
                                  limit_price=_price_for(sym),
                                  order_type=order_type,
                                  note=note or "rebalance_to sell",
                                  max_order_amount=max_order_amount,
                                  strategy_id=strategy_id)
                    submitted.append(o)
                    frame_sell_proceeds += sell_qty * _est_px(sym)
                except engine_mod.TradingRejectError as e:
                    logger.warning("rebalance sell %s qty=%d rejected: %s", sym, sell_qty, e)

        # 再买
        for sym in current.keys() | target_dict.keys():
            cur_qty = current.get(sym, 0)
            tgt_qty = target_dict.get(sym, 0)
            if cur_qty >= tgt_qty:
                continue
            if skip_hold and cur_qty > 0:
                continue
            buy_qty = tgt_qty - cur_qty
            # 同帧有效现金 = 落账 + 同帧 sells 所得 - 同帧 buys 已占用
            # engine.submit 内部还会 max(., account.cash) 兜底, 这里只补 sell proceeds 部分
            effective = starting_cash + frame_sell_proceeds - frame_buy_used
            try:
                o = self.buy(sym, buy_qty,
                             limit_price=_price_for(sym),
                             order_type=order_type,
                             note=note or "rebalance_to buy",
                             max_order_amount=max_order_amount,
                             effective_cash=effective,
                             strategy_id=strategy_id)
                submitted.append(o)
                frame_buy_used += buy_qty * _est_px(sym)
            except engine_mod.TradingRejectError as e:
                logger.warning("rebalance buy %s qty=%d rejected: %s", sym, buy_qty, e)

        return submitted

    # ── 查询 ─────────────────────────────────────────
    def positions(self) -> list[Position]:
        return self._engine.positions(self._data_dir)

    def account(self) -> Account:
        return self._engine.account(self._data_dir)

    def pending_orders(self) -> list[Order]:
        """未完成委托 (NEW + PARTIAL)。"""
        from app.services.trading.store import pending_orders
        return pending_orders(self._data_dir)

    def list_orders(self, *, statuses=None, symbol: str | None = None, limit: int | None = None) -> list[Order]:
        return self._engine.list_orders(self._data_dir, statuses=statuses, symbol=symbol, limit=limit)

    def list_fills(self, *, symbol: str | None = None, limit: int | None = None) -> list[Fill]:
        return self._engine.list_fills(self._data_dir, symbol=symbol, limit=limit)


__all__ = ["TradingRunner"]
