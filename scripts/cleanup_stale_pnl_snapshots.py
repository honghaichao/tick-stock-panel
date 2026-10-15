#!/usr/bin/env python3
"""清理 data/user_data/trading/pnl_snapshots.jsonl 里的历史脏快照.

识别两类脏快照 (任一命中即剔除):

1) `market_value == 0` (session_start 早期)
   历史 bug (2026-09-29 ~ 2026-10-13): engine 启动瞬间 quote_service
   还没拉到行情, `_refresh_account_market_value` 把 `market_value=0`
   算出来落盘 → 总权益 = cash + 0 ≈ 2,603. 污染 `max_drawdown` (peak
   vs trough 看起来 -99.75%) 和收益曲线"突降到 0"毛刺. 区分"真无持仓"
   和"估值未就绪"不现实 — 前一轮已加守卫阻止新半态产生; 当前账户始终
   有持仓, jsonl 里所有 mv=0 都是脏的.

2) `cash + mv ≈ 2 × initial_cash` (双计; session_start 中段)
   session 启动后第一笔 sell 落账时, cash += proceeds 但 mv 还没刷掉
   卖出份额 → cash 和 mv 同时按"卖出前 + 卖出后"两个状态入账 → 双计.
   检测口径: 取 mv>0 快照的 `cash + mv - realized - unrealized` 中位数
   作为 estimated initial_cash (干净快照此值 = initial_cash), 命中
   `> 1.5 × initial_cash` 视为双计. 2026-10-15 10:24 ~ 10:37 的 18 条
   此类脏快照是该 bug 的历史残留, 之前没被拦下.

后续动作:
  - 备份原文件 → `pnl_snapshots.jsonl.bak.YYYYMMDDHHMMSS` (不进 git,
    7 天后人工确认可 rm).
  - 原子写 (tmp → rename) 避免半路失败留下损坏 jsonl.

用法:
  python scripts/cleanup_stale_pnl_snapshots.py --dry-run   # 只打印, 不动
  python scripts/cleanup_stale_pnl_snapshots.py --apply     # 备份 + 清理
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO_ROOT / "data" / "user_data" / "trading" / "pnl_snapshots.jsonl"


def derive_initial_cash(rows: list[dict]) -> float:
    """从 mv>0 快照反推 initial_cash.

    干净快照满足 `equity = cash + mv + total_pnl = initial_cash + total_pnl`,
    移项得 `cash + mv - realized - unrealized = initial_cash`.
    双计脏快照此值 ≈ 2 × initial_cash. 取中位数避开双计污染,
    退化场景 (无干净快照) 退到 1_000_000 默认值.
    """
    candidates: list[float] = []
    for r in rows:
        mv = r.get("market_value") or 0
        if mv > 0:
            cash = r.get("cash") or 0
            realized = r.get("realized_pnl") or 0
            unrealized = r.get("unrealized_pnl") or 0
            candidates.append(cash + mv - realized - unrealized)
    if not candidates:
        return 1_000_000.0
    candidates.sort()
    return candidates[len(candidates) // 2]


def is_double_counted(r: dict, initial_cash: float) -> bool:
    """双计检测 (rule 2). mv>0 前提下, `cash + mv - realized - unrealized`
    远大于 initial_cash → sell 后 cash 已加 proceeds, mv 还没刷卖出份额.
    """
    mv = r.get("market_value") or 0
    if mv <= 0:
        return False  # 归 rule 1 (mv=0) 处理
    cash = r.get("cash") or 0
    realized = r.get("realized_pnl") or 0
    unrealized = r.get("unrealized_pnl") or 0
    implied = cash + mv - realized - unrealized
    return implied > initial_cash * 1.5


def collect_stats(rows: list[dict], initial_cash: float) -> dict:
    """统计: 总数 / 保留 / 各类删除 (mv=0 + 双计), 按日期 / trigger 分布."""
    from collections import Counter
    out: dict = {
        "total": len(rows),
        "initial_cash_est": initial_cash,
        "kept": 0,
        "removed_mv_eq_0": 0,
        "removed_double_counted": 0,
        "by_trigger": Counter(),
        "removed_by_trigger": Counter(),
        "removed_mv_eq_0_by_date": Counter(),
        "removed_double_counted_by_date": Counter(),
    }
    for r in rows:
        out["by_trigger"][r.get("trigger", "?")] += 1
        mv = r.get("market_value")
        if mv is not None and mv > 0:
            if is_double_counted(r, initial_cash):
                out["removed_double_counted"] += 1
                out["removed_by_trigger"][r.get("trigger", "?")] += 1
                out["removed_double_counted_by_date"][r.get("trade_date", "?")] += 1
            else:
                out["kept"] += 1
        else:
            out["removed_mv_eq_0"] += 1
            out["removed_by_trigger"][r.get("trigger", "?")] += 1
            out["removed_mv_eq_0_by_date"][r.get("trade_date", "?")] += 1
    return out


def filter_rows(rows: list[dict], initial_cash: float) -> list[dict]:
    """保留合法快照. 排除两类脏快照:
    1. market_value == 0 (estimate 未就绪)
    2. cash + mv ≈ 2 × initial_cash (sell 后 mv 未刷新 → 双计)
    """
    kept: list[dict] = []
    for r in rows:
        mv = r.get("market_value") or 0
        if mv <= 0:
            continue
        if is_double_counted(r, initial_cash):
            continue
        kept.append(r)
    return kept


def run(target: Path, *, apply: bool) -> int:
    if not target.exists():
        print(f"  ! {target} 不存在, 跳过")
        return 0

    raw = target.read_text(encoding="utf-8")
    rows: list[dict] = []
    parse_err = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            parse_err += 1

    initial_cash = derive_initial_cash(rows)
    stats = collect_stats(rows, initial_cash)
    keep_rows = filter_rows(rows, initial_cash)
    total = stats["total"]
    print(f"  estimated initial_cash (median of mv>0 cash+mv-pnl): ¥{initial_cash:,.2f}")

    print(f"=== {target.name} ===")
    print(f"  总快照:        {stats['total']:>6}  (parse err: {parse_err})")
    print(f"  保留:           {stats['kept']:>6}  ({stats['kept']/max(total,1)*100:.1f}%)")
    print(f"  删除 mv=0:      {stats['removed_mv_eq_0']:>6}  ({stats['removed_mv_eq_0']/max(total,1)*100:.1f}%)")
    print(f"  删除 双计:      {stats['removed_double_counted']:>6}  ({stats['removed_double_counted']/max(total,1)*100:.1f}%)")
    print(f"  删除按 trigger (top 5):")
    for trig, n in stats["removed_by_trigger"].most_common(5):
        print(f"    {trig:>30} × {n}")
    print(f"  删除 mv=0 按 trade_date (top 5):")
    for d, n in stats["removed_mv_eq_0_by_date"].most_common(5):
        print(f"    {d} × {n}")
    print(f"  删除 双计 按 trade_date (top 5):")
    for d, n in stats["removed_double_counted_by_date"].most_common(5):
        print(f"    {d} × {n}")

    if not apply:
        print()
        print("  --dry-run 模式: 不动文件. 跑 --apply 才执行.")
        return 0

    # 备份
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    bak = target.with_name(f"{target.name}.bak.{ts}")
    shutil.copy2(target, bak)
    print(f"  备份: {bak.name} ({target.stat().st_size} bytes)")

    # 原子写: tmp + rename (rename 是原子, 同分区保证)
    tmp = target.with_name(f"{target.name}.tmp.{ts}")
    with tmp.open("w", encoding="utf-8") as f:
        # 按 ts 升序写回 (原始 jsonl 已经是这个顺序, 但 filter 重新保险一次)
        keep_rows.sort(key=lambda r: r.get("ts", ""))
        for r in keep_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(target)
    print(f"  写新文件: {target.name} ({len(keep_rows)} rows)")
    print()
    print("  ✓ 完成. 验证:")
    print(f"    curl http://127.0.0.1:3018/api/trading/pnl/metrics | jq .max_drawdown")
    print(f"    浏览器刷实盘页, 收益曲线无 2603.x 平台")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_PATH,
        help=f"pnl_snapshots.jsonl 路径 (默认: {DEFAULT_PATH})",
    )
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", action="store_true", help="只读, 打印统计, 不写文件")
    grp.add_argument("--apply", action="store_true", help="备份 + 过滤 + 原子写 (默认 --apply)")
    args = parser.parse_args()
    apply = args.apply or not args.dry_run  # 默认 apply
    return run(args.path, apply=apply)


if __name__ == "__main__":
    sys.exit(main())
