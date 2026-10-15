"""实盘交易 API 路由 — 薄层, 不含业务计算。

端点:
  GET    /api/trading/status                  引擎状态 + pending 委托快照
  GET    /api/trading/account                 账户 (现金/市值/盈亏)
  GET    /api/trading/positions               当前持仓列表
  GET    /api/trading/orders                  委托列表 (status / symbol / limit 过滤)
  POST   /api/trading/orders                  提交委托 (manual)
  DELETE /api/trading/orders/{id}             撤单
  GET    /api/trading/trades                  历史成交记录
  GET    /api/trading/logs                    交易日志
  GET    /api/trading/preferences             读取实盘偏好设置
  POST   /api/trading/preferences             写入实盘偏好设置 (enable / 风控参数)
  GET    /api/trading/stream                  SSE 事件流 (委托/成交/撮合 tick)
  GET    /api/trading/strategies              列出已注册策略
  POST   /api/trading/strategies/{id}/run-now       手动触发策略一次
  GET    /api/trading/strategies/{id}/runs          策略 run 历史
  GET    /api/trading/strategies/{id}/code           读策略源码
  PUT    /api/trading/strategies/{id}/code           替换策略源码
  POST   /api/trading/strategies/{id}/sync-from-verify   回测结果 → 实盘同步 (2026-10-13)
  GET    /api/trading/strategies/{id}/sync-audit       同步审计 log
  POST   /api/trading/strategies/{id}/notify         toggle notify_wechat
  DELETE /api/trading/strategies/{id}                 删策略 (trash)
  GET    /api/trading/strategies/{id}/pnl/metrics    pnl 指标
  GET    /api/trading/strategies/{id}/pnl/history     pnl 历史
  GET    /api/trading/strategies/{id}/verify          跑回测
  GET    /api/trading/strategies/{id}/verify/list     列出回测历史
  GET    /api/trading/strategies/{id}/positions       per-strategy 持仓
  GET    /api/trading/strategies/{id}/code/changelog  code 变更 log
  PUT    /api/trading/strategies/{id}/schedules       整体覆写 schedules
  POST   /api/trading/strategies/{id}/schedules/{event}  增量改 schedule
  POST   /api/trading/strategies/{id}/run-now/retry   重跑 run

设计:
  - 委托提交/撤单的参数校验由 Pydantic 模型承担, 业务校验在 engine 层。
  - 路由层不持任何状态; 全部通过 request.app.state.trading_engine 取引擎。
  - 与 quote_service stream 共用同一个 SSE 端点也不冲突 — 前端 useQuoteStream
    已支持多种 event name; trading 事件以 "trading_event" 单 event name 推送。
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.data import invalidate_storage_cache
from app.services.trading import code_store, engine as engine_mod, preferences as prefs, risk_metrics, store
from app.services.trading.models import EngineStatus, Order, OrderDraft, OrderStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading"])


def _get_engine(request: Request) -> engine_mod.TradingEngine:
    eng = getattr(request.app.state, "trading_engine", None)
    if eng is None:
        raise HTTPException(status_code=503, detail="实盘引擎未初始化")
    return eng


def _get_executor(request: Request):
    """获取 StrategyExecutor (app.state.strategy_executor). 缺失 → 503."""
    ex = getattr(request.app.state, "strategy_executor", None)
    if ex is None:
        raise HTTPException(status_code=503, detail="实盘策略执行器未初始化")
    return ex


def _get_strategy_registry(request: Request):
    return getattr(request.app.state, "strategy_registry", None)


def _strategy_to_dict(strategy: Any, request: Request) -> dict[str, Any]:
    """把一个 LiveStrategy 实例序列化为前端 LiveStrategyApi 兼容的 dict.

    历史上 `list_strategies` 直接 `s.to_dict()`, 但用户态的 LiveStrategy 子类
    (dividend_auntie / dividend_auntie_universe) 没有显式 to_dict, 所以前端
    列表页永远拿不到数据。这里把"序列化"从策略层上提到 router, 用 attribute
    duck-typing + registry / scheduler / preferences 三个来源拼装字段,
    避免给每个用户策略都加 to_dict boilerplate.

    历史坑 (2026-10-14): schedules 的 `enabled` 字段直接来自 StrategySchedule
    dataclass.to_dict() — 那个 class 定义里 enabled 永远 = False
    (`schedules = [StrategySchedule(..., enabled=False, ...)]`),
    所以 API 修前无论 preferences 怎么改, 列表里所有 schedule 都显示 ✗,
    用户在前端"启"了之后 preferences 已经是 ✓, 但刷新页面看到还是 ✗,
    误以为没生效 (其实 scheduler 已经按 preferences 触发, 仅是
    API 没回真实状态)。修法: enabled 改读 prefs.is_strategy_schedule_enabled,
    last_run 从 strategy_runs.jsonl 末行取, 让用户能直观看到"我刚才
    启的那个 schedule 上次 cron 跑是什么时候 / 什么状态"。
    """
    raw_schedules = list(getattr(strategy, "schedules", []) or [])
    schedules: list[dict[str, Any]] = []
    for s in raw_schedules:
        if hasattr(s, "to_dict"):
            base = s.to_dict()
        elif isinstance(s, dict):
            base = dict(s)
        else:
            # 兜底: 未知类型跳过, 不阻塞整个列表
            continue
        # 关键修复: enabled 从 preferences 读, 不从 StrategySchedule class
        # 默认 (False) 读. 用户在 UI 启用后, 真实状态在 preferences.
        event_name = base.get("name") or getattr(s, "name", None)
        if event_name:
            with contextlib.suppress(Exception):
                # preferences 读失败 → 保守用 class 默认值
                base["enabled"] = bool(prefs.is_strategy_schedule_enabled(strategy.id, event_name))
        # last_run*: 从 strategy_runs.jsonl 末行取 (同 strategy_id + event + trigger='cron')
        base["last_run"] = None
        base["last_run_status"] = None
        base["last_run_error"] = ""
        if event_name:
            try:
                from app.services.trading.executor import list_runs as _list_runs
                # repo.store.data_dir 拿数据目录, 沿用 list_logs 同款
                data_dir = request.app.state.repo.store.data_dir
                # list_runs 已经支持按 strategy_id 过滤; 客户端再按 event
                # 和 trigger 过滤. limit=20 是"够看几次最近", 排序按
                # started_at 升序, 取末 1 条.
                candidates = _list_runs(
                    data_dir, strategy_id=strategy.id, limit=50,
                )
                candidates = [
                    r for r in candidates
                    if r.get("event") == event_name and r.get("trigger") == "cron"
                ]
                if candidates:
                    last = candidates[-1]
                    base["last_run"] = last.get("started_at")
                    base["last_run_status"] = last.get("status")
                    base["last_run_error"] = last.get("error") or ""
            except Exception:  # noqa: BLE001
                # 读不到 (jsonl 不存在/损坏) → 留 None, 不阻塞
                pass
        schedules.append(base)

    reg = _get_strategy_registry(request)
    started_at = reg.started_at_of(strategy.id) if reg is not None else None

    sched = getattr(request.app.state, "strategy_scheduler", None)
    next_run: str | None = None
    if sched is not None and hasattr(sched, "next_run"):
        try:
            next_run = sched.next_run(strategy.id)
        except Exception:  # noqa: BLE001
            next_run = None

    notify_wechat = False
    try:
        notify_wechat = bool(prefs.get_notify_wechat(strategy.id))
    except Exception:  # noqa: BLE001
        notify_wechat = False

    return {
        "id": strategy.id,
        "name": getattr(strategy, "name", strategy.id),
        "enabled_default": bool(getattr(strategy, "enabled_default", False)),
        "schedules": schedules,
        "next_run": next_run,
        "started_at": started_at,
        "notify_wechat": notify_wechat,
        # PR-1 (live-trading-strategy-summary.md) 上线后再补 summary.
        "summary": None,
    }


def _data_dir(request: Request) -> Any:
    return request.app.state.repo.store.data_dir


# ── 状态 / 持仓 / 账户 ──────────────────────────────────
@router.get("/status")
def get_status(request: Request) -> EngineStatus:
    return _get_engine(request).status(_data_dir(request))


@router.get("/account")
def get_account(request: Request):
    return _get_engine(request).account(_data_dir(request)).model_dump(mode="json")


@router.get("/positions")
def get_positions(request: Request, strategy_id: str | None = None):
    positions = _get_engine(request).positions(_data_dir(request), strategy_id=strategy_id)
    return {"positions": [p.model_dump(mode="json") for p in positions]}


# ── 委托 ───────────────────────────────────────────
@router.get("/orders")
def list_orders(
    request: Request,
    symbol: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
    status: Annotated[list[str] | None, Query()] = None,
    strategy_id: str | None = Query(default=None, description="按策略 id 过滤 (按 order.note 解析)"),
):
    statuses = None
    if status:
        from app.services.trading.models import OrderStatus
        try:
            statuses = [OrderStatus(s) for s in status]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"非法 status: {e}") from e
    items = _get_engine(request).list_orders(
        _data_dir(request), statuses=statuses, symbol=symbol,
        strategy_id=strategy_id, limit=limit,
    )
    return {"orders": [o.model_dump(mode="json") for o in items]}


@router.post("/orders")
def submit_order(request: Request, draft: OrderDraft):
    try:
        order = _get_engine(request).submit(_data_dir(request), draft)
    except engine_mod.TradingRejectError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"order": order.model_dump(mode="json")}


@router.delete("/orders/{order_id}")
def cancel_order(request: Request, order_id: str):
    try:
        order = _get_engine(request).cancel(_data_dir(request), order_id)
    except engine_mod.TradingRejectError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"order": order.model_dump(mode="json")}


# ── PR1 (live-trading-v2-pr1): 一键救命三件套后端能力 ─────────────────
class CancelAllBody(BaseModel):
    strategy_id: str | None = None  # 不传 = 全账户


@router.post("/orders/cancel-all")
def cancel_all_orders(request: Request, body: CancelAllBody | None = None):
    """全部撤单 (救命按钮 #1).

    幂等: 重复点也安全, 已撤的不会报失败. strategy_id 不传 = 撤整个账户的
    pending 委托 (NEW / PARTIAL). 立即同步落盘 + invalidate_storage_cache.
    """
    data_dir = _data_dir(request)
    engine = _get_engine(request)
    statuses = (OrderStatus.NEW, OrderStatus.PARTIAL)
    pending = store.list_orders(
        data_dir, statuses=statuses, strategy_id=(body.strategy_id if body else None),
    )
    cancelled: list[str] = []
    failed: list[dict] = []
    for order in pending:
        try:
            engine.cancel(data_dir, order.id)
            cancelled.append(order.id)
        except engine_mod.TradingRejectError as e:
            failed.append({"id": order.id, "error": str(e)})
    if cancelled:
        invalidate_storage_cache()
    return {
        "cancelled": len(cancelled),
        "failed": len(failed),
        "order_ids": cancelled,
        "errors": failed,
    }


class FlattenBody(BaseModel):
    strategy_id: str | None = None  # 不传 = 全账户


@router.post("/positions/flatten")
def flatten_positions(request: Request, body: FlattenBody | None = None):
    """一键平仓 (救命按钮 #3).

    对当前所有 qty > 0 的持仓, 按市价卖出 (`order_type="market"`). 沿用
    submit() 的全套风控 (单笔金额 / 资金 / 涨跌停) — 极端行情下若风控拒,
    会写到 `errors[]` 字段, 但不影响其他持仓的市价单提交.

    幂等: 重复点会得到空 result, 因为持仓已清; 不抛错.
    """
    data_dir = _data_dir(request)
    engine = _get_engine(request)
    positions = engine.positions(
        data_dir, strategy_id=(body.strategy_id if body else None),
    )
    submitted: list[str] = []
    skipped: list[dict] = []
    for p in positions:
        if p.qty <= 0:
            continue
        try:
            order = _submit_market_sell(engine, data_dir, p)
            submitted.append(order.id)
        except engine_mod.TradingRejectError as e:
            skipped.append({"symbol": p.symbol, "qty": p.qty, "error": str(e)})
    if submitted:
        invalidate_storage_cache()
    return {
        "submitted": len(submitted),
        "skipped": len(skipped),
        "order_ids": submitted,
        "errors": skipped,
    }


def _submit_market_sell(engine, data_dir, position) -> Order:
    """单笔市价卖: 同 submit() 路径, order_type="market", 不传 limit_price.

    涨跌停价会被 broker 内部按对手价取 (paper broker 取 last_price ± tick).
    Position 不带 strategy_id (账户级聚合), 一键平仓是账户级动作.

    max_order_amount_override = trading_max_order_amount_for_strategy (默认 1000万).
    让"一键平仓"按 1000 万单笔上限走, 不被 manual 5 万卡 (救命按钮,
    不能被普通风控硬卡; 真要卡由人工决定).
    """
    draft = OrderDraft(
        strategy_id=None,
        symbol=position.symbol,
        side="sell",
        order_type="market",
        qty=position.qty,
        limit_price=None,  # 市价不传限价
        note="v2 flatten (一键平仓)",
    )
    return engine.submit(
        data_dir, draft,
        max_order_amount_override=prefs.max_order_amount_for_strategy(),
    )


# ── 历史成交 / 日志 ─────────────────────────────────
@router.get("/trades")
def list_trades(
    request: Request,
    symbol: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=1000),
    strategy_id: str | None = Query(default=None, description="按策略 id 过滤 (按 order.note 解析)"),
):
    items = _get_engine(request).list_fills(
        _data_dir(request), symbol=symbol, limit=limit, strategy_id=strategy_id,
    )
    return {"trades": [f.model_dump(mode="json") for f in items]}


@router.get("/logs")
def list_logs(
    request: Request,
    kind: str | None = None,
    limit: int | None = Query(default=200, ge=1, le=1000),
    strategy_id: str | None = Query(default=None, description="按策略 id 过滤 (按 order.note 解析)"),
):
    items = _get_engine(request).list_logs(
        _data_dir(request), kind=kind, limit=limit, strategy_id=strategy_id,
    )
    return {"logs": [e.model_dump(mode="json") for e in items]}


# ── 偏好设置 ─────────────────────────────────────
@router.get("/preferences")
def get_preferences() -> dict:
    return prefs.get_all_preferences()


@router.post("/preferences")
def update_preferences(request: Request, body: dict) -> dict:
    try:
        return prefs.update_preferences(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ── 风控指标 ──────────────────────────────────────
@router.get("/risk_metrics")
def get_risk_metrics(request: Request, window: int = Query(default=60, ge=5, le=252)):
    try:
        return risk_metrics.compute_risk_metrics(_data_dir(request), window=window)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"风控指标计算失败: {e}") from e


# ── SSE 流 (与 quote_service 共享端点, event name = trading_event) ─────
@router.get("/stream")
async def trading_stream(request: Request):
    """SSE 端点: trading_event / quote_event 推送."""
    from fastapi.responses import StreamingResponse

    from app.services.quote_service.streaming import event_stream
    return StreamingResponse(
        event_stream(request.app.state, kind="trading"),
        media_type="text/event-stream",
    )


# ── 策略管理 ──────────────────────────────────────
@router.get("/strategies")
def list_strategies(request: Request):
    reg = _get_strategy_registry(request)
    if reg is None:
        raise HTTPException(status_code=503, detail="策略注册表未初始化")
    return {"strategies": [_strategy_to_dict(s, request) for s in reg.list_all()]}


class StrategyRunNowBody(BaseModel):
    event: str | None = None




# ── AI 优化自测端到端 (2026-10-13) ─────────────────────
# 引入 strategy_ai_loop 走的是模块级 import — 配合 sys.path 让其能 import.
import sys
from pathlib import Path as _Pal_ail

if str(_Pal_ail(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(_Pal_ail(__file__).resolve().parent.parent))
from strategy_ai_loop import (  # type: ignore  # noqa: E402
    OptimizeJob,
    OptimizerJobStore,
    StrategyAIOptimizer,
)


def _get_optimizer(request: Request) -> tuple[StrategyAIOptimizer, OptimizerJobStore] | None:
    """返回 (StrategyAIOptimizer, OptimizerJobStore) — 缺失返 None."""
    opt = getattr(request.app.state, "strategy_optimizer", None)
    store = getattr(request.app.state, "strategy_optimizer_jobs", None)
    if opt is None or store is None:
        return None
    return opt, store


class OptimizeBody(BaseModel):
    focus: list[str] = []
    auto_apply: bool = False


@router.post("/strategies/{strategy_id}/optimize")
def start_optimize(strategy_id: str, request: Request, body: OptimizeBody | None = None):
    """触发 AI 优化自测 (2026-10-13).
    异步 background task; 返 202 + opt_id. 客户端轮询 /optimize/{opt_id} 看 status.
    """
    pair = _get_optimizer(request)
    if pair is None:
        raise HTTPException(status_code=503, detail="strategy_optimizer 未初始化")
    opt, store = pair
    if store.is_in_cooldown(strategy_id):
        raise HTTPException(status_code=429, detail="cooldown_active")
    if store.is_running(strategy_id):
        existing = store.is_running(strategy_id)
        raise HTTPException(status_code=409, detail=f"已有 optimize 在跑: {existing}")
    body = body or OptimizeBody()
    # Allocate opt_id synchronously
    from datetime import datetime, timedelta, timezone
    CN_TZ = timezone(timedelta(hours=8))
    opt_id = f"opt_{datetime.now(CN_TZ).strftime('%Y%m%d_%H%M%S_%f')}"
    job = OptimizeJob(
        opt_id=opt_id, strategy_id=strategy_id,
        focus=body.focus, auto_apply=body.auto_apply,
        started_at=datetime.now(CN_TZ).isoformat(),
    )
    store.save(job)
    # Schedule background task via FastAPI BackgroundTasks OR asyncio
    import asyncio
    async def _run():
        try:
            await opt.run(strategy_id, focus=body.focus, auto_apply=body.auto_apply)
            # Update the job in store (in-place mutation)
            # Note: the run() method creates its own OptimizeJob and returns it.
            # We need to capture the result and update.
            # Since we passed a pre-built job, opt.run() doesn't see it.
            # Workaround: call run() which returns a NEW OptimizeJob, then merge back.
            result_job = await opt.run(strategy_id, focus=body.focus, auto_apply=body.auto_apply)
            # Update fields in the stored job
            stored = store.get(opt_id)
            if stored:
                stored.status = result_job.status
                stored.diff = result_job.diff
                stored.applied = result_job.applied
                stored.error = result_job.error
                stored.parsed_diff_lines = result_job.parsed_diff_lines
                stored.verifier_passed = result_job.verifier_passed
                stored.verifier_summary = result_job.verifier_summary
                stored.finished_at = result_job.finished_at
                store.save(stored)
        finally:
            store.finish(opt_id, strategy_id)
    # Run in background
    loop = asyncio.get_event_loop()
    loop.create_task(_run())
    return {"opt_id": opt_id, "status": "running", "polling_url": f"/api/trading/strategies/{strategy_id}/optimize/{opt_id}"}


@router.get("/strategies/{strategy_id}/optimize/{opt_id}")
def get_optimize_status(strategy_id: str, opt_id: str, request: Request):
    """轮询 optimize 状态. completed/failed 后停."""
    pair = _get_optimizer(request)
    if pair is None:
        raise HTTPException(status_code=503, detail="strategy_optimizer 未初始化")
    opt, store = pair
    job = store.get(opt_id)
    if job is None:
        raise HTTPException(status_code=404, detail="opt_id 不存在")
    if job.strategy_id != strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id 不匹配")
    return job.to_dict()


@router.post("/strategies/{strategy_id}/run-now")
def run_strategy_now(
    strategy_id: str, request: Request, body: StrategyRunNowBody | None = None,
):
    """手动触发策略一次. 不传 body.event → 用 schedules[0] 作为默认事件."""
    reg = _get_strategy_registry(request)
    if reg is None or reg.get(strategy_id) is None:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 未注册")
    event = body.event if body else None
    try:
        report = _get_executor(request).run_now(strategy_id, trigger="api", event=event)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"策略执行失败: {e}") from e
    return report.to_dict()


# ── sync-from-verify (2026-10-13) ───────────────────────
@router.post("/strategies/{strategy_id}/sync-from-verify")
def sync_from_verify(strategy_id: str, request: Request, body: dict):
    """回测结果 → 实盘同步.

    body = {verify_run_id: str, dry_run: bool = True, confirm_token: str = ""}
    - dry_run=true:  构造 would_submit_orders + 落 audit, 返 confirm_token 给客户端二次确认
    - dry_run=false + confirm_token: 走 executor.run_with_target 真实下单
    """
    data_dir = _data_dir(request)
    verify_run_id = body.get("verify_run_id", "").strip()
    dry_run = bool(body.get("dry_run", True))
    confirm_token = body.get("confirm_token", "")
    if not verify_run_id:
        raise HTTPException(status_code=400, detail="verify_run_id 必填")
    orch = getattr(request.app.state, "sync_orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="sync_orchestrator 未初始化")
    from app.services.trading.sync import (
        AllTargetsBlocked,
        CodeChangedSinceVerify,
        InvalidConfirmToken,
        StaleReport,
        SyncError,
        SyncInFlight,
        VerifyRunNotFound,
        VerifyStrategyMismatch,
    )
    try:
        if dry_run:
            plan = orch.prepare_sync(strategy_id, verify_run_id, dry_run=True)
            return {
                "verify_run_id": plan.verify_run_id, "as_of": plan.as_of,
                "code_version_match": plan.code_version_match,
                "target_count": plan.target_count,
                "would_submit_orders": plan.would_submit_orders,
                "drop_reasons": plan.drop_reasons, "warnings": plan.warnings,
                "audit_id": plan.audit_id, "confirm_token": plan.confirm_token,
            }
        else:
            if not confirm_token:
                raise HTTPException(status_code=400, detail="confirm_token 必填 (先 dry_run 拿 token)")
            result = orch.execute_sync(strategy_id, verify_run_id, confirm_token=confirm_token)
            return result.to_dict()
    except VerifyRunNotFound:
        raise HTTPException(status_code=404, detail="verify_run_id 不存在")
    except VerifyStrategyMismatch as e:
        raise HTTPException(status_code=400, detail=str(e.code))
    except StaleReport:
        raise HTTPException(status_code=422, detail="stale_report")
    except CodeChangedSinceVerify:
        raise HTTPException(status_code=422, detail="code_changed_since_verify")
    except AllTargetsBlocked as e:
        raise HTTPException(status_code=422, detail=e.code)
    except InvalidConfirmToken as e:
        raise HTTPException(status_code=401, detail=f"confirm_token 校验失败: {e.code}")
    except SyncInFlight as e:
        raise HTTPException(status_code=409, detail=f"sync 已在跑, audit_id={e.detail.get('audit_id','')}")
    except SyncError as e:
        raise HTTPException(status_code=e.status, detail=e.code)


@router.get("/strategies/{strategy_id}/sync-audit")
def list_sync_audit_endpoint(
    strategy_id: str, request: Request,
    limit: int = 20, dry_run: bool | None = None,
):
    """返回 sync_audit.jsonl 中该 strategy 的最近 N 条 (倒序)."""
    from app.services.trading.sync import list_sync_audit as _list_audit
    data_dir = _data_dir(request)
    return {"entries": _list_audit(data_dir, strategy_id, limit=limit, dry_run=dry_run)}


# ── 跑回测 (verify) ─────────────────────────────────
class VerifyRequest(BaseModel):
    start_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    simulate_fills: bool = True
    initial_cash: float = Field(default=1_000_000.0, ge=0)
    events: list[str] | None = None      # None = 跑 strategy 全部 schedule
    save: bool = True                   # 落盘到 verifications/<id>/<run_id>.json


@router.get("/strategies/{strategy_id}/verify")
def verify_strategy(
    strategy_id: str, request: Request,
    start_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    simulate_fills: bool = Query(default=True),
    initial_cash: float = Query(default=1_000_000.0, ge=0),
    events: str | None = None,
    save: bool = Query(default=True),
):
    """读回测 (run strategy on historical data) + 可选落盘.
    save=true (默认): 落盘到 verifications/<id>/<run_id>.json, sync-from-verify 可用.
    events 接受半角逗号分隔的 event 列表 (e.g. 'rebalance,pre_market') 或 null (= 全部).
    """
    reg = _get_strategy_registry(request)
    if reg is None or reg.get(strategy_id) is None:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 未注册")
    events_list = [e.strip() for e in events.split(",")] if events else None
    try:
        from app.services.trading.verifier import StrategyVerifier
        verifier = getattr(request.app.state, "strategy_verifier", None)
        if verifier is None:
            verifier = StrategyVerifier(reg, request.app.state.repo)
        report = verifier.verify(
            strategy_id, start_date=start_date, end_date=end_date,
            simulate_fills=simulate_fills, initial_cash=initial_cash,
            events=events_list,
        )
        if save:
            from app.services.trading.verifier import save_verification
            save_verification(_data_dir(request), report)
        return report.to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"verify 失败: {e}") from e


@router.get("/strategies/{strategy_id}/verify/list")
def list_verifications(
    strategy_id: str, request: Request, limit: int = Query(default=20, ge=1, le=200),
):
    from app.services.trading.verifier import list_verifications as _list
    reports = _list(_data_dir(request), strategy_id)
    return {"reports": reports[:limit], "total": len(reports)}


# ── 策略 notify 切换 ─────────────────────────────────
class StrategyNotifyBody(BaseModel):
    enabled: bool


@router.post("/strategies/{strategy_id}/notify")
def set_strategy_notify(
    strategy_id: str, request: Request, body: StrategyNotifyBody,
):
    from app.services.trading import preferences as _prefs
    try:
        _prefs.set_notify_wechat(strategy_id, body.enabled)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"strategy_id": strategy_id, "notify_wechat": body.enabled}


# ── 通知中心 (跨策略聚合, frontend MessagesStream / Monitor 用) ─────────
class TradingNotifyIn(BaseModel):
    kind: str = "info"
    text: str
    source: str = "user"
    extra: dict | None = None


@router.get("/notify")
def list_notifications(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    kind: str | None = Query(default=None),
):
    """跨策略通知列表 (前端顶部通知中心 / 实时流)。"""
    from app.services.trading import notify_store
    items = notify_store.list_recent(_data_dir(request), limit=limit, kind=kind)
    return {"items": items, "total": len(items)}


@router.post("/notify")
def post_notification(body: TradingNotifyIn, request: Request):
    """手动追加一条通知 (测试 / 调试 / 用户操作入口)。"""
    from app.services.trading import notify_store
    entry = notify_store.append(
        _data_dir(request),
        kind=body.kind,
        text=body.text,
        source=body.source,
        extra=body.extra,
    )
    return {"status": "ok", "entry": entry}


# ── 跨策略聚合 PnL (无 strategy_id 时, frontend 顶栏 'all') ───────────
@router.get("/pnl/metrics")
def get_aggregate_pnl_metrics(request: Request):
    """无 strategy_id 的 PnL 指标聚合 — 取账户 + 全部 fills + 全部 snapshots。"""
    from app.services.trading import pnl as pnl_mod
    try:
        return pnl_mod.compute_metrics(_data_dir(request)).to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"aggregate pnl metrics 失败: {e}") from e


@router.get("/pnl/history")
def get_aggregate_pnl_history(
    request: Request,
    limit: int = Query(default=500, ge=1, le=5000),
):
    """跨策略 PnL 历史 (按时间正序)。"""
    from app.services.trading import pnl as pnl_mod
    snapshots = pnl_mod.list_history(_data_dir(request), limit=limit)
    return {"snapshots": snapshots}


# ── 策略删除 (trash) ──────────────────────────────
@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: str, request: Request):
    """删策略: prefs 清空 + registry 注销 + scheduler 移除 + .py 移 .trash/.

    B4 (PR-1): 保留 .py 在 .trash 目录, 不真删, 7 天可恢复.
    B5: 当前 run 即使进行中也不强行中断, 接受 race.
    """
    from app.services.trading import code_store as code_store_mod
    reg = _get_strategy_registry(request)
    if reg is None:
        raise HTTPException(status_code=503, detail="策略注册表未初始化")
    data_dir = _data_dir(request)
    src = code_store_mod._strategy_file(data_dir, strategy_id)
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 不存在")
    deleted_keys = prefs.delete_strategy_keys(strategy_id)
    reg.unregister(strategy_id)
    sched_obj = getattr(request.app.state, "strategy_scheduler", None)
    jobs_removed = 0
    if sched_obj is not None and hasattr(sched_obj, "_job_ids"):
        keys_to_remove = [k for k in list(sched_obj._job_ids.keys()) if k[0] == strategy_id]
        for k in keys_to_remove:
            jid = sched_obj._job_ids.pop(k, None)
            if jid:
                sched_obj._safe_remove(jid)
                jobs_removed += 1
    trashed = code_store_mod.move_to_trash(data_dir, strategy_id)
    return {
        "deleted": True, "strategy_id": strategy_id,
        "kept_source_file": True,
        "trashed_to": str(trashed.relative_to(data_dir / "user_data")) if trashed else None,
        "deleted_pref_keys": deleted_keys, "removed_jobs": jobs_removed,
    }


# ── PnL (per-strategy) ────────────────────────────
@router.get("/strategies/{strategy_id}/pnl/metrics")
def get_strategy_pnl_metrics(
    strategy_id: str, request: Request,
):
    from app.services.trading.pnl import compute_metrics
    try:
        return compute_metrics(_data_dir(request), strategy_id=strategy_id).to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"pnl metrics 失败: {e}") from e


@router.get("/strategies/{strategy_id}/pnl/history")
def get_strategy_pnl_history(
    strategy_id: str, request: Request,
    limit: int = Query(default=500, ge=1, le=5000),
):
    # pnl 不暴露 list_strategy_history helper; 直接调 list_history 后按 strategy_id 过滤
    from app.services.trading.pnl import list_history
    snapshots = [s for s in list_history(_data_dir(request), strategy_id=strategy_id, limit=limit)]
    return {"snapshots": snapshots}


# ── strategy runs 历史 ────────────────────────────
@router.get("/strategies/{strategy_id}/runs")
def list_strategy_runs(
    strategy_id: str, request: Request,
    limit: int = Query(default=20, ge=1, le=500),
):
    from app.services.trading.executor import list_runs
    return {"runs": list_runs(_data_dir(request), strategy_id=strategy_id, limit=limit)}


# ── strategy code 读写 ────────────────────────────
@router.get("/strategies/{strategy_id}/code")
def get_strategy_code(strategy_id: str, request: Request):
    try:
        content = code_store.read_code(_data_dir(request), strategy_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"strategy_id": strategy_id, "content": content, "size": len(content)}


@router.put("/strategies/{strategy_id}/code")
def put_strategy_code(
    strategy_id: str, request: Request, body: dict,
):
    content = body.get("content", "")
    note = body.get("note", "")
    run_after = bool(body.get("run_after", False))
    verify = bool(body.get("verify", False))   # F0-4 (2026-10-13)
    try:
        entry = code_store.write_code(
            _data_dir(request), strategy_id, content, note=note, verify=verify,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except SyntaxError as e:
        # F0-1: ast.parse 失败 → 400 + 详情 (line/col/msg)
        detail = {
            "error": "syntax_error",
            "message": str(e),
            "filename": getattr(e, "filename", None),
            "line": getattr(e, "lineno", None),
            "offset": getattr(e, "offset", None),
            "text": getattr(e, "text", None),
        }
        raise HTTPException(status_code=400, detail=detail) from e
    except RuntimeError as e:
        # F0-4: verify gate 失败 → 422 + 详情 (code 未落盘)
        msg = str(e)
        detail = {"error": "verify_failed", "raw": msg}
        if msg.startswith("verify_failed:"):
            parts = msg.split(":", 2)
            if len(parts) == 3:
                detail["verify_kind"] = parts[1]
                detail["verify_msg"] = parts[2]
        raise HTTPException(status_code=422, detail=detail) from e
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写文件失败: {e}") from e
    try:
        sched = getattr(request.app.state, "trading_scheduler", None)
        if sched is not None:
            reg = _get_strategy_registry(request)
            if reg is not None:
                sched.check_code_changed(
                    list(reg._strategies.values()),
                    code_store._strategies_dir(_data_dir(request)),
                )
    except Exception as e:
        logger.warning("check_code_changed failed: %s", e)
    if run_after:
        try:
            ex = _get_executor(request)
            ex.run_now(strategy_id, trigger="api", event=None)
        except Exception as e:
            logger.warning("run_after failed: %s", e)
    return {
        "strategy_id": strategy_id, "size": len(content), "changelog_entry": entry,
        "ran_after": run_after,
        "verified": verify,
    }


@router.get("/strategies/{strategy_id}/drift")
def get_strategy_drift(
    strategy_id: str, request: Request,
    window_days: int = Query(default=7, ge=1, le=90),
):
    """F1-4: 实盘 vs verify drift 检测.

    对比最近 window_days 天的 per-strategy fills (去重 symbol)
    vs 最近一次 verify 报告的 last_rebalance_target.
    """
    from app.services.trading import drift
    return drift.compute_drift(_data_dir(request), strategy_id, window_days).to_dict()


@router.post("/strategies/{strategy_id}/promote-to-live")
def promote_strategy_to_live(strategy_id: str, request: Request, body: dict):
    """F1-1: 把 ai/ 或 custom/ 域的策略晋级到实盘域 (live/).

    body:
      - source: "ai" | "custom" (必填)
      - verify: bool = True (F0-4 dry-verify gate)
      - overwrite: bool = False (目标已存在时拒绝)

    流程:
      1. 读 data/strategies/{source}/{strategy_id}.py
      2. 调 code_store.write_code(verify=...) 落盘到 live/{strategy_id}.py
         (ast.parse + 可选 dry-verify)
      3. 重新触发 Registry 扫描, 注册新策略
      4. 返 200 + 路径 + 注册数

    失败:
      - source 不在白名单 → 400
      - 源文件不存在 → 404
      - 目标已存在 + overwrite=False → 409
      - 语法错 / verify 失败 → 422 / 400 (继承 write_code 行为)
    """
    from app.services.trading import promote as promote_mod

    source = body.get("source", "")
    verify = bool(body.get("verify", True))
    overwrite = bool(body.get("overwrite", False))

    result = promote_mod.promote_to_live(
        _data_dir(request), strategy_id, source,
        verify=verify, overwrite=overwrite,
    )
    if result.error:
        if "不支持" in result.error:
            raise HTTPException(status_code=400, detail=result.error)
        if "源文件不存在" in result.error:
            raise HTTPException(status_code=404, detail=result.error)
        if "目标已存在" in result.error:
            raise HTTPException(status_code=409, detail=result.error)
        if result.error.startswith("SyntaxError"):
            raise HTTPException(status_code=400, detail=result.error)
        if result.error.startswith("RuntimeError"):
            raise HTTPException(status_code=422, detail=result.error)
        if result.error.startswith("ValueError"):
            raise HTTPException(status_code=400, detail=result.error)
        raise HTTPException(status_code=500, detail=result.error)

    # 让 Registry 看到新策略
    reg = _get_strategy_registry(request)
    sched = getattr(request.app.state, "trading_scheduler", None)
    registered_count = 0
    if reg is not None:
        try:
            registered_count = promote_mod.discover_live_after_promote(reg, _data_dir(request))
        except Exception as e:
            logger.warning("promote discover failed: %s", e)
    if sched is not None and reg is not None:
        try:
            sched.check_code_changed(
                list(reg._strategies.values()),
                code_store._strategies_dir(_data_dir(request)),
            )
        except Exception as e:
            logger.warning("promote scheduler refresh failed: %s", e)

    return {
        "strategy_id": result.strategy_id,
        "source": result.source,
        "target_path": str(result.target_path),
        "registered_count": registered_count,
    }


# ── F1-5: 快照 / diff / 回滚 (2026-10-13) ─────────────────────────────────
@router.get("/strategies/{strategy_id}/code/history")
def get_strategy_code_history(strategy_id: str, request: Request):
    """列策略历史快照 (按 ts 倒序, 最多 20)."""
    from app.services.trading import history as history_mod
    items = history_mod.list_history(_data_dir(request), strategy_id)
    return {
        "strategy_id": strategy_id,
        "history": [asdict(m) for m in items],
        "max": history_mod.MAX_HISTORY,
    }


@router.get("/strategies/{strategy_id}/code/diff")
def get_strategy_code_diff(strategy_id: str, request: Request, ts: str = Query(...)):
    """返 ts 快照 vs 当前 live/{id}.py 的 unified diff."""
    from app.services.trading import history as history_mod
    d = history_mod.diff_snapshot_vs_current(_data_dir(request), strategy_id, ts)
    if d is None:
        raise HTTPException(status_code=404, detail=f"快照不存在: ts={ts}")
    return {"strategy_id": strategy_id, **d}


@router.post("/strategies/{strategy_id}/code/rollback")
def post_strategy_code_rollback(strategy_id: str, request: Request, body: dict):
    """回滚到 ts 快照. 回滚前会先 snapshot 当前版, 避免数据丢失.

    body: {"ts": "20261013_120000Z"}
    副作用 (D10): 同步 disable 所有 schedules, 防止回滚后被 cron 自动重跑。
    """
    from app.services.trading import history as history_mod
    from app.services.trading import preferences as prefs
    ts = body.get("ts", "")
    if not ts:
        raise HTTPException(status_code=400, detail="ts 必填")
    try:
        info = history_mod.rollback(_data_dir(request), strategy_id, ts)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    # F1-5 (D10): 联动 disable 所有 schedules
    try:
        # 取当前 schedules → 全部设 enabled=False → 整体 PUT
        current = prefs.get_strategy_schedules(strategy_id) or []
        new_schedules = [{**s, "enabled": False} for s in current]
        if new_schedules:
            prefs.set_strategy_schedules(strategy_id, new_schedules)
    except Exception as e:
        logger.warning("rollback 联动 disable schedules 失败: %s", e)

    return {
        "strategy_id": strategy_id,
        "rolled_back_to": info,
        "schedules_disabled": True,
    }


@router.get("/strategies/{strategy_id}/code/changelog")
def get_strategy_code_changelog(
    strategy_id: str, request: Request, limit: int = Query(default=50, ge=1, le=500),
):
    return {"entries": code_store.list_changelog(_data_dir(request), strategy_id, limit=limit)}


# ── schedules ──────────────────────────────────
class StrategySchedulesBody(BaseModel):
    schedules: list[dict]


class StrategySchedulePatch(BaseModel):
    enabled: bool | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
    day_of_week: str | None = None


def _reschedule(request: Request, reg) -> None:
    sched = getattr(request.app.state, "trading_scheduler", None)
    if sched is None:
        return
    sched.apply_all(reg.list_all())


@router.put("/strategies/{strategy_id}/schedules")
def put_strategy_schedules(
    strategy_id: str, request: Request, body: StrategySchedulesBody,
):
    reg = _get_strategy_registry(request)
    if reg is None or reg.get(strategy_id) is None:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 未注册")
    try:
        schedules = prefs.set_strategy_schedules(strategy_id, body.schedules)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _reschedule(request, reg)
    return {"strategy_id": strategy_id, "schedules": schedules}


@router.post("/strategies/{strategy_id}/schedules/{event}")
def patch_strategy_schedule(
    strategy_id: str, event: str, request: Request, body: StrategySchedulePatch,
):
    reg = _get_strategy_registry(request)
    if reg is None or reg.get(strategy_id) is None:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 未注册")
    try:
        schedules = prefs.update_strategy_schedule(
            strategy_id, event,
            enabled=body.enabled, hour=body.hour, minute=body.minute,
            day_of_week=body.day_of_week,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _reschedule(request, reg)
    return {"strategy_id": strategy_id, "event": event, "schedules": schedules}
