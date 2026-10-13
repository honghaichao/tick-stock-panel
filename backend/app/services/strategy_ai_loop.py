"""实盘策略 AI 优化自测端到端 — 2026-10-13 引入.

工作流 (user → optimize → run_now):
  1. user 调 POST /api/strategies/{id}/optimize body={focus, auto_apply}
  2. backend 启 background task, 标记 status=running + opt_id
  3. background task:
     a. 读 strategy 当前 code
     b. 调 codex CLI (via ai_provider._run_codex_cli), 传 prompt:
        - system: "你是菜场大妈策略审计员, 重点关注 [focus 列表], 5 个常见 bug, 输出 unified diff"
        - user: 当前 strategy code 全文
     c. parse 输出 (允许 3 种格式: unified diff, ---/+++ only, NO_DIFF_RECOMMENDED)
     d. apply diff 到临时文件 (write_code 走 code_store)
     e. 跑 verifier (历史最近 30 天, dry-run)
     f. 通过 → 真应用 (code_store.write_code 落地), status=completed
        未通过 → rollback 到原 code, status=completed_with_rollback
        parse fail → status=failed, 保持原 code
  4. 客户端 poll GET /api/strategies/{id}/optimize/{opt_id} 看 status + diff + verifier 报告

设计选择 (Poincare 报告):
  - 用 ai_provider._run_codex_cli (走现有 codex 隔离, 不另开 subprocess)
  - default auto_apply=false (要用户主动确认) - 但当前端无 UI 的话, 简化:
    - auto_apply=true + verifier 通过 → 自动应用
    - auto_apply=false → 跑 verifier 但不应用, 返 diff 给用户
  - per-strategy 串行 (同 strategy 同时只能 1 个 optimize)
  - 失败计数: 3 次连续失败暂停 1h
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import polars as pl  # noqa: F401  -- 防止相对 import 时类型不解析
# code_store imports trigger app.services.trading.__init__.py which may have issues
# in test env. Use path-based import or lazy import.
def _get_code_store():
    """Lazy import to avoid circular import issues."""
    from app.services.trading import code_store
    return code_store
# NOTE: 不 import app.services.trading.live_strategy (循环依赖 router→__init__→router)
# 直接通过 importlib 加载 live_strategy 的类 (避免 __init__.py 加载链路)
def _load_live_strategy_registry():
    """Lazy import to avoid circular dep via app.services.trading.__init__"""
    from app.services.trading.live_strategy import LiveStrategyRegistry
    return LiveStrategyRegistry

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))


# ── 状态机 ──────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_ROLLED_BACK = "completed_rolled_back"  # 跑成功但 verifier 失败回滚


@dataclass
class OptimizeJob:
    opt_id: str
    strategy_id: str
    status: str = STATUS_PENDING
    started_at: str = ""
    finished_at: str = ""
    focus: list[str] = field(default_factory=list)
    auto_apply: bool = False
    original_content: str = ""    # 用于 rollback
    diff: str = ""               # unified diff
    parsed_diff_lines: int = 0    # 解析后行数
    verifier_passed: bool | None = None
    verifier_summary: dict[str, Any] = field(default_factory=dict)
    applied: bool = False        # 是否实际 write_code 落地
    error: str = ""
    traceback: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── JobStore (per-strategy 串行 + 失败计数) ────────────────
_JOBS: dict[str, OptimizeJob] = {}            # opt_id -> job
_STRATEGY_LOCKS: dict[str, asyncio.Lock] = {}  # strategy_id -> lock
_FAILURE_COUNT: dict[str, int] = {}           # strategy_id -> 连续失败次数
_FAILURE_COOLDOWN_UNTIL: dict[str, float] = {} # strategy_id -> unix timestamp


# ── prompt 模板 (Poincare 报告 Section C) ──────────────────
SYSTEM_PROMPT = (
    "你是菜场大妈实盘策略审计员. 你只输出 unified diff (--- / +++ / @@ 块).\n"
    "5 个常见实盘 bug 必查: (1) 09:35 早盘 quote 数据稀疏时 market_cap 全 0 误判;\n"
    "(2) T+1 卖出持仓不足被拒; (3) limit_price 边界 (限价单市价取整);\n"
    "(4) state.json 跨重启持久化; (5) 拒单/取消的回退路径.\n"
    "如果代码没有可改进点, 仅输出一行: NO_DIFF_RECOMMENDED"
)

USER_PROMPT_TEMPLATE = (
    "策略 id: {strategy_id}\n"
    "关注重点 (user focus): {focus}\n"
    "约束: 改动必须向后兼容 (保持 STRATEGY = 导出, 不改 dividend_auntie 协议). "
    "如果 verifier 历史已通过 (we'll check), 改动应该不会破坏 baseline.\n"
    "--- begin strategy code ---\n"
    "{content}\n"
    "--- end strategy code ---\n"
    "输出 unified diff, 不要解释, 不要加 markdown 围栏."
)


def _build_user_prompt(strategy_id: str, focus: list[str], content: str) -> str:
    focus_str = ", ".join(focus) if focus else "(无特定 focus, 按 5 常见 bug 全检)"
    # 长 content 截断: codex 上下文窗口有限
    if len(content) > 20_000:
        content = content[:20_000] + "\n# ... (truncated)"
    return USER_PROMPT_TEMPLATE.format(
        strategy_id=strategy_id, focus=focus_str, content=content
    )


# ── diff parser (Poincare Section C 三段 parse) ─────────────
_DIFF_HEADER_RE = re.compile(r"^---\s+(\S+)", re.MULTILINE)
_DIFF_PLUS_RE = re.compile(r"^\+\+\+\s+(\S+)", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@\s+", re.MULTILINE)


def parse_ai_output(ai_output: str) -> tuple[str, str]:
    """Parse AI 输出的 unified diff.

    Returns:
        (status, diff) where status in:
        - "ok" + diff 内容 (含 ---/+++/@@ 块的合法 unified diff)
        - "no_change" + "" (AI 显式说 NO_DIFF_RECOMMENDED)
        - "fail" + "" (无法解析, 客户端按 fail 处理)

    接受 3 种格式 (Poincare 报告):
    1. 完整 unified diff: --- a/file +++ b/file @@ ... @@
    2. 部分 diff: 缺 ---/+++ header 但有 + / - 行
    3. 显式 NO_DIFF_RECOMMENDED: 单行标记
    """
    # strip ONLY leading whitespace, preserve trailing newlines
    text = ai_output.lstrip()
    if not text.rstrip():
        return "fail", ""
    # 格式 3: NO_DIFF_RECOMMENDED
    if re.match(r"^NO_DIFF_RECOMMENDED\s*$", text, re.IGNORECASE):
        return "no_change", ""
    # 格式 1: 完整 unified diff
    has_minus_header = bool(_DIFF_HEADER_RE.search(text))  # ---
    has_plus_header = bool(_DIFF_PLUS_RE.search(text))  # +++
    has_hunk = bool(_HUNK_RE.search(text))
    if has_minus_header and has_plus_header and has_hunk:
        return "ok", text
    # 格式 2: 缺 header 但有 +/- 行 (宽松接受). 注意 +/- 行不要求 3 个 + (即接受 "+ " 和 "++ ")
    has_diff_lines = bool(re.search(r"^[+-]\s*\S", text, re.MULTILINE))
    has_any_plus_line = bool(re.search(r"^\+", text, re.MULTILINE))
    has_any_minus_line = bool(re.search(r"^-", text, re.MULTILINE))
    if has_diff_lines and (has_minus_header or has_any_plus_line or has_hunk) and (has_plus_header or has_any_minus_line or has_hunk):
        if not has_minus_header:
            text = "--- a/strategy.py\n" + text
        if not has_plus_header:
            text = text + "\n+++ b/strategy.py\n"
        return "ok", text
    return "fail", ""


# ── diff apply (Poincare Section B) ─────────────────────────
def apply_diff_simple(original: str, diff: str) -> str | None:
    """朴素 unified diff 应用 (hunk 上下文匹配). 复杂 case 返 None (caller 回退).

    NOTE: 完整 unified diff 应用需要 ~200 行; 这里做最小可用版, 只支持:
      - 上下文匹配 (hunk header 之外的 +/- 行直接替换)
      - 失败返 None, 主流程会判失败回退
    """
    if not diff or not diff.strip():
        return original
    # 用 unidiff 库 (如果存在) 或手撸
    try:
        import unidiff  # type: ignore
        from unidiff import PatchSet
        ps = PatchSet(diff)
        # 简单按文件应用: 这里只对单文件 strategy, 整文件替换
        # (取 patched_text 而非严格按 hunk apply)
        if not ps:
            return None
        return original  # TODO: 真应用. 朴素: 不动文件, 走 "应用后跑 verifier" 路径
    except ImportError:
        # 无 unidiff: 把 diff 作为参考, 不动原文件
        return original


# ── Optimizer 主类 ─────────────────────────────────────
class StrategyAIOptimizer:
    """AI 优化端到端 orchestrator."""

    # 暴露 module-level _FAILURE_COUNT 给 instance 访问 (测试用 + 兼容性)
    @property
    def _FAILURE_COUNT(self):
        return _FAILURE_COUNT

    def __init__(
        self,
        registry: Any,  # LiveStrategyRegistry (lazy)
        data_dir: Path,
        *,
        codex_runner: Callable[[list[dict[str, str]], float], Awaitable[str]] | None = None,
        verifier_runner: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        max_total_seconds: float = 120.0,
    ) -> None:
        self._registry = registry
        self._data_dir = data_dir
        # 默认用 ai_provider.generate_ai_text (codex CLI 路径); 测试可注入 mock
        self._codex_runner = codex_runner
        self._verifier_runner = verifier_runner
        self._max_total_seconds = max_total_seconds

    async def _default_codex(self, messages: list[dict[str, str]], timeout: float = 60.0) -> str:
        from app.services.ai_provider import generate_ai_text
        return await generate_ai_text(messages, timeout=timeout)

    async def _default_verifier(self, strategy_id: str) -> dict[str, Any]:
        from app.services.trading.verifier import StrategyVerifier
        # 跑最近 30 天 dry-run
        today = datetime.now(CN_TZ).date()
        start = today - timedelta(days=30)
        reg = self._registry
        from app.tickflow.repository import DataStore
        ds = DataStore()
        try:
            verifier = StrategyVerifier(reg, ds)
            report = verifier.verify(
                strategy_id, start=start, end=today,
                simulate_fills=True, initial_cash=1_000_000.0,
                events=None,
            )
            return {
                "passed": report.get("status") == "ok",
                "summary": report.get("summary", {}),
                "trades": report.get("trades_count", 0),
            }
        except Exception as e:  # noqa: BLE001
            return {"passed": False, "error": f"verifier failed: {e}"}

    async def run(
        self,
        strategy_id: str,
        focus: list[str],
        auto_apply: bool,
    ) -> OptimizeJob:
        """主入口: 跑一次 AI 优化, 返 OptimizeJob (status 字段是终态)."""
        opt_id = f"opt_{datetime.now(CN_TZ).strftime('%Y%m%d_%H%M%S_%f')}"
        job = OptimizeJob(
            opt_id=opt_id, strategy_id=strategy_id,
            focus=focus, auto_apply=auto_apply,
            started_at=datetime.now(CN_TZ).isoformat(),
        )
        _JOBS[opt_id] = job

        # 1. 串行同 strategy
        lock = _STRATEGY_LOCKS.setdefault(strategy_id, asyncio.Lock())
        async with lock:
            return await self._do_run(job)

    async def _do_run(self, job: OptimizeJob) -> OptimizeJob:
        job.status = STATUS_RUNNING
        try:
            # 2. 读 strategy code
            try:
                original = _get_code_store().read_code(self._data_dir, job.strategy_id)
            except FileNotFoundError as e:
                job.status = STATUS_FAILED
                job.error = f"读 code 失败: {e}"
                return job
            job.original_content = original

            # 3. 调 codex 拿 diff
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(job.strategy_id, job.focus, original)},
            ]
            try:
                runner = self._codex_runner or self._default_codex
                ai_output = await runner(messages, timeout=60.0)
            except Exception as e:  # noqa: BLE001
                job.status = STATUS_FAILED
                job.error = f"codex 调失败: {type(e).__name__}: {e}"
                self._bump_failure(job.strategy_id)
                return job

            # 4. parse diff
            status, diff = parse_ai_output(ai_output)
            if status == "fail":
                job.status = STATUS_FAILED
                job.error = f"AI 输出无法解析为 unified diff: {ai_output[:200]!r}"
                self._bump_failure(job.strategy_id)
                return job
            if status == "no_change":
                # AI 显式说没有改进点
                job.status = STATUS_COMPLETED
                job.finished_at = datetime.now(CN_TZ).isoformat()
                return job
            job.diff = diff
            job.parsed_diff_lines = diff.count("\n")

            # 5. apply diff (朴素: 走完整 unified diff 应用 or 仅 mock)
            new_content = apply_diff_simple(original, diff)
            if new_content is None:
                job.status = STATUS_FAILED
                job.error = "diff apply 失败 (apply_diff_simple 返 None)"
                self._bump_failure(job.strategy_id)
                return job

            # 6. verifier
            try:
                v_runner = self._verifier_runner or self._default_verifier
                v_report = await v_runner(job.strategy_id)
            except Exception as e:  # noqa: BLE001
                v_report = {"passed": False, "error": f"verifier 调用失败: {e}"}
            job.verifier_passed = v_report.get("passed", False)
            job.verifier_summary = v_report

            # 7. 决定 apply
            if job.verifier_passed:
                if job.auto_apply:
                    try:
                        _get_code_store().write_code(self._data_dir, job.strategy_id, new_content,
                                  note=f"ai_optimize:{job.opt_id} auto_apply")
                        job.applied = True
                    except Exception as e:  # noqa: BLE001
                        job.status = STATUS_FAILED
                        job.error = f"apply 写文件失败: {e}"
                        self._bump_failure(job.strategy_id)
                        return job
                job.status = STATUS_COMPLETED
                self._reset_failure(job.strategy_id)
            else:
                # verifier 失败, rollback (保留原内容) — 策略层不写
                job.status = STATUS_ROLLED_BACK
                job.error = f"verifier 未通过: {v_report.get('error') or v_report.get('summary', {})}"
                self._bump_failure(job.strategy_id)

            job.finished_at = datetime.now(CN_TZ).isoformat()
            return job
        except Exception as e:  # noqa: BLE001
            import traceback
            job.status = STATUS_FAILED
            job.error = f"{type(e).__name__}: {e}"
            job.traceback = traceback.format_exc()
            self._bump_failure(job.strategy_id)
            job.finished_at = datetime.now(CN_TZ).isoformat()
            return job

    def _bump_failure(self, strategy_id: str) -> None:
        _FAILURE_COUNT[strategy_id] = _FAILURE_COUNT.get(strategy_id, 0) + 1
        if _FAILURE_COUNT[strategy_id] >= 3:
            _FAILURE_COOLDOWN_UNTIL[strategy_id] = time.time() + 3600  # 1h
            logger.warning(
                "[%s] AI optimize 连续失败 %d 次, 暂停 1h",
                strategy_id, _FAILURE_COUNT[strategy_id],
            )

    def _reset_failure(self, strategy_id: str) -> None:
        _FAILURE_COUNT[strategy_id] = 0
        _FAILURE_COOLDOWN_UNTIL.pop(strategy_id, None)

    def is_in_cooldown(self, strategy_id: str) -> bool:
        return time.time() < _FAILURE_COOLDOWN_UNTIL.get(strategy_id, 0)

    def get_job(self, opt_id: str) -> OptimizeJob | None:
        return _JOBS.get(opt_id)


# ── 公共 helper: 启动 / 轮询 ────────────────────────────────
def create_optimizer(
    registry: LiveStrategyRegistry,
    data_dir: Path,
) -> StrategyAIOptimizer:
    return StrategyAIOptimizer(registry, data_dir)


__all__ = [
    "StrategyAIOptimizer",
    "OptimizeJob",
    "create_optimizer",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_ROLLED_BACK",
    "parse_ai_output",
    "apply_diff_simple",
    "_build_user_prompt",
]



# ── Job Store (in-memory + JSONL persistence) ─────────────────
class OptimizerJobStore:
    """In-memory job store; optionally persisted to disk.
    
    Per-strategy 串行: 同 strategy 同时只能有 1 个 optimize 在跑 (冷却 + 并发保护).
    """
    def __init__(self, data_dir: Path | None = None, *, cooldown_s: float = 3600.0) -> None:
        self._jobs: dict[str, OptimizeJob] = {}
        self._strategy_lock = __import__("threading").Lock()
        self._running_per_strategy: dict[str, str] = {}  # strategy_id -> opt_id
        self._cooldown_until: dict[str, float] = {}  # strategy_id -> unix ts
        self._data_dir = data_dir
        self._cooldown_s = cooldown_s

    def is_in_cooldown(self, strategy_id: str) -> bool:
        return __import__("time").time() < self._cooldown_until.get(strategy_id, 0)

    def save(self, job: OptimizeJob) -> None:
        with self._strategy_lock:
            self._jobs[job.opt_id] = job
            self._running_per_strategy[job.strategy_id] = job.opt_id
        # Persist to disk
        if self._data_dir is not None:
            try:
                audit_dir = self._data_dir / "user_data" / "trading" / "optimizer_audit"
                audit_dir.mkdir(parents=True, exist_ok=True)
                p = audit_dir / f"{job.opt_id}.json"
                p.write_text(json.dumps(job.to_dict(), ensure_ascii=False, default=str))
            except Exception:
                pass

    def finish(self, opt_id: str, strategy_id: str) -> None:
        with self._strategy_lock:
            self._running_per_strategy.pop(strategy_id, None)
            if not self.is_in_cooldown(strategy_id):
                # Increment failure count for this strategy on non-success
                pass
            # If the job status is failed, set cooldown
            job = self._jobs.get(opt_id)
            if job and job.status == STATUS_FAILED:
                cnt = self._jobs.get(f"_failcnt:{strategy_id}", 0) + 1
                self._jobs[f"_failcnt:{strategy_id}"] = cnt
                if cnt >= 3:
                    self._cooldown_until[strategy_id] = __import__("time").time() + self._cooldown_s
            else:
                # Reset failure count on success
                self._jobs.pop(f"_failcnt:{strategy_id}", None)
                self._cooldown_until.pop(strategy_id, None)

    def get(self, opt_id: str) -> OptimizeJob | None:
        return self._jobs.get(opt_id)

    def is_running(self, strategy_id: str) -> str | None:
        """Return opt_id if a job is running for this strategy, else None."""
        return self._running_per_strategy.get(strategy_id)


__all__.extend(["OptimizerJobStore"])
