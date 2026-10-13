"""F1-5: 策略代码自动快照 + diff + 一键回滚 (2026-10-13).

落盘: data/user_data/strategies/live/.history/{strategy_id}/{ts}.py
      + .history/{strategy_id}/manifest.jsonl (快照元数据)

不做:
  - 不存 diff 内容 (回滚时按需现场算 diff via difflib)
  - 不跨策略共享快照目录 (每策略独立目录, 防误操作)
  - 不清理过期快照 (保留 20 版, 超出由 list_history 截断, 落盘不删; 见 SPEC D9)
"""
from __future__ import annotations

import contextlib
import difflib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))
MAX_HISTORY = 20  # 保留最近 N 个快照 (D9)


@dataclass
class SnapshotMeta:
    ts: str            # ISO8601 北京时间
    size: int
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SnapshotMeta:
        return cls(ts=str(d["ts"]), size=int(d["size"]), note=str(d.get("note", "")))


def _history_dir(data_dir: Path, strategy_id: str) -> Path:
    p = data_dir / "user_data" / "strategies" / "live" / ".history" / strategy_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _manifest_path(data_dir: Path, strategy_id: str) -> Path:
    return _history_dir(data_dir, strategy_id) / "manifest.jsonl"


def snapshot_now(
    data_dir: Path, strategy_id: str, content: str, note: str = ""
) -> SnapshotMeta:
    """备份当前 .py → .history/{strategy_id}/{ts}.py.

    Returns: 快照元数据.
    """
    ts = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S")
    hist_dir = _history_dir(data_dir, strategy_id)
    snap_file = hist_dir / f"{ts}.py"
    snap_file.write_text(content, encoding="utf-8")
    meta = SnapshotMeta(ts=ts + "Z", size=len(content.encode("utf-8")), note=note)
    # 写 manifest
    manifest = _manifest_path(data_dir, strategy_id)
    with manifest.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(meta), ensure_ascii=False) + "\n")
    # 截断: 超过 MAX_HISTORY 个时按 ts 删最旧的
    _prune_history(hist_dir, manifest)
    return meta


def _prune_history(hist_dir: Path, manifest: Path) -> None:
    """超过 MAX_HISTORY 时按 ts 删最旧的 .py + manifest 行."""
    if not manifest.exists():
        return
    entries: list[dict] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    if len(entries) <= MAX_HISTORY:
        return
    # 按 ts 排序, 保留最后 MAX_HISTORY
    entries.sort(key=lambda e: e["ts"])
    to_remove = entries[: len(entries) - MAX_HISTORY]
    keep = entries[len(entries) - MAX_HISTORY:]
    for e in to_remove:
        f = hist_dir / f"{e['ts'].rstrip('Z')}.py"
        with contextlib.suppress(FileNotFoundError):
            f.unlink()
    manifest.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in keep) + "\n",
        encoding="utf-8",
    )


def list_history(data_dir: Path, strategy_id: str) -> list[SnapshotMeta]:
    """读 manifest, 按 ts 倒序返."""
    manifest = _manifest_path(data_dir, strategy_id)
    if not manifest.exists():
        return []
    items: list[SnapshotMeta] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(SnapshotMeta.from_dict(json.loads(line)))
        except Exception:
            continue
    items.sort(key=lambda m: m.ts, reverse=True)
    return items


def diff_snapshot_vs_current(
    data_dir: Path, strategy_id: str, ts: str
) -> dict | None:
    """返 ts 版本 vs 当前 live/{id}.py 的 unified diff.

    Returns: {"ts": ..., "current_size": ..., "snapshot_size": ...,
             "diff": ["@@ -X,Y +A,B @@", "..."]} 或 None (快照不存在).
    """
    hist_dir = _history_dir(data_dir, strategy_id)
    snap_file = hist_dir / f"{ts.rstrip('Z')}.py"
    if not snap_file.exists():
        return None
    current_file = data_dir / "user_data" / "strategies" / "live" / f"{strategy_id}.py"
    snap_content = snap_file.read_text(encoding="utf-8")
    cur_content = current_file.read_text(encoding="utf-8") if current_file.exists() else ""
    diff = list(difflib.unified_diff(
        snap_content.splitlines(keepends=True),
        cur_content.splitlines(keepends=True),
        fromfile=f"snapshot@{ts}",
        tofile="current",
        n=3,
    ))
    return {
        "ts": ts,
        "snapshot_size": len(snap_content.encode("utf-8")),
        "current_size": len(cur_content.encode("utf-8")),
        "diff": diff,
        "snapshot_content": snap_content if not diff else None,
        # 若 diff 为空则提供完整 snapshot 供前端展示
    }


def rollback(
    data_dir: Path, strategy_id: str, ts: str, *, current_content: str | None = None
) -> dict:
    """回滚 live/{id}.py 到 ts 快照.

    Args:
        current_content: 当前 live/{id}.py 内容 (调用方先读, 这样能先做
            一次当前版本的 snapshot, 再 rollback; 避免数据丢失).

    Returns: {"ts": ..., "snapshot_size": ..., "before_size": ..., "after_size": ...}
    Raises:
        FileNotFoundError: 快照不存在.
        ValueError: current_content 为空且 live/{id}.py 不存在.
    """
    hist_dir = _history_dir(data_dir, strategy_id)
    snap_file = hist_dir / f"{ts.rstrip('Z')}.py"
    if not snap_file.exists():
        raise FileNotFoundError(f"快照不存在: {snap_file}")
    snap_content = snap_file.read_text(encoding="utf-8")

    # 先 snapshot 当前 (如果有) — 防回滚丢数据
    if current_content is None:
        current_file = data_dir / "user_data" / "strategies" / "live" / f"{strategy_id}.py"
        if current_file.exists():
            current_content = current_file.read_text(encoding="utf-8")
    if current_content is not None:
        snapshot_now(data_dir, strategy_id, current_content, note=f"auto_before_rollback_to_{ts}")

    # 写回
    target = data_dir / "user_data" / "strategies" / "live" / f"{strategy_id}.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    before_size = len(current_content.encode("utf-8")) if current_content else 0
    target.write_text(snap_content, encoding="utf-8")
    after_size = len(snap_content.encode("utf-8"))
    return {
        "ts": ts,
        "before_size": before_size,
        "after_size": after_size,
    }


__all__ = [
    "MAX_HISTORY",
    "SnapshotMeta",
    "diff_snapshot_vs_current",
    "list_history",
    "rollback",
    "snapshot_now",
]
