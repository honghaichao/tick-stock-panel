"""实盘交易引擎 — 撮合调度 + 持仓/资金聚合 + SSE 推送。

职责:

  1. submit(order_draft) → 受理 (合法性 + 风控 + broker.submit) → 落库
  2. cancel(order_id) → broker.cancel → 落库 (status=CANCELED)
  3. 后台撮合线程 (start/stop): 每 N 秒拉一次 quote_service 实时缓存,
     找到 pending 订单对应 symbol 的当前快照/分钟 K, 调 broker.match_tick,
     落账 (更新 order 状态、append_fill、改 account、调 store.append_log)。
  4. 推送 SSE 事件 `trading_event` (前端 useQuoteStream 监听, 触发相关 query invalidation)。

不在此模块:
  - 委托/资金的最终落盘 (store.py); 撮合本身的规则 (paper_broker.py)。
  - quote 数据拉取 (quote_service, repository); engine 只读。

并发安全 (CONTRIBUTING §6.2):
  - _lock 保护所有内存态修改 (positions 索引 / 账户余额 / SSE 队列)。
  - 撮合线程不持锁做 IO: 拉 quote → broker.match (无锁) → 持锁落账。
  - quote 线程 / SSE generator 线程 / API 线程 共用同一锁。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import polars as pl

from app.services.trading import broker as broker_mod
from app.services.trading import paper_broker as paper_broker_mod
from app.services.trading import preferences as prefs
from app.services.trading import store
from app.services.trading.clearance import run_daily_clearance_if_due
from app.services.trading.models import (
    Account,
    EngineStatus,
    Fill,
    MatchDecision,
    Order,
    OrderDraft,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TradeLogEntry,
    TradeLogKind,
)
from app.services.trading.pnl import PnLSnapshot, append_snapshot
from app.services.trading.slippage import SlippageConfig
from app.services.trading.store import _new_id  # BUG-B: 共享 id 生成器 (was: secrets.token_hex)

logger = logging.getLogger(__name__)

# 持仓聚合 T+1 用: 同一交易日内的买入不参与当日可卖量
def _cn_today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()





class TradingEngine:
    """单例 (每个 app 进程一个), 通过 main.py 注入 app.state.trading_engine."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_match_ts: str | None = None
        self._last_heartbeat_ts: float = 0.0  # time.time(); _last_match_ts 是 ISO 字符串, 用浮点便于差分
        # PnL 快照节流: 每分钟最多写一次 (撮合轮每 5s, 无成交时也保持曲线连续).
        self._last_pnl_ts = None
        self._broker: broker_mod.BrokerAdapter = paper_broker_mod.PaperBroker()
        # SSE 通过 quote_service.push_trading_event 转发 — 不在自身维护订阅者列表。

    # ── 启动 / 关闭 ──────────────────────────────────
    def boot_check(self) -> None:
        """main.py 启动时调用 — 按 preferences 决定是否起撮合线程。"""
        if prefs.is_enabled():
            self.start()
        else:
            logger.info("实盘未启用 (preferences.trading_enabled=False), 撮合线程未启动")

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.info("实盘撮合线程已在运行, 不重复启动")
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._matcher_loop,
                name="trading-matcher",
                daemon=True,
            )
            self._thread.start()
            logger.info("实盘撮合线程已启动")

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
            self._thread = None
        if t is not None:
            t.join(timeout=5.0)

    # ── 委托提交 ──────────────────────────────────────
    def submit(
        self,
        data_dir,
        draft: OrderDraft,
        *,
        max_order_amount_override: float | None = None,
        effective_cash: float | None = None,
    ) -> Order:
        """同步受理委托: 校验 → 资金校验 → broker.submit → 落库。

        资金校验: 估算最大所需现金 (qty * limit_price / qty * 涨停价)
        与 max_order_amount 比较; 与账户余额 + 持仓可卖现金头寸比较。
        max_order_amount_override: 单笔金额上限 override (None=用 prefs.max_order_amount 手动默认 5万).
            runner.rebalance_to 传 prefs.max_order_amount_for_strategy() (默认 1000万), 让菜场大妈类
            JoinQuant 策略按原版等权分仓 (5 票 x 200k) 不被 TSP 风控硬卡.
        effective_cash: 可用现金 override (None=用落账 account.cash). 由 runner.rebalance_to 在
            一帧内累计"本帧已提交但未成交的 sells 所得 + 当前 cash"传入, 解决同帧 sell→buy 时
            buy 委托在资金校验阶段 cash 还是落账旧值的时序错位 (5 个 buy 全部 "可用现金不足" 被拒).
            仅用于 buy 校验; 卖单的 T+1 不动. None = 走落账 cash. 真实环境不推荐外部传.
        """
        order = Order(
            id=_new_id("ord"),
            symbol=draft.symbol,
            side=OrderSide(draft.side),
            order_type=OrderType(draft.order_type),
            qty=draft.qty,
            limit_price=draft.limit_price,
            note=draft.note,
            strategy_id=draft.strategy_id,            # 第一性字段 (per-strategy 过滤走这里)
            created_at=store.now_iso(),
        )

        # ── 风险控制 ──
        # 限价单必须能取到参考价 (amount / 资金校验都需要); 市价单下一根 K 即时成交,
        # 撮合前没有参考价 — 放过 ref_price=0, 让撮合侧按开盘价落账, 风控在成交后才
        # 体现 (PnL 实时减扣). 否则策略遇到 quote_service 还没拉到的小票 → 市价单
        # 也被拒, executor 拆单就失去意义.
        ref_price = draft.limit_price
        if ref_price is None:
            ref_price = _estimate_market_price(data_dir, draft.symbol) or 0.0
        if ref_price <= 0 and draft.order_type != "market":
            self._log_reject(data_dir, order, "无法获取标的最新价, 暂不可下单")
            order.status = OrderStatus.REJECTED
            order.reject_reason = "无法获取标的最新价"
            store.append_order(data_dir, order)
            raise TradingRejectError(order.reject_reason)

        amount = draft.qty * ref_price
        effective_limit = (
            max_order_amount_override
            if max_order_amount_override is not None
            else prefs.max_order_amount()
        )
        # 市价单且 ref_price=0: amount=0, 跳过金额上限检查 (成交时按实际价);
        # 限价单 amount 仍受 effective_limit 约束.
        if ref_price > 0 and amount > effective_limit:
            self._reject(data_dir, order, f"超过单笔金额上限 ¥{effective_limit:.0f}")
            raise TradingRejectError(f"单笔金额 ¥{amount:.0f} 超过上限 ¥{effective_limit:.0f}")

        # 资金校验 (仅买单校验资金不足, 卖单校验可卖数量)
        account = store.load_account(data_dir, initial_cash=prefs.initial_cash())
        if draft.side == "buy":
            # effective_cash override: runner.rebalance_to 同帧累计 sells proceeds 后注入,
            # 解决 sell→buy 异步撮合导致的 buy 资金校验误拒 (历史 bug: 09-29 14:02 5 个 buy 全 rejected).
            cash_for_check = effective_cash if effective_cash is not None else account.cash
            # override 永远不小于落账 cash, 防御性兜底 (e.g. caller 算错)
            cash_for_check = max(cash_for_check, account.cash)
            if cash_for_check < amount:
                self._reject(data_dir, order, "可用现金不足")
                raise TradingRejectError(
                    f"可用现金 ¥{cash_for_check:.0f} 不足以支付 ¥{amount:.0f}"
                )
            # F0-3 (2026-10-13): 持仓只数 + 单票占比风控生效 (仅买单)
            self._enforce_position_risk(data_dir, order, draft, amount, account)
        else:
            # T+1: 可卖数量
            available = _available_sell_qty(data_dir, draft.symbol)
            if draft.qty > available:
                self._reject(
                    data_dir, order,
                    f"可卖数量不足 (T+1): 可卖 {available} 股, 申请 {draft.qty}",
                )
                raise TradingRejectError(
                    f"可卖数量不足 (T+1): 可卖 {available} 股, 申请 {draft.qty}"
                )

        # ── broker 受理 ──
        ack = self._broker.submit(draft, order)
        if not ack.accepted:
            self._reject(data_dir, order, ack.reject_reason or "broker 拒单")
            raise TradingRejectError(ack.reject_reason or "broker 拒单")

        # 落库
        order.submitted_at = store.now_iso()
        store.append_order(data_dir, order)
        self._log_accept(data_dir, order)
        self._broadcast_event({"type": "order_submitted", "order": order.model_dump()})
        self._notify(data_dir, "order_submitted",
                     f"委托提交: {order.side.value} {order.symbol} {order.qty}股 @ {order.limit_price or '市价'}",
                     source=f"order:{order.id}")
        return order


    # ── F0-3: 持仓只数 / 单票占比风控 (2026-10-13) ──────────────────────────
    def _enforce_position_risk(
        self, data_dir, order: Order, draft: OrderDraft, amount: float, account
    ) -> None:
        """submit 阶段预检: 持仓只数 / 单票市值占比 (F0-3).

        仅对买单生效. 卖单不增加敞口, 跳过.
        拒单 → 落 REJECTED + 抛 TradingRejectError (caller 已捕获)。
        max_position_count: 已有持仓 symbol 数 + 新买入 symbol 数 ≤ limit。
                            仅当 draft.symbol 是 NEW symbol 时 +1。
        max_position_pct:   新增市值 / total_equity ≤ limit。
                            total_equity 用当前账户的 (含未成交委托, 保守)。
        """
        limit_count = prefs.max_position_count()      # 默认 20
        limit_pct = prefs.max_position_pct()          # 默认 0.20

        positions = self.positions(data_dir)
        held_symbols = {p.symbol for p in positions if p.qty > 0}
        new_symbol = draft.symbol not in held_symbols

        # 持仓只数: 仅在买入新 symbol 时校验
        if new_symbol and len(held_symbols) + 1 > limit_count:
            msg = (
                f"max_position_count: 持仓 {len(held_symbols)} 只, "
                f"买入 {draft.symbol} 后将达 {len(held_symbols) + 1} 只, "
                f"已超 limit={limit_count}"
            )
            self._reject(data_dir, order, msg)
            raise TradingRejectError(msg)

        # 单票占比: 任何买单都校验 (含加仓已持仓 symbol)
        if account.total_equity > 0:
            # 已持仓该 symbol 的市值 + 本次新增 = 新单票市值
            existing_mv = next(
                ((p.market_value or 0.0) for p in positions if p.symbol == draft.symbol),
                0.0,
            )
            new_mv = existing_mv + amount
            # ratio >= limit 视为超限 (严格大于, 允许 ratio=limit 通过, 即 limit=1.0=100% 通过)
            if new_mv / account.total_equity > limit_pct + 1e-9:
                msg = (
                    f"max_position_pct: 单票 {draft.symbol} 预估市值 ¥{new_mv:.0f}, "
                    f"超 {limit_pct:.0%} 总资产 ¥{account.total_equity:.0f}"
                )
                self._reject(data_dir, order, msg)
                raise TradingRejectError(msg)


    def cancel(self, data_dir, order_id: str) -> Order:
        with self._lock:
            order = store.get_order(data_dir, order_id)
            if order is None:
                raise TradingRejectError("订单不存在")
            if order.status not in (OrderStatus.NEW, OrderStatus.PARTIAL):
                raise TradingRejectError(f"订单状态为 {order.status.value}, 不可撤单")
            ok, reason = self._broker.cancel(order)
            if not ok:
                raise TradingRejectError(reason or "broker 拒绝撤单")
            order.status = OrderStatus.CANCELED
            order.canceled_at = store.now_iso()
            store.update_order(data_dir, order)
            entry = TradeLogEntry(
                id=_new_id("lg"), kind=TradeLogKind.CANCEL,
                ts=store.now_iso(),
                symbol=order.symbol, order_id=order.id,
                strategy_id=order.strategy_id,
                message="已撤单",
            )
            store.append_log(data_dir, entry)
            self._broadcast_event({"type": "order_canceled", "order": order.model_dump()})
            return order

    # ── 查询 ──────────────────────────────────────────
    def status(self, data_dir) -> EngineStatus:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            pending = store.pending_orders(data_dir)
            filled = [
                o for o in store.list_orders(data_dir, statuses=[OrderStatus.FILLED])
            ]
            account = store.load_account(data_dir, initial_cash=prefs.initial_cash())
            return EngineStatus(
                enabled=prefs.is_enabled(),
                running=running,
                broker=self._broker.name,
                initial_cash=account.initial_cash,
                pending_order_count=len(pending),
                filled_order_count=len(filled),
                matcher_last_ts=self._last_match_ts,
                matcher_interval_seconds=prefs.match_interval_seconds(),
                pending=pending,
            )

    def list_orders(self, data_dir, *, statuses=None, symbol=None, strategy_id=None, limit=None):
        return store.list_orders(
            data_dir, statuses=statuses, symbol=symbol, strategy_id=strategy_id, limit=limit,
        )

    def list_fills(self, data_dir, *, symbol=None, strategy_id=None, limit=None):
        return store.list_fills(
            data_dir, symbol=symbol, strategy_id=strategy_id, limit=limit,
        )

    def list_logs(self, data_dir, *, limit=None, kind=None, strategy_id=None):
        return store.list_logs(data_dir, limit=limit, kind=kind, strategy_id=strategy_id)

    def account(self, data_dir) -> Account:
        return store.load_account(data_dir, initial_cash=prefs.initial_cash())

    def positions(self, data_dir, *, strategy_id: str | None = None) -> list[Position]:
        """聚合持仓: 由 fills 重算 (加权平均成本 + 实时价/昨收).

        fills 必须按时间正序遍历 (旧 -> 新), 否则先卖后买的顺序会让 qty
        进入负分支并被静默修到 0, 把"先卖 30 再买 100"误读成"无变动"。
        list_fills 默认倒序, 这里显式反转为正序。

        Args:
            strategy_id: 按策略 id 过滤 fills (per-strategy 持仓).
                None = 全部 fills (账户级持仓).
        """
        fills = list(reversed(store.list_fills(data_dir, limit=None, strategy_id=strategy_id)))
        if not fills:
            return []
        # 按 symbol 聚合
        per: dict[str, dict[str, Any]] = {}
        for f in fills:
            # fills 已按时间倒序, 反转为时间正序计算
            entry = per.setdefault(f.symbol, {
                "qty": 0, "cost_basis": 0.0, "first_buy": None, "last_buy": None,
            })
            if f.side == OrderSide.BUY:
                entry["cost_basis"] += f.price * f.qty
                entry["qty"] += f.qty
                d = f.trade_date
                if entry["first_buy"] is None or d < entry["first_buy"]:
                    entry["first_buy"] = d
                if entry["last_buy"] is None or d > entry["last_buy"]:
                    entry["last_buy"] = d
            else:
                # SELL: 移动加权成本按比例缩放
                if entry["qty"] > 0:
                    ratio = max(0.0, (entry["qty"] - f.qty) / entry["qty"])
                    entry["cost_basis"] *= ratio
                    entry["qty"] -= f.qty
                else:
                    entry["qty"] -= f.qty
                if entry["qty"] <= 0:
                    entry["qty"] = 0
                    entry["cost_basis"] = 0.0
                    entry["first_buy"] = None
                    entry["last_buy"] = None

        # 实时价 + 昨收
        last_prices, prev_closes = _latest_prices_and_prev_closes(data_dir)

        positions: list[Position] = []
        for sym, entry in per.items():
            qty = entry["qty"]
            if qty <= 0:
                continue
            avg_cost = entry["cost_basis"] / qty if qty > 0 else 0.0
            lp = last_prices.get(sym)
            pc = prev_closes.get(sym)
            market_value = (lp * qty) if lp is not None else None
            today_pnl = ((lp - pc) * qty) if (lp is not None and pc is not None) else None
            today_pnl_pct = ((lp - pc) / pc) if (lp and pc) else None
            accu_pnl = ((lp - avg_cost) * qty) if lp is not None else None
            accu_pnl_pct = ((lp - avg_cost) / avg_cost) if (lp and avg_cost > 0) else None
            holding_days = (_cn_today() - entry["last_buy"]).days if entry["last_buy"] else None
            positions.append(Position(
                symbol=sym,
                qty=qty,
                avg_cost=avg_cost,
                last_price=lp,
                prev_close=pc,
                market_value=market_value,
                total_pnl=today_pnl,
                total_pnl_pct=today_pnl_pct,
                accumulated_pnl=accu_pnl,
                accumulated_pnl_pct=accu_pnl_pct,
                first_buy_date=entry["first_buy"],
                last_buy_date=entry["last_buy"],
                holding_days=holding_days,
            ))
        positions.sort(key=lambda p: p.symbol)
        return positions

    # ── 撮合循环 ──────────────────────────────────────
    def _matcher_loop(self) -> None:
        """后台线程 — 每 N 秒扫一次 pending, 调用 broker.match_tick, 落账。

        撮合循环不直接调用 quote_service, 而是被外部 tick 触发: 当前实现下
        每 N 秒读一次仓库分钟 K 当日最新一根 (不引 quote 线程, 见 §6.2)。

        每分钟 (分内 0 秒附近) 写一条 heartbeat, 把撮合线程还活着这件事暴给 screenlog.
        历史诊断 2026-10-10: 撮合线程启动后无 per-tick log, 后端跑 14h 中断后人眼无法
        判断线程是否健康; 此条 INFO 是低成本的"我还活着"信号, 不增加写盘 IO.
        """
        while not self._stop.is_set():
            interval = prefs.match_interval_seconds()
            try:
                self._tick_once()
            except Exception as e:
                logger.exception("撮合轮询异常: %s", e)
            # 心跳 (60s 一次; 取自身 wall clock 与上次记录差分)
            now_ts = time.time()
            if now_ts - self._last_heartbeat_ts >= 60.0:
                self._last_heartbeat_ts = now_ts
                from datetime import datetime as _hb_dt
                from datetime import timedelta as _hb_td
                from datetime import timezone as _hb_tz
                _hb_now = _hb_dt.now(_hb_tz(_hb_td(hours=8)))
                _hb_pending = "n/a"
                try:
                    _hb_app = _APP_STATE
                    if _hb_app is not None:
                        _hb_pending = str(len(store.pending_orders(_hb_app.repo.store.data_dir)))
                except Exception:
                    pass
                logger.info(
                    "撮合心跳: alive=%s, pending=%s, last_match=%s, interval=%ss",
                    _hb_now.strftime("%H:%M:%S"),
                    _hb_pending,
                    self._last_match_ts or "(never)",
                    interval,
                )
            # 退避 (可被 stop 打断)
            self._stop.wait(timeout=interval)

    def _tick_once(self) -> None:
        """执行一次撮合扫描 — 同步方法, 撮合线程调用。

        撮合 K 线选择: 上一根已收盘 K (close_time <= now - 60s), 跟聚宽一致.
        - 9:30 第一根 K 标识 first_bar_of_day, broker 内部用 bar.open 撮合市价单.
        - 9:31 后撮合用 9:30 K 的 close (上一根 K 的 close).
        - 撮合 K 必须完整收盘, 未收盘 K 不撮合 (等下一轮).
        """
        app_state = _APP_STATE
        if app_state is None:
            return
        repo = getattr(app_state, "repo", None)
        if repo is None:
            return
        data_dir = repo.store.data_dir
        with self._lock:
            self._last_match_ts = store.now_iso()
            # 17:00 清算 (每天第一轮 tick 命中 17:00 时跑, 跑完置 _last_clearance_date)
            try:
                run_daily_clearance_if_due(data_dir, self)
            except Exception as e:
                logger.warning("17:00 清算异常: %s", e)
            pending = store.pending_orders(data_dir)
            if not pending:
                self._tick_refresh_when_idle(data_dir)
                return

            # 撮合 K 线: 上一根已收盘 (close_time <= now - 60s)
            symbols = sorted({o.symbol for o in pending})
            bar_by_symbol: dict[str, pl.DataFrame] = {}
            first_bar_of_day: dict[str, bool] = {}
            today = _cn_today()
            now = datetime.now(timezone(timedelta(hours=8)))
            threshold = now - timedelta(seconds=60)   # 已收盘阈值
            # K 线 datetime 是 naive (仓库惯例视为北京时间); threshold 是 aware,
            # polars pl.lit().cast(pl.Datetime) 会把 aware 转 UTC, 导致 closed 永远是空。
            # 提前去掉 tzinfo, 让两边都用 Beijing naive 比较 (数值一致)。
            threshold_naive = threshold.replace(tzinfo=None)
            for sym in symbols:
                try:
                    df = repo.get_minute_batch([sym], today)
                    if df.is_empty():
                        continue
                    # 过滤: 已收盘 K (close_time = datetime + 1m <= threshold)
                    # polars 表达式: datetime + 1m <= threshold
                    if "datetime" in df.columns:
                        closed = df.filter(
                            (pl.col("datetime").cast(pl.Datetime) + pl.duration(minutes=1))
                            <= pl.lit(threshold_naive).cast(pl.Datetime)
                        ).sort("datetime")
                    else:
                        closed = df.sort("datetime")
                    if closed.is_empty():
                        continue
                    last_bar = closed.tail(1)
                    bar_by_symbol[sym] = last_bar
                    # 9:30 第一根 K 标识
                    first_dt = closed.row(0, named=True).get("datetime")
                    if first_dt is not None and hasattr(first_dt, "hour"):
                        first_bar_of_day[sym] = (first_dt.hour == 9 and first_dt.minute == 30)
                except Exception as e:
                    logger.debug("撮合拉 %s 分钟 K 失败: %s", sym, e)

            # 滑点 + 量限
            slippage = SlippageConfig(
                kind=prefs.slippage_kind(),
                value=prefs.slippage_value(),
            )
            order_volume_ratio = prefs.order_volume_ratio()
            decisions = self._broker.match_tick(
                pending, bar_by_symbol,
                slippage=slippage,
                order_volume_ratio=order_volume_ratio,
                first_bar_of_day=first_bar_of_day,
            )
            self._apply_decisions(data_dir, decisions)

    def _apply_decisions(self, data_dir, decisions: list[MatchDecision]) -> None:
        any_fill = False
        for d in decisions:
            if d.matched:
                self._settle_fill(data_dir, d)
                any_fill = True
            # 不成交: 不写日志 (撮合轮询每 5s 都跑, 写日志会爆)
        # 撮合后广播一次整体刷新事件 (前端按节奏拉 positions/account, 不逐笔推)
        if decisions:
            self._broadcast_event({"type": "matcher_tick", "ts": store.now_iso()})

        # PnL 快照: 即使无成交也写, 让权益曲线连续 (受 last_price 实时变化驱动).
        # 节流: 每分钟最多一次, 避免高频写盘.
        if not any_fill:
            now = datetime.now(timezone(timedelta(hours=8)))
            last_ts = self._last_pnl_ts
            if last_ts is None or (now - last_ts).total_seconds() >= 60:
                self._write_pnl_snapshot(data_dir, trigger="tick")
                self._last_pnl_ts = now

    def _refresh_account_market_value(self, data_dir, account=None) -> None:
        """从 positions() 拉最新价, 刷新 account.market_value / unrealized_pnl / total_equity / total_pnl.

        必须独立成 helper:
          1. fill 后调用 (已有调用点)
          2. matcher 循环无 fill 时也调用 — 否则持仓全程没有 fill 时 account.market_value
             永远停在 0, PnL 看板一直 0. (历史 bug: 2026-09-29 5 票全成交后
             pending=0, _tick_once 直接 return, account.json market_value 永远 0)

        last_price 缺失 (quote_service 没拉到的小票) → 该 symbol 贡献 0; 与 positions() 同口径.
        total_equity 会略偏低 (不会假阳).
        """
        if account is None:
            account = store.load_account(data_dir, initial_cash=prefs.initial_cash())
        positions = self.positions(data_dir)
        account.market_value = 0.0
        for p in positions:
            if p.last_price is not None:
                account.market_value += p.last_price * p.qty
        account.total_equity = account.cash + account.market_value
        account.unrealized_pnl = 0.0
        for p in positions:
            if p.last_price is not None:
                account.unrealized_pnl += (p.last_price - p.avg_cost) * p.qty
        account.total_pnl = account.realized_pnl + account.unrealized_pnl
        store.save_account(data_dir, account)

    def _tick_refresh_when_idle(self, data_dir) -> None:
        """撮合空转时 (无 pending 委托) 刷新账户市值 + 节流写 PnL 快照.

        拆分节流:
          - account.json: 每 5s 写一次 (matcher 节奏, 实时; 文件 ~200B, 无 IO 压力)
          - pnl_snapshots.jsonl: 60s 节流 (历史曲线, 每 5s 写一天 17000 行, 文件膨胀)

        PnL 看板读 account.json, 因此前端 8s 轮询 → 数据延迟 ≤ 13s (5s + 8s).
        """
        try:
            self._refresh_account_market_value(data_dir)
        except Exception as e:
            logger.warning("idle 刷新账户市值失败: %s", e)
            return
        now = datetime.now(timezone(timedelta(hours=8)))
        last_ts = self._last_pnl_ts
        if last_ts is not None and (now - last_ts).total_seconds() < 60:
            return
        try:
            self._write_pnl_snapshot(data_dir, trigger="tick")
            self._last_pnl_ts = now
        except Exception as e:
            logger.warning("idle 写 PnL 快照失败: %s", e)

    def _write_pnl_snapshot(self, data_dir, *, trigger: str) -> None:
        """写一条 PnL 快照; 失败仅记 WARN, 不影响撮合."""
        try:
            account = store.load_account(data_dir, initial_cash=prefs.initial_cash())
            snapshot = PnLSnapshot(
                ts=store.now_iso(),
                trade_date=datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
                total_equity=account.total_equity,
                cash=account.cash,
                market_value=account.market_value,
                realized_pnl=account.realized_pnl,
                unrealized_pnl=account.unrealized_pnl,
                total_pnl=account.total_pnl,
                trigger=trigger,
            )
            append_snapshot(data_dir, snapshot)
        except Exception as e:
            logger.warning("PnL 快照 (%s) 失败: %s", trigger, e)

    def _settle_fill(self, data_dir, decision: MatchDecision) -> None:
        order = store.get_order(data_dir, decision.order_id)
        if order is None or order.status in (OrderStatus.FILLED, OrderStatus.CANCELED):
            return
        assert decision.fill_price is not None and decision.fill_qty is not None
        today = _cn_today()

        # 费税 (按 preferences 中的费率)
        gross = decision.fill_qty * decision.fill_price
        fee = max(prefs.fee_min(), gross * prefs.fee_rate())
        tax = gross * prefs.stamp_tax_rate() if order.side == OrderSide.SELL else 0.0
        amount = gross  # 会计用的"成交金额" = qty * price, 不含费税

        fill = Fill(
            id=_new_id("fl"),
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=decision.fill_qty,
            price=decision.fill_price,
            fee=fee,
            tax=tax,
            amount=amount,
            filled_at=store.now_iso(),
            trade_date=today,
            strategy_id=order.strategy_id,           # 撮合时刻从关联 Order 继承 (per-strategy 隔离)
        )
        # 资金校验 (在 append_fill 前):
        # 撮合异步: submit 时 cash_for_check 通过了 effective_cash 估值, 但撮合时刻真实 cash 可能更少.
        # 若 buy 撮合时 cash 不足, 拒收 fill, order 保持 NEW 等待下一轮 (不要扣 cash 进负数).
        # 历史 bug: 2026-10-12 用户反复触发 rebalance, 撮合 33 笔 buy, cash 跌到 -27,788,255 (负 27M).
        account = store.load_account(data_dir, initial_cash=prefs.initial_cash())
        if order.side == OrderSide.BUY:
            need = amount + fee
            if account.cash < need:
                logger.warning(
                    "撮合拒收 buy: order=%s symbol=%s qty=%d price=%.3f need=¥%.0f cash=¥%.0f (资金不足, 等下一轮)",
                    order.id, order.symbol, decision.fill_qty, decision.fill_price,
                    need, account.cash,
                )
                return  # order 维持 NEW, 等 cash 补上 (e.g. 同帧 sells 撮合后) 下一轮再试
        order.filled_qty += decision.fill_qty
        order.avg_fill_price = decision.fill_price
        order.filled_at = fill.filled_at
        order.status = OrderStatus.FILLED
        store.append_fill(data_dir, fill)
        self._notify(data_dir, "order_filled",
                     f"成交: {fill.side.value} {fill.symbol} {fill.qty}股 @ {fill.price:.3f}",
                     source=f"order:{order.id}")
        store.update_order(data_dir, order)

        # 账户更新 (资金 - 买入金额 / + 卖出金额; 卖出扣费税)
        if order.side == OrderSide.BUY:
            account.cash -= (amount + fee)
        else:
            account.cash += (amount - fee - tax)
        # market_value / total_equity 必须在 save 前刷新, 否则 PnL 看板永远 0:
        # 老版本 fill 后只动 cash, market_value 一直停在 0, total_equity = cash + 0 != 实际权益,
        # 引发 today_pnl / total_pnl / unrealized_pnl 全错. 用最近价 × qty 重算 (与 positions() 同口径).
        # 注意: quote_service 没拉到的小票 last_price=None → 该 symbol 贡献 0; 这与"无最新价"语义一致,
        #   total_equity 会略偏低于真实市值, 但 cash + 0 < cash + 真实市值, PnL 不会假阳.
        self._refresh_account_market_value(data_dir, account)

        # 写交易日志 (成交事件)
        entry = TradeLogEntry(
            id=_new_id("lg"),
            kind=TradeLogKind.FILL,
            ts=store.now_iso(),
            symbol=order.symbol,
            order_id=order.id,
            fill_id=fill.id,
            strategy_id=order.strategy_id,             # 继承 order 的 strategy_id
            message=(
                f"{order.symbol} {order.side.value} {decision.fill_qty}股 @ "
                f"¥{decision.fill_price:.3f}"
            ),
            details={"fee": fee, "tax": tax, "reason": decision.reason},
        )
        store.append_log(data_dir, entry)

        # 推送 fill + 持仓/账户刷新提示
        self._broadcast_event({
            "type": "fill",
            "fill": fill.model_dump(mode="json"),
            "order_id": order.id,
            "symbol": order.symbol,
        })

        # PnL 快照 (fills 是权益变化的会计事实, 每次落一笔)
        try:
            account = store.load_account(data_dir, initial_cash=prefs.initial_cash())
            snapshot = PnLSnapshot(
                ts=store.now_iso(),
                trade_date=datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
                total_equity=account.total_equity,
                cash=account.cash,
                market_value=account.market_value,
                realized_pnl=account.realized_pnl,
                unrealized_pnl=account.unrealized_pnl,
                total_pnl=account.total_pnl,
                trigger="fill",
            )
            append_snapshot(data_dir, snapshot)
        except Exception as e:
            logger.warning("PnL 快照 (fill) 失败: %s", e)

    # ── 内部 ──────────────────────────────────────────
    def _reject(self, data_dir, order: Order, reason: str) -> None:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        store.append_order(data_dir, order)
        entry = TradeLogEntry(
            id=_new_id("lg"), kind=TradeLogKind.REJECT,
            ts=store.now_iso(),
            symbol=order.symbol, order_id=order.id,
            strategy_id=order.strategy_id,
            message=f"拒单: {reason}",
        )
        store.append_log(data_dir, entry)
        self._broadcast_event({
            "type": "order_rejected",
            "order": order.model_dump(),
            "reason": reason,
        })
        self._notify(data_dir, "order_rejected",
                     f"拒单: {order.symbol} {order.qty}股 — {reason}",
                     source=f"order:{order.id}")

    def _log_reject(self, data_dir, order: Order, reason: str) -> None:
        # 仅写日志, 不落订单 (订单还没生成)
        entry = TradeLogEntry(
            id=_new_id("lg"), kind=TradeLogKind.REJECT,
            ts=store.now_iso(),
            symbol=order.symbol, order_id=None,
            strategy_id=order.strategy_id,           # pre-validate, 但 order 字段已构造好
            message=f"预校验拒单: {reason}",
        )
        store.append_log(data_dir, entry)

    def _log_accept(self, data_dir, order: Order) -> None:
        entry = TradeLogEntry(
            id=_new_id("lg"), kind=TradeLogKind.ACCEPT,
            ts=store.now_iso(),
            symbol=order.symbol, order_id=order.id,
            strategy_id=order.strategy_id,
            message=(
                f"{order.side.value} {order.symbol} {order.qty}股 "
                f"{'@限价' + f'{order.limit_price:.3f}' if order.limit_price else '@市价'}"
            ),
        )
        store.append_log(data_dir, entry)    # ── SSE ─────────────────────────────────────
    # 实盘事件统一通过 quote_service.push_trading_event 转发 (与 alerts/reviews 同一通道),
    # 由 /api/intraday/stream SSE 端点以 'trading_event' 事件名推到前端。
    # 不另起 SSE 端点 — 实盘页 + 看板 / 自选页 都复用同一条连接。
    def _broadcast_event(self, event: dict) -> None:
        qs = _get_quote_service()
        if qs is None:
            return
        try:
            qs.push_trading_event(event)
        except Exception as e:
            logger.warning("trading event push failed: %s", e)

    def _notify(self, data_dir, kind: str, text: str, source: str = "trading") -> None:
        """聚宽式自动通知: 落 notify.jsonl + SSE + wecom/feishu 推送 (F0-2, 2026-10-13).

        落盘 + 推送分离: 落 notify.jsonl 失败不阻塞推送, 反之亦然。
        """
        # 1. 落 notify.jsonl (既有)
        try:
            from app.services.trading.notify_store import append as _notify
            _notify(data_dir, kind=kind, text=text, source=source)
        except Exception as e:
            logger.warning("notify.append failed: %s", e)
        # 2. F0-2: wecom/feishu webhook 推送 (静默降级)
        try:
            from app.services.trading import push
            push.push_event(kind, text, source=source)
        except Exception as e:
            logger.warning("notify.push failed: %s", e)


# ── 工具 ──────────────────────────────────────────────
class TradingRejectError(Exception):
    """用户面向的拒单异常 — 路由层捕获后映射 HTTP 400。"""


# 全局 app.state 引用 (main.py 在启动时注入)
_APP_STATE: Any = None


def set_app_state(app_state: Any) -> None:
    global _APP_STATE
    _APP_STATE = app_state


def _available_sell_qty(data_dir, symbol: str) -> int:
    """T+1 可卖数量 = 总持仓 - 今日买入."""
    fills = store.list_fills(data_dir, symbol=symbol, limit=None)
    qty = 0
    today = _cn_today()
    for f in fills:
        if f.side == OrderSide.BUY:
            qty += f.qty
            if f.trade_date == today:
                qty -= f.qty   # 今日买入不计入可卖
        else:
            qty -= f.qty
    return max(0, qty)


def _latest_prices_and_prev_closes(data_dir) -> tuple[dict[str, float], dict[str, float]]:
    """读 quote_service 内存缓存的最新价 + 昨收 (轻量, 不引网络)."""
    last_price_map: dict[str, float] = {}
    prev_close_map: dict[str, float] = {}
    qs = _get_quote_service()
    if qs is None:
        return last_price_map, prev_close_map
    try:
        df = qs.get_quotes_compat()
    except Exception:
        return last_price_map, prev_close_map
    if df.is_empty():
        return last_price_map, prev_close_map
    for row in df.iter_rows(named=True):
        sym = row.get("symbol")
        lp = row.get("last_price") or row.get("close")
        pc = row.get("prev_close")
        if sym and lp is not None:
            last_price_map[sym] = float(lp)
        if sym and pc is not None:
            prev_close_map[sym] = float(pc)
    return last_price_map, prev_close_map


def _estimate_market_price(data_dir, symbol: str) -> float | None:
    """市价单估算价: 用 quote_service 缓存的 last_price."""
    last, _ = _latest_prices_and_prev_closes(data_dir)
    return last.get(symbol)


def _get_quote_service():
    if _APP_STATE is None:
        return None
    return getattr(_APP_STATE, "quote_service", None)


__all__ = ["TradingEngine", "TradingRejectError", "set_app_state"]