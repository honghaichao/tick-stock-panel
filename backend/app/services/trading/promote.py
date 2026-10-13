"""F1-1: 策略晋级 API (2026-10-13).

把 data/strategies/{ai,custom}/{id}.py 复制到 data/user_data/strategies/live/{id}.py
并重新触发 Registry 扫描, 让策略在实盘域可见。

设计:
  - ai/ + custom/ 路径不变 (向后兼容, 老用户不受影响)
  - promote 是显式操作: 用户在 UI 选 "把这个策略晋级到实盘" 才触发
  - 复用 code_store.write_code: 自动获 ast.parse + dry-verify 闸 (verify=True)
  - 默认不启用 schedule (用户仍要进实盘页打开)

不做:
  - 不改 ai_generator 落盘路径 (避免破坏 ai/custom 工作流)
  - 不动 registry 的扫描规则 (ai/custom 仍归回测域, live 归实盘域)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# 源域白名单 (F1-1: 不允许从任意目录晋级, 防路径穿越)
ALLOWED_SOURCES = frozenset({"ai", "custom"})

SourceKind = Literal["ai", "custom"]


@dataclass
class PromoteResult:
    """晋级结果."""
    strategy_id: str
    source: str
    target_path: Path
    registered: bool
    target_count: int | None = None  # dry-verify 返回的 target 数 (verify=True 时)
    error: str | None = None


def _source_dir(data_dir: Path, source: SourceKind) -> Path:
    return data_dir / "strategies" / source


def _live_dir(data_dir: Path) -> Path:
    return data_dir / "user_data" / "strategies" / "live"


def promote_to_live(
    data_dir: Path,
    strategy_id: str,
    source: SourceKind,
    *,
    verify: bool = True,
    overwrite: bool = False,
) -> PromoteResult:
    """把 ai/custom/{id}.py 复制到 live/{id}.py.

    Args:
        data_dir: 仓库 data_dir 根.
        strategy_id: 策略 id (与文件名一致, 不带 .py 后缀).
        source: 源域 ("ai" | "custom").
        verify: F0-4 dry-verify 闸 (True = 落盘前跑一次).
        overwrite: 已存在时是否覆盖 (False = 返 error).
    """
    if source not in ALLOWED_SOURCES:
        return PromoteResult(
            strategy_id=strategy_id, source=source, target_path=Path(),
            registered=False, error=f"不支持的 source: {source} (允许: ai, custom)",
        )

    src = _source_dir(data_dir, source) / f"{strategy_id}.py"
    if not src.exists():
        return PromoteResult(
            strategy_id=strategy_id, source=source, target_path=Path(),
            registered=False, error=f"源文件不存在: {src}",
        )

    live_target_dir = _live_dir(data_dir)
    dst = live_target_dir / f"{strategy_id}.py"
    if dst.exists() and not overwrite:
        return PromoteResult(
            strategy_id=strategy_id, source=source, target_path=dst,
            registered=False, error=f"目标已存在, 拒绝覆盖: {dst} (overwrite=False)",
        )

    # 落盘 (复用 code_store.write_code 拿 ast.parse + dry-verify)
    from app.services.trading import code_store
    content = src.read_text(encoding="utf-8")
    try:
        code_store.write_code(
            data_dir, strategy_id, content,
            note=f"promote_from_{source}",
            verify=verify,
        )
    except (ValueError, SyntaxError, RuntimeError, OSError) as e:
        return PromoteResult(
            strategy_id=strategy_id, source=source, target_path=dst,
            registered=False, error=f"{type(e).__name__}: {e}",
        )

    return PromoteResult(
        strategy_id=strategy_id, source=source, target_path=dst,
        registered=False, target_count=None,
    )


def discover_live_after_promote(registry, data_dir: Path) -> int:
    """晋级后调用: 重新扫描 live/ 目录, 把新策略加入 Registry.

    Returns: 新增注册数.
    """
    return registry.discover_from(_live_dir(data_dir))


__all__ = ["ALLOWED_SOURCES", "PromoteResult", "discover_live_after_promote", "promote_to_live"]
