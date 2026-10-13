"""StrategyExecutor — 把策略 on_event → TradingRunner.rebalance_to (含 state 持久化).

职责:
  1. 加载 strategy 持久化 state (state.json), 合并到 context.state.
  2. 构造 LiveStrategyContext (positions / account / recent fills / market snapshot / event / state).
  3. 调用 strategy.on_event(event, context) → dict[symbol, qty].
  4. 调用 TradingRunner.rebalance_to(target, prices=..., order_type=...) 走完整引擎.
  5. context.state 写回 state.json (跨重启 + 跨日 保留).
  6. 落一次 RunReport 到 strategy_runs.jsonl.
  7. 异常隔离: 策略抛异常 → 记 ERROR, 当前 run 失败, 不影响其他 run / 其他策略.

State 持久化:
  - data/user_data/trading/live_strategy_state/<strategy_id>.json
  - 仅保存 dict[str, Any]; 写满后由 fs_utils.atomic_write_text 原子替换.
  - state 在 on_event 调用前是只读快照, 调用后 executor 用 updated dict 全量覆写.
    注意: context 是 frozen dataclass, 策略必须 ctx.state[\"x\"] = y (mutate dict),
    或新建一个 dict 让 context = replace(ctx, state=new_dict). 后者更安全, 但 dict
    mutation 已足够 (Python 在 frozen dataclass 上对 dict 字段不做 deep freeze).
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.trading import preferences as prefs
from app.services.trading.live_strategy import (
    LiveStrategyContext,
    LiveStrategyRegistry,
)
from app.services.trading.pnl import PnLSnapshot, StrategyPnLSnapshot, append_snapshot
from app.services.trading.pnl import write_strategy_snapshot as write_strategy_snapshot_pnl
from app.services.trading.runner import TradingRunner

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(CN_TZ).isoformat()


# ── State 持久化 ─────────────────────────────────
def _state_path(data_dir: Path, strategy_id: str) -> Path:
    p = data_dir / "user_data" / "trading" / "live_strategy_state" / f"{strategy_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_state(data_dir: Path, strategy_id: str) -> dict[str, Any]:
    p = _state_path(data_dir, strategy_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("state 加载失败 %s: %s; 用空 state 继续", strategy_id, e)
        return {}


def save_state(data_dir: Path, strategy_id: str, state: dict[str, Any]) -> None:
    p = _state_path(data_dir, strategy_id)
    from app.services.fs_utils import atomic_write_text
    atomic_write_text(p, json.dumps(state, ensure_ascii=False, indent=2))


# ── Run 历史 ─────────────────────────────────────
@dataclass
class RunReport:
    strategy_id: str
    event: str
    started_at: str
    finished_at: str = ""
    duration_ms: int = 0
    trigger: str = "manual"   # 'manual' | 'cron' | 'api'
    status: str = "running"   # 'running' | 'success' | 'error' | 'no_target'
    target: dict[str, int] = field(default_factory=dict)
    universe: list[str] | None = None
    submitted: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    error: str = ""
    # 全栈 traceback (仅 status='error' 时填), 跟 error 字段一起落 strategy_runs.jsonl,
    # 便于幂等追溯潜伏的代码路径 bug, 不加 f"{type}: {msg}" 之外的额外 IO.
    traceback: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _runs_path(data_dir: Path) -> Path:
    p = data_dir / "user_data" / "trading" / "strategy_runs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def list_runs(data_dir: Path, *, strategy_id: str | None = None, limit: int | None = 50) -> list[dict]:
    items: list[dict] = []
    p = _runs_path(data_dir)
    if not p.exists():
        return items
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if strategy_id and rec.get("strategy_id") != strategy_id:
            continue
        items.append(rec)
    items.sort(key=lambda r: r.get("started_at", ""), reverse=False)
    if limit:
        items = items[-limit:]
    return items


def _append_run(data_dir: Path, report: RunReport) -> None:
    line = json.dumps(report.to_dict(), ensure_ascii=False)
    with _runs_path(data_dir).open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _parse_strategy_event_note(note):
    """从 order.note 解析 (strategy_id, event).

    支持两种格式 (covers 历史脏数据 + 当前写入路径):
      - Phase 4 后 (新):  `[<sid>] <event>`         e.g. "[dividend_auntie] rebalance"
      - Phase 4 前 (旧):  `strategy:<sid>:<event>`   e.g. "strategy:dividend_auntie:rebalance"

    Returns:
        (strategy_id, event) 或 (None, None) — 任一格式不匹配时.

    NOTE: 这是 BUG-A 的 fix helper; 主调用方在 StrategyExecutor.execute() 的
    跨策略 in-flight dedup 段. 同时供未来诊断工具使用 (不再侵入主路径).
    """
    if not note:
        return None, None
    import re as _parse_re
    # 新格式: [<sid>] <event>
    m = _parse_re.match(r"^\[([^\]]+)\] (\S+)$", note)
    if m:
        return m.group(1), m.group(2)
    # 旧格式 (历史兼容): strategy:<sid>:<event>
    m = _parse_re.match(r"^strategy:([^:]+):([^:]+)$", note)
    if m:
        return m.group(1), m.group(2)
    return None, None

# ── 执行器 ───────────────────────────────────────

# ── Module-level per-event lock (跨 StrategyExecutor 实例共享) ──────────
_EVENT_LOCKS: dict[str, threading.RLock] = {}
_EVENT_LOCKS_LOCK = threading.Lock()


def _event_locks_for(event: str) -> threading.RLock:
    """Module-level helper. 拿 "rebalance" 总是同一 RLock, 不论哪个 executor 实例."""
    with _EVENT_LOCKS_LOCK:
        lk = _EVENT_LOCKS.get(event)
        if lk is None:
            lk = threading.RLock()
            _EVENT_LOCKS[event] = lk
        return lk

class StrategyExecutor:
    """同步执行器; 按 (strategy_id, event) 加锁防并发 (避免同一策略同一事件重复跑)."""

    def __init__(self, registry: LiveStrategyRegistry, runner: TradingRunner) -> None:
        self._registry = registry
        self._runner = runner
        # Per-(strategy_id, event) lock: 防止同一策略被同一事件并发触发
        # (e.g. 调度器瞬时抖动 + 手动 run_now 同时进入).
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_lock = threading.Lock()
        # P1 优化 (2026-10-13): in_flight tracking — 同 (strategy_id, event) 已有 run 进行中时,
        # 立即返 in_flight 报告, 不进入 _build_context, 不抢锁.
        self._in_flight: dict[tuple[str, str], Any] = {}
        self._in_flight_lock = threading.RLock()

    def is_in_flight(self, strategy_id: str, event: str) -> Any | None:
        """P1: 不抢锁地查询同 strategy+event 是否已有 run 进行中.

        Returns:
            RunReport if in-flight, None otherwise.
        """
        with self._in_flight_lock:
            return self._in_flight.get((strategy_id, event))

    def _event_lock_for(self, event: str) -> threading.RLock:
        """per-event 全局 RLock (跨 StrategyExecutor 实例共享 — module 级).

        跨实例共享是关键: 两个 StrategyExecutor 实例在两台不同 worker / 两个
        apscheduler fork 同时跑同 event, 必须拿到同一个 RLock 才能序列化.
        (e.g. main.py 多 worker; 不同 cron 触发的 on_event 入口都在新 executor
        实例上; per-instance dict 形不成跨实例互斥.)

        BUG-C fix: 跨策略 + 同 event 的 dedup 需要 read+act 在同一锁内.
        per-event RLock 让 A 和 B 串行跑同一 event — B 启动时 A 的 pending 已
        落库, dedup 自然看到 A 的 in-flight 单并跳过.

        不同 event 可并行 (rebalance 不阻塞 pre_market).
        """
        return _event_locks_for(event)

    def _lock_for(self, strategy_id: str, event: str) -> threading.Lock:
        key = (strategy_id, event)
        with self._locks_lock:
            lk = self._locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._locks[key] = lk
            return lk

    def _make_in_flight_report(self, existing: RunReport) -> RunReport:
        """P1: 构造 in_flight 报告 — 模拟正在跑那份的当前快照.

        不修改原 report (它在另一个 run 的 finally 里会被清 in_flight + 写 _append_run).
        这里返的 in_flight 报告能告诉客户端"已经有 run 在跑, 你排队等".
        """
        now = datetime.now(CN_TZ)
        try:
            started = datetime.fromisoformat(existing.started_at)
        except Exception:
            started = now
        in_flight = RunReport(
            strategy_id=existing.strategy_id,
            event=existing.event,
            started_at=existing.started_at,
            trigger=f"in_flight:{existing.trigger}",
            status="in_flight",
        )
        in_flight.finished_at = ""
        in_flight.duration_ms = int((now - started).total_seconds() * 1000)
        in_flight.error = f"另一个 trigger={existing.trigger} 的 run 仍在执行中, 始于 {existing.started_at}"
        return in_flight

    def run_now(
        self,
        strategy_id: str,
        *,
        trigger: str = "manual",
        event: str | None = None,
        note: str | None = None,
    ) -> RunReport:
        strategy = self._registry.get(strategy_id)
        if strategy is None:
            raise ValueError(f"策略 {strategy_id} 未注册")

        # 事件路由: 没传 event 时, 取 schedule[0] 的 name (默认事件).
        schedules = getattr(strategy, "schedules", []) or []
        if event is None:
            if not schedules:
                raise ValueError(
                    f"策略 {strategy_id} 没有声明 schedules, 也没指定 event"
                )
            event = schedules[0].name if hasattr(schedules[0], "name") else schedules[0]["name"]

        report = RunReport(
            strategy_id=strategy_id,
            event=event,
            started_at=_now_iso(),
            trigger=trigger,
        )
        # 2026-10-13 (H2 fix): 阻塞等 quote cache 就绪之后再 _build_context.
        # 必须在拿锁前等 — 否则 build_data_provider() 已经把 market_cap frozen 了,
        # 此时再 wait 永远无法更新 dp 的 snapshot, 走原 bug 路径.
        from app.services.trading import engine as _engine_for_wait
        strat_state = load_state(self._runner.data_dir, strategy_id)
        _app_state = _engine_for_wait._APP_STATE
        repo = getattr(_app_state, "repo", None) if _app_state else None
        qs_for_wait = getattr(_app_state, "quote_service", None) if _app_state else None
        try:
            from app.services.trading.data_readiness import wait_for_quote_data
            _expected = (
                repo.get_instruments().height
                if repo is not None and hasattr(repo, "get_instruments")
                else 5882
            )
        except Exception:
            _expected = 5882
        # P1 优化 (2026-10-13): in_flight 早期检查 — 同 (strategy_id, event) 已有 run 在跑,
        # 立即返 in_flight 报告, 不抢 strat_lk/evt_lk, 不阻塞 60s 等待 quote cache.
        with self._in_flight_lock:
            existing = self._in_flight.get((strategy_id, event))
            if existing is not None:
                logger.info(
                    "[%s] event=%s in_flight early-return (started_at=%s, dur_so_far=%dms)",
                    strategy_id, event, existing.started_at,
                    int((datetime.now(CN_TZ) - datetime.fromisoformat(existing.started_at)).total_seconds() * 1000),
                )
                return self._make_in_flight_report(existing)
        # 标记 in_flight (正常路径)
        with self._in_flight_lock:
            self._in_flight[(strategy_id, event)] = report

        try:
            wait_for_quote_data(
                quote_service=qs_for_wait,
                min_coverage=0.80,
                max_wait_s=60.0,
                poll_s=2.0,
                expected_count=_expected,
                event=event,
            )
        finally:
            pass  # try-finally 占位: 真正常路径不会走 finally 异常处理 (P1 清理已在 with 块尾部)

        # 加载 state (同步; 已经走过 wait, _build_context 接着会立刻就绪)
        strat_lk = self._lock_for(strategy_id, event)
        # BUG-C: 跨策略同 event 串行化 (per-event 全局锁覆盖所有 strategy 的同一 event).
        # 同 strategy 同 event 由 strat_lk 保护; cross-strategy by evt_lk.
        evt_lk = self._event_lock_for(event)
        with strat_lk, evt_lk:
            # 加载 state
            state = strat_state
            try:
                ctx = self._build_context(event, state)
                target = strategy.on_event(event, ctx)
                if not isinstance(target, dict):
                    raise TypeError(
                        f"策略 {strategy_id}.on_event({event}) 必须返回 dict[str, int]; "
                        f"实际 {type(target).__name__}"
                    )
                target = {str(k): int(v) for k, v in target.items()}
                report.target = target

                if not target:
                    # 空 target = 不调仓 (用于 'pre_market' / 'limit_check' 等准备/检查事件)
                    report.status = "no_target"
                    logger.info("[%s] event=%s 返回空 target, 不调仓", strategy_id, event)
                else:
                    # universe 过滤 (可选)
                    universe = None
                    if hasattr(strategy, "symbols_universe"):
                        try:
                            u = strategy.symbols_universe(ctx)
                            if u is not None:
                                universe = list(u)
                                report.universe = universe
                                target = self._filter_to_universe(target, universe, current_qty=ctx.positions)
                        except Exception as e:
                            logger.warning("策略 %s.symbols_universe 异常: %s", strategy_id, e)

                    # 把 target 按"有最新价 / 无最新价"拆开:
                    #   - 有价: LIMIT, 走 rebalance_to 的限价撮合, 与原版策略语义一致.
                    #   - 无价 (quote_service 还没拉到的小盘 / 冷启动): 单独走 MARKET 单,
                    #     不让 broker 因为 limit_price=None 而拒单 (paper_broker 会 reject "限价单必须填写价格").
                    # 原版的隐性 bug: 有价 + 无价一起喂给 rebalance_to(prices={...}), 无价
                    # 的 symbol 拿到 limit_price=None → broker 拒 → 策略侧表现为"莫名其妙某只票没成交".
                    limit_target: dict[str, int] = {}
                    limit_prices: dict[str, float] = {}
                    market_target: dict[str, int] = {}
                    # 拆单口径: 价格存在且 > 0 才走限价 (否则走市价).
                    # 修复前: ctx.market[sym]=0.0 时 "sym in ctx.market" 仍 True,
                    # 0 价进 limit_target → runner 传 limit_price=0 → engine.py:142 拒 "无法获取标的最新价"
                    # 或 paper_broker.py:82 拒 "限价单必须填写价格"。
                    for sym, qty in target.items():
                        px = ctx.market.get(sym)
                        if px is not None and px > 0:
                            limit_target[sym] = qty
                            limit_prices[sym] = px
                        else:
                            market_target[sym] = qty
                    if market_target:
                        logger.warning(
                            "策略 %s event=%s: %d 只 symbol 无最新价, 改市价单: %s",
                            strategy_id, event, len(market_target), sorted(market_target),
                        )

                    # ── 跨策略 in-flight 去重 ──
                    # 同 event (e.g. 两个 strategy 的 rebalance 同时 9:35 cron) 在 1ms 内
                    # 触发的重复委托 (同 symbol + 同方向) 会被 broker 接受两份, 重复扣钱
                    # / 重复建仓。读 store 里 60s 内的 NEW/PARTIAL 委托, 解析 note
                    # 找到其他策略同 event 的同 symbol 委托, 在本次 target 里剔除。
                    DEDUP_WINDOW_SEC = 60
                    try:
                        from app.services.trading import store as _store
                        from app.services.trading.models import OrderStatus as _OS
                        recent = _store.list_orders(
                            self._runner.data_dir,
                            statuses=(_OS.NEW, _OS.PARTIAL),
                            limit=200,
                        )
                        now_dt = datetime.now(timezone(timedelta(hours=8)))
                        inflight: set[tuple[str, str]] = set()
                        cur_qty_map = {p.symbol: p.qty for p in self._runner.positions()}
                        for o in recent:
                            try:
                                ts = datetime.fromisoformat(o.created_at)
                                if ts.tzinfo is None:
                                    ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
                                if (now_dt - ts).total_seconds() > DEDUP_WINDOW_SEC:
                                    continue
                            except Exception:
                                continue
                            # BUG-A fix: 兼容 Phase 4 前的 legacy `strategy:<sid>:<event>` 格式
                            #         + Phase 4 后的新格式 `[<sid>] <event>`.
                            note_sid, note_event = _parse_strategy_event_note(o.note)
                            if note_sid is None or note_event is None:
                                continue
                            if note_sid == strategy_id or note_event != event:
                                continue
                            inflight.add((o.symbol, o.side.value if hasattr(o.side, "value") else str(o.side)))
                        if inflight:
                            new_lt = {}; new_lp = {}; new_mt = {}
                            skipped: list[str] = []
                            for sym, qty in limit_target.items():
                                side = "buy" if qty > cur_qty_map.get(sym, 0) else "sell"
                                if (sym, side) in inflight:
                                    skipped.append(f"{sym}/{side}")
                                else:
                                    new_lt[sym] = qty
                                    new_lp[sym] = limit_prices[sym]
                            for sym, qty in market_target.items():
                                side = "buy" if qty > cur_qty_map.get(sym, 0) else "sell"
                                if (sym, side) in inflight:
                                    skipped.append(f"{sym}/{side}")
                                else:
                                    new_mt[sym] = qty
                            if skipped:
                                logger.warning(
                                    "策略 %s event=%s: %d 只 symbol 被跨策略去重跳过 (其他策略同 event 在 %ds 内已 in-flight): %s",
                                    strategy_id, event, len(skipped), DEDUP_WINDOW_SEC, skipped,
                                )
                                limit_target = new_lt; limit_prices = new_lp; market_target = new_mt
                    except Exception as e:
                        logger.warning("跨策略 in-flight 去重失败 (非致命): %s", e)

                    submitted: list = []
                    # D3 (live-trading-strategy-isolation): note 不再用 "strategy:<sid>:<event>"
                    # 字符串前缀格式. 第一性字段 (Order.strategy_id) 才是 per-strategy 过滤的真值源;
                    # note 改为自然语言标识 (D2 老 note 保留不动 — 仅当手动或脚本显式传 strategy: 前缀时仍允许).
                    strategy_note_default = f"[{strategy_id}] {event}"
                    if limit_target:
                        submitted.extend(self._runner.rebalance_to(
                            limit_target,
                            prices=limit_prices,
                            order_type="limit",
                            note=note or strategy_note_default,
                            max_order_amount=prefs.max_order_amount_for_strategy(),
                            strategy_id=strategy_id,
                        ))
                    if market_target:
                        submitted.extend(self._runner.rebalance_to(
                            market_target,
                            prices=None,
                            order_type="market",
                            note=note or strategy_note_default,
                            max_order_amount=prefs.max_order_amount_for_strategy(),
                            strategy_id=strategy_id,
                        ))
                    report.submitted = [o.id for o in submitted]
                    report.status = "success"

                # state 落盘 (策略可能修改了 ctx.state)
                save_state(self._runner.data_dir, strategy_id, ctx.state)
            except Exception as e:
                logger.exception("策略执行失败: %s event=%s", strategy_id, event)
                report.status = "error"
                # 全栈 traceback 写到 strategy_runs.jsonl, 便于下次再爆 prefs NameError
                # 等潜伏 bug 时有人能定位 — 现场 task 2026-09-29 11:51:41 prefs NameError
                # 因只存了 "{type}: {msg}" 没栈, 根因排查单靠数据文件无法继续.
                report.error = f"{type(e).__name__}: {e}"
                report.traceback = traceback.format_exc()
                # state 即使失败也保存 (策略可能改了部分字段)
                with contextlib.suppress(Exception):
                    save_state(self._runner.data_dir, strategy_id, ctx.state)
            finally:
                report.finished_at = _now_iso()
                try:
                    started = datetime.fromisoformat(report.started_at)
                    finished = datetime.fromisoformat(report.finished_at)
                    report.duration_ms = int((finished - started).total_seconds() * 1000)
                except Exception:
                    report.duration_ms = 0
                _append_run(self._runner.data_dir, report)
                self._snapshot_after_run(strategy_id, event)
                # P1: 清除 in_flight 标记 (成功/异常路径都执行)
                with self._in_flight_lock:
                    self._in_flight.pop((strategy_id, event), None)
        return report

    def run_with_target(
        self,
        strategy_id: str,
        *,
        event: str,
        override_target: dict[str, int],
        trigger: str = "sync-from-verify",
        note: str | None = None,
    ) -> RunReport:
        """跳过 strategy.on_event, 直接拿 override_target 走完整 rebalance 流程 (2026-10-13).

        sync-from-verify 用 — verify 报告里已有 target, 跑 on_event 既浪费又会因 ctx.state / 实时价
        差异产生 drift. 走 strat_lk + evt_lk + cross-strategy dedup 锁, 但跳过 on_event.
        """
        from datetime import datetime, timedelta, timezone
        CN_TZ_LOCAL = timezone(timedelta(hours=8))
        strategy = self._registry.get(strategy_id)
        if strategy is None:
            raise ValueError(f"策略 {strategy_id} 未注册")
        if not override_target:
            raise ValueError("override_target 不能为空 (sync-from-verify 拒绝空 target)")

        report = RunReport(
            strategy_id=strategy_id, event=event, started_at=_now_iso(), trigger=trigger,
        )

        # in_flight 早期检查 (与 run_now 一致)
        with self._in_flight_lock:
            existing = self._in_flight.get((strategy_id, event))
            if existing is not None:
                logger.info(
                    "[%s] event=%s run_with_target in_flight early-return", strategy_id, event,
                )
                return self._make_in_flight_report(existing)
        with self._in_flight_lock:
            self._in_flight[(strategy_id, event)] = report

        try:
            strat_lk = self._lock_for(strategy_id, event)
            evt_lk = self._event_lock_for(event)
            with strat_lk, evt_lk:
                state = load_state(self._runner.data_dir, strategy_id)
                # 构造 minimal ctx (data_provider 不需要 on_event)
                from app.services.trading.live_strategy import LiveStrategyContext
                positions_dict = {p.symbol: p.qty for p in self._runner.positions()}
                account_dict = self._runner.account().model_dump(mode="json")
                market: dict[str, float] = {}
                from app.services.trading import engine as engine_mod
                _app_state = engine_mod._APP_STATE
                qs = getattr(_app_state, "quote_service", None) if _app_state else None
                if qs is not None:
                    try:
                        df, _ = qs.get_enriched_today()
                        if df is not None and not df.is_empty() and "symbol" in df.columns and "close" in df.columns:
                            for row in df.iter_rows(named=True):
                                sym = row.get("symbol")
                                cl = row.get("close")
                                if sym and cl is not None and cl > 0:
                                    market[sym] = float(cl)
                    except Exception:
                        pass
                _ = LiveStrategyContext(
                    positions=positions_dict, account=account_dict,
                    fills_recent=[], market=market,
                    trade_date=datetime.now(CN_TZ_LOCAL).date(),
                    as_of=_now_iso(), event=event, state=state, data_provider=None,
                )
                target = {str(k): int(v) for k, v in override_target.items()}
                report.target = target
                if not target:
                    report.status = "no_target"
                    logger.info("[%s] run_with_target: 空 target, 不调仓", strategy_id)
                else:
                    note_final = note or f"sync-from-verify:trigger={trigger}"
                    try:
                        self._runner.rebalance_to(
                            target, prices=market or None,
                            order_type="limit", note=note_final,
                        )
                        report.status = "success"
                    except Exception as e:
                        logger.exception("rebalance_to failed: %s", e)
                        report.status = "error"
                        report.error = f"{type(e).__name__}: {e}"
                        report.traceback = traceback.format_exc()
                save_state(self._runner.data_dir, strategy_id, state)
        finally:
            with self._in_flight_lock:
                self._in_flight.pop((strategy_id, event), None)

        # 写 run report + snapshot (与 run_now 收尾一致)
        try:
            started = datetime.fromisoformat(report.started_at)
            report.finished_at = _now_iso()
            finished = datetime.fromisoformat(report.finished_at)
            report.duration_ms = int((finished - started).total_seconds() * 1000)
        except Exception:
            report.duration_ms = 0
        _append_run(self._runner.data_dir, report)
        self._snapshot_after_run(strategy_id, event)
        return report

    # ── helpers ──────────────────────────────────
    def _build_context(self, event: str, state: dict[str, Any]) -> LiveStrategyContext:
        positions_dict: dict[str, int] = {p.symbol: p.qty for p in self._runner.positions()}
        account_dict: dict[str, float] = self._runner.account().model_dump(mode="json")
        fills = self._runner.list_fills(limit=50)
        fills_recent = [f.model_dump(mode="json") for f in reversed(fills)]

        market: dict[str, float] = {}
        from app.services.trading import engine as engine_mod
        _app_state = engine_mod._APP_STATE
        qs = getattr(_app_state, "quote_service", None) if _app_state else None
        if qs is not None:
            try:
                df = qs.get_quotes_compat()
                if not df.is_empty():
                    for row in df.iter_rows(named=True):
                        sym = row.get("symbol")
                        lp = row.get("last_price") or row.get("close")
                        if sym and lp is not None:
                            market[sym] = float(lp)
            except Exception as e:
                logger.warning("构造策略上下文: 读 quote_service 失败: %s", e)

        from datetime import date as _date

        from app.services.trading.data_provider import build_data_provider

        repo = getattr(engine_mod._APP_STATE, "repo", None) if engine_mod._APP_STATE else None
        data_provider = build_data_provider(repo, qs) if repo is not None else None

        # 注入 send_message: 聚宽式通知接口, 落 notify.jsonl + SSE 推
        def send_message(text: str, kind: str = "user_message", extra: dict | None = None) -> None:
            try:
                from app.services.trading.notify_store import append as _notify
                _notify(self._runner.data_dir, kind=kind, text=text,
                        source=f"strategy:{event}", extra=extra or {})
            except Exception as e:
                logger.warning("send_message failed: %s", e)

        return LiveStrategyContext(
            positions=positions_dict,
            account=account_dict,
            fills_recent=fills_recent,
            market=market,
            trade_date=_date.today(),
            as_of=_now_iso(),
            event=event,
            state=state,
            data_provider=data_provider,
            send_message=send_message,
        )

    def _filter_to_universe(
        self,
        target: dict[str, int],
        universe: list[str],
        *,
        current_qty: dict[str, int],
    ) -> dict[str, int]:
        uni = set(universe)
        filtered = {sym: qty for sym, qty in target.items() if sym in uni}
        for sym, qty in current_qty.items():
            if sym not in uni and qty > 0 and sym not in filtered:
                filtered[sym] = 0
        return filtered

    def _snapshot_after_run(self, strategy_id: str, event: str) -> None:
        try:
            account = self._runner.account()
            snapshot = PnLSnapshot(
                ts=_now_iso(),
                trade_date=datetime.now(CN_TZ).strftime("%Y-%m-%d"),
                total_equity=account.total_equity,
                cash=account.cash,
                market_value=account.market_value,
                realized_pnl=account.realized_pnl,
                unrealized_pnl=account.unrealized_pnl,
                total_pnl=account.total_pnl,
                trigger=f"strategy:{strategy_id}:{event}",
            )
            append_snapshot(self._runner.data_dir, snapshot)
        except Exception as e:
            logger.warning("PnL 快照失败: %s", e)

        # ── per-strategy 闭环 (PR-1 §5.1 / B2=A) ─────────────────────────
        # 同一轮 run 后, 写一条 pnl_strategy_snapshots.jsonl, 字段含 strategy_id,
        # 前端 compute_strategy_summary 据此派生 7 字段 summary.
        # cash_delta 用本次的 cash 变动 (粗略, 后续 fill attribution 可加 per-strategy realized PnL)
        # error 路径也写一条, status 体现.
        try:
            strat_snap = StrategyPnLSnapshot(
                ts=_now_iso(),
                strategy_id=strategy_id,
                event=event,
                total_pnl=account.total_pnl,
                cash_delta=account.cash - getattr(self, "_last_cash_per_strategy", {}).get(strategy_id, account.cash),
                filled_qty_by_symbol={o.symbol: o.filled_qty for o in (self._runner.list_fills(limit=10) or []) if hasattr(o, "filled_qty")},
            )
            # 防 自引用: 只在第一次见到 strategy 时记 cash baseline
            prev = getattr(self, "_last_cash_per_strategy", None)
            if prev is None:
                self._last_cash_per_strategy = {}
                prev = self._last_cash_per_strategy
            # 把 baseline 维持 = 上一次的 cash (在写入后更新)
            write_strategy_snapshot_pnl(self._runner.data_dir, strat_snap)
            prev[strategy_id] = account.cash
        except Exception as e:
            logger.warning("per-strategy 快照失败 (%s/%s): %s", strategy_id, event, e)


__all__ = [
    "RunReport",
    "StrategyExecutor",
    "list_runs",
    "load_state",
    "save_state",
]
