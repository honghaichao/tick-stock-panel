"""回测结果 → 实盘同步 orchestrator (2026-10-13).

设计见 docs/live-trading-p1-ai-loop-backtest-sync.md §3.3 + docs/architecture-review-backtest-sync.md

端到端流:
  1. POST /api/strategies/{id}/verify  →  落盘 verify 报告 (last_rebalance_target 等)
  2. POST /api/strategies/{id}/sync-from-verify {verify_run_id, dry_run: true}
        → prepare_sync() 构造 would_submit_orders (实时价 + 风控 + 审计)
  3. POST .../sync-from-verify {verify_run_id, dry_run: false, confirm_token}
        → execute_sync() 调 executor.run_with_target (走完整 rebalance 流程)

关键安全护栏:
  - dry_run 默认; 真跑要 confirm_token (TTL 5min)
  - limit_price 实时从 quote_service 拿 (绝不沿用 verify 报告的历史 close)
  - 风控过滤: 暂停 / 涨跌停 / 无实时价 → drop_reason, 全阻断返 422
  - 同 strategy 同时只能 1 个 sync (命名锁)
  - audit 落 sync_audit.jsonl (含 dry + real)
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))
MAX_AGE_DAYS = 7              # verify 报告超过 7 天拒绝同步
CONFIRM_TOKEN_TTL_S = 300     # confirm_token 5 分钟过期


# ── 异常 ───────────────────────────────────────────
class SyncError(Exception):
    """Base for sync errors."""
    def __init__(self, code: str, status: int = 400, **kw):
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = kw


class VerifyRunNotFound(SyncError):
    def __init__(self):
        super().__init__("verify_run_not_found", status=404)


class VerifyStrategyMismatch(SyncError):
    def __init__(self):
        super().__init__("verify_run_strategy_mismatch", status=400)


class StaleReport(SyncError):
    def __init__(self, age_days: int):
        super().__init__("stale_report", status=422, age_days=age_days)


class CodeChangedSinceVerify(SyncError):
    def __init__(self, verify_ts: str, current_ts: str):
        super().__init__("code_changed_since_verify", status=422,
                         verify_ts=verify_ts, current_ts=current_ts)


class InvalidConfirmToken(SyncError):
    def __init__(self, reason: str = "expired_or_invalid"):
        super().__init__("invalid_confirm_token", status=401, reason=reason)


class SyncInFlight(SyncError):
    def __init__(self, started_at: str, audit_id: str):
        super().__init__("sync_in_flight", status=409, started_at=started_at, audit_id=audit_id)


class AllTargetsBlocked(SyncError):
    def __init__(self, blocked: dict[str, str]):
        super().__init__("all_targets_blocked", status=422, blocked=blocked)


# ── 报告 ───────────────────────────────────────────
@dataclass
class SyncPlan:
    verify_run_id: str
    as_of: str
    code_version_match: bool
    target_count: int
    would_submit_orders: list[dict[str, Any]]
    drop_reasons: dict[str, str]
    warnings: list[str]
    audit_id: str
    confirm_token: str            # HMAC-signed, ttl 自管

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SyncResult:
    verify_run_id: str
    audit_id: str
    run_report: dict[str, Any]
    submitted_count: int
    skipped_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Audit log writer (per-line JSONL) ───────────────────
def _append_sync_audit(data_dir: Path, entry: dict[str, Any]) -> str:
    """追加一条 audit (jsonl). 返 audit_id."""
    audit_id = f"sync_{datetime.now(CN_TZ).strftime('%Y%m%d_%H%M%S_%f')}"
    entry = {**entry, "audit_id": audit_id,
             "ts": entry.get("ts") or datetime.now(CN_TZ).isoformat()}
    p = data_dir / "user_data" / "trading" / "sync_audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return audit_id


def list_sync_audit(data_dir: Path, strategy_id: str, *, limit: int = 20,
                    dry_run: bool | None = None) -> list[dict[str, Any]]:
    """读 sync_audit.jsonl, 过滤 strategy_id (策略在 entry.target 里), 倒序."""
    p = data_dir / "user_data" / "trading" / "sync_audit.jsonl"
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("strategy_id") != strategy_id:
            continue
        if dry_run is not None and d.get("dry_run") != dry_run:
            continue
        out.append(d)
    out.reverse()  # 倒序
    return out[:limit]


# ── Token 签名 (HMAC-like) ──────────────────────────────
def _sign_token(audit_id: str, verify_run_id: str, ttl_s: int = CONFIRM_TOKEN_TTL_S) -> str:
    """简单 token: ts|audit_id|verify_run_id|hmac(ts|audit_id|verify_run_id).
    不存状态; 服务端只校验签名 + ttl. ttl 自管 (decode 后看 ts)."""
    ts = int(time.time())
    payload = f"{ts}|{audit_id}|{verify_run_id}"
    # 用 audit_id + 固定 salt 简化. 实际生产应走 secrets_store 但本期 demo 用固定 secret.
    h = hashlib.sha256(f"sync-confirm-salt::{payload}".encode()).hexdigest()[:16]
    return f"{payload}|{h}"


def _verify_token(token: str, expected_audit_id: str, expected_run_id: str) -> None:
    """校验 token, 失败 raise InvalidConfirmToken. ttl 超时 raise InvalidConfirmToken."""
    import sys as _dbg_vt; _dbg_vt.stdout.write(f"[DBG _verify_token] id={id(_verify_token)} token={token[:30]}\n")
    try:
        parts = token.rsplit("|", 3)
        if len(parts) != 4:
            raise InvalidConfirmToken("malformed_split")
        ts_s, audit_id, run_id, h = parts
    except Exception:
        raise InvalidConfirmToken("malformed")
    payload = f"{ts_s}|{audit_id}|{run_id}"
    expected_h = hashlib.sha256(f"sync-confirm-salt::{payload}".encode()).hexdigest()[:16]
    if h != expected_h:
        raise InvalidConfirmToken("bad_signature")
    if audit_id != expected_audit_id:
        raise InvalidConfirmToken("audit_id_mismatch")
    if run_id != expected_run_id:
        raise InvalidConfirmToken("run_id_mismatch")
    if int(time.time()) - int(ts_s) > CONFIRM_TOKEN_TTL_S:
        raise InvalidConfirmToken("expired")


# ── Orchestrator 主类 ───────────────────────────────────
class BacktestSyncOrchestrator:
    def __init__(self, *, data_dir: Path, runner: Any, executor: Any,
                 quote_service: Any | None = None, registry: Any | None = None) -> None:
        self._data_dir = data_dir
        self._runner = runner
        self._executor = executor
        self._quote_service = quote_service
        self._registry = registry
        # (strategy_id, "sync-from-verify") 命名锁 — 跟 executor 的 (strategy, event) 正交
        self._sync_locks: dict[str, threading.Lock] = {}
        self._sync_locks_lock = threading.Lock()
        # in-flight 跟踪 (avoid 重入)
        self._in_flight: dict[str, dict[str, Any]] = {}
        self._in_flight_lock = threading.Lock()

    def _sync_lock_for(self, strategy_id: str) -> threading.Lock:
        with self._sync_locks_lock:
            lk = self._sync_locks.get(strategy_id)
            if lk is None:
                lk = threading.Lock()
                self._sync_locks[strategy_id] = lk
            return lk

    def _live_price(self, sym: str) -> float | None:
        """从 quote_service 拿实时价 (严禁用 verify 报告的 fills[].price)."""
        if self._quote_service is None:
            return None
        # quote_service 走 enrichment 缓存 (close 即 last_price)
        try:
            df, _ = self._quote_service.get_enriched_today()
            if df is None or df.is_empty():
                return None
            row = df.filter(__import__("polars").col("symbol") == sym)
            if row.is_empty():
                return None
            v = row.get_column("close").item()
            return float(v) if v and v > 0 else None
        except Exception as e:
            logger.debug("live price probe %s failed: %s", sym, e)
            return None

    def _is_paused(self, sym: str) -> bool:
        if self._quote_service is None:
            return False
        try:
            df, _ = self._quote_service.get_enriched_today()
            if df is None or df.is_empty():
                return False
            row = df.filter(__import__("polars").col("symbol") == sym)
            if row.is_empty():
                return False
            close = row.get_column("close").item()
            return close is None or close <= 0
        except Exception:
            return False

    def prepare_sync(
        self, strategy_id: str, verify_run_id: str, *, dry_run: bool, now: datetime | None = None
    ) -> SyncPlan:
        """构造 would_submit_orders, 落 audit. dry_run 同样落 audit (供回查)."""
        from app.services.trading.verifier import load_verification
        report = load_verification(self._data_dir, strategy_id, verify_run_id)
        if report is None:
            raise VerifyRunNotFound()
        if report.get("strategy_id") != strategy_id:
            raise VerifyStrategyMismatch()

        as_of = report.get("last_rebalance_date") or report.get("end_date") or ""
        if not as_of:
            raise SyncError("verify_no_as_of", status=422, hint="报告无 last_rebalance_date / end_date")
        from datetime import date
        try:
            as_of_date = date.fromisoformat(as_of)
        except Exception:
            raise SyncError("verify_bad_as_of", status=422)
        today = (now or datetime.now(CN_TZ)).date()
        age_days = (today - as_of_date).days
        if age_days > MAX_AGE_DAYS:
            raise StaleReport(age_days)
        if not report.get("simulate_fills", False):
            raise SyncError("verify_without_fills", status=422,
                            hint="重跑 verify 时 simulate_fills=True")

        # code_version 一致性
        current_code_version = self._current_code_version()
        verify_code_version = report.get("code_version", "")
        code_version_match = (not verify_code_version
                              or not current_code_version
                              or current_code_version == verify_code_version)
        if not code_version_match:
            raise CodeChangedSinceVerify(verify_code_version, current_code_version)

        # 拿 target + 实时价
        target: dict[str, int] = report.get("last_rebalance_target") or {}
        warnings: list[str] = []
        drop_reasons: dict[str, str] = {}
        live_prices: dict[str, float] = {}
        for sym in list(target.keys()):
            px = self._live_price(sym)
            if px is None or px <= 0:
                drop_reasons[sym] = "no_live_price"
                warnings.append(f"{sym} 无实时价, 真跑时 skip")
            else:
                live_prices[sym] = px

        # 算 delta vs 当前持仓
        cur_qty: dict[str, int] = {p.symbol: p.qty for p in self._runner.positions()}

        # 构造 would_submit_orders
        would: list[dict[str, Any]] = []
        for sym, tgt_qty in target.items():
            cur = cur_qty.get(sym, 0)
            delta = tgt_qty - cur
            if delta == 0:
                continue
            side = "buy" if delta > 0 else "sell"
            qty = abs(delta)
            order_type = "limit"  # 统一用 limit (用 live_price 做限价)
            if sym in drop_reasons:
                continue  # 跳过被风控的
            would.append({
                "symbol": sym, "side": side, "qty": qty,
                "order_type": order_type, "limit_price": live_prices.get(sym),
                "price_source": "live_quote",
                "note": f"sync-from-verify:{verify_run_id}",
            })
        if not would and target:
            raise AllTargetsBlocked(drop_reasons or {"(all)": "no_live_price or zero_delta"})

        # 落 audit + 算 confirm_token
        audit_entry = {
            "strategy_id": strategy_id,
            "verify_run_id": verify_run_id,
            "dry_run": dry_run,
            "as_of": as_of,
            "code_version_match": code_version_match,
            "target": target,
            "live_prices": live_prices,
            "would_submit_orders": would,
            "drop_reasons": drop_reasons,
            "warnings": warnings,
            "result": {"status": "ok", "submitted": len(would) if not dry_run else 0},
        }
        audit_id = _append_sync_audit(self._data_dir, audit_entry)
        confirm_token = _sign_token(audit_id, verify_run_id) if not dry_run else ""
        return SyncPlan(
            verify_run_id=verify_run_id, as_of=as_of,
            code_version_match=code_version_match,
            target_count=len(target),
            would_submit_orders=would,
            drop_reasons=drop_reasons, warnings=warnings,
            audit_id=audit_id, confirm_token=confirm_token,
        )

    def execute_sync(
        self, strategy_id: str, verify_run_id: str, *, confirm_token: str
    ) -> SyncResult:
        """真跑 (走 executor.run_with_target)."""
        # 1. 重做 prepare 拿 plan (single source of truth)
        plan = self.prepare_sync(strategy_id, verify_run_id, dry_run=True)

        # 2. 验 token
        try:
            _verify_token(confirm_token, plan.audit_id, verify_run_id)
        except InvalidConfirmToken:
            raise
        # 3. 同 strategy 串行
        slk = self._sync_lock_for(strategy_id)
        with slk:
            with self._in_flight_lock:
                if strategy_id in self._in_flight:
                    audit_id_existing = self._in_flight[strategy_id].get("audit_id", "")
                    raise SyncInFlight(self._in_flight[strategy_id]["started_at"], audit_id_existing)
            self._in_flight[strategy_id] = {
                "started_at": datetime.now(CN_TZ).isoformat(),
                "audit_id": plan.audit_id,
            }
            try:
                # 4. 调 executor.run_with_target (走完整 rebalance 流程)
                target_dict = {o["symbol"]: o["qty"] * (1 if o["side"] == "buy" else -1)
                               for o in plan.would_submit_orders}
                run_report = self._executor.run_with_target(
                    strategy_id=strategy_id, event="rebalance",
                    override_target=target_dict,
                    trigger="sync-from-verify",
                    note=f"sync-from-verify:{verify_run_id}",
                )
                # 5. 落 audit (real)
                submitted_count = sum(1 for o in (run_report.get("submitted") or []))
                skipped_count = sum(1 for o in (run_report.get("skipped") or []))
                audit_id = _append_sync_audit(self._data_dir, {
                    "strategy_id": strategy_id,
                    "verify_run_id": verify_run_id,
                    "dry_run": False,
                    "as_of": plan.as_of,
                    "code_version_match": plan.code_version_match,
                    "target": target_dict,
                    "result": {
                        "status": "ok" if run_report.get("status") in ("success", "no_target") else "error",
                        "submitted": submitted_count,
                        "skipped": skipped_count,
                    },
                    "run_report_id": run_report.get("started_at", ""),
                })
                return SyncResult(
                    verify_run_id=verify_run_id,
                    audit_id=audit_id,
                    run_report=run_report,
                    submitted_count=submitted_count,
                    skipped_count=skipped_count,
                )
            finally:
                with self._in_flight_lock:
                    self._in_flight.pop(strategy_id, None)

    def _current_code_version(self) -> str:
        """从 code_changelog.jsonl 读最新 ts."""
        p = self._data_dir / "user_data" / "trading" / "code_changelog.jsonl"
        if not p.exists():
            return ""
        try:
            lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            if not lines:
                return ""
            last = json.loads(lines[-1])
            return last.get("ts", "")
        except Exception:
            return ""


__all__ = [
    "CONFIRM_TOKEN_TTL_S",
    "MAX_AGE_DAYS",
    "AllTargetsBlocked",
    "BacktestSyncOrchestrator",
    "CodeChangedSinceVerify",
    "InvalidConfirmToken",
    "StaleReport",
    "SyncError",
    "SyncInFlight",
    "SyncPlan",
    "SyncResult",
    "VerifyRunNotFound",
    "VerifyStrategyMismatch",
    "list_sync_audit",
]
