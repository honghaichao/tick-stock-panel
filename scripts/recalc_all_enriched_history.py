#!/usr/bin/env python3
"""全部修正 enriched parquet + mainline_history。

为什么走 compute_enriched (写盘路径) 而非 compute_enriched_history_window:
- compute_enriched_history_window 不调 _apply_adj_factor, 没法修 OHLC=null (Tushare
  ex_factor 累积到 inf 的股票 — 300006.SZ 等).
- compute_enriched 走 _apply_adj_factor, 我们已修 ratio 兜底 (ratio=is_finite 否则 1.0),
  OHLC 完整.
- raw 从 kline_daily 读 (8 列无前复权污染), 写完整 partition。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
import time
from pathlib import Path

# 保证从项目根目录运行 (脚本 cwd 可能是 backend/)
script_path = Path(__file__).resolve()
project_root = script_path.parent
if Path.cwd() != project_root:
    # 但如果 cwd 是 backend, project_root 是 /Volumes/.../tickflow-stock-panel
    # data 应该在 <project_root>/data/
    if (project_root / 'data' / 'kline_daily_enriched').exists():
        os.chdir(project_root)
    elif (project_root.parent / 'data' / 'kline_daily_enriched').exists():
        # cwd 是 backend, 找上层
        os.chdir(project_root.parent)

sys.path.insert(0, 'backend')

import polars as pl

BACKUP_DIR_TMPL = 'data/.bak_pre_recalc_all_{ts}'
ENRICHED_DIR = Path('data/kline_daily_enriched')
MAINLINE_PATH = Path('data/mainline_history/part.parquet')
ATOMIC_RETRIES = 10
ATOMIC_DELAY = 0.5
SYMBOL_BATCH = 200


def _atomic_replace(src: Path, dst: Path) -> None:
    last: Exception | None = None
    for _ in range(ATOMIC_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last = e
            time.sleep(ATOMIC_DELAY)
    raise last  # type: ignore


def _atomic_write_parquet(df: pl.DataFrame, out: Path) -> None:
    tmp = out.with_name(out.name + '.tmp')
    df.write_parquet(tmp)
    _atomic_replace(tmp, out)


def _now_ts() -> str:
    return dt.datetime.now().strftime('%Y%m%d_%H%M%S')


def _check_continue(prompt: str) -> None:
    print(prompt)
    ans = input('Continue? [yes/NO]: ').strip().lower()
    if ans != 'yes':
        print('Aborted by user.')
        sys.exit(1)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2020-01-02')
    ap.add_argument('--end', default='2026-10-09')
    ap.add_argument('--symbol-batch', type=int, default=SYMBOL_BATCH)
    ap.add_argument('--skip-mainline', action='store_true')
    ap.add_argument('--mainline-only', action='store_true')
    return ap.parse_args()


def _phase_a_fix_enriched(repo, ds, args, start: dt.date, end: dt.date,
                          backup_root: Path) -> None:
    """按 symbol 批跑全 enriched 重算 (走写盘路径 compute_enriched: 修 ratio + OHLC + 其它 14 列)."""
    print(f'\nPhase A: enriched parquet rebuild (per-symbol batch)')
    inst = repo.get_instruments_asset('stock')
    hs = repo.get_historical_shares()
    from app.indicators.pipeline import compute_enriched, _select_storage_cols, _load_factors
    factors_all = _load_factors(ds.data_dir / "adj_factor" / "all.parquet")

    # 1) 列出全部 symbols (从 enriched 全量)
    sym_lf = pl.scan_parquet(str(ENRICHED_DIR / '**' / 'part.parquet')).select('symbol').unique().sort('symbol')
    symbols = sym_lf.collect()['symbol'].to_list()
    print(f'  universe: {len(symbols)} stocks')

    s = start.isoformat()
    e = end.isoformat()

    # 2) 预读全 kline_daily (限定 start/end) → 8 列无前复权污染
    print(f'  loading daily raw from {s} ~ {e}...')
    t_load = time.time()
    daily_files = []
    for ddir in (ds.data_dir / 'kline_daily').iterdir():
        if not (ddir.is_dir() and ddir.name.startswith('date=')):
            continue
        ds_str = ddir.name[5:]
        if s <= ds_str <= e:
            for f in ddir.glob('part.parquet'):
                daily_files.append(f)
    daily_files.sort()
    raws = []
    for f in daily_files:
        try:
            x = pl.read_parquet(f, columns=['symbol','date','open','high','low','close','volume','amount'])
            if x.height:
                # daily 'date' 是 String → 转 Date
                if x.schema['date'] == pl.Utf8:
                    x = x.with_columns(pl.col('date').str.to_date('%Y-%m-%d', strict=False).alias('date'))
                raws.append(x)
        except Exception:
            pass
    raw_all = pl.concat(raws, how='diagonal_relaxed') if raws else pl.DataFrame()
    print(f'  raw loaded: {raw_all.height:,} rows, {time.time()-t_load:.1f}s')

    n_batches = (len(symbols) + args.symbol_batch - 1) // args.symbol_batch
    t_total = time.time()
    written = 0
    for bi in range(0, len(symbols), args.symbol_batch):
        batch = symbols[bi:bi + args.symbol_batch]
        bno = bi // args.symbol_batch + 1
        t0 = time.time()
        if raw_all.is_empty():
            df_new = pl.DataFrame()
        else:
            # 走 compute_enriched — ratio 兜底 + alias raw_*
            df_new = _select_storage_cols(compute_enriched(
                raw_all.filter(pl.col('symbol').is_in(batch)),
                factors=factors_all,
                instruments=inst, historical_shares=hs,
            ))
        if df_new.is_empty():
            print(f'  batch {bno}/{n_batches} ({len(batch)} syms): empty df, skip')
            continue
        # 3) 按 date 分区: 读原表, join 覆盖, atomic 写完整 14 列
        date_strs = df_new['date'].dt.strftime('%Y-%m-%d').unique().sort().to_list()
        for ds_str in date_strs:
            parquet_path = ENRICHED_DIR / f'date={ds_str}' / 'part.parquet'
            if not parquet_path.exists():
                continue
            old = pl.read_parquet(parquet_path)
            df_day = df_new.filter(pl.col('date').dt.strftime('%Y-%m-%d') == ds_str)
            if df_day.is_empty():
                continue
            # batch 的 200 stocks 完整 14 列覆盖到 old 中
            joined = old.join(df_day, on='symbol', how='left', suffix='_new')
            update_cols = [c for c in df_day.columns if c != 'symbol']
            coalesce_exprs = []
            for c in update_cols:
                col_ne = f'{c}_new'
                if col_ne in joined.columns:
                    coalesce_exprs.append(pl.coalesce([pl.col(col_ne), pl.col(c)]).alias(c))
            joined = joined.with_columns(coalesce_exprs).select(old.columns)
            _atomic_write_parquet(joined, parquet_path)
            written += 1
        print(f'  batch {bno}/{n_batches} ({len(batch)} syms): '
              f'compute {time.time()-t0:.1f}s, fixed {len(date_strs)} dates '
              f'(total files written: {written})')
    print(f'  Phase A done. {written} enriched files written in {time.time()-t_total:.0f}s')


def _phase_b_fix_mainline(repo, ds, start: dt.date, end: dt.date) -> None:
    print(f'\nPhase B: mainline_history full recompute')
    from app.services.market_mainline import compute_mainline_range, mainline_path
    from app.services.regime_builder import enriched_date_set
    all_dates = enriched_date_set(repo)
    ml_dates = sorted(d for d in all_dates if start <= d <= end)
    print(f'  computing for {len(ml_dates)} days × 2 kinds...')
    t0 = time.time()
    all_rows = []
    for kind in ('concept', 'industry'):
        rows = compute_mainline_range(repo, ds.data_dir, ml_dates[0], ml_dates[-1], kind=kind)
        if not rows.is_empty():
            all_rows.append(rows)
            print(f'    {kind}: {rows.height} rows')
    if not all_rows:
        print('  no mainline data, skip')
        return
    combined = pl.concat(all_rows, how='vertical_relaxed').sort(['date', 'kind', 'rank'])
    out = mainline_path(ds.data_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(combined, out)
    print(f'  wrote mainline_history/part.parquet, {combined.height} rows')


def main() -> int:
    args = _parse_args()
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    backup_ts = _now_ts()
    backup_root = Path(BACKUP_DIR_TMPL.format(ts=backup_ts))
    backup_root.mkdir(parents=True, exist_ok=False)

    print(f'== 全部修正 enriched history (compute_enriched 写盘路径, 修 OHLC null) ==')
    print(f'  range: {start} ~ {end}')
    print(f'  symbol batch: {args.symbol_batch}')
    print(f'  backup: {backup_root}')
    if not args.mainline_only:
        print(f'  Will overwrite ~1637 enriched parquet files (full 14 cols, atomic)')
    if not args.skip_mainline:
        print(f'  Will regenerate mainline_history/part.parquet')
    _check_continue(f'⚠ 批量覆盖 enriched + mainline (本脚本为 explicit-confirmation 操作, 已备份到 {backup_root})')

    from app.tickflow.repository import DataStore, KlineRepository
    ds = DataStore()
    repo = KlineRepository(ds)

    # Phase 0: 备份
    # 备份粒度按"是否会被本脚本覆盖"判断, 不与 --mainline-only / 默认 full-run 强绑定:
    #   - enriched 仅 Phase A 重写 → 仅在非 mainline-only 时备份
    #   - mainline Phase B 在两种模式下都会重写 → 一律备份 (2026-10-12 fix:
    #     原条件 `not args.mainline_only` 会跳过 mainline 备份, 留下空目录)
    if not args.mainline_only and ENRICHED_DIR.exists():
        bak = backup_root / 'kline_daily_enriched'
        if bak.exists():
            shutil.rmtree(bak)
        shutil.copytree(ENRICHED_DIR, bak, symlinks=True, ignore=shutil.ignore_patterns('*.tmp'))
        print(f'  backup enriched -> {bak}')
    if not args.skip_mainline and MAINLINE_PATH.exists():
        bak = backup_root / 'mainline_history_part.parquet'
        shutil.copy2(MAINLINE_PATH, bak)
        print(f'  backup mainline -> {bak}')

    if not args.mainline_only:
        _phase_a_fix_enriched(repo, ds, args, start, end, backup_root)
    if not args.skip_mainline:
        _phase_b_fix_mainline(repo, ds, start, end)

    print(f'\n== DONE ==')
    print(f'  backup: {backup_root}')
    print(f'  建议重启后端刷新 in-memory cache')
    return 0


if __name__ == '__main__':
    sys.exit(main())
