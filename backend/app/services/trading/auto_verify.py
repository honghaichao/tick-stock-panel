"""F0-4: 保存策略代码前的 dry-verify (2026-10-13).

不依赖 repo 数据, 只做 3 件事:
  1. importlib 加载 → STRATEGY 实例存在
  2. 构造 mock LiveStrategyContext
  3. 调 on_event("rebalance", mock_ctx) → 返回 dict 不抛异常

通过 → caller 落盘; 失败 → 422 + 详情 (不落盘)。

不做:
  - 不跑 1 天真实 verify (需要 repo 数据, 慢; 见 docs/strategy-to-live-flow-implementation.md §3 F0-4 备注)
  - 不检查 target 质量 (mock 数据下无法判定)
  - 不动 registry (只是模拟一次 on_event)
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import logging
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))


@dataclass
class DryVerifyResult:
    """dry-verify 结果.

    Attributes:
        passed: 通过
        error_kind: 失败原因分类 (import_error / no_strategy_instance /
                   on_event_error / syntax_error)
        error_msg: 错误详情 (含 line / col / tb 摘要)
        target_count: on_event 返回的 target symbol 数 (通过时才有意义)
    """
    passed: bool
    error_kind: str | None = None
    error_msg: str | None = None
    target_count: int | None = None


def _build_mock_context() -> Any:
    """构造空 LiveStrategyContext, 让 on_event 可以跑一次.

    字段: positions / account / fills_recent / market / trade_date /
          as_of / event / state / data_provider / send_message.
    """
    from app.services.trading.live_strategy import LiveStrategyContext

    today = datetime.now(CN_TZ).date()
    return LiveStrategyContext(
        positions={},
        account={"cash": 0.0, "total_equity": 0.0, "market_value": 0.0},
        fills_recent=[],
        market={},
        trade_date=today,
        as_of=datetime.now(CN_TZ).isoformat(timespec="seconds"),
        event="rebalance",
        state={},
        data_provider=None,
        send_message=None,
    )


def dry_verify_strategy_code(content: str, *, prefer_event: str = "rebalance") -> DryVerifyResult:
    """保存前 dry-verify.

    Args:
        content: 完整 .py 文件内容.
        prefer_event: 优先尝试的事件名 (默认 rebalance; 若策略 schedules 不含则尝试第一个).

    Returns:
        DryVerifyResult.
    """
    # 1. ast.parse 已经由 caller (code_store.write_code) 保证, 这里不再重复.
    # 2. 临时模块加载.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    mod_name = f"_dry_verify_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(mod_name, tmp_path)
    if spec is None or spec.loader is None:
        return DryVerifyResult(
            passed=False,
            error_kind="import_error",
            error_msg=f"无法构造模块 spec: {tmp_path}",
        )

    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    except Exception as e:
        return DryVerifyResult(
            passed=False,
            error_kind="import_error",
            error_msg=f"{type(e).__name__}: {e}",
        )
    finally:
        sys.modules.pop(mod_name, None)
        with contextlib.suppress(Exception):
            tmp_path.unlink()

    # 3. 取 STRATEGY 实例.
    strategy = getattr(module, "STRATEGY", None)
    if strategy is None:
        return DryVerifyResult(
            passed=False,
            error_kind="no_strategy_instance",
            error_msg="模块级未导出 STRATEGY 实例",
        )

    # 4. 选事件名.
    event = prefer_event
    schedules = getattr(strategy, "schedules", None) or []
    if schedules and prefer_event not in {s.name for s in schedules}:
        event = schedules[0].name

    # 5. mock ctx + 调 on_event.
    try:
        ctx = _build_mock_context()
        target = strategy.on_event(event, ctx)
        if not isinstance(target, dict):
            return DryVerifyResult(
                passed=False,
                error_kind="on_event_error",
                error_msg=f"on_event 应返 dict, 实际 {type(target).__name__}",
            )
        return DryVerifyResult(
            passed=True,
            target_count=len(target),
        )
    except Exception as e:
        tb = traceback.format_exc(limit=5)
        return DryVerifyResult(
            passed=False,
            error_kind="on_event_error",
            error_msg=f"{type(e).__name__}: {e}\n{tb}",
        )


__all__ = ["DryVerifyResult", "dry_verify_strategy_code"]
