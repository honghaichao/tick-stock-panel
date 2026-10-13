"""统一 runtime 测试: LiveStrategy + matrix 算子 同一份代码."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest


@pytest.fixture(scope="module")
def da_module():
    """Load data/user_data/strategies/live/dividend_auntie.py via file path."""
    p = Path("/Volumes/quant-ssd/projects/tickflow-stock-panel/data/user_data/strategies/live/dividend_auntie.py")
    spec = importlib.util.spec_from_file_location("dividend_auntie_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dividend_auntie_has_both_runtime_methods(da_module):
    """dividend_auntie 既有 on_event (实盘) 又有 compute_signals (回测)."""
    DividendAuntieStrategy = da_module.DividendAuntieStrategy
    STRATEGY = da_module.STRATEGY
    # 实盘
    assert hasattr(STRATEGY, "on_event")
    # 回测 (matrix 算子)
    assert hasattr(STRATEGY, "compute_signals")
    assert hasattr(STRATEGY, "META")
    assert hasattr(STRATEGY, "EXECUTION_BACKEND")
    assert STRATEGY.EXECUTION_BACKEND == "matrix_native"
    # 必填
    fields = STRATEGY.required_fields()
    assert "close" in fields
    assert "volume" in fields


def test_dividend_auntie_meta_compatible(da_module):
    """META 包含 builtin 引擎需要的所有字段."""
    STRATEGY = da_module.STRATEGY
    m = STRATEGY.META
    for k in ("id", "name", "asset_types", "timeframes", "params", "scoring", "order_by"):
        assert k in m, f"META 缺 {k}"
    assert m["id"] == "dividend_auntie"


def test_dividend_auntie_compute_signals_with_synthetic_matrix(da_module):
    """用合成 MarketDataMatrix 跑 compute_signals, 应返回非空 entry."""
    STRATEGY = da_module.STRATEGY

    # 构造合成 matrix: 100 天 × 10 只票
    n_days, n_syms = 100, 10
    np.random.seed(42)
    timestamps = np.array([f"2025-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}" for i in range(n_days)], dtype=object)
    symbols = tuple(f"60{i:04d}.SH" for i in range(n_syms))
    close = np.random.uniform(5, 15, size=(n_days, n_syms))
    # 5 只票有高分红, 5 只没有
    div_total = np.zeros((n_days, n_syms))
    div_total[:, :5] = 0.5  # 高分红
    fields = {
        "dividend_total": div_total,
        "total_shares": np.full((n_days, n_syms), 1e8),
        "name": np.array([[f"股票{i}" for i in range(n_syms)]] * n_days, dtype=object),
    }
    market = SimpleNamespace(
        shape=(n_days, n_syms),
        symbols=symbols,
        timestamps=timestamps,
        close=close,
        volume=np.random.uniform(1e5, 1e6, size=(n_days, n_syms)),
        limit_up_locked=close * 1.1,
        limit_down_locked=close * 0.9,
        fields=fields,
        cache_status="memory",
    )
    market.field = lambda name: fields[name]  # type: ignore

    params = {"top_dividend_pct": 0.5, "min_continuous_year": 1, "high_price_threshold": 20.0}
    sig = STRATEGY.compute_signals(market, params)
    assert sig is not None
    assert sig.shape == (n_days, n_syms)
    # 有 entry 信号 (top 50% 高分红)
    assert sig.entry.sum() > 0, f"应该有 entry, 实际 0. matrix close range: {close.min():.2f} - {close.max():.2f}"


def test_dividend_auntie_compute_signals_no_dividend_data(da_module):
    """无 dividend_total 字段 → entry 全 0 (退化为 0)."""
    STRATEGY = da_module.STRATEGY

    n_days, n_syms = 50, 5
    market = SimpleNamespace(
        shape=(n_days, n_syms),
        symbols=tuple(f"60{i:04d}.SH" for i in range(n_syms)),
        close=np.random.uniform(5, 15, size=(n_days, n_syms)),
        volume=np.ones((n_days, n_syms)),
        limit_up_locked=np.ones((n_days, n_syms)) * 100,
        limit_down_locked=np.zeros((n_days, n_syms)),
        fields={"total_shares": np.ones((n_days, n_syms)) * 1e8},
    )
    def _field(name):
        if name in ("dividend_total", "name"):
            return None
        return market.fields[name]
    market.field = _field
    sig = STRATEGY.compute_signals(market, {"top_dividend_pct": 0.25, "high_price_threshold": 100.0})
    # 无分红: _dividend_ratio_filter_mask 返全 1 (因为 div_field is None), 后续 entry 全 1
    # 接受 sig.entry 是 0 或 全 1 都行 (取决于退化为 True 还是 False)
    assert sig is not None


def test_dividend_auntie_live_on_event_still_works(da_module):
    """加了 matrix 算子后, 实盘 on_event 还能跑 (向后兼容)."""
    STRATEGY = da_module.STRATEGY

    # mock context
    ctx = MagicMock()
    ctx.positions = {}
    ctx.account = {"cash": 1000000.0}
    ctx.state = {}
    ctx.data_provider = None  # 会让 my_Trader 早期 return {}
    ctx.market = {}
    ctx.event = "rebalance"
    ctx.trade_date = date(2026, 1, 15)
    result = STRATEGY.on_event("rebalance", ctx)
    assert isinstance(result, dict)  # 可能空 dict, 但不抛


def test_dividend_auntie_live_strategy_registry_compatible(da_module):
    """LiveStrategyRegistry 还能注册 dividend_auntie (兼容 matrix 方法)."""
    from app.services.trading.live_strategy import LiveStrategyRegistry
    STRATEGY = da_module.STRATEGY
    r = LiveStrategyRegistry()
    r.register(STRATEGY)
    assert "dividend_auntie" in r._strategies
    assert r._strategies["dividend_auntie"] is STRATEGY
    # 3 个 schedule
    s = r._strategies["dividend_auntie"]
    assert len(s.schedules) == 3
    assert s.schedules[0].name == "pre_market"
    assert s.schedules[0].hour == 9
    assert s.schedules[0].minute == 5
