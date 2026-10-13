#!/usr/bin/env python3
"""用 Tushare 补齐 kline_minute 的 2023-03-03 / 2023-03-06 ~ 2023-03-10 共 6 个交易日。

Phase 2 + 默认 + 不改 preferences:
- 仅覆盖确实缺失的 6 个交易日
- 每只股票 1 次 Tushare stk_mins 调用, end_date=20230317
- filter 只保留 6 天范围
- 复用 kline_sync._write_minute_partition
- 失败写入 /tmp/ts-fill/fill_tushare_minute_2023_03.failed.txt, --failed 重跑

约束:
- 不修改 preferences.json
- 不覆盖已存在的本地分钟数据 (filter 限定 6 天 + merge-upsert)
- 限速 ~150 RPM
"""
import os
import sys
import time
import argparse
from datetime import datetime

sys.path.insert(0, 'backend')

# ---- bootstrap .env (脚本独立运行时没有 uvicorn 的 env_file 注入) ----
def _bootstrap_env():
    import os as _os
    from pathlib import Path as _P
    env_path = _P(__file__).resolve().parent.parent / '.env'
    if not env_path.exists():
        return
    for _ln in env_path.read_text().splitlines():
        _ln = _ln.strip()
        if not _ln or _ln.startswith('#') or '=' not in _ln:
            continue
        _k, _v = _ln.split('=', 1)
        _os.environ.setdefault(_k.strip(), _v.strip())
_bootstrap_env()


import polars as pl

FAILED_FILE = '/tmp/ts-fill/fill_tushare_minute_2023_03.failed.txt'

TARGET_DATES = ['2023-03-03', '2023-03-06', '2023-03-07', '2023-03-08', '2023-03-09', '2023-03-10']
END_DATE = '20230317'


def log(msg):
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}', file=sys.stderr, flush=True)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--failed', action='store_true')
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--rpm', type=float, default=150.0)
    return p.parse_args()


def _to_minute_df(ts_df):
    if ts_df is None or ts_df.empty:
        return pl.DataFrame()
    ts_df = ts_df[ts_df['trade_time'].str[:10].isin(TARGET_DATES)].copy()
    if ts_df.empty:
        return pl.DataFrame()
    records = []
    for _, r in ts_df.iterrows():
        sym = r['ts_code']
        tt = str(r['trade_time']).strip()
        try:
            dt = datetime.strptime(tt, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        vol = r.get('vol')
        records.append({
            'symbol': sym,
            'datetime': dt,
            'open': float(r['open']) if r.get('open') is not None else None,
            'high': float(r['high']) if r.get('high') is not None else None,
            'low': float(r['low']) if r.get('low') is not None else None,
            'close': float(r['close']) if r.get('close') is not None else None,
            'volume': (float(vol) / 100.0) if vol is not None else None,
            'amount': float(r['amount']) if r.get('amount') is not None else None,
        })
    if not records:
        return pl.DataFrame()
    df = pl.DataFrame(records)
    df = df.with_columns([
        pl.col('symbol').cast(pl.Utf8),
        pl.col('datetime').cast(pl.Datetime(time_unit='us', time_zone=None)),
        pl.col('open').cast(pl.Float64),
        pl.col('high').cast(pl.Float64),
        pl.col('low').cast(pl.Float64),
        pl.col('close').cast(pl.Float64),
        pl.col('volume').cast(pl.Float64),
        pl.col('amount').cast(pl.Float64),
    ])
    return df


def main():
    args = parse_args()
    sleep_s = 60.0 / max(1.0, args.rpm)
    log(f'start (failed_only={args.failed} limit={args.limit} rpm={args.rpm})')

    from app.tickflow.repository import DataStore, KlineRepository
    from app.services import kline_sync

    store = DataStore()
    repo = KlineRepository(store)
    minute_dir = repo.store.data_dir / 'kline_minute'

    import tushare as ts
    from app.plugins.tushare.provider import get_api_key
    token = get_api_key()
    if not token:
        log('ERROR: TUSHARE_TOKEN 未配置')
        return 2
    log(f'token loaded (len={len(token)})')
    pro = ts.pro_api(token)

    if args.failed:
        if not os.path.exists(FAILED_FILE):
            log('no failed file, exit')
            return 0
        symbols = []
        with open(FAILED_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    symbols.append(line)
        if args.limit:
            symbols = symbols[:args.limit]
        log(f'failed_only mode: {len(symbols)} symbols')
    else:
        daily_root = repo.store.data_dir / 'kline_daily'
        latest = sorted(d.name for d in daily_root.iterdir() if d.is_dir() and d.name.startswith('date='))[-1]
        daily_part = daily_root / latest / 'part.parquet'
        symbols = sorted(pl.read_parquet(daily_part, columns=['symbol'])['symbol'].unique().to_list())
        if args.limit:
            symbols = symbols[:args.limit]
        log(f'symbols universe: {len(symbols)} (from {latest})')

    if not symbols:
        log('empty universe, exit')
        return 0

    if not args.failed and os.path.exists(FAILED_FILE):
        os.remove(FAILED_FILE)
        log(f'cleared {FAILED_FILE}')

    t_total = time.time()
    ok = empty = fail = 0
    written_rows = 0
    last_progress = time.time()

    for i, sym in enumerate(symbols, 1):
        t0 = time.time()
        try:
            ts_df = pro.stk_mins(
                ts_code=sym, freq='1min',
                start_date='20230101', end_date=END_DATE,
            )
        except Exception as ex:
            log(f'  [{i}/{len(symbols)}] {sym} EXC: {type(ex).__name__}: {str(ex)[:120]}')
            with open(FAILED_FILE, 'a') as f:
                f.write(f'{sym}\n')
            fail += 1
            time.sleep(sleep_s)
            continue

        if ts_df is None or ts_df.empty:
            empty += 1
        else:
            df = _to_minute_df(ts_df)
            if df.is_empty():
                empty += 1
            else:
                n = kline_sync._write_minute_partition(df, minute_dir)
                written_rows += n
                ok += 1

        elapsed = time.time() - t0
        if elapsed < sleep_s:
            time.sleep(sleep_s - elapsed)

        if time.time() - last_progress > 30 or i % 200 == 0 or i == len(symbols):
            elapsed_total = time.time() - t_total
            rate = i / max(elapsed_total, 0.001) * 60
            log(f'  [{i}/{len(symbols)}] ok={ok} empty={empty} fail={fail} rows={written_rows:,} rate={rate:.0f}/min elapsed={elapsed_total/60:.1f}min')

    elapsed_total = time.time() - t_total
    log(f'DONE: ok={ok} empty={empty} fail={fail} rows={written_rows:,} in {elapsed_total/60:.1f}min')

    if fail and os.path.exists(FAILED_FILE):
        log(f'failed symbols saved to {FAILED_FILE} ({fail} entries)')

    return 0 if fail == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
