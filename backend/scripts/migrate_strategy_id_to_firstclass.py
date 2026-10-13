"""Migrate orders.jsonl / fills.jsonl / logs.jsonl to add strategy_id as first-class field.

For each existing Order/Fill/TradeLogEntry record:
  - Order: if strategy_id is None, derive from note via extract_strategy_id()
  - Fill:  if strategy_id is None, look up the linked order and inherit
  - Log:   if strategy_id is None, look up the linked order (or fill -> order) and inherit

Behavior:
  - Atomic: writes to .tmp then renames. Originals are backed up to *.<ts>.bak first.
  - Idempotent: re-running is a no-op (already-set strategy_id won't be overwritten).
  - Reports: prints counts (N filled / M unmatched / K errors).

Usage:
  python -m scripts.migrate_strategy_id_to_firstclass --data-dir /path/to/data [--confirm]

Without --confirm it runs in dry-run mode (prints diff but does NOT write).
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

# allow running from backend dir
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.trading.models import Fill, Order, TradeLogEntry  # noqa: E402
from app.services.trading.store import _STRATEGY_NOTE_RE  # noqa: E402

logger = logging.getLogger(__name__)

BACKUP_SUFFIX = ".{ts}.bak"

# exact paths relative to data_dir
ORDERS = "user_data/trading/orders.jsonl"
FILLS = "user_data/trading/fills.jsonl"
LOGS = "user_data/trading/logs.jsonl"


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _back_up(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = _ts()
    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX.format(ts=ts))
    shutil.copy2(path, backup)
    return backup


def _resolve_strategy_id_from_note(note: str | None) -> str | None:
    if not note:
        return None
    m = _STRATEGY_NOTE_RE.match(note)
    return m.group(1) if m else None


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append((line_no, json.loads(line)))
        except json.JSONDecodeError as e:
            logger.warning("skip malformed line %s:%d: %s", path, line_no, e)
    return out


def _atomic_write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


def migrate_orders(path: Path, *, dry_run: bool) -> tuple[int, int, int]:
    """Returns (filled, unmatched, errors)."""
    raw = _load_jsonl(path)
    order_strategy: dict[str, str | None] = {}
    filled = unmatched = errors = 0
    out_records: list[dict] = []
    for line_no, rec in raw:
        try:
            order = Order(**rec)
        except Exception as e:
            errors += 1
            logger.warning("Order parse failed at %s:%d: %s", path, line_no, e)
            out_records.append(rec)
            continue
        if order.strategy_id is None:
            sid = _resolve_strategy_id_from_note(order.note)
            if sid is not None:
                order = order.model_copy(update={"strategy_id": sid})
                filled += 1
            else:
                unmatched += 1
        order_strategy[order.id] = order.strategy_id
        out_records.append(order.model_dump(mode="json"))
    if not dry_run and filled > 0:
        _atomic_write_jsonl(path, out_records)
    return filled, unmatched, errors


def migrate_fills(path: Path, order_strategy: dict[str, str | None], *, dry_run: bool) -> tuple[int, int, int]:
    raw = _load_jsonl(path)
    filled = unmatched = errors = 0
    out_records: list[dict] = []
    for line_no, rec in raw:
        try:
            fill = Fill(**rec)
        except Exception as e:
            errors += 1
            logger.warning("Fill parse failed at %s:%d: %s", path, line_no, e)
            out_records.append(rec)
            continue
        if fill.strategy_id is None:
            sid = order_strategy.get(fill.order_id)
            if sid is not None:
                fill = fill.model_copy(update={"strategy_id": sid})
                filled += 1
            else:
                unmatched += 1
        out_records.append(fill.model_dump(mode="json"))
    if not dry_run and filled > 0:
        _atomic_write_jsonl(path, out_records)
    return filled, unmatched, errors


def migrate_logs(path: Path, order_strategy: dict[str, str | None], *, dry_run: bool) -> tuple[int, int, int]:
    raw = _load_jsonl(path)
    filled = unmatched = errors = 0
    out_records: list[dict] = []
    for line_no, rec in raw:
        try:
            entry = TradeLogEntry(**rec)
        except Exception as e:
            errors += 1
            logger.warning("TradeLogEntry parse failed at %s:%d: %s", path, line_no, e)
            out_records.append(rec)
            continue
        if entry.strategy_id is None and entry.order_id is not None:
            sid = order_strategy.get(entry.order_id)
            if sid is not None:
                entry = entry.model_copy(update={"strategy_id": sid})
                filled += 1
            else:
                unmatched += 1
        elif entry.strategy_id is None and entry.order_id is None:
            # log 没有关联 order (e.g. 启动诊断类) — 留 None
            unmatched += 1
        out_records.append(entry.model_dump(mode="json"))
    if not dry_run and filled > 0:
        _atomic_write_jsonl(path, out_records)
    return filled, unmatched, errors


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, required=True, help="项目 data/ 根目录")
    p.add_argument("--confirm", action="store_true", help="实际写入; 不传 = dry-run 模式")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG level logging")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.confirm:
        logger.warning("LIVE mode — will modify files in %s", args.data_dir)
    else:
        logger.warning("DRY-RUN mode — no files modified (pass --confirm to write)")

    data_dir: Path = args.data_dir
    orders_path = data_dir / ORDERS
    fills_path = data_dir / FILLS
    logs_path = data_dir / LOGS

    files = [orders_path, fills_path, logs_path]
    if args.confirm:
        for f in files:
            if f.exists():
                backup = _back_up(f)
                if backup:
                    logger.info("backup: %s -> %s", f, backup)

    # Order first — fills/logs need the order_id -> strategy_id map
    o_filled, o_unmatched, o_errors = migrate_orders(orders_path, dry_run=not args.confirm)
    logger.info("orders.jsonl: filled=%d unmatched=%d errors=%d", o_filled, o_unmatched, o_errors)

    # Build map from raw file (in case we just wrote and re-loaded)
    order_strategy: dict[str, str | None] = {}
    for line_no, rec in _load_jsonl(orders_path):
        try:
            o = Order(**rec)
            order_strategy[o.id] = o.strategy_id
        except Exception:
            continue

    f_filled, f_unmatched, f_errors = migrate_fills(fills_path, order_strategy, dry_run=not args.confirm)
    logger.info("fills.jsonl:  filled=%d unmatched=%d errors=%d", f_filled, f_unmatched, f_errors)

    l_filled, l_unmatched, l_errors = migrate_logs(logs_path, order_strategy, dry_run=not args.confirm)
    logger.info("logs.jsonl:   filled=%d unmatched=%d errors=%d", l_filled, l_unmatched, l_errors)

    total_filled = o_filled + f_filled + l_filled
    total_unmatched = o_unmatched + f_unmatched + l_unmatched
    total_errors = o_errors + f_errors + l_errors
    logger.info("==== SUMMARY ====")
    logger.info("total filled (strategy_id set):   %d", total_filled)
    logger.info("total unmatched (left as None):   %d", total_unmatched)
    logger.info("total errors (parse failures):    %d", total_errors)
    if not args.confirm:
        logger.info("(dry-run; pass --confirm to apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
