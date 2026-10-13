"""合并 kline_minute 新嵌套布局（symbol=XXX/part.parquet）回单文件 part.parquet。

背景: 2026-08 之后, minute_refresh 的全天修复轮把每个 (date, symbol) 落盘成单独文件,
导致 data/kline_minute 累积到 188,899 个 parquet。DuckDB / Polars 在启动时用
**/*.parquet glob 枚举会非常慢 (实测 11s 仅 CREATE VIEW, lifespan 累计 80+ s,
整个 startup 3-5 min)。

每日期原子迁移:
  date=X/symbol=* (NEW) → date=X/.staging_old (rename)
  date=X/.staging_old → polars lazy union → date=X/part.parquet.new (写)
  校验行数 == sum(staging_old rows)
  date=X/part.parquet.new → date=X/part.parquet (rename)
  rm -rf date=X/.staging_old

如果混合布局 (OLD + NEW 同存于一个 date=X), 旧 part.parquet 不参与合并,
但会备份到 date=X/part.parquet.bak-<ts> 以防回滚需要。

干跑模式 --dry-run 只打印计划不动数据。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

BASE = Path("/Volumes/quant-ssd/projects/tickflow-stock-panel/data/kline_minute")


def plan(base: Path) -> list[dict]:
    out: list[dict] = []
    for dd in sorted(base.glob("date=*")):
        if not dd.is_dir():
            continue
        date = dd.name[5:]
        sym_files = sorted(dd.glob("symbol=*/part.parquet"))
        old_part = dd / "part.parquet"
        out.append({
            "date": date,
            "dir": dd,
            "n_sym_files": len(sym_files),
            "has_old_part": old_part.exists(),
        })
    return out


def consolidate_one(dd: Path, dry_run: bool) -> dict:
    """原子化合并单个日期分区。

    返回 dict 含新文件行数 / 校验状态 / 耗时。
    """
    started = time.perf_counter()
    sym_files = sorted(dd.glob("symbol=*/part.parquet"))
    old_part = dd / "part.parquet"
    stat: dict = {
        "date": dd.name[5:],
        "n_sym_files": len(sym_files),
        "old_part_existed": old_part.exists(),
        "staged_rows": 0,
        "written_rows": 0,
        "ok": False,
        "skipped": False,
        "elapsed_s": 0.0,
        "msg": "",
    }

    if not sym_files:
        stat["skipped"] = True
        stat["msg"] = "no symbol= files"
        stat["elapsed_s"] = time.perf_counter() - started
        return stat

    # 1. 计算源行数 (lazy collect, 不读整张表)
    src_rows = int(
        pl.scan_parquet([str(p) for p in sym_files]).select(pl.len()).collect().item()
    )
    stat["staged_rows"] = src_rows

    staging = dd / ".staging_old"
    new_file = dd / "part.parquet.new"

    if dry_run:
        stat["ok"] = True
        stat["msg"] = "dry-run; would rename + write + cleanup"
        stat["elapsed_s"] = time.perf_counter() - started
        return stat

    # 2. 把 symbol=* 移走 (原子 rename, 不复制数据)
    if staging.exists():
        raise RuntimeError(f"staging 残留: {staging}")
    # macOS / POSIX: 不能把 dir rename 为自身的子目录。
    # 把 date=X 下的所有 symbol=* 子目录 rename 到 .staging_old 子目录;
    # .staging_old 与 part.parquet (如果有) 平级。
    for sd in dd.iterdir():
        if sd.is_dir() and sd.name.startswith("symbol="):
            shutil.move(str(sd), str(staging / sd.name))
        else:
            # 非 symbol= 内容 (例如遗留 .DS_Store) 暂留原位 — 不影响新 part.parquet
            pass

    # 4. 流式聚合, 写到 part.parquet.new
    # NEW 文件 schema: ['symbol', 'datetime', 'close', 'open', 'high', 'low', 'volume', 'amount', 'date']
    # OLD part.parquet (可能并存) schema: ['symbol', 'datetime', 'open', 'high', 'low', 'close', 'volume', 'amount']
    # 用 pl.concat (diagonal_relaxed) 自动对齐 — 多余列填 null, 缺列补 null
    sym_files_after = sorted(staging.glob("symbol=*/part.parquet"))
    if not sym_files_after:
        shutil.rmtree(staging)
        stat["skipped"] = True
        stat["msg"] = "rename 后无 symbol 文件"
        stat["elapsed_s"] = time.perf_counter() - started
        return stat
    lf = pl.scan_parquet([str(p) for p in sym_files_after])
    df = lf.collect(engine="streaming")

    # 校验: union 出来的新 df 行数 == staging rows
    if df.height != src_rows:
        # 失败: 清理
        shutil.rmtree(work)
        stat["ok"] = False
        stat["msg"] = f"row mismatch: src={src_rows} got={df.height}"
        stat["elapsed_s"] = time.perf_counter() - started
        return stat

    # 5. 写新文件 (用 snappy 压缩, polars 默认 zstd 也可, 选 zstd 与仓库其他表一致)
    df.write_parquet(new_file, compression="zstd")
    written_rows = pl.read_parquet(new_file).height
    stat["written_rows"] = written_rows

    if written_rows != src_rows:
        # 写盘后 read-back 不一致 — 极端异常 (zstd 写不该丢行)
        new_file.unlink()
        shutil.rmtree(work)
        stat["ok"] = False
        stat["msg"] = f"write-back mismatch: src={src_rows} written={written_rows}"
        stat["elapsed_s"] = time.perf_counter() - started
        return stat

    # 6. 备份 OLD part.parquet (如果存在) 到 .bak-<ts>
    if old_part.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = dd / f"part.parquet.bak-{ts}"
        shutil.move(str(old_part), str(bak))
        stat["old_part_backed_up_to"] = str(bak)

    # 7. 原子 rename: part.parquet.new → part.parquet
    new_file.rename(old_part)

    # 8. 删 staging (整目录树秒级)
    shutil.rmtree(staging)

    stat["ok"] = True
    stat["msg"] = "merged"
    stat["elapsed_s"] = time.perf_counter() - started
    return stat


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    parser.add_argument("--date", help="只处理单个日期 (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个日期 (0=全部)")
    parser.add_argument("--keep-going", action="store_true", help="失败时继续下一个日期")
    args = parser.parse_args()

    items = plan(BASE)
    if args.date:
        items = [it for it in items if it["date"] == args.date]
    if args.limit > 0:
        items = items[: args.limit]

    if not items:
        print("no matching dates", file=sys.stderr)
        return 1

    total_sym_files = sum(it["n_sym_files"] for it in items)
    print(f"plan: {len(items)} dates, {total_sym_files} symbol files")

    if args.dry_run:
        for it in items[:20]:
            print(f"  date={it['date']}  sym_files={it['n_sym_files']}  old_part={it['has_old_part']}")
        if len(items) > 20:
            print(f"  ... {len(items) - 20} more")
        return 0

    ok = 0
    skipped = 0
    failed = 0
    started = time.perf_counter()
    for i, it in enumerate(items, 1):
        prefix = f"[{i}/{len(items)}] date={it['date']} sym={it['n_sym_files']}"
        try:
            r = consolidate_one(it["dir"], dry_run=False)
            if r.get("skipped"):
                skipped += 1
                print(f"{prefix}  SKIP ({r['msg']})")
            elif r["ok"]:
                ok += 1
                print(f"{prefix}  OK rows={r['written_rows']:,} elapsed={r['elapsed_s']:.1f}s")
            else:
                failed += 1
                print(f"{prefix}  FAIL: {r['msg']}", file=sys.stderr)
                if not args.keep_going:
                    return 2
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"{prefix}  EXC: {type(e).__name__}: {e}", file=sys.stderr)
            if not args.keep_going:
                return 3

    print()
    print(f"done: ok={ok} skipped={skipped} failed={failed} elapsed={time.perf_counter() - started:.1f}s")
    return 0 if failed == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
