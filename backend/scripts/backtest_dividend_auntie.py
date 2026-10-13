"""菜场大妈 dividend_auntie 真实回测 (聚宽式统一 runtime).

跑法:
  uv run --frozen python scripts/backtest_dividend_auntie.py

输出:
  - stdout: 调校前 vs 调校后收益对比
  - reports/live-trading-backtest-{ts}/equity_curve.png  收益曲线图
  - reports/live-trading-backtest-{ts}/monthly_returns.csv  月度收益
  - reports/live-trading-backtest-{ts}/summary.json  KPI 摘要

调校项 (跟聚宽原版对齐):
  - 手续费 fee_pct = 0.0003 (双边)
  - 印花税 stamp_tax_pct = 0.001 (只卖)
  - 滑点 slippage_bps = 5 (双边 5 bps)
  - 退出 = 每日 9:35 rebalance 真实行为: 不在新 entry 里的清仓 (不再 5 天强平)
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pandas as pd
import numpy as np

# 关键: 把仓库根目录加到 sys.path, 这样 data.user_data.strategies.live.dividend_auntie 可 import
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.backtest.matrix import MarketDataMatrix  # noqa: E402
from data.user_data.strategies.live.dividend_auntie import DividendAuntieStrategy  # noqa: E402

# ── 调校参数 ──
FEE_PCT = 0.0003            # 手续费双边 (跟 joinquant set_order_cost)
STAMP_TAX_PCT = 0.001        # 印花税只卖 (A 股标准)
SLIPPAGE_BPS = 5             # 滑点 5 bps 双边
INITIAL_CASH = 1_000_000.0
STOCK_NUM = 5
HOLD_DAYS_HARD_LIMIT = 30    # 30 天硬上限 (避免单只票无限持仓)
HIGH_PRICE_THRESHOLD = 9.0   # 9 元以下才买 (跟 dividend_auntie 原版一致)
TOP_DIVIDEND_PCT = 0.25
MIN_CONTINUOUS_YEAR = 3
DATA_DIR = REPO_ROOT / "data"
YEAR = 2025

# matplotlib 延迟 import (脚本可不依赖)
plt = None
def _ensure_matplotlib():
    global plt
    if plt is None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
    return plt


# ── 1. 准备 matrix ──
def prepare_matrix(year: int = YEAR) -> tuple[MarketDataMatrix, list, int]:
    """准备日 K + instruments + 分红 -> MarketDataMatrix (top 500 活跃股)."""
    kline_paths = sorted((DATA_DIR / "kline_daily_enriched").glob(f"date={year}-*"))
    if not kline_paths:
        raise SystemExit(f"no kline data for {year}")
    dfs = [pl.read_parquet(p) for p in kline_paths]
    kline_all = pl.concat(dfs, how="vertical_relaxed").sort(["symbol", "date"])
    print(f"  kline: {kline_all.height} 行, {kline_all['symbol'].n_unique()} 标的")

    # top 500 by avg amount
    avg_amount = (kline_all.group_by("symbol")
                  .agg(pl.col("amount").mean().alias("avg_amount"))
                  .sort("avg_amount", descending=True))
    top_500 = avg_amount.head(500)["symbol"].to_list()
    kline_top = kline_all.filter(pl.col("symbol").is_in(top_500))

    dates_sorted = sorted(kline_top["date"].unique().to_list())
    n_dates = len(dates_sorted); n_syms = len(top_500)
    date_to_idx = {d: i for i, d in enumerate(dates_sorted)}
    sym_to_idx = {s: i for i, s in enumerate(top_500)}

    close_arr = np.zeros((n_dates, n_syms))
    open_arr = np.zeros((n_dates, n_syms))
    high_arr = np.zeros((n_dates, n_syms))
    low_arr = np.zeros((n_dates, n_syms))
    volume_arr = np.zeros((n_dates, n_syms))
    for row in kline_top.iter_rows(named=True):
        si = sym_to_idx[row["symbol"]]; di = date_to_idx[row["date"]]
        close_arr[di, si] = row.get("close") or 0.0
        open_arr[di, si] = row.get("open") or 0.0
        high_arr[di, si] = row.get("high") or 0.0
        low_arr[di, si] = row.get("low") or 0.0
        volume_arr[di, si] = row.get("volume") or 0.0

    # instruments
    inst = pl.read_parquet(DATA_DIR / "instruments" / "instruments.parquet").filter(pl.col("symbol").is_in(top_500))
    inst_latest = inst.group_by("symbol").agg([
        pl.col("name").last(), pl.col("total_shares").last(),
        pl.col("limit_up").last(), pl.col("limit_down").last(),
    ])
    total_shares_arr = np.zeros((n_dates, n_syms))
    limit_up_arr = np.zeros((n_dates, n_syms))
    limit_down_arr = np.zeros((n_dates, n_syms))
    name_arr = np.empty((n_dates, n_syms), dtype=object); name_arr[:] = ""
    for row in inst_latest.iter_rows(named=True):
        if row["symbol"] not in sym_to_idx: continue
        si = sym_to_idx[row["symbol"]]
        total_shares_arr[:, si] = row.get("total_shares") or 0.0
        limit_up_arr[:, si] = row.get("limit_up") or 0.0
        limit_down_arr[:, si] = row.get("limit_down") or 0.0
        name_arr[:, si] = row.get("name") or ""

    # dividend_total: 365 天累计
    divs = pd.read_csv(DATA_DIR / "user_data" / "dividends.csv")
    divs["ex_date"] = pd.to_datetime(divs["ex_date"]).dt.date
    divs = divs[(divs["ex_date"] >= date(year - 1, 1, 1))
                & (divs["ex_date"] <= date(year, 12, 31))
                & (divs.symbol.isin(top_500))]
    divs_by_sym = dict(list(divs.groupby("symbol")))
    dividend_total = np.zeros((n_dates, n_syms))
    for di, d in enumerate(dates_sorted):
        window_start = d - timedelta(days=365)
        for sym_idx, sym in enumerate(top_500):
            if sym not in divs_by_sym: continue
            sd = divs_by_sym[sym]
            cum = sd[(sd.ex_date >= window_start) & (sd.ex_date <= d)]["bonus_per_share"].sum()
            dividend_total[di, sym_idx] = cum

    market = MarketDataMatrix(
        timestamps=np.array([str(d) for d in dates_sorted], dtype=object),
        timestamp_labels=("date",), session_ids=np.arange(n_dates),
        symbols=tuple(top_500), names=tuple(top_500),
        open=open_arr, high=high_arr, low=low_arr, close=close_arr, volume=volume_arr,
        tradable=np.ones_like(close_arr, dtype=bool),
        limit_up_locked=limit_up_arr, limit_down_locked=limit_down_arr,
        fields={"dividend_total": dividend_total, "total_shares": total_shares_arr, "name": name_arr},
        cache_status="memory",
    )
    return market, dates_sorted, n_syms


# ── 2. 撮合 ──
def run_backtest(market, dates_sorted, top_500, *,
                 fee_pct: float = 0.0, stamp_tax_pct: float = 0.0, slippage_bps: float = 0.0,
                 mode: str = "rebalance") -> dict:
    """撮合回测.

    mode:
      "rebalance" — 每天 9:35 真实行为: 不在新 entry 里清仓, 新的 buy 补齐 5 只
      "hold_5d"   — 旧版: 5 天强平
    """
    sym_to_idx = {s: i for i, s in enumerate(top_500)}
    n_dates = len(dates_sorted)
    n_syms = len(top_500)
    close = market.close

    cash = INITIAL_CASH
    holdings: dict[str, dict] = {}  # sym -> {qty, entry_price, entry_date}
    trades = []
    equity_curve = []
    slip = slippage_bps / 10000.0

    strat = DividendAuntieStrategy()
    sig = strat.compute_signals(market, {
        "stock_num": STOCK_NUM, "top_dividend_pct": TOP_DIVIDEND_PCT,
        "min_continuous_year": MIN_CONTINUOUS_YEAR, "high_price_threshold": HIGH_PRICE_THRESHOLD,
    })

    for di, d in enumerate(dates_sorted):
        # ── 1. 每日先做 rebalance: 不在新 entry 里清仓 ──
        if mode == "rebalance":
            entry_set_today = set()
            entry_today = np.where(sig.entry[di] > 0)[0][:STOCK_NUM]
            for si in entry_today:
                sym = top_500[si]
                px = close[di, si]
                if px > 0 and px < HIGH_PRICE_THRESHOLD:
                    entry_set_today.add(sym)
            # 清仓: 持仓不在 entry_set_today 里
            for sym in list(holdings.keys()):
                if sym not in entry_set_today:
                    qty = holdings[sym]["qty"]
                    px = close[di, sym_to_idx[sym]]
                    if px > 0 and qty > 0:
                        sell_px = px * (1 - slip)
                        fee = qty * sell_px * fee_pct
                        tax = qty * sell_px * stamp_tax_pct
                        proceeds = qty * sell_px - fee - tax
                        cash += proceeds
                        trades.append({"date": str(d), "symbol": sym, "side": "sell",
                                       "qty": qty, "price": sell_px, "fee": fee, "tax": tax})
                        del holdings[sym]
            # 买新仓: 按 entry 顺序
            for sym in entry_set_today:
                if sym in holdings: continue
                px = close[di, sym_to_idx[sym]]
                if px <= 0: continue
                buy_px = px * (1 + slip)
                budget = INITIAL_CASH / STOCK_NUM  # 等权 5 票
                target = int(budget / buy_px / 100) * 100
                if target <= 0: target = 100
                cost = target * buy_px
                fee = cost * fee_pct
                total_cost = cost + fee
                if total_cost > cash:
                    target = int((cash * 0.97) / buy_px / 100) * 100
                    if target <= 0: continue
                    cost = target * buy_px
                    fee = cost * fee_pct
                    total_cost = cost + fee
                    if total_cost > cash: continue
                cash -= total_cost
                holdings[sym] = {"qty": target, "entry_price": buy_px, "entry_date": d}
                trades.append({"date": str(d), "symbol": sym, "side": "buy",
                               "qty": target, "price": buy_px, "fee": fee})
        else:
            # hold_5d 旧模式: 5 天强平
            for sym in list(holdings.keys()):
                if (d - holdings[sym]["entry_date"]).days >= 5:
                    qty = holdings[sym]["qty"]
                    px = close[di, sym_to_idx[sym]]
                    if px > 0 and qty > 0:
                        sell_px = px * (1 - slip)
                        fee = qty * sell_px * fee_pct
                        tax = qty * sell_px * stamp_tax_pct
                        cash += qty * sell_px - fee - tax
                        trades.append({"date": str(d), "symbol": sym, "side": "sell",
                                       "qty": qty, "price": sell_px, "fee": fee, "tax": tax})
                        del holdings[sym]
            entry_today = np.where(sig.entry[di] > 0)[0][:STOCK_NUM]
            for si in entry_today:
                sym = top_500[si]
                if sym in holdings: continue
                px = close[di, si]
                if px <= 0 or px >= HIGH_PRICE_THRESHOLD: continue
                if cash < 100 * px: continue
                buy_px = px * (1 + slip)
                target = int(200000 / buy_px / 100) * 100
                if target <= 0: continue
                cost = target * buy_px
                fee = cost * fee_pct
                if cost + fee > cash: continue
                cash -= cost + fee
                holdings[sym] = {"qty": target, "entry_price": buy_px, "entry_date": d}
                trades.append({"date": str(d), "symbol": sym, "side": "buy",
                               "qty": target, "price": buy_px, "fee": fee})

        # 30 天硬上限 (兜底)
        for sym in list(holdings.keys()):
            if (d - holdings[sym]["entry_date"]).days >= HOLD_DAYS_HARD_LIMIT:
                qty = holdings[sym]["qty"]
                px = close[di, sym_to_idx[sym]]
                if px > 0 and qty > 0:
                    sell_px = px * (1 - slip)
                    fee = qty * sell_px * fee_pct
                    tax = qty * sell_px * stamp_tax_pct
                    cash += qty * sell_px - fee - tax
                    trades.append({"date": str(d), "symbol": sym, "side": "sell",
                                   "qty": qty, "price": sell_px, "fee": fee, "tax": tax,
                                   "reason": "30d_limit"})
                    del holdings[sym]

        mv = sum(holdings[s]["qty"] * close[di, sym_to_idx[s]]
                 for s in holdings if close[di, sym_to_idx[s]] > 0)
        equity_curve.append({"date": str(d), "equity": cash + mv, "cash": cash, "mv": mv,
                             "holdings": len(holdings)})

    return {"trades": trades, "equity_curve": equity_curve, "final_cash": cash,
            "final_holdings": list(holdings.keys())}


def compute_kpis(result: dict) -> dict:
    ec = result["equity_curve"]
    if not ec: return {}
    eqs = [e["equity"] for e in ec]
    final = eqs[-1]
    total_return = (final - INITIAL_CASH) / INITIAL_CASH
    peak = max(eqs)
    trough = min(eqs)
    max_dd = (peak - trough) / peak if peak > 0 else 0
    # 简单 Sharpe (年化)
    daily_ret = [(eqs[i] - eqs[i - 1]) / eqs[i - 1] for i in range(1, len(eqs)) if eqs[i - 1] > 0]
    sharpe = (np.mean(daily_ret) / np.std(daily_ret) * np.sqrt(252)) if daily_ret and np.std(daily_ret) > 0 else 0
    # 月度收益
    monthly: dict = {}
    for e in ec:
        m = e["date"][:7]
        monthly.setdefault(m, e["equity"])
    monthly_returns = []
    prev_eq = INITIAL_CASH
    for m in sorted(monthly):
        eq = monthly[m]
        ret = (eq - prev_eq) / prev_eq
        monthly_returns.append({"month": m, "return": ret, "equity": eq})
        prev_eq = eq
    return {
        "final_equity": final,
        "total_return": total_return,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "n_buys": sum(1 for t in result["trades"] if t["side"] == "buy"),
        "n_sells": sum(1 for t in result["trades"] if t["side"] == "sell"),
        "monthly_returns": monthly_returns,
    }


# ── 3. 画 equity curve ──
def plot_equity_curve(equity_curves: dict, out_path: Path) -> None:
    _ensure_matplotlib()
    fig, ax = plt.subplots(figsize=(12, 5))
    for label, ec in equity_curves.items():
        if not ec: continue
        dates = [datetime.fromisoformat(e["date"]).date() for e in ec]
        eqs = [e["equity"] for e in ec]
        ax.plot(dates, eqs, label=label, linewidth=1.5)
    ax.axhline(INITIAL_CASH, color="gray", linestyle="--", alpha=0.5, label="初始资金")
    ax.set_title(f"dividend_auntie 回测 equity curve ({YEAR})", fontsize=13)
    ax.set_xlabel("日期"); ax.set_ylabel("账户权益 (¥)")
    ax.legend(loc="best"); ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


# ── 4. main ──
def main(years: list[int] | None = None) -> int:
    if years is None:
        years = [YEAR]
    print("=" * 70)
    print(f"dividend_auntie 真实回测 (top 500, years={years}, 调校 fee/tax/slip + 真实 rebalance)")
    print("=" * 70)
    multi_year = len(years) > 1
    all_year_results: dict[int, dict] = {}
    all_year_kpis: dict[int, dict] = {}
    for year in years:
        print(f"\n=== year={year} ===")
        print("\n[1/4] 准备 matrix ...")
        market, dates_sorted, n_syms = prepare_matrix(year)
        top_500 = list(market.symbols)
        n_dates = len(dates_sorted)

        print("\n[2/4] 跑 4 个变体对比 ...")
        variants = [
            ("0_no_fee_rebalance_5d", dict(fee_pct=0.0, stamp_tax_pct=0.0, slippage_bps=0.0, mode="hold_5d")),
            ("1_fee_only_rebalance_5d", dict(fee_pct=FEE_PCT, stamp_tax_pct=STAMP_TAX_PCT, slippage_bps=0.0, mode="hold_5d")),
            ("2_fee_slip_rebalance_5d", dict(fee_pct=FEE_PCT, stamp_tax_pct=STAMP_TAX_PCT, slippage_bps=SLIPPAGE_BPS, mode="hold_5d")),
            ("3_fee_slip_real_rebalance", dict(fee_pct=FEE_PCT, stamp_tax_pct=STAMP_TAX_PCT, slippage_bps=SLIPPAGE_BPS, mode="rebalance")),
        ]
        results = {}
        kpis = {}
        for name, kwargs in variants:
            r = run_backtest(market, dates_sorted, top_500, **kwargs)
            k = compute_kpis(r)
            results[name] = r
            kpis[name] = k
            print(f"  {name:35s} 收益 {k['total_return']*100:+7.2f}%  回撤 {k['max_drawdown']*100:6.2f}%  "
                  f"Sharpe {k['sharpe']:5.2f}  买 {k['n_buys']:3d} 卖 {k['n_sells']:3d}")

        all_year_results[year] = results
        all_year_kpis[year] = kpis
        if not multi_year:
            _write_report(year, results, kpis)

    if multi_year:
        _write_multi_year_report(years, all_year_results, all_year_kpis)
    return 0


def _write_report(year: int, results: dict, kpis: dict) -> None:
    """单年份报告."""
    print("\n[3/4] 写报告 ...")
    ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    out_dir = REPO_ROOT / "docs" / "reports" / f"live-trading-backtest-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    final_name = "3_fee_slip_real_rebalance"
    final_kpi = kpis[final_name]
    final_result = results[final_name]

    plot_equity_curve({k: r["equity_curve"] for k, r in results.items()}, out_dir / "equity_curve.png")
    pd.DataFrame(final_kpi["monthly_returns"]).to_csv(out_dir / "monthly_returns.csv", index=False)
    summary = {
        "ran_at": ts, "year": year, "universe": "top_500_by_avg_amount",
        "params": {"stock_num": STOCK_NUM, "top_dividend_pct": TOP_DIVIDEND_PCT,
                   "min_continuous_year": MIN_CONTINUOUS_YEAR, "high_price_threshold": HIGH_PRICE_THRESHOLD},
        "calibration": {"fee_pct": FEE_PCT, "stamp_tax_pct": STAMP_TAX_PCT,
                        "slippage_bps": SLIPPAGE_BPS, "hold_days_hard_limit": HOLD_DAYS_HARD_LIMIT},
        "variants": {n: {"total_return": k["total_return"], "max_drawdown": k["max_drawdown"],
                          "sharpe": k["sharpe"], "n_buys": k["n_buys"], "n_sells": k["n_sells"]}
                      for n, k in kpis.items()},
        "final": {"initial_cash": INITIAL_CASH, "final_equity": final_kpi["final_equity"],
                  "total_return": final_kpi["total_return"], "max_drawdown": final_kpi["max_drawdown"],
                  "sharpe": final_kpi["sharpe"], "n_buys": final_kpi["n_buys"], "n_sells": final_kpi["n_sells"],
                  "monthly_returns": final_kpi["monthly_returns"]},
        "artifacts": {"equity_curve_png": str(out_dir / "equity_curve.png"),
                       "monthly_csv": str(out_dir / "monthly_returns.csv")},
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report_md(year, results, kpis, out_dir, final_name, final_kpi)
    print(f"  报告: {out_dir / 'report.md'}")
    print("\n[4/4] 摘要")
    print(f"  调校前: {kpis['0_no_fee_rebalance_5d']['total_return']*100:+.2f}%")
    print(f"  调校后: {final_kpi['total_return']*100:+.2f}%")
    print(f"  调校成本: {(kpis['0_no_fee_rebalance_5d']['total_return']-final_kpi['total_return'])*100:+.2f}%")


def _write_multi_year_report(years, all_results, all_kpis) -> None:
    """多年份综合报告."""
    print("\n[3/4] 写多年份报告 ...")
    ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    out_dir = REPO_ROOT / "docs" / "reports" / f"live-trading-backtest-multiyear-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    final_name = "3_fee_slip_real_rebalance"
    # 画多年 equity curve
    _ensure_matplotlib()
    fig, ax = plt.subplots(figsize=(12, 6))
    for year, results in all_results.items():
        ec = results[final_name]["equity_curve"]
        if ec:
            dates = [datetime.fromisoformat(e["date"]).date() for e in ec]
            eqs = [e["equity"] for e in ec]
            label = f"{year} (调校后)"
            ax.plot(dates, eqs, label=label, linewidth=1.5)
    ax.axhline(INITIAL_CASH, color="gray", linestyle="--", alpha=0.5, label="初始资金")
    ax.set_title(f"dividend_auntie 多年回测 equity curve ({years[0]}~{years[-1]})", fontsize=13)
    ax.set_xlabel("日期"); ax.set_ylabel("账户权益 (¥)")
    ax.legend(loc="best"); ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_dir / "equity_curve_multiyear.png", dpi=120, bbox_inches="tight")
    plt.close(fig)

    # 多年份 summary
    summary = {
        "ran_at": ts, "years": years, "universe": "top_500_by_avg_amount",
        "params": {"stock_num": STOCK_NUM, "top_dividend_pct": TOP_DIVIDEND_PCT,
                   "min_continuous_year": MIN_CONTINUOUS_YEAR, "high_price_threshold": HIGH_PRICE_THRESHOLD},
        "calibration": {"fee_pct": FEE_PCT, "stamp_tax_pct": STAMP_TAX_PCT,
                        "slippage_bps": SLIPPAGE_BPS, "hold_days_hard_limit": HOLD_DAYS_HARD_LIMIT},
        "per_year": {},
    }
    final_table = []
    for year in years:
        fk = all_kpis[year][final_name]
        summary["per_year"][str(year)] = {
            "final_equity": fk["final_equity"],
            "total_return": fk["total_return"],
            "max_drawdown": fk["max_drawdown"],
            "sharpe": fk["sharpe"],
            "n_buys": fk["n_buys"], "n_sells": fk["n_sells"],
            "monthly_returns": fk["monthly_returns"],
        }
        final_table.append({"year": year, "return": fk["total_return"],
                            "drawdown": fk["max_drawdown"], "sharpe": fk["sharpe"]})
    pd.DataFrame(final_table).to_csv(out_dir / "yearly_summary.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_multiyear_report_md(years, all_kpis, out_dir, final_name)
    print(f"  多年份报告: {out_dir / 'report.md'}")
    print("\n[4/4] 多年份摘要")
    for y in years:
        k = all_kpis[y][final_name]
        print(f"  {y}: 收益 {k['total_return']*100:+.2f}%  回撤 {k['max_drawdown']*100:.2f}%  Sharpe {k['sharpe']:.2f}")


def _write_report_md(year, results, kpis, out_dir, final_name, final_kpi) -> None:
    n_dates = len(results[final_name]["equity_curve"])
    rpt = out_dir / "report.md"
    with open(rpt, "w", encoding="utf-8") as f:
        f.write(f"# dividend_auntie 真实回测报告 ({year})\n\n")
        f.write(f"**生成时间**: {datetime.now(timezone(timedelta(hours=8))).isoformat()}\n\n")
        f.write(f"**期间**: {year}-01-02 ~ {year}-12-31 ({n_dates} 交易日)\n\n")
        f.write(f"**Universe**: top 500 活跃股\n\n")
        f.write(f"**调校**: fee={FEE_PCT*100:.3f}%, tax={STAMP_TAX_PCT*100:.3f}%, slip={SLIPPAGE_BPS}bps\n\n")
        f.write("## 4 个变体\n\n| 变体 | 收益 | 回撤 | Sharpe | 买 | 卖 |\n|---|---|---|---|---|---|\n")
        for n, k in kpis.items():
            f.write(f"| `{n}` | {k['total_return']*100:+.2f}% | {k['max_drawdown']*100:.2f}% | {k['sharpe']:.2f} | {k['n_buys']} | {k['n_sells']} |\n")
        f.write(f"\n## 调校后 ({final_name})\n\n")
        f.write(f"- 终值: ¥{final_kpi['final_equity']:,.0f} (收益 **{final_kpi['total_return']*100:+.2f}%**)\n")
        f.write(f"- 最大回撤: {final_kpi['max_drawdown']*100:.2f}%\n")
        f.write(f"- Sharpe: {final_kpi['sharpe']:.2f}\n\n")
        f.write(f"![equity curve]({out_dir.relative_to(REPO_ROOT / 'docs')}/equity_curve.png)\n\n")
        f.write("## 月度收益\n\n| 月份 | 收益 | 权益 |\n|---|---|---|\n")
        for m in final_kpi["monthly_returns"]:
            f.write(f"| {m['month']} | {m['return']*100:+.2f}% | ¥{m['equity']:,.0f} |\n")


def _write_multiyear_report_md(years, all_kpis, out_dir, final_name) -> None:
    rpt = out_dir / "report.md"
    with open(rpt, "w", encoding="utf-8") as f:
        f.write(f"# dividend_auntie 多年份回测报告 ({years[0]}~{years[-1]})\n\n")
        f.write(f"**生成时间**: {datetime.now(timezone(timedelta(hours=8))).isoformat()}\n\n")
        f.write(f"**Universe**: top 500 活跃股\n\n")
        f.write(f"**调校**: fee={FEE_PCT*100:.3f}%, tax={STAMP_TAX_PCT*100:.3f}%, slip={SLIPPAGE_BPS}bps, 真实 rebalance 退出\n\n")
        f.write("## 年份对比 (调校后)\n\n| 年份 | 收益 | 最大回撤 | Sharpe | 买 | 卖 |\n|---|---|---|---|---|---|\n")
        for y in years:
            k = all_kpis[y][final_name]
            f.write(f"| {y} | {k['total_return']*100:+.2f}% | {k['max_drawdown']*100:.2f}% | {k['sharpe']:.2f} | {k['n_buys']} | {k['n_sells']} |\n")
        # 累计收益
        cumulative = 1.0
        for y in years:
            k = all_kpis[y][final_name]
            cumulative *= (1 + k["total_return"])
        cumulative -= 1
        f.write(f"\n**累计复合收益 ({years[0]}~{years[-1]})**: {cumulative*100:+.2f}%\n\n")
        f.write(f"![equity curve multiyear]({out_dir.relative_to(REPO_ROOT / 'docs')}/equity_curve_multiyear.png)\n\n")
        # 月度细节
        for y in years:
            f.write(f"\n### {y} 月度\n\n| 月 | 收益 | 权益 |\n|---|---|---|\n")
            for m in all_kpis[y][final_name]["monthly_returns"]:
                f.write(f"| {m['month']} | {m['return']*100:+.2f}% | ¥{m['equity']:,.0f} |\n")


if __name__ == "__main__":
    import sys
    years = [int(y) for y in sys.argv[1].split(",")] if len(sys.argv) > 1 else None
    raise SystemExit(main(years))
