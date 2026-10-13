"""dividend_auntie 活跃股版 + 全量股版 + filter bug fix 测试."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load(name: str):
    """Load data/user_data/strategies/live/{name}.py."""
    p = Path(f"/Volumes/quant-ssd/projects/tickflow-stock-panel/data/user_data/strategies/live/{name}.py")
    spec = importlib.util.spec_from_file_location(f"test_{name}", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def dau():
    return _load("dividend_auntie")


@pytest.fixture
def dau_universe():
    return _load("dividend_auntie_universe")


# ── filter_kcbj_stock bug fix ──
def test_filter_kcbj_stock_excludes_688(dau):
    """688 开头 (科创板) 必被过滤."""
    assert "688981.SH" not in dau.filter_kcbj_stock(["688981.SH", "600519.SH"])


def test_filter_kcbj_stock_excludes_300_301(dau):
    """300/301 开头 (创业板) 必被过滤."""
    assert "300750.SZ" not in dau.filter_kcbj_stock(["300750.SZ", "301080.SZ", "002104.SZ"])


def test_filter_kcbj_stock_excludes_BJ(dau):
    """.BJ 结尾 (北交所新格式) 必被过滤."""
    out = dau.filter_kcbj_stock(["920395.BJ", "830001.BJ", "600519.SH"])
    assert "920395.BJ" not in out
    assert "830001.BJ" not in out
    assert "600519.SH" in out


def test_filter_kcbj_stock_keeps_main_board(dau):
    """主板保留: 600xxx / 002xxx / 601xxx / 000xxx."""
    keep = ["600519.SH", "002104.SZ", "601318.SH", "000001.SZ", "002887.SZ"]
    out = dau.filter_kcbj_stock(keep[:])
    assert set(out) == set(keep)


def test_filter_kcbj_stock_legacy_8xxx(dau):
    """聚宽原版 8xxx 老格式仍被过滤 (向后兼容)."""
    assert "830001.SZ" not in dau.filter_kcbj_stock(["830001.SZ", "600519.SH"])


def test_filter_kcbj_stock_dau_universe_same(dau, dau_universe):
    """dividend_auntie_universe 跟 dividend_auntie 用同一个 filter."""
    test_list = ["600519.SH", "688981.SH", "920395.BJ", "300750.SZ"]
    assert dau.filter_kcbj_stock(test_list[:]) == dau_universe.filter_kcbj_stock(test_list[:])


# ── 活跃股 vs 全量股 ──
def test_dividend_auntie_universe_active(dau):
    """dividend_auntie 默认 universe=active."""
    assert dau.DividendAuntieStrategy().universe == "active"


def test_dividend_auntie_id(dau):
    assert dau.DividendAuntieStrategy().id == "dividend_auntie"


def test_dividend_auntie_universe_id(dau_universe):
    assert dau_universe.DividendAuntieStrategy().id == "dividend_auntie_universe"


def test_dividend_auntie_universe_default_universe(dau_universe):
    """dividend_auntie_universe 不设 universe (全量股) — 暂保留旧行为 (不报错)."""
    strat = dau_universe.DividendAuntieStrategy()
    # 全量股版没 universe 字段 (或没设置) - 不强制要求
    assert not hasattr(strat, "universe") or strat.universe != "active"


# ── compute_signals universe 注入 ──
def test_compute_signals_with_active_universe(dau):
    """compute_signals 接受 universe 参数."""
    import numpy as np

    class M:
        shape = (50, 20)
        symbols = tuple(f"60{i:04d}.SH" for i in range(10)) + tuple(f"00{i:04d}.SZ" for i in range(10))
        timestamps = np.array([f"2025-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}" for i in range(50)], dtype=object)
        timestamp_labels = ("date",)
        session_ids = np.arange(50)
        close = np.random.RandomState(42).uniform(5, 15, size=(50, 20))
        open = close
        high = close * 1.05
        low = close * 0.95
        volume = np.ones((50, 20)) * 1e5
        tradable = np.ones_like(close, dtype=bool)
        limit_up_locked = close * 1.1
        limit_down_locked = close * 0.9
        cache_status = "memory"

        def __init__(self):
            self.fields = {
                "dividend_total": np.zeros((50, 20)),
                "total_shares": np.ones((50, 20)) * 1e8,
                "name": np.array([[f"stock{i}" for i in range(20)]] * 50, dtype=object),
                "active_universe": list(self.symbols[:5]),  # 活跃股 = 前 5
            }

        def field(self, name):
            return self.fields[name]

    m = M()
    # 让前 5 只有高分红
    m.fields["dividend_total"][:, :5] = 0.5
    sig = dau.DividendAuntieStrategy().compute_signals(m, {
        "stock_num": 5, "top_dividend_pct": 0.5, "high_price_threshold": 100.0,
        "universe": "active",
    })
    # 应该 entry 集中在前 5 (活跃股 + 高分红)
    # 至少有一些 entry (具体数量取决于 close 范围)
    assert sig is not None
    assert sig.entry.sum() > 0, "活跃股 universe 内应该有 entry 信号"
