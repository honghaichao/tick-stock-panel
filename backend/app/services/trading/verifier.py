"""实盘策略回测验证 — 让策略在历史区间上 dry-run 一次, 输出策略行为画像 + 模拟 PnL.

与 TradingExecutor 的区别 (CONTRIBUTING §1.4):
  - 只读: 不写 store / 不下真实单 / 不推 SSE / 不消耗 preferences.
  - 用历史数据构造每个 event 时的 ctx:
      data_provider.instruments      ← 仓储今日 (real 实例)
      data_provider.{limit_up,...}  ← 历史快照 (mock 出该交易日为 as_of 的快照)
      data_provider.minute_close    ← 历史 close (用 daily close 近似 1m close)
      data_provider.dividends(...)   ← TushareDividendSource (实时 pro.dividend), CSV 仅 fallback
  - 调 strategy.on_event(event, ctx) 收集 target.
  - simulate_fills=True 时按当日 close 价格假设 fill, 累计 PnL / 最大回撤 / 胜率.

不是回测引擎:
  - 回测走 strategy.lots.META + backtest.worker, 是 META 输出选股列表; 实盘走 on_event.
  - 本模块不与回测引擎共享矩阵 / 优化逻辑, 仅做"实盘策略在历史数据上的可行性验证".

不写入持久层:
  - 报告可选保存到 data/user_data/trading/verifications/<strategy_id>/<run_id>.json,
    用于跨会话对比. 默认不保存, 仅返回.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl

from app.services.trading.live_strategy import (
    LiveStrategy,
    LiveStrategyContext,
    LiveStrategyRegistry,
    StrategySchedule,
)

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(CN_TZ).isoformat()


def _stock_num_lots(symbol: str, qty: int) -> int:
    """A 股最小 100 股, 不对齐就按整百取整."""
    return int(qty // 100) * 100


# ── 单日单事件快照 ───────────────────────────────────
@dataclass
class DayEvent:
    date: str                       # YYYY-MM-DD
    event: str
    status: str                     # 'success' / 'no_target' / 'error'
    target: dict[str, int] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0


# ── 模拟成交记录 ─────────────────────────────────────
@dataclass
class SimulatedFill:
    date: str
    symbol: str
    side: str          # 'buy' / 'sell'
    qty: int
    price: float
    note: str = ""


# ── 模拟 PnL 聚合 ─────────────────────────────────────
@dataclass
class SimulatedPnL:
    initial_cash: float
    final_cash: float
    final_market_value: float
    final_total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    total_pnl_pct: float          # 总收益 / 初始资金
    max_drawdown: float            # 最大回撤比例 (正数)
    max_drawdown_date: str | None
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_pnl_per_trade: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── 验证报告 (顶层) ──────────────────────────────────
@dataclass
class VerificationReport:
    # 必填字段 (无默认值)
    run_id: str
    strategy_id: str
    started_at: str
    start_date: str
    end_date: str
    # 可选字段 (带默认值) — 必须放最后, dataclass 强制
    finished_at: str = ""
    duration_ms: int = 0
    trading_days: int = 0
    events: list[DayEvent] = field(default_factory=list)
    simulate_fills: bool = False
    initial_cash: float = 0.0
    pnl: SimulatedPnL | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    fills: list[SimulatedFill] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pnl"] = self.pnl.to_dict() if self.pnl else None
        d["fills"] = [asdict(f) for f in self.fills]
        return d


# ── 模拟账户 (轻量, 不依赖 store) ─────────────────────
class _SimAccount:
    """最小可用的账户视图: 现金 + 持仓 + 已实现盈亏."""
    def __init__(self, initial_cash: float, prices: dict[str, float]) -> None:
        self.cash = initial_cash
        self.holdings: dict[str, int] = {}      # symbol → qty
        self.cost: dict[str, float] = {}        # symbol → 移动加权平均成本 (per share)
        self.realized = 0.0
        self.last_prices = prices              # 当日 close, 用于 mark-to-market

    def fill(self, symbol: str, side: str, qty: int, price: float) -> SimulatedFill:
        """模拟一笔成交: 更新现金 + 持仓 + 成本 + 已实现."""
        qty = _stock_num_lots(symbol, qty)
        if qty <= 0 or price <= 0:
            return SimulatedFill(date="", symbol=symbol, side=side, qty=0, price=price)
        if side == "buy":
            self.cash -= qty * price
            cur = self.holdings.get(symbol, 0)
            cur_cost = self.cost.get(symbol, 0.0)
            self.cost[symbol] = (cur_cost * cur + price * qty) / max(cur + qty, 1)
            self.holdings[symbol] = cur + qty
        else:  # sell
            cur = self.holdings.get(symbol, 0)
            sell_qty = min(qty, cur)
            if sell_qty <= 0:
                # 无持仓可卖 (T+1 时差)
                return SimulatedFill(date="", symbol=symbol, side="sell", qty=0, price=price)
            cost_per = self.cost.get(symbol, price)
            self.realized += (price - cost_per) * sell_qty
            self.cash += sell_qty * price
            self.holdings[symbol] = cur - sell_qty
            if self.holdings[symbol] <= 0:
                self.holdings.pop(symbol, None)
                self.cost.pop(symbol, None)
        return SimulatedFill(
            date="", symbol=symbol, side=side, qty=qty if side == "buy" else sell_qty, price=price,
        )

    def total_equity(self) -> float:
        mv = sum(self.holdings.get(s, 0) * self.last_prices.get(s, 0.0) for s in self.holdings)
        return self.cash + mv


# ── 交易日列表 (用 kline_daily_enriched 取全部 trade_date) ──
def _trading_days(repo: Any, start: _date, end: _date) -> list[_date]:
    """返回 [start, end] 范围内所有交易日 (按 enriched 实际存在的 date).

    2026-10-13 BUGFIX: date 列可能含混合类型 (datetime.date / str), 强转 date.
    """
    out: list[_date] = []
    try:
        if hasattr(repo, "get_enriched_range"):
            ed = repo.get_enriched_range(start, end, columns=["date"])
            if ed is not None and not ed.is_empty():
                for d in ed["date"].to_list():
                    if hasattr(d, "date") and callable(d.date):
                        d = d.date()
                    if isinstance(d, _date) and start <= d <= end:
                        out.append(d)
                if out:
                    return sorted(set(out))
    except Exception as e:
        logger.warning("verifier: enriched_range 失败, 回退 parquet: %s", e)
    # 回退 1: parquet 落盘目录
    try:
        return _trading_days_from_parquet(repo, start, end)
    except Exception as e:
        logger.warning("verifier: parquet fallback 失败: %s", e)
        return []


def _trading_days_from_parquet(repo: Any, start: _date, end: _date) -> list[_date]:
    """回退: 扫 kline_daily_enriched/ 下的 date=YYYY-MM-DD/ 目录名, 拿 [start, end] 范围.

    cache 暖机只覆盖 ~300 天; 长窗口 (≥ 1 年) 回测会超出 cache 范围, 此时直接
    读磁盘落盘目录, 不再走内存 cache. 单测里也用得到 (清空 cache 后验证回退).
    """
    enriched_dir = getattr(getattr(repo, "store", None), "data_dir", None)
    if enriched_dir is None:
        return []
    enriched_dir = enriched_dir / "kline_daily_enriched"
    if not enriched_dir.exists():
        return []
    out: list[_date] = []
    for d in enriched_dir.iterdir():
        if not d.name.startswith("date="):
            continue
        try:
            day = _date.fromisoformat(d.name.removeprefix("date="))
        except ValueError:
            continue
        if start <= day <= end:
            out.append(day)
    return sorted(out)


# ── 历史日内 close (模拟 1m close) ─────────────────────
def _historical_close_map(repo: Any, target_date: _date) -> dict[str, float]:
    """取 target_date 当日所有股票 close → {symbol: close}; 用于近似 minute_close."""
    try:
        inst = repo.get_instruments()
        if inst.is_empty():
            return {}
        symbols = inst["symbol"].to_list() if "symbol" in inst.columns else []
        if not symbols:
            return {}
        df = repo.get_daily_batch(symbols, target_date, target_date)
        if df is None or df.is_empty():
            return {}
        out: dict[str, float] = {}
        # prefer raw_close (不复权, 与撮合口径一致)
        for r in df.iter_rows(named=True):
            sym = r.get("symbol")
            close = r.get("raw_close") or r.get("close")
            if sym and close is not None and close > 0:
                out[sym] = float(close)
        return out
    except Exception as e:
        logger.warning("verifier: 取 %s close 失败: %s", target_date, e)
        return {}


def _historical_instruments_snapshot(repo: Any, target_date: _date) -> dict[str, dict[str, Any]]:
    """取 target_date 当日 instruments 快照 (limit_up / limit_down / name / total_shares)."""
    try:
        inst = repo.get_instruments()
        if inst.is_empty():
            return {}
        snap: dict[str, dict[str, Any]] = {}
        for r in inst.iter_rows(named=True):
            sym = r.get("symbol")
            if not sym:
                continue
            snap[sym] = {
                "symbol": sym,
                "limit_up": float(r.get("limit_up") or 0.0),
                "limit_down": float(r.get("limit_down") or 0.0),
                "name": str(r.get("name") or ""),
                "total_shares": float(r.get("total_shares") or 0.0),
                "type": str(r.get("type") or "stock"),
                "exchange": str(r.get("exchange") or ""),
                # listing_date: instruments.parquet 里是 String (YYYY-MM-DD),
                # 转 Date 给策略端做 <= context.trade_date 比较 (策略原意: 只取
                # trade_date 时已上市的票, 模拟原版 get_all_securities('stock', dt_last)).
                "listing_date": _date.fromisoformat(r.get("listing_date")) if r.get("listing_date") else None,
            }
        return snap
    except Exception as e:
        logger.warning("verifier: 取 instruments 快照失败: %s", e)
        return {}


# ── Verifier 主类 ──────────────────────────────────────
class StrategyVerifier:
    """回测验证: 拿历史数据构造每个交易日每个 event 的 ctx, 调 on_event, 记录 target.

    与 executor 共享 LiveStrategyRegistry, 但不写 store / 不推 SSE / 不启动 scheduler.
    """

    def __init__(self, registry: LiveStrategyRegistry, repo: Any) -> None:
        self._registry = registry
        self._repo = repo
        self._lock = threading.Lock()

    def verify(
        self,
        strategy_id: str,
        *,
        start_date: _date,
        end_date: _date,
        simulate_fills: bool = True,
        initial_cash: float = 1_000_000.0,
        events: list[str] | None = None,
    ) -> VerificationReport:
        """运行回测验证. events=None → 用 strategy 所有 enabled schedule.

        Returns: VerificationReport (含每事件 snapshot + 可选模拟 PnL).
        """
        strategy = self._registry.get(strategy_id)
        if strategy is None:
            raise ValueError(f"策略 {strategy_id} 未注册")

        with self._lock:
            report = VerificationReport(
                run_id=uuid.uuid4().hex[:12],
                strategy_id=strategy_id,
                started_at=_now_iso(),
                start_date=str(start_date),
                end_date=str(end_date),
                simulate_fills=simulate_fills,
                initial_cash=initial_cash,
            )
            schedules = self._resolve_schedules(strategy, events)
            # DEBUG 2026-10-13: log all relevant state before _trading_days
            logger.warning(
                "DEBUG verify [INNER]: self._repo=%s cache=%s warming=%s",
                id(self._repo),
                getattr(self._repo, "_enriched_history_cache", None) is not None,
                getattr(self._repo, "_enriched_warming", "N/A"),
            )
            trading_days = _trading_days(self._repo, start_date, end_date)
            report.trading_days = len(trading_days)

            sim_account = _SimAccount(initial_cash, {}) if simulate_fills else None
            sim_equity_curve: list[tuple[_date, float]] = []
            sim_fills: list[SimulatedFill] = []
            target_distribution: dict[str, int] = {}  # 出现过目标 symbol → 次数
            total_signals = 0

            for d in trading_days:
                # 1. 拉当日的 instruments 快照 (limit_up/limit_down/name/shares)
                inst_snap = _historical_instruments_snapshot(self._repo, d)
                # 2. 拉当日 close 字典 (近似 1m close 给 ctx.market)
                close_map = _historical_close_map(self._repo, d)
                # 4. 验证 update sim_account last_prices
                if sim_account is not None:
                    sim_account.last_prices = close_map

                for sch in schedules:
                    day_event = DayEvent(date=str(d), event=sch.name, status="success")
                    started = datetime.now()
                    try:
                        ctx = self._build_context(d, sch, strategy, close_map, inst_snap, sim_account, initial_cash)
                        target = strategy.on_event(sch.name, ctx)
                        if not isinstance(target, dict):
                            raise TypeError(
                                f"策略 {strategy_id}.on_event({sch.name}) 必须返回 dict; 实际 {type(target).__name__}"
                            )
                        target = {str(k): int(v) for k, v in target.items()}
                        day_event.target = target
                        total_signals += 1
                        for sym in target:
                            target_distribution[sym] = target_distribution.get(sym, 0) + 1
                        # 模拟成交
                        if sim_account is not None and target:
                            self._simulate_rebalance(sim_account, d, target, close_map, sim_fills)
                        if not target:
                            day_event.status = "no_target"
                    except Exception as e:
                        logger.warning("verifier: %s %s/%s failed: %s", d, strategy_id, sch.name, e)
                        day_event.status = "error"
                        day_event.error = f"{type(e).__name__}: {e}"
                        report.warnings.append(f"{d} {sch.name}: {day_event.error}")
                    finally:
                        day_event.duration_ms = int(
                            (datetime.now() - started).total_seconds() * 1000
                        )
                    report.events.append(day_event)

                # 记录当日权益
                if sim_account is not None:
                    sim_equity_curve.append((d, sim_account.total_equity()))

            # ── 虚拟平仓: 结束时未平掉的持仓按 end_date close 全量 close ──
            # 让 total_trades / winning_trades / losing_trades 反映完整 round-trip;
            # 否则持仓未平仓时 PnL 只记在 unrealized_pnl, 不进 total_trades,
            # total_trades=0 + final_market_value > 0 看起来像"没交易".
            # 这是 verifier 增强, 不影响实盘 executor 的行为.
            if simulate_fills and sim_account is not None and trading_days:
                last_day = trading_days[-1]
                last_close_map = _historical_close_map(self._repo, last_day)
                for sym, qty in list(sim_account.holdings.items()):
                    if qty <= 0:
                        continue
                    close = last_close_map.get(sym, 0.0)
                    if close <= 0:
                        continue
                    f = sim_account.fill(sym, "sell", qty, close)
                    if f.qty > 0:
                        f.date = str(last_day)
                        sim_fills.append(f)

            # ── 聚合 ──
            report.summary = {
                "total_signals": total_signals,
                "total_events": len(report.events),
                "event_distribution": {
                    ev: sum(1 for e in report.events if e.event == ev)
                    for ev in {e.event for e in report.events}
                },
                "target_distribution": dict(
                    sorted(target_distribution.items(), key=lambda x: -x[1])[:20]
                ),
                "status_distribution": {
                    s: sum(1 for e in report.events if e.status == s)
                    for s in {"success", "no_target", "error"}
                },
                "errors_count": sum(1 for e in report.events if e.status == "error"),
                "no_target_count": sum(1 for e in report.events if e.status == "no_target"),
            }

            if simulate_fills and sim_account is not None:
                report.pnl = self._compute_simulated_pnl(
                    sim_account, sim_fills, sim_equity_curve, initial_cash
                )
                report.fills = list(sim_fills)
                report.equity_curve = [
                    {"date": str(d), "equity": eq}
                    for (d, eq) in sim_equity_curve
                ]

            report.finished_at = _now_iso()
            try:
                started_dt = datetime.fromisoformat(report.started_at)
                finished_dt = datetime.fromisoformat(report.finished_at)
                report.duration_ms = int((finished_dt - started_dt).total_seconds() * 1000)
            except Exception:
                pass
        return report

    # ── helpers ──────────────────────────────────
    def _resolve_schedules(
        self,
        strategy: LiveStrategy,
        events: list[str] | None,
    ) -> list[StrategySchedule]:
        if events is not None:
            sub = [s for s in (strategy.schedules or []) if s.name in events]
            if not sub:
                raise ValueError(f"events={events} 不在 strategy.schedules 里")
            return sub
        return list(strategy.schedules or [])

    def _build_context(
        self,
        target_date: _date,
        schedule: StrategySchedule,
        strategy: LiveStrategy,
        close_map: dict[str, float],
        inst_snap: dict[str, dict[str, Any]],
        sim_account: _SimAccount | None,
        initial_cash: float = 0.0,
    ) -> LiveStrategyContext:
        """构造一个 read-only 的 LiveStrategyContext, 数据全是历史快照."""
        # 持仓 (来自 sim_account 模拟) + account 视图
        if sim_account is not None:
            positions = dict(sim_account.holdings)
            account = {
                "cash": sim_account.cash,
                "initial_cash": sim_account.cash + sum(
                    self._cost_basis(qty, inst_snap, sym)
                    for sym, qty in sim_account.holdings.items()
                ),  # 粗略: 现金 + 持仓成本 (用于 max_position_pct)
                "market_value": sum(
                    qty * close_map.get(sym, 0.0) for sym, qty in sim_account.holdings.items()
                ),
                "total_equity": sim_account.total_equity(),
                "realized_pnl": sim_account.realized,
                "unrealized_pnl": 0.0,
                "total_pnl": sim_account.realized,
            }
        else:
            # 没用 sim_account (simulate_fills=False) 时, account 仍传 initial_cash
            # 让 my_Trader 能正确算 psize. 之前硬编码 0 导致 buy 永远返回 0.
            positions = {}
            account = {
                "cash": initial_cash,
                "initial_cash": initial_cash,
                "market_value": 0.0,
                "total_equity": initial_cash,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "total_pnl": 0.0,
            }

        from app.services.trading.data_provider import StrategyDataProvider
        dp = StrategyDataProvider(
            repo=self._repo,
            instruments=pl.DataFrame(list(inst_snap.values())) if inst_snap else pl.DataFrame(),
            minute_close=dict(close_map),
            paused={s: close_map.get(s, 0.0) <= 0 for s in close_map},
            limit_up={s: inst_snap.get(s, {}).get("limit_up", 0.0) for s in close_map},
            limit_down={s: inst_snap.get(s, {}).get("limit_down", 0.0) for s in close_map},
            name={s: inst_snap.get(s, {}).get("name", "") for s in close_map},
            total_shares={s: inst_snap.get(s, {}).get("total_shares", 0.0) for s in close_map},
            market_cap={s: inst_snap.get(s, {}).get("total_shares", 0.0) * close_map.get(s, 0.0) for s in close_map},
        )
        # 分红源: 默认走 StrategyDataProvider.dividends() 内部的 Tushare → CSV fallback 链.
        # 这里不主动注入 CSVDividendSource, 让数据源优先级符合最新策略 (Tushare 实时, CSV 仅作缓存兜底).

        return LiveStrategyContext(
            positions=positions,
            account=account,
            fills_recent=[],
            market=dict(close_map),
            trade_date=target_date,
            as_of=_now_iso(),
            event=schedule.name,
            state={},  # 不共享: 每次 on_event 用全新 state; 真持久化在多次 run 间不保
            data_provider=dp,
        )

    def _cost_basis(self, qty: int, inst_snap: dict[str, dict[str, Any]], sym: str) -> float:
        return 0.0  # 占位: simulator 内部已跟踪 cost, 这里不重复计算

    def _simulate_rebalance(
        self,
        sim: _SimAccount,
        target_date: _date,
        target: dict[str, int],
        close_map: dict[str, float],
        fills: list[SimulatedFill],
    ) -> None:
        """简化调仓: 每个 target symbol 按当日 close 全量成交 (不分批). 真实成交用当日均值近似.

        简化: 对每个目标 symbol, 直接 fill 到 target qty; 不做 T+1 校验 (回测验证
        偏向"策略能否跑通 + 调仓方向对不对", 不强制模拟撮合细节).
        """
        for sym, tgt_qty in target.items():
            # tgt_qty=0 是策略约定的 sell-all 信号 (对应原版 order_target(s, 0)).
            # 改用 < 0 而不是 <= 0 让 sell-all 能通过. < 0 永不出现 (策略不下负数单).
            tgt_qty = _stock_num_lots(sym, int(tgt_qty))
            cur_qty = sim.holdings.get(sym, 0)
            close = close_map.get(sym, 0.0)
            if close <= 0 or tgt_qty < 0:
                continue
            if tgt_qty == 0:
                if cur_qty <= 0:
                    continue  # 没持仓不卖
                f = sim.fill(sym, "sell", cur_qty, close)
            elif tgt_qty > cur_qty:
                f = sim.fill(sym, "buy", tgt_qty - cur_qty, close)
            elif tgt_qty < cur_qty:
                f = sim.fill(sym, "sell", cur_qty - tgt_qty, close)
            else:
                continue  # tgt_qty == cur_qty, 无变化
            if f.qty > 0:
                f.date = str(target_date)
                fills.append(f)

    def _compute_simulated_pnl(
        self,
        sim: _SimAccount,
        fills: list[SimulatedFill],
        equity_curve: list[tuple[_date, float]],
        initial_cash: float,
    ) -> SimulatedPnL:
        """聚合: 已实现盈亏 + 当前未实现 (按 last close) + 最大回撤 + 胜率."""
        final_cash = sim.cash
        final_mv = sum(
            sim.holdings.get(s, 0) * sim.last_prices.get(s, 0.0)
            for s in sim.holdings
        )
        final_equity = final_cash + final_mv

        realized = sim.realized
        unrealized = final_equity - final_cash - (initial_cash - realized)
        # 简化: unrealized = sum(qty * (close - cost)) — 用实际成本
        unrealized = 0.0
        for s, qty in sim.holdings.items():
            cost = sim.cost.get(s, 0.0)
            close = sim.last_prices.get(s, 0.0)
            unrealized += qty * (close - cost)

        total_pnl = realized + unrealized
        total_pnl_pct = total_pnl / initial_cash if initial_cash > 0 else 0.0

        # 最大回撤 (从 equity_curve)
        max_dd, max_dd_date = 0.0, None
        if equity_curve:
            peak = -math.inf
            for d, eq in equity_curve:
                if eq > peak:
                    peak = eq
                if peak > 0:
                    dd = (peak - eq) / peak
                    if dd > max_dd:
                        max_dd = dd
                        max_dd_date = str(d)

        # 胜率: 按 sell fills 的实现盈亏 (需要 cost_basis → 改用 FIFO 配对买入 fills)
        # 简化: 从 fills 按时间正序配对 buy/sell, 算每笔 realized
        buys_by_sym: dict[str, list[tuple[float, int]]] = {}
        trade_pnls: list[float] = []
        for f in fills:
            if f.side == "buy":
                buys_by_sym.setdefault(f.symbol, []).append((f.price, f.qty))
            else:
                remaining = f.qty
                entry_cost = 0.0
                while remaining > 0 and buys_by_sym.get(f.symbol):
                    bp, bq = buys_by_sym[f.symbol][0]
                    if bq <= remaining:
                        entry_cost += bp * bq
                        remaining -= bq
                        buys_by_sym[f.symbol].pop(0)
                    else:
                        entry_cost += bp * remaining
                        remaining = 0
                if f.qty > 0:
                    proceeds = f.price * f.qty
                    trade_pnls.append(proceeds - entry_cost)

        wins = sum(1 for p in trade_pnls if p > 0)
        losses = sum(1 for p in trade_pnls if p < 0)
        win_rate = wins / len(trade_pnls) if trade_pnls else 0.0

        return SimulatedPnL(
            initial_cash=initial_cash,
            final_cash=final_cash,
            final_market_value=final_mv,
            final_total_equity=final_equity,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            max_drawdown=max_dd,
            max_drawdown_date=max_dd_date,
            total_trades=len(trade_pnls),
            winning_trades=wins,
            losing_trades=losses,
            win_rate=win_rate,
            avg_pnl_per_trade=sum(trade_pnls) / len(trade_pnls) if trade_pnls else 0.0,
        )


# ── 报告持久化 (可选) ─────────────────────────────────
def save_verification(data_dir: Path, report: VerificationReport) -> Path:
    """保存到 data/user_data/trading/verifications/<strategy>/<run_id>.json."""
    out_dir = data_dir / "user_data" / "trading" / "verifications" / report.strategy_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # 落盘前补 sync-from-verify 用字段 (last_rebalance_target + committed_at + code_version)
    code_dir = data_dir / "user_data" / "trading"
    _populate_sync_fields(report, code_dir)
    path = out_dir / f"{report.run_id}.json"
    from app.services.fs_utils import atomic_write_text
    atomic_write_text(path, json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return path


def list_verifications(data_dir: Path, strategy_id: str) -> list[dict]:
    out: list[dict] = []
    d = data_dir / "user_data" / "trading" / "verifications" / strategy_id
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def load_verification(data_dir: Path, strategy_id: str, run_id: str) -> dict | None:
    """加载已落盘的 verify 报告 (供 sync-from-verify 用). 找不到返 None."""
    path = data_dir / "user_data" / "trading" / "verifications" / strategy_id / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _populate_sync_fields(report, code_dir: Path) -> None:
    """从 report.events[] 算 last_rebalance_target/last_rebalance_date/last_event_date;
    从 code_changelog.jsonl 读最新 ts 作 code_version; 写 committed_at."""
    from datetime import datetime, timedelta, timezone
    CN_TZ = timezone(timedelta(hours=8))
    rebalance_event = None
    last_event = None
    for e in reversed(report.events):
        if last_event is None:
            last_event = e
        if rebalance_event is None and e.event == "rebalance" and e.status == "success":
            rebalance_event = e
    if rebalance_event is not None:
        report.last_rebalance_target = dict(rebalance_event.target) if rebalance_event.target else {}
        report.last_rebalance_date = str(rebalance_event.date) if rebalance_event.date else ""
        report.target_symbols = sorted(report.last_rebalance_target.keys())
    if last_event is not None:
        report.last_event_date = str(last_event.date) if last_event.date else ""
    # code_version from code_changelog.jsonl
    changelog = code_dir / "code_changelog.jsonl"
    if changelog.exists():
        try:
            lines = [l for l in changelog.read_text(encoding="utf-8").splitlines() if l.strip()]
            if lines:
                last = json.loads(lines[-1])
                report.code_version = last.get("ts", "")
        except Exception:
            pass
    report.committed_at = datetime.now(CN_TZ).isoformat()


__all__ = [
    "DayEvent",
    "SimulatedFill",
    "SimulatedPnL",
    "StrategyVerifier",
    "VerificationReport",
    "list_verifications",
    "load_verification",
    "save_verification",
]
