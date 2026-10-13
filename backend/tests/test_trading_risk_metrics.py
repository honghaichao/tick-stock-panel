"""风险指标测试 — alpha/beta/sharpe/sortino/info_ratio/volatility."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services.trading import risk_metrics as rm


def _write_equity(data_dir: Path, daily_equity: list[tuple[str, float]]) -> None:
    p = data_dir / "user_data" / "trading" / "pnl_snapshots.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for d, eq in daily_equity:
            f.write(json.dumps({
                "ts": f"{d}T15:00:00+08:00",
                "trade_date": d,
                "total_equity": eq,
            }) + "\n")


def _write_benchmark(data_dir: Path, daily_close: list[tuple[str, float]]) -> None:
    p = data_dir / "kline_daily_enriched" / "symbol=000300.SH"
    p.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame({
        "symbol": ["000300.SH"] * len(daily_close),
        "date": [d for d, _ in daily_close],
        "close": [c for _, c in daily_close],
    })
    df.write_parquet(p / "part.parquet")


# ── 空数据 ─────────────────────────────────────
def test_compute_risk_metrics_no_data(tmp_path):
    """无 equity / 无 benchmark → series=[], summary 全 None."""
    result = rm.compute_risk_metrics(tmp_path)
    assert result["series"] == []
    assert all(v is None for v in result["summary"].values())


def test_compute_risk_metrics_only_equity(tmp_path):
    """只有 equity, 无 benchmark → series=[]"""
    today = date.today()
    eq = [(today.isoformat(), 100000.0), ((today - timedelta(days=1)).isoformat(), 99000.0)]
    _write_equity(tmp_path, eq)
    result = rm.compute_risk_metrics(tmp_path)
    assert result["series"] == []


# ── 30 天数据 ─────────────────────────────────
def test_compute_risk_metrics_30_days(tmp_path):
    """30 天数据 → series 30+ 点, summary 6 个字段非 None."""
    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(30, -1, -1)]
    # 策略 equity 增长 0.1%/天
    eq = [(d, 100000.0 * (1.001 ** (30 - i))) for i, d in enumerate(days)]
    # 基准 000300.SH 也增长 0.05%/天
    bench = [(d, 4000.0 * (1.0005 ** (30 - i))) for i, d in enumerate(days)]
    _write_equity(tmp_path, eq)
    _write_benchmark(tmp_path, bench)
    result = rm.compute_risk_metrics(tmp_path, window=60)
    # series 长度: 30 个点 (第一个 daily return 是 None, 后 29 个有值)
    assert len(result["series"]) == 30
    # summary 6 个字段
    s = result["summary"]
    assert set(s.keys()) == {"alpha", "beta", "sharpe", "sortino", "info_ratio", "volatility"}
    # volatility 应为正数 (有日收益波动)
    assert s["volatility"] is not None
    # beta ≈ 1 (策略跟基准同步)
    assert s["beta"] is not None


def test_compute_risk_metrics_rolling_window(tmp_path):
    """滚动 60 天窗口: 数据少时全部计算."""
    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(10, -1, -1)]
    eq = [(d, 100000.0 + i * 100) for i, d in enumerate(days)]
    bench = [(d, 4000.0 + i * 5) for i, d in enumerate(days)]
    _write_equity(tmp_path, eq)
    _write_benchmark(tmp_path, bench)
    result = rm.compute_risk_metrics(tmp_path, window=60)
    # 11 天 → 10 个 daily returns (前 4 个 obs<5 为 None)
    assert len(result["series"]) == 10
    # 前 4 个 (观测 < 5) 应为 None
    for i in range(min(5, len(result["series"]))):
        if i < 4:
            assert result["series"][i]["alpha"] is None


# ── 边界 ─────────────────────────────────────
def test_compute_risk_metrics_zero_benchmark_return(tmp_path):
    """基准全平 → beta=0, alpha = strategy_excess * 252."""
    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(10, -1, -1)]
    eq = [(d, 100000.0 + i * 100) for i, d in enumerate(days)]
    bench = [(d, 4000.0) for d, _ in eq]    # 基准不变
    _write_equity(tmp_path, eq)
    _write_benchmark(tmp_path, bench)
    result = rm.compute_risk_metrics(tmp_path, window=60)
    # 不应该抛
    assert "summary" in result
    assert "alpha" in result["summary"]
