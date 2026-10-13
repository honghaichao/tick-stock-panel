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

import logging
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services.trading import code_store, risk_metrics
from app.services.trading import engine as engine_mod
from app.services.trading import preferences as prefs
from app.services.trading.models import EngineStatus, OrderDraft

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
    return {"strategies": [s.to_dict() for s in reg.list_all()]}


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
    from app.services.trading.pnl import compute_strategy_metrics
    try:
        return compute_strategy_metrics(_data_dir(request), strategy_id).to_dict()
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


@router.get("/strategies/{strategy_id}/code/changelog")
def get_strategy_code_changelog(
    strategy_id: str, request: Request, limit: int = Query(default=50, ge=1, le=500),
):
    return {"entries": code_store.list_changelog(_data_dir(request), strategy_id, limit=limit)}


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
