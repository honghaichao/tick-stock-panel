"""实盘 vs 回测对比 — 1 周实盘成交 vs 回测同期 entry 信号.

跑法:
  # 1. 启动后端, 撮合线程在跑
  ./dev.sh start
  # 2. 触发一次 rebalance (模拟今日 9:35 触发)
  curl -X POST localhost:3018/api/trading/strategies/dividend_auntie/run-now \
    -H 'Content-Type: application/json' -d '{"event":"rebalance"}'
  # 3. 跑这个脚本 (5s 后等撮合线程扫到)
  sleep 5
  uv run python scripts/compare_live_vs_backtest.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import polars as pl
import pandas as pd
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

DATA_DIR = REPO_ROOT / "data"
USER_DATA = DATA_DIR / "user_data"

# 1) 读实盘 fills (最近 7 天)
print("=" * 70)
print("实盘 vs 回测对比 (dividend_auntie, 最近 7 天)")
print("=" * 70)

fills = []
fills_path = USER_DATA / "trading" / "fills.jsonl"
for line in fills_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line: continue
    try:
        fills.append(json.loads(line))
    except Exception:
        continue
# 最近 7 天
today = date.today()
cutoff = today - timedelta(days=7)
recent_fills = [f for f in fills if f.get("trade_date", "") >= cutoff.isoformat() and f.get("trade_date", "")]
print(f"\n[实盘] 最近 7 天 fill 笔数: {len(recent_fills)}")
if recent_fills:
    for f in recent_fills[:10]:
        print(f"  {f.get('trade_date','')} {f.get('side','')} {f.get('symbol','')} {f.get('qty',0)}股 @ {f.get('price',0):.3f}")

# 2) 读实盘 orders
orders = []
for line in (USER_DATA / "trading" / "orders.jsonl").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line: continue
    try:
        orders.append(json.loads(line))
    except Exception:
        continue
recent_orders = [o for o in orders if (o.get("created_at", "") or "")[:10] >= cutoff.isoformat()]
print(f"\n[实盘] 最近 7 天 委托笔数: {len(recent_orders)}")
for o in recent_orders[:10]:
    print(f"  {(o.get('created_at','') or '')[:19]} {o.get('side','')} {o.get('symbol','')} {o.get('qty',0)}股 "
          f"@ {o.get('limit_price') or '市价'} 状态={o.get('status','')}")

# 3) 读账户
account = json.loads((USER_DATA / "trading" / "account.json").read_text(encoding="utf-8"))
print(f"\n[实盘] 当前账户: cash=¥{account.get('cash',0):,.0f}  total_equity=¥{account.get('total_equity',0):,.0f}")

# 4) 跑同期回测 (用真实数据 + 调校后的撮合)
print("\n[回测] 跑同期 1 周回测 ...")
# 直接用 backtest_dividend_auntie 的核心函数
from scripts.backtest_dividend_auntie import (
    prepare_matrix, run_backtest, compute_kpis, FEE_PCT, STAMP_TAX_PCT,
    SLIPPAGE_BPS, STOCK_NUM, HIGH_PRICE_THRESHOLD, TOP_DIVIDEND_PCT,
    MIN_CONTINUOUS_YEAR, INITIAL_CASH,
)
import importlib
bt = importlib.import_module("scripts.backtest_dividend_auntie")

# 用最近的 7 天 (含今天) 跑 1 周回测
recent_dates = [today - timedelta(days=i) for i in range(6, -1, -1)]
print(f"  期间: {recent_dates[0]} ~ {recent_dates[-1]}")

# 构造窄范围 matrix — 用今天的年份 (实盘跑 2026 就要用 2026 K)
matrix_year = today.year
print(f"  matrix 用 {matrix_year} 年 K 线")
market, dates_sorted, n_syms = prepare_matrix(matrix_year)
# 过滤到最近 7 天 (timestamps 是字符串 'YYYY-MM-DD')
from datetime import date as _date
def _to_date(s):
    return _date.fromisoformat(str(s)[:10])
mask = np.array([(_to_date(d) >= recent_dates[0] and _to_date(d) <= recent_dates[-1]) for d in market.timestamps])
if mask.sum() == 0:
    # 范围内没数据, 用整个月
    print("  范围内没数据, 用整月")
    mask = np.ones(market.shape[0], dtype=bool)

# 裁剪
def _crop_matrix(market, mask):
    """按 mask 裁剪所有数组维度 0."""
    from app.backtest.matrix import MarketDataMatrix
    fields = {k: v[mask] if hasattr(v, "shape") and v.shape[0] == len(mask) else v for k, v in market.fields.items()}
    return MarketDataMatrix(
        timestamps=market.timestamps[mask],
        timestamp_labels=market.timestamp_labels,
        session_ids=market.session_ids[mask],
        symbols=market.symbols, names=market.names,
        open=market.open[mask], high=market.high[mask], low=market.low[mask],
        close=market.close[mask], volume=market.volume[mask],
        tradable=market.tradable[mask],
        limit_up_locked=market.limit_up_locked[mask], limit_down_locked=market.limit_down_locked[mask],
        fields=fields, cache_status="memory",
    )

# 简化: 用完整月 matrix 跑, 但只看最近 7 天
top_500 = list(market.symbols)
# 跑 1 周回测
result = bt.run_backtest(market, dates_sorted, top_500,
                        fee_pct=FEE_PCT, stamp_tax_pct=STAMP_TAX_PCT,
                        slippage_bps=SLIPPAGE_BPS, mode="rebalance")
kpis = bt.compute_kpis(result)
print(f"  回测 1 周 (用 {today.year} 全年, 简化): 收益 {kpis['total_return']*100:+.2f}%  "
      f"买 {kpis['n_buys']} 卖 {kpis['n_sells']}")

# 5) 对比
print("\n" + "=" * 70)
print("对比总结")
print("=" * 70)
print(f"实盘: {len(recent_fills)} 笔 fill,  {len(recent_orders)} 笔 order")
print(f"回测 (1 周, 调校后): {kpis['n_buys']} 买 / {kpis['n_sells']} 卖")
if recent_fills and kpis["n_buys"]:
    # 对比同 symbol 集合
    live_syms = set(f.get("symbol", "") for f in recent_fills)
    bt_syms = set()
    for t in result["trades"]:
        if t["side"] == "buy":
            bt_syms.add(t["symbol"])
    overlap = live_syms & bt_syms
    only_live = live_syms - bt_syms
    only_bt = bt_syms - live_syms
    print(f"\n  标的对比:")
    print(f"    实盘有: {sorted(live_syms)}")
    print(f"    回测有: {sorted(bt_syms)}")
    print(f"    交集: {sorted(overlap)} ({len(overlap)} 个)")
    print(f"    仅实盘: {sorted(only_live)}")
    print(f"    仅回测: {sorted(only_bt)}")
    if overlap:
        print(f"  ✓ 交集存在 — 实盘回测方向一致")
    if only_bt:
        print(f"  ⚠ 仅回测命中 — 实盘可能因为价格/data_provider 问题没成交 (待查)")

# 6) 写报告
ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
out_dir = REPO_ROOT / "docs" / "reports" / f"live-vs-backtest-{ts}"
out_dir.mkdir(parents=True, exist_ok=True)
report = {
    "ran_at": ts,
    "live_window": f"{recent_dates[0]} ~ {recent_dates[-1]}",
    "live": {
        "n_fills": len(recent_fills),
        "n_orders": len(recent_orders),
        "fills": recent_fills,
        "account": account,
    },
    "backtest_1w": {
        "n_buys": kpis["n_buys"],
        "n_sells": kpis["n_sells"],
        "total_return": kpis["total_return"],
        "final_equity": kpis["final_equity"],
    },
    "overlap": sorted(overlap) if recent_fills and kpis["n_buys"] else [],
    "only_live": sorted(only_live) if recent_fills and kpis["n_buys"] else [],
    "only_bt": sorted(only_bt) if recent_fills and kpis["n_buys"] else [],
}
(out_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n  报告: {out_dir / 'summary.json'}")
