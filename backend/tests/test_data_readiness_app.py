"""Test for the app/services/trading/data_readiness.py module (NEW path: 2026-10-13 H2 fix).

The previous test_data_readiness.py covers the OLD _data_readiness.py shim.
This new test covers the live quote_service-poll path that executor actually uses.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _load_app():
    """Load app/services/trading/data_readiness.py as a standalone module."""
    path = Path(__file__).parent.parent / "app" / "services" / "trading" / "data_readiness.py"
    spec = importlib.util.spec_from_file_location("app_data_readiness", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["app_data_readiness"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def h():
    return _load_app()


# ─── new live-quote_service path ────────────────────────────────────────

class TestWaitForQuoteDataLiveQS:
    def test_returns_immediately_when_qs_full(self, h):
        """qs.get_quotes_compat() returns 95% filled → wait returns immediately."""
        import polars as pl
        ctx = MagicMock()
        qs = MagicMock()
        # 5882 rows where close > 0 (95% = 5588)
        pl_df = pl.DataFrame({
            "symbol": [f"60{i:04d}.SH" for i in range(5588)] + [f"60{i:04d}.SH" for i in range(5588, 5882)],
            "close":  [10.0] * 5588 + [0.0] * 294,
        })
        qs.get_quotes_compat.return_value = pl_df
        ctx.event = "rebalance"

        t0 = time.time()
        result = h.wait_for_quote_data(qs, min_coverage=0.80, max_wait_s=10.0, expected_count=5882, event="rebalance")
        elapsed = time.time() - t0
        assert result == 5588
        assert elapsed < 0.5

    def test_wait_with_qs_and_progression_unblocks(self, h):
        """Proper test: 50% → 90% in 2s, min_coverage=0.80 should unblock ~2s."""
        import polars as pl
        qs = MagicMock()
        nonlocal_holder = {"df": pl.DataFrame({
            "symbol": [f"60{i:04d}.SH" for i in range(50)],
            "close":  [10.0] * 50,
        })}
        qs.get_quotes_compat = lambda: nonlocal_holder["df"].clone()

        def fill():
            time.sleep(2.0)
            nonlocal_holder["df"] = pl.DataFrame({
                "symbol": [f"60{i:04d}.SH" for i in range(100)],
                "close":  [10.0] * 100,
            })
        threading.Thread(target=fill, daemon=True).start()

        t0 = time.time()
        result = h.wait_for_quote_data(qs, min_coverage=0.80, max_wait_s=20.0, expected_count=100, event="rebalance", poll_s=0.3)
        elapsed = time.time() - t0
        assert result == 100, f"expected 100, got {result}"
        assert 1.5 < elapsed < 8.0, f"elapsed {elapsed:.2f}s not in (1.5, 8.0)"

    def test_wait_with_qs_timeout(self, h):
        """qs never fills → TIMEOUT, return current count."""
        import polars as pl
        qs = MagicMock()
        qs.get_quotes_compat = lambda: pl.DataFrame({
            "symbol": [f"60{i:04d}.SH" for i in range(10)],
            "close":  [10.0] * 10,
        })

        t0 = time.time()
        result = h.wait_for_quote_data(qs, min_coverage=0.80, max_wait_s=2.0, expected_count=100, event="rebalance", poll_s=0.3)
        elapsed = time.time() - t0
        assert result == 10
        assert 1.5 < elapsed < 4.0

    def test_wait_with_qs_exception_falls_back_to_dp(self, h):
        """qs.get_quotes_compat() raises → use dp.market_cap fallback."""
        ctx = MagicMock()
        ctx.event = "rebalance"
        # dp has 100% filled
        ctx.data_provider = MagicMock()
        ctx.data_provider.market_cap = {f"60{i:04d}.SH": 1e9 for i in range(5882)}

        qs = MagicMock()
        def boom():
            raise RuntimeError("network blip")
        qs.get_quotes_compat = boom

        result = h.wait_for_quote_data(qs, context=ctx, min_coverage=0.80, max_wait_s=3.0, expected_count=5882, event="rebalance", poll_s=0.3)
        assert result == 5882  # fall back to dp.count

    def test_wait_with_qs_empty_df_returns_zero(self, h):
        """qs returns empty df → 0, falls to polling."""
        import polars as pl
        ctx = MagicMock()
        ctx.event = "rebalance"
        qs = MagicMock()
        qs.get_quotes_compat = lambda: pl.DataFrame()
        result = h.wait_for_quote_data(qs, min_coverage=0.80, max_wait_s=1.0, expected_count=5882, event="rebalance", poll_s=0.2)
        assert result == 0  # timed out with 0

    def test_no_qs_no_dp_returns_zero(self, h):
        """Defensive: no source → returns 0 (warning logged)."""
        ctx = MagicMock()
        ctx.event = "?"
        result = h.wait_for_quote_data(None, context=ctx, min_coverage=0.80, max_wait_s=0.5, poll_s=0.1)
        assert result == 0

    def test_uses_last_price_column_when_close_absent(self, h):
        """qs returns df with `last_price` (not `close`) — should still count correctly."""
        import polars as pl
        qs = MagicMock()
        # 95% filled
        qs.get_quotes_compat = lambda: pl.DataFrame({
            "symbol": [f"60{i:04d}.SH" for i in range(5588)] + [f"60{i:04d}.SH" for i in range(5588, 5882)],
            "last_price": [10.0] * 5588 + [0.0] * 294,
        })
        result = h.wait_for_quote_data(qs, min_coverage=0.80, max_wait_s=2.0, expected_count=5882, event="rebalance", poll_s=0.2)
        assert result == 5588

    def test_throttles_log_messages(self, h):
        """Same count shouldn't spam logs."""
        import polars as pl
        log_records = []
        h.logger.addFilter(lambda r: (log_records.append(r), True)[-1])
        try:
            qs = MagicMock()
            qs.get_quotes_compat = lambda: pl.DataFrame({
                "symbol": [f"60{i:04d}.SH" for i in range(50)],
                "close":  [10.0] * 50,
            })
            h.wait_for_quote_data(qs, min_coverage=0.80, max_wait_s=1.5, expected_count=100, event="test", poll_s=0.3)
        finally:
            h.logger.removeFilter(lambda r: (log_records.append(r), True)[-1])
        assert len(log_records) <= 3, f"too many log lines: {len(log_records)}"
