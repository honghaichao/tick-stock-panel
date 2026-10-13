#!/usr/bin/env python3
"""补齐 kline_minute 2023-03 ~ 2026-08-06 的断档。

特点:
- 自动跳过已存在日期(可中断续跑)
- 失败的窗口段记录到 fill_minute_gap.failed.txt,可后续 --failed 重跑
- 每窗口段调一次 TickFlow kline.minute.batch
- 进度日志打印到 stderr (行 <10s)

用法:
    backend/.venv/bin/python fill_minute_gap.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
    backend/.venv/bin/python fill_minute_gap.py --failed   # 只跑上次失败的段

输出:
    /tmp/ts-fill/fill_minute_gap.failed.txt — 失败的窗口
    /tmp/ts-fill/fill_minute_gap.log — 完整进度日志(可 tee)
"""
import sys, os, time
sys.path.insert(0, 'backend')

import polars as pl
from datetime import datetime, timedelta, date

FAILED_FILE = '/tmp/ts-fill/fill_minute_gap.failed.txt'

def parse_args():
    only_failed = '--failed' in sys.argv
    start = end = None
    args = sys.argv[1:]
    # 支持 --start=YYYY-MM-DD 或 --start YYYY-MM-DD
    for i, a in enumerate(args):
        if a == '--start' and i+1 < len(args):
            start = args[i+1]
        elif a.startswith('--start='):
            start = a.split('=', 1)[1]
        elif a == '--end' and i+1 < len(args):
            end = args[i+1]
        elif a.startswith('--end='):
            end = a.split('=', 1)[1]
    return start, end, only_failed

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] {msg}', file=sys.stderr, flush=True)

def main():
    user_start, user_end, only_failed = parse_args()
    log(f'start (user_start={user_start} user_end={user_end} only_failed={only_failed})')

    from app.tickflow.repository import DataStore, KlineRepository
    from app.tickflow.policy import detect_capabilities
    from app.services import kline_sync
    from app.services.minute_adjust import minute_basis_is_raw

    store = DataStore()
    repo = KlineRepository(store)
    capset = detect_capabilities(force=False)

    df = pl.read_parquet('data/kline_daily/date=2026-09-30/part.parquet', columns=['symbol'])
    symbols = sorted(df['symbol'].unique().to_list())
    log(f'symbols: {len(symbols)}')

    # 真实交易日
    import glob
    trade_dates = sorted(
        os.path.basename(p).replace('date=','')
        for p in glob.glob('data/kline_daily/date=*')
    )
    trade_set = set(trade_dates)
    log(f'trade_dates: {len(trade_dates)}')

    if only_failed:
        if not os.path.exists(FAILED_FILE):
            log('no failed file, exit')
            return 0
        failed_ranges = []
        with open(FAILED_FILE) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                parts = line.split('|')
                if len(parts) == 2:
                    failed_ranges.append((parts[0], parts[1]))
        log(f'failed_ranges: {len(failed_ranges)}')
        ranges = failed_ranges
    else:
        # 计算缺口
        have_set = set(
            os.path.basename(p).replace('date=','')
            for p in glob.glob('data/kline_minute/date=*')
        )
        miss = sorted(d for d in trade_dates if d not in have_set)
        if user_start:
            miss = [d for d in miss if d >= user_start]
        if user_end:
            miss = [d for d in miss if d <= user_end]
        log(f'missing_dates in range: {len(miss)}')

        # 按 30 天窗口分组, 跳过 TickFlow API 不支持的日期 (<2025-08-01)
        API_MIN_DATE = '2025-08-01'
        miss = [d for d in miss if d >= API_MIN_DATE]
        skipped_old = len([d for d in (sorted(d for d in trade_dates if d not in have_set) if user_start is None else []) if d < API_MIN_DATE])
        if skipped_old:
            log(f'跳过 API 不支持范围 (<{API_MIN_DATE}): {skipped_old} 天')
        ranges = []
        if miss:
            cur_start = date.fromisoformat(miss[0])
            cur_end = cur_start
            for d in miss[1:]:
                dd = date.fromisoformat(d)
                if (dd - cur_end).days > 1:
                    ranges.append((cur_start.isoformat(), cur_end.isoformat()))
                    cur_start = dd
                cur_end = dd
            ranges.append((cur_start.isoformat(), cur_end.isoformat()))

        # 把超长 30 天的 range 切小 (减少单次 API 失败影响)
        final = []
        for s, e in ranges:
            ds = date.fromisoformat(s)
            de = date.fromisoformat(e)
            while (de - ds).days > 30:
                final.append((ds.isoformat(), (ds + timedelta(days=29)).isoformat()))
                ds = ds + timedelta(days=30)
            final.append((ds.isoformat(), de.isoformat()))
        ranges = final

    log(f'planned ranges: {len(ranges)}')
    if not ranges:
        log('nothing to do, exit')
        return 0

    # 清空 failed
    if not only_failed and os.path.exists(FAILED_FILE):
        os.remove(FAILED_FILE)

    minute_dir = repo.store.data_dir / 'kline_minute'

    ok_count = 0
    fail_count = 0
    total_written = 0
    t_total = time.time()
    for i, (s, e) in enumerate(ranges, 1):
        start_dt = datetime.fromisoformat(s + ' 09:30:00')
        end_dt = datetime.fromisoformat(e + ' 15:00:00')
        log(f'[{i}/{len(ranges)}] {s}..{e} ...')
        t0 = time.time()
        written_box = [0]

        def _persist(seg):
            with repo._write_lock:
                written_box[0] += kline_sync._write_minute_partition(seg, minute_dir)

        try:
            kline_sync.sync_minute_batch(
                symbols,
                start_time=start_dt,
                end_time=end_dt,
                batch_size=100,
                rpm=30,
                segment_trading_days=15,
                on_segment=_persist,
                asset_type='stock',
                raw_basis=minute_basis_is_raw(repo.store.data_dir),
            )
            elapsed = time.time() - t0
            log(f'  OK {written_box[0]:,} rows in {elapsed:.0f}s')
            ok_count += 1
            total_written += written_box[0]
        except KeyboardInterrupt:
            log('KeyboardInterrupt, exit')
            sys.exit(130)
        except Exception as ex:
            elapsed = time.time() - t0
            log(f'  FAIL after {elapsed:.0f}s: {type(ex).__name__}: {ex}')
            with open(FAILED_FILE, 'a') as f:
                f.write(f'{s}|{e}\n')
            fail_count += 1

    elapsed_total = time.time() - t_total
    log(f'DONE: {ok_count} ok, {fail_count} fail, {total_written:,} rows in {elapsed_total/60:.1f}min')

    if fail_count:
        log(f'failed ranges saved to {FAILED_FILE}')

    # 刷新视图
    try:
        from app.jobs.daily_pipeline import _refresh_single_view
        _refresh_single_view(repo, 'kline_minute')
        log('view refreshed')
    except Exception as ex:
        log(f'view refresh failed: {ex}')

    return 0 if fail_count == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
