"""Regression tests for the 3 live-trading bug fixes:
  A. executor dedup regex now handles both legacy `strategy:<sid>:<event>` AND new `[<sid>] <event>`.
  B. clearance / retry ids are collision-free (no more `lg_HHMMSSμμμμμμ` collisions).
  C. StrategyExecutor per-event lock prevents concurrent cross-strategy same-event execution.

Date: 2026-10-13.
"""
from __future__ import annotations

import importlib
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch as _mock_patch

import pytest


def _load(modname, filepath):
    """Load module without triggering trading/__init__.py heavy imports."""
    spec = importlib.util.spec_from_file_location(modname, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 共享 fixture: 加载 executor 模块 ──────────────────────────────
@pytest.fixture
def executor_mod():
    sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
    return _load(
        "executor_test_bugfix",
        str(Path(__file__).parent.parent / "app/services/trading/executor.py"),
    )


@pytest.fixture
def store_mod():
    sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
    return _load(
        "store_test_bugfix",
        str(Path(__file__).parent.parent / "app/services/trading/store.py"),
    )


# ─────────────────────────────────────────────────────────────────────
# A. dedup regex compat (handle both formats)
# ─────────────────────────────────────────────────────────────────────
class TestDedupHelperFormatCompat:
    def test_new_format_bracket(self, executor_mod):
        """Phase 4 后: `[<sid>] <event>` 必须能解析."""
        sid, ev = executor_mod._parse_strategy_event_note("[dividend_auntie] rebalance")
        assert sid == "dividend_auntie"
        assert ev == "rebalance"

    def test_legacy_format_colon(self, executor_mod):
        """Phase 4 前: `strategy:<sid>:<event>` 必须仍能解析 (历史脏数据)."""
        sid, ev = executor_mod._parse_strategy_event_note("strategy:dividend_auntie:rebalance")
        assert sid == "dividend_auntie"
        assert ev == "rebalance"

    def test_other_event_in_legacy(self, executor_mod):
        sid, ev = executor_mod._parse_strategy_event_note("strategy:dividend_auntie:pre_market")
        assert (sid, ev) == ("dividend_auntie", "pre_market")

    def test_manual_note_returns_none(self, executor_mod):
        """manual 委托 note='突破追单' 不应被错误归类."""
        assert executor_mod._parse_strategy_event_note("突破追单") == (None, None)
        assert executor_mod._parse_strategy_event_note(None) == (None, None)
        assert executor_mod._parse_strategy_event_note("") == (None, None)

    def test_random_text_returns_none(self, executor_mod):
        assert executor_mod._parse_strategy_event_note("hello world") == (None, None)

    def test_partial_format_returns_none(self, executor_mod):
        """半匹配的 '[only_one_token' 不应通过."""
        assert executor_mod._parse_strategy_event_note("[only_one_token") == (None, None)


# ─────────────────────────────────────────────────────────────────────
# B. _new_id collision-free
# ─────────────────────────────────────────────────────────────────────
class TestNewIdCollision:
    def test_burst_1000_unique(self, store_mod):
        """1000 ids 必须全 unique."""
        ids = [store_mod._new_id("lg") for _ in range(1000)]
        assert len(set(ids)) == 1000

    def test_burst_across_prefixes(self, store_mod):
        """fl / lg / ord 跨前缀 1000 each 也 unique."""
        all_ids = []
        for prefix in ("fl", "lg", "ord"):
            all_ids.extend([store_mod._new_id(prefix) for _ in range(1000)])
        assert len(set(all_ids)) == len(all_ids)

    def test_concurrent_callers_unique(self, store_mod):
        """多线程并发调用也 unique."""
        all_ids = []
        lock = threading.Lock()

        def worker():
            local = [store_mod._new_id("fl") for _ in range(200)]
            with lock:
                all_ids.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(all_ids)) == len(all_ids) == 2000


# ─────────────────────────────────────────────────────────────────────
# C. per-event lock for executor
# ─────────────────────────────────────────────────────────────────────
class TestPerEventLock:
    def test_event_lock_acquired_globally(self, executor_mod):
        """两个 StrategyExecutor 实例跑同 event 必须同一锁串行."""
        from app.services.trading.live_strategy import LiveStrategyRegistry
        reg = MagicMock()
        runner = MagicMock()
        e1 = executor_mod.StrategyExecutor(reg, runner)
        e2 = executor_mod.StrategyExecutor(reg, runner)

        lk1 = e1._event_lock_for("rebalance")
        lk2 = e2._event_lock_for("rebalance")
        assert lk1 is lk2, "同 event 必须共享同一 RLock"

        lk1_other_event = e1._event_lock_for("pre_market")
        assert lk1_other_event is not lk1, "不同 event 必须不同锁 (可并行)"

    def test_rlock_reentrant_within_same_thread(self, executor_mod):
        """RLock 同线程内可重入; 跨线程阻塞."""
        reg = MagicMock()
        runner = MagicMock()
        e1 = executor_mod.StrategyExecutor(reg, runner)
        e2 = executor_mod.StrategyExecutor(reg, runner)
        lk = e1._event_lock_for("rebalance")

        acquired_main = []
        def inner():
            with lk:
                acquired_main.append("inner")
        def outer():
            with lk:
                acquired_main.append("outer")
                time.sleep(0.05)  # ensure inner can interleave if RLock were broken
                inner()

        # Run outer on one thread, inner on another — outer must complete before inner
        # (RLock only grants reentry for SAME thread, blocks DIFFERENT thread).
        results = []
        def t_outer():
            outer(); results.append("outer_done")
        def t_inner():
            time.sleep(0.01)  # ensure outer holds first
            inner(); results.append("inner_done")

        th1 = threading.Thread(target=t_outer)
        th2 = threading.Thread(target=t_inner)
        th1.start(); th2.start()
        th1.join(); th2.join()
        # outer must complete before inner (which started but had to wait)
        assert results == ["outer_done", "inner_done"], f"RLock reentry failed: {results}"


# ─────────────────────────────────────────────────────────────────────
# B (further): clearance uses store._new_id, not datetime
# ─────────────────────────────────────────────────────────────────────
class TestClearanceUsesStoreNewId:
    def test_clearance_module_does_not_use_datetime_id(self):
        """clearance.py 不应再有 `id=f\"lg_{datetime.now` 这种格式."""
        clearance_path = Path(__file__).parent.parent / "app/services/trading/clearance.py"
        src = clearance_path.read_text()
        # The old format string: id=f"lg_{datetime.now()...
        assert 'id=f"lg_{datetime.now().strftime' not in src, (
            "clearance.py still uses datetime-based id (BUG-B regression)"
        )
        # It SHOULD use store._new_id
        assert 'store._new_id("lg")' in src

    def test_retry_module_does_not_use_datetime_id(self):
        retry_path = Path(__file__).parent.parent / "app/services/trading/retry.py"
        src = retry_path.read_text()
        assert 'id=f"fl_retry_{datetime.now().strftime' not in src, (
            "retry.py still uses datetime-based id (BUG-B regression)"
        )
        assert 'store._new_id("fl")' in src


# ─────────────────────────────────────────────────────────────────────
# regression guards
# ─────────────────────────────────────────────────────────────────────
class TestNoRegression:
    def test_store_new_id_in_store_module(self, store_mod):
        """store.py 仍然导出 _new_id."""
        assert hasattr(store_mod, "_new_id")
        assert callable(store_mod._new_id)

    def test_engine_no_longer_defines_new_id(self):
        """engine.py 不再自己定义 _new_id (避免双实现)."""
        engine_path = Path(__file__).parent.parent / "app/services/trading/engine.py"
        src = engine_path.read_text()
        assert "def _new_id(prefix: str) -> str" not in src, (
            "engine.py still defines _new_id; should delegate to store"
        )
        # And it imports from store
        assert "from app.services.trading.store import _new_id" in src
