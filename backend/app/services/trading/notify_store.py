"""通知中心 — 落盘 + 读 + SSE 推.

聚宽式 send_message + 自动通知 (委托/成交/拒单/17:00 清算/失败重试).

落盘: data/user_data/trading/notify.jsonl
读: list_recent(limit, kind)
写: append(kind, text, source) -> entry
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))

VALID_KINDS = {
    "info", "success", "warn", "error",
    "order_submitted", "order_filled", "order_rejected", "order_cleared",
    "clearance", "retry", "strategy_run", "user_message",
}


def _path(data_dir: Path) -> Path:
    p = data_dir / "user_data" / "trading" / "notify.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def append(data_dir: Path, kind: str, text: str, source: str = "system",
          extra: dict[str, Any] | None = None) -> dict:
    """追加一条通知. 返回 entry dict (含 ts)."""
    if kind not in VALID_KINDS:
        kind = "info"
    entry = {
        "ts": datetime.now(CN_TZ).isoformat(),
        "kind": kind,
        "text": text,
        "source": source,
    }
    if extra:
        entry["extra"] = extra
    p = _path(data_dir)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def list_recent(data_dir: Path, limit: int = 100, kind: str | None = None,
                since: str | None = None) -> list[dict]:
    """读最近 N 条. 可按 kind / since 过滤."""
    p = _path(data_dir)
    if not p.exists():
        return []
    items: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if kind and rec.get("kind") != kind:
            continue
        if since and rec.get("ts", "") < since:
            continue
        items.append(rec)
    items.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return items[:limit]


def reset_for_tests() -> None:
    """测试用."""
    pass


__all__ = ["VALID_KINDS", "append", "list_recent", "reset_for_tests"]
