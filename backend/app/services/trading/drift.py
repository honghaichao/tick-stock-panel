"""F1-4: 实盘 vs verify drift 检测 (2026-10-13).

对比:
  - live: 最近 N 天 fills.jsonl 中去重的 symbol 集合 (per-strategy)
  - verify: 最近一次 verify 报告的 last_rebalance_target.symbol 集合

输出:
  - overlap: 交集 (live ∩ verify)
  - only_live: live 有但 verify 没有 (额外持仓)
  - only_verify: verify 有但 live 没有 (缺漏)
  - overlap_pct: overlap / (live | verify)
  - as_of: 数据时间点 (用于缓存 key)

不做:
  - 不算 weight drift (持仓权重 vs target 权重)
  - 不算时间 drift (fill 时机 vs verify 时机)
  - 不存历史 drift (每次实时算, caller 决定缓存与否)
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))


@dataclass
class DriftReport:
    strategy_id: str
    window_days: int
    live_symbols: list[str]
    verify_symbols: list[str]
    overlap: list[str]
    only_live: list[str]
    only_verify: list[str]
    overlap_pct: float          # 0.0 ~ 1.0
    fill_count: int             # 用于 "样本量过少" 提示
    verify_run_id: str | None
    as_of: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _recent_live_symbols(data_dir: Path, strategy_id: str, window_days: int) -> tuple[set[str], int]:
    """读 fills.jsonl, 按 strategy_id 过滤 + 时间窗, 返 (unique symbols, total fills)."""
    from app.services.trading import store

    cutoff = datetime.now(CN_TZ) - timedelta(days=window_days)
    cutoff_iso = cutoff.isoformat()
    symbols: set[str] = set()
    count = 0
    fills = store.list_fills(data_dir, limit=None, strategy_id=strategy_id)
    for f in fills:
        if f.filled_at and f.filled_at >= cutoff_iso:
            symbols.add(f.symbol)
            count += 1
    return symbols, count


def _latest_verify_target(data_dir: Path, strategy_id: str) -> tuple[set[str], str | None]:
    """读最新 verify 报告的 last_rebalance_target. 找不到返 ({}, None)."""
    from app.services.trading import verifier

    reports = verifier.list_verifications(data_dir, strategy_id)
    if not reports:
        return set(), None
    latest = reports[0]
    target = latest.get("last_rebalance_target") or {}
    run_id = latest.get("run_id")
    return set(target.keys()), run_id


def compute_drift(
    data_dir: Path, strategy_id: str, window_days: int = 7
) -> DriftReport:
    """算 live vs verify drift.

    Args:
        data_dir: 仓库 data_dir.
        strategy_id: 策略 id.
        window_days: live fills 的时间窗 (默认 7 天).

    Returns:
        DriftReport.
    """
    live_set, fill_count = _recent_live_symbols(data_dir, strategy_id, window_days)
    verify_set, run_id = _latest_verify_target(data_dir, strategy_id)

    overlap = sorted(live_set & verify_set)
    only_live = sorted(live_set - verify_set)
    only_verify = sorted(verify_set - live_set)
    union_size = len(live_set | verify_set)
    overlap_pct = (len(overlap) / union_size) if union_size > 0 else 1.0

    return DriftReport(
        strategy_id=strategy_id,
        window_days=window_days,
        live_symbols=sorted(live_set),
        verify_symbols=sorted(verify_set),
        overlap=overlap,
        only_live=only_live,
        only_verify=only_verify,
        overlap_pct=overlap_pct,
        fill_count=fill_count,
        verify_run_id=run_id,
        as_of=datetime.now(CN_TZ).isoformat(timespec="seconds"),
    )


__all__ = ["DriftReport", "compute_drift"]
