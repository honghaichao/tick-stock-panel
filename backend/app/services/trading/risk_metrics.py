"""风险统计指标 — alpha/beta/sharpe/sortino/info_ratio/volatility.

数据源:
  - 策略 daily_return: pnl_snapshots.jsonl 的 total_equity 序列
  - 基准 (沪深 300): kline_daily_enriched 的 000300.SH 收盘价
  - 窗口: 滚动 60 天 (默认), 不足则用全部可用数据
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# 行业惯例
RISK_FREE_RATE = 0.03          # 年化无风险利率 (3%)
TRADING_DAYS_PER_YEAR = 252
DEFAULT_WINDOW = 60            # 滚动窗口 (天)
MIN_OBS = 5                    # 最少观测数 (低于此返回 None)


def _load_equity_series(data_dir: Path) -> list[dict]:
    """读 pnl_snapshots.jsonl → [{date, equity}] 序列 (按日期升序, 去重最后一条)."""
    p = data_dir / "user_data" / "trading" / "pnl_snapshots.jsonl"
    if not p.exists():
        return []
    by_date: dict[str, float] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = __import__("json").loads(line)
            d = rec.get("trade_date")
            eq = rec.get("total_equity")
            if d and eq is not None:
                by_date[d] = float(eq)
        except Exception:
            continue
    return [{"date": d, "equity": e} for d, e in sorted(by_date.items())]


def _load_benchmark_series(data_dir: Path) -> list[dict]:
    """读 kline_daily_enriched 的 000300.SH 收盘价 → [{date, close}]. 无数据返空."""
    pq = data_dir / "kline_daily_enriched" / "symbol=000300.SH" / "part.parquet"
    if not pq.exists():
        return []
    try:
        df = pl.read_parquet(pq)
    except Exception as e:
        logger.warning("read benchmark 000300.SH failed: %s", e)
        return []
    if "date" not in df.columns or "close" not in df.columns:
        return []
    return [
        {"date": str(d), "close": float(c)}
        for d, c in zip(df["date"].to_list(), df["close"].to_list())
        if d is not None and c is not None
    ]


def _daily_returns(equity_series: list[float]) -> list[float | None]:
    """算日收益率: r[t] = equity[t]/equity[t-1] - 1. 第一个为 None."""
    out: list[float | None] = [None]
    for i in range(1, len(equity_series)):
        prev = equity_series[i - 1]
        cur = equity_series[i]
        if prev <= 0 or cur <= 0:
            out.append(None)
        else:
            out.append(cur / prev - 1.0)
    return out


def _align_returns(
    equity: list[dict], bench: list[dict]
) -> tuple[list[float], list[float], list[str]]:
    """按日期交集对齐, 返回 (策略日收益, 基准日收益, 日期)."""
    bench_map = {b["date"]: b["close"] for b in bench}
    bench_returns: list[float | None] = [None]
    bench_closes = [b["close"] for b in bench]
    for i in range(1, len(bench_closes)):
        prev = bench_closes[i - 1]
        cur = bench_closes[i]
        bench_returns.append(cur / prev - 1.0 if prev > 0 and cur > 0 else None)
    bench_idx = {b["date"]: i for i, b in enumerate(bench)}
    sp_returns = _daily_returns([e["equity"] for e in equity])
    s: list[float] = []
    b: list[float] = []
    d: list[str] = []
    for i, e in enumerate(equity):
        if e["date"] not in bench_idx:
            continue
        j = bench_idx[e["date"]]
        sr = sp_returns[i]
        br = bench_returns[j]
        if sr is None or br is None:
            continue
        s.append(sr)
        b.append(br)
        d.append(e["date"])
    return s, b, d


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _cov(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) - 1)


def _rolling_metrics(
    s: list[float], b: list[float], dates: list[str], window: int = DEFAULT_WINDOW
) -> list[dict]:
    """滚动计算 6 个指标, 每日一个点 (用截止当日的窗口)."""
    out: list[dict] = []
    rf_daily = RISK_FREE_RATE / TRADING_DAYS_PER_YEAR
    for i in range(len(s)):
        lo = max(0, i - window + 1)
        sw = s[lo: i + 1]
        bw = b[lo: i + 1]
        if len(sw) < MIN_OBS:
            out.append({
                "date": dates[i], "alpha": None, "beta": None,
                "sharpe": None, "sortino": None, "info_ratio": None,
                "volatility": None,
            })
            continue
        # beta = cov(s,b) / var(b)
        var_b = sum((x - _mean(bw)) ** 2 for x in bw) / (len(bw) - 1) if len(bw) > 1 else 0.0
        beta = _cov(sw, bw) / var_b if var_b > 0 else 0.0
        # alpha (annualized) = mean((s - rf - beta*(b - rf))) * 252
        excess_s = [x - rf_daily for x in sw]
        excess_b = [x - rf_daily for x in bw]
        mean_excess_s = _mean(excess_s)
        mean_excess_b = _mean(excess_b)
        alpha = (mean_excess_s - beta * mean_excess_b) * TRADING_DAYS_PER_YEAR
        # sharpe
        std_s = _std(sw)
        sharpe = (mean_excess_s / std_s * math.sqrt(TRADING_DAYS_PER_YEAR)) if std_s > 0 else 0.0
        # sortino (downside std)
        downside = [x for x in excess_s if x < 0]
        std_down = _std(downside) if downside else 0.0
        sortino = (mean_excess_s / std_down * math.sqrt(TRADING_DAYS_PER_YEAR)) if std_down > 0 else 0.0
        # info_ratio = (cumulative(s) - cumulative(b)) / tracking_error * sqrt(252)
        cum_s = sum(sw)
        cum_b = sum(bw)
        te = math.sqrt(sum((a - c) ** 2 for a, c in zip(excess_s, excess_b)) / (len(sw) - 1)) if len(sw) > 1 else 0.0
        info_ratio = ((cum_s - cum_b) / te * math.sqrt(TRADING_DAYS_PER_YEAR)) if te > 0 else 0.0
        # volatility (annualized)
        vol = std_s * math.sqrt(TRADING_DAYS_PER_YEAR)
        out.append({
            "date": dates[i],
            "alpha": round(alpha, 4),
            "beta": round(beta, 4),
            "sharpe": round(sharpe, 4),
            "sortino": round(sortino, 4),
            "info_ratio": round(info_ratio, 4),
            "volatility": round(vol, 4),
        })
    return out


def _summary(series: list[dict]) -> dict:
    """取 series 最后一点作为 summary (None if 不足)."""
    if not series:
        return {k: None for k in ("alpha", "beta", "sharpe", "sortino", "info_ratio", "volatility")}
    last = series[-1]
    return {k: last.get(k) for k in ("alpha", "beta", "sharpe", "sortino", "info_ratio", "volatility")}


def compute_risk_metrics(data_dir: Path, window: int = DEFAULT_WINDOW) -> dict:
    """主入口. 返回 {series, summary}."""
    equity = _load_equity_series(data_dir)
    bench = _load_benchmark_series(data_dir)
    if not equity or not bench:
        return {"series": [], "summary": _summary([])}
    eq_values = [e["equity"] for e in equity]
    eq_dates = [e["date"] for e in equity]
    s, b, d = _align_returns(equity, bench)
    if not s:
        return {"series": [], "summary": _summary([])}
    series = _rolling_metrics(s, b, d, window=window)
    return {"series": series, "summary": _summary(series)}


__all__ = ["DEFAULT_WINDOW", "RISK_FREE_RATE", "compute_risk_metrics"]
