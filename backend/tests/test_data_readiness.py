"""Test wait_for_quote_data: 关键路径 — 数据空时阻塞, 数据已就绪时秒返."""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _load_helper():
    """Load _data_readiness.py as a standalone module (data/user_data/strategies/live is not on sys.path)."""
    helper_path = Path(__file__).parent.parent.parent / "data" / "user_data" / "strategies" / "live" / "_data_readiness.py"
    spec = importlib.util.spec_from_file_location("test_data_readiness_helper", helper_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def h():
    return _load_helper()


# ─── helper test cases ──────────────────────────────────────────────────

class TestWaitForQuoteDataContract:
    def test_returns_immediately_when_data_ready(self, h):
        """coverage > 80% 不等"""
        ctx = MagicMock()
        ctx.event = "rebalance"
        dp = MagicMock()
        dp.instruments.height = 5882
        # 95% coverage = 5588 non-zero
        dp.market_cap = {f"60{i:04d}.SH": 1e9 for i in range(5588)}
        # Fill remaining with 0
        for i in range(5588, 5882):
            dp.market_cap[f"60{i:04d}.SH"] = 0
        ctx.data_provider = dp

        t0 = time.time()
        result = h.wait_for_quote_data(ctx)
        elapsed = time.time() - t0
        assert result == 5588
        assert elapsed < 0.5, f"should return fast, took {elapsed:.2f}s"

    def test_blocks_then_returns_when_data_arrives(self, h):
        """初始 50% coverage, 后台 5s 后填到 80%+: 应阻塞 ~5s 后返回."""
        ctx = MagicMock()
        ctx.event = "rebalance"
        dp = MagicMock()
        dp.instruments.height = 100
        # Initial: 50 non-zero, 50 zero
        mp = {f"sym{i:03d}": (1e9 if i < 50 else 0) for i in range(100)}
        dp.market_cap = mp
        ctx.data_provider = dp

        # Background thread: after 4 seconds, fill to 90%
        def add_more():
            time.sleep(4.0)
            for i in range(50, 90):
                mp[f"sym{i:03d}"] = 1e9
        t = threading.Thread(target=add_more, daemon=True)
        t.start()

        t0 = time.time()
        result = h.wait_for_quote_data(ctx, min_coverage=0.80, poll_s=0.5, max_wait_s=20.0)
        elapsed = time.time() - t0

        assert result >= 80, f"expected ≥80, got {result}"
        # Should not have waited the full timeout; should not have returned too fast
        assert 3.0 < elapsed < 10.0, f"elapsed {elapsed:.2f}s not in (3,10)"

    def test_timeout_proceeds_with_sparse_data(self, h):
        """后台不补数据时, max_wait_s 后超时返回 (即使未达标)."""
        ctx = MagicMock()
        ctx.event = "rebalance"
        dp = MagicMock()
        dp.instruments.height = 100
        # Only 10% covered, never updates
        mp = {f"sym{i:03d}": (1e9 if i < 10 else 0) for i in range(100)}
        dp.market_cap = mp
        ctx.data_provider = dp

        t0 = time.time()
        result = h.wait_for_quote_data(ctx, min_coverage=0.80, poll_s=0.5, max_wait_s=3.0)
        elapsed = time.time() - t0

        assert result == 10, f"expected 10 (current coverage at timeout), got {result}"
        assert 2.5 < elapsed < 5.0, f"elapsed {elapsed:.2f}s not in (2.5,5.0)"

    def test_returns_zero_when_no_data_provider(self, h):
        ctx = MagicMock()
        ctx.event = "rebalance"
        ctx.data_provider = None
        assert h.wait_for_quote_data(ctx) == 0

    def test_returns_zero_when_instruments_empty(self, h):
        import polars as pl
        ctx = MagicMock()
        ctx.event = "rebalance"
        dp = MagicMock()
        empty_df = pl.DataFrame()
        dp.instruments = empty_df
        ctx.data_provider = dp
        assert h.wait_for_quote_data(ctx) == 0

    def test_returns_zero_when_no_market_cap_attr(self, h):
        """缺 market_cap 属性 (老 build_data_provider 路径) 也允许, 返回 0 早退."""
        import polars as pl
        ctx = MagicMock()
        ctx.event = "rebalance"
        dp = MagicMock()
        df = pl.DataFrame({"symbol": ["a"], "x": [1]})
        dp.instruments = df
        del dp.market_cap  # 没这字段
        ctx.data_provider = dp
        assert h.wait_for_quote_data(ctx) == 0

    def test_throttles_log_messages(self, h):
        """同 count 不重复 log (不会 spam)."""
        import logging
        # Capture logger output
        log_records = []
        h.logger.addFilter(lambda record: (log_records.append(record), True)[-1])

        ctx = MagicMock()
        ctx.event = "rebalance"
        dp = MagicMock()
        dp.instruments.height = 100
        mp = {f"sym{i:03d}": (1e9 if i < 50 else 0) for i in range(100)}
        dp.market_cap = mp
        ctx.data_provider = dp

        # Run with short timeout; expect ≤3 log lines (initial + maybe timeout)
        h.wait_for_quote_data(ctx, min_coverage=0.80, poll_s=0.3, max_wait_s=1.5)
        h.logger.removeFilter(lambda record: (log_records.append(record), True)[-1])
        # Filters are tricky; just bound the count: at most 3 messages for 1.5s polling
        assert len(log_records) <= 3, f"too many log lines: {len(log_records)}"
