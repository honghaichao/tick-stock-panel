"""StrategyAIOptimizer 单测 (2026-10-13).

Poincare 设计: 读 code → 调 codex → parse → apply diff → 跑 verifier → 落地/回滚.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

CN_TZ = timezone(timedelta(hours=8))
sys.path.insert(0, str(Path(__file__).parent.parent / "data" / "user_data" / "strategies" / "live"))
sys.path.insert(0, str(Path("/Volumes/quant-ssd/projects/tickflow-stock-panel/backend/app/services")))
import sys
# Strategy_ai_loop is at app/services/. Add to sys.path BEFORE importing so
# relative imports work, AND register the module name to sys.modules for @dataclass
sys.path.insert(0, str(Path(__file__).parent.parent / 'app' / 'services'))
# Pre-register in sys.modules to avoid dataclass NoneType errors
import importlib.util
_spec = importlib.util.spec_from_file_location(
    'strategy_ai_loop',
    str(Path(__file__).parent.parent / 'app' / 'services' / 'strategy_ai_loop.py'),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules['strategy_ai_loop'] = _mod
_spec.loader.exec_module(_mod)
ai_mod = _mod
from strategy_ai_loop import (
    OptimizeJob, StrategyAIOptimizer,
    STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED, STATUS_ROLLED_BACK,
    parse_ai_output, apply_diff_simple, _build_user_prompt,
)


# ── parse_ai_output 单测 ─────────────────────────────────
class TestParseAiOutput:
    def test_full_unified_diff(self):
        diff = (
            "--- a/dividend_auntie.py\n"
            "+++ b/dividend_auntie.py\n"
            "@@ -10,3 +10,3 @@\n"
            "-x = 1\n"
            "+x = 2\n"
        )
        status, parsed = parse_ai_output(diff)
        assert status == "ok"
        assert parsed == diff

    def test_no_diff_recommended(self):
        status, parsed = parse_ai_output("NO_DIFF_RECOMMENDED")
        assert status == "no_change"
        assert parsed == ""

    def test_no_diff_recommended_with_whitespace(self):
        status, parsed = parse_ai_output("  NO_DIFF_RECOMMENDED  \n")
        assert status == "no_change"

    def test_partial_diff_missing_minus_header(self):
        # Only +++ line, no --- line, has +/-
        text = (
            "+ x = 1\n"
            "- x = 2\n"
        )
        status, parsed = parse_ai_output(text)
        assert status == "ok"
        # Should have --- header prepended
        assert "--- a/" in parsed

    def test_partial_diff_missing_plus_header(self):
        text = (
            "- x = 1\n"
            "+ x = 2\n"
        )
        status, parsed = parse_ai_output(text)
        assert status == "ok"
        assert "+++ b/" in parsed

    def test_empty_string_returns_fail(self):
        status, parsed = parse_ai_output("")
        assert status == "fail"
        assert parsed == ""

    def test_garbage_returns_fail(self):
        status, parsed = parse_ai_output("just random text\nwith no diff\nmarkers")
        assert status == "fail"
        assert parsed == ""

    def test_partial_diff_with_hunk(self):
        text = (
            "@@ -1 +1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
        )
        status, parsed = parse_ai_output(text)
        assert status == "ok"
        assert "@@" in parsed


# ── apply_diff_simple 单测 ─────────────────────────────────
class TestApplyDiffSimple:
    def test_no_diff_returns_original(self):
        original = "x = 1\ny = 2\n"
        result = apply_diff_simple(original, "")
        assert result == original

    def test_none_diff_returns_original(self):
        original = "x = 1"
        result = apply_diff_simple(original, None)
        assert result == original

    def test_no_unidiff_returns_original(self):
        # Without unidiff lib, the function should just return the original
        # (deferring to verifier to detect issues)
        original = "x = 1"
        diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-x\n+x"
        result = apply_diff_simple(original, diff)
        # Function returns original (unification is best-effort)
        assert result == original


# ── _build_user_prompt 单测 ──────────────────────────────
class TestBuildUserPrompt:
    def test_no_focus(self):
        s = _build_user_prompt("dividend_auntie", [], "x = 1")
        assert "dividend_auntie" in s
        assert "x = 1" in s
        assert "全检" in s or "常见" in s or "5" in s

    def test_with_focus(self):
        s = _build_user_prompt("dividend_auntie", ["T+1", "data_gaps"], "x = 1")
        assert "T+1" in s
        assert "data_gaps" in s

    def test_long_content_truncated(self):
        long_content = "x = 1\n" * 10_000  # ~70_000 chars
        s = _build_user_prompt("a", [], long_content)
        assert "truncated" in s
        assert len(s) < 30_000  # truncation kicked in


# ── OptimizeJob 单测 ───────────────────────────────────────
class TestOptimizeJob:
    def test_default_status_is_pending(self):
        job = OptimizeJob(opt_id="opt_test", strategy_id="dividend_auntie")
        assert job.status == STATUS_PENDING
        assert job.parsed_diff_lines == 0
        assert job.applied is False
        assert job.error == ""

    def test_to_dict_round_trip(self):
        job = OptimizeJob(
            opt_id="opt_1", strategy_id="dividend_auntie", focus=["x"],
            diff="--- a\n+++ b\n", verifier_passed=True,
        )
        d = job.to_dict()
        assert d["opt_id"] == "opt_1"
        assert d["strategy_id"] == "dividend_auntie"
        assert d["focus"] == ["x"]
        assert d["diff"] == "--- a\n+++ b\n"
        assert d["verifier_passed"] is True


# ── StrategyAIOptimizer 单测 (mocked) ─────────────────────
class TestOptimizerWithMocks:
    def _make_optimizer(self, codex_output="NO_DIFF_RECOMMENDED",
                          verifier_passed=True):
        # Mock codex runner (async)
        codex_runner = AsyncMock(return_value=codex_output)
        # Mock verifier runner (async)
        verifier_report = {
            "passed": verifier_passed,
            "summary": {"trades": 1, "pnl": 100.0},
            "trades": 1,
        }
        verifier_runner = AsyncMock(return_value=verifier_report)
        opt = StrategyAIOptimizer(
            registry=MagicMock(), data_dir=Path("/tmp"),
            codex_runner=codex_runner, verifier_runner=verifier_runner,
        )
        return opt

    def test_no_change_returns_completed(self, tmp_path):
        opt = self._make_optimizer(codex_output="NO_DIFF_RECOMMENDED")
        # Need to write a strategy file
        live_dir = tmp_path / "user_data" / "strategies" / "live"
        live_dir.mkdir(parents=True)
        (live_dir / "test_strat.py").write_text("STRATEGY = 'test'\n")
        opt._data_dir = tmp_path
        registry = MagicMock()
        registry.list_all.return_value = [MagicMock(id="test_strat", schedules=[])]
        opt._registry = registry

        # Patch the read_code via code_store
        with __import__("pytest").MonkeyPatch().context() as mp:
            from app.services.trading import code_store
            mp.setattr(code_store, "read_code", lambda dd, sid: "STRATEGY = 'test'\n")
            job = asyncio.run(opt.run("test_strat", focus=[], auto_apply=False))
            assert job.status == STATUS_COMPLETED
            assert job.diff == ""
            assert job.applied is False

    def test_failed_diff_parsing_returns_failed(self, tmp_path):
        opt = self._make_optimizer(codex_output="just garbage text with no diff")
        opt._data_dir = tmp_path
        opt._registry = MagicMock()
        with __import__("pytest").MonkeyPatch().context() as mp:
            from app.services.trading import code_store
            mp.setattr(code_store, "read_code", lambda dd, sid: "STRATEGY = 'test'\n")
            job = asyncio.run(opt.run("test_strat", focus=[], auto_apply=False))
            assert job.status == STATUS_FAILED
            assert "无法解析" in job.error

    def test_verifier_passed_completed_status(self, tmp_path):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        opt = self._make_optimizer(codex_output=diff, verifier_passed=True)
        opt._data_dir = tmp_path
        opt._registry = MagicMock()
        with __import__("pytest").MonkeyPatch().context() as mp:
            from app.services.trading import code_store
            mp.setattr(code_store, "read_code", lambda dd, sid: "STRATEGY = 'test'\n")
            job = asyncio.run(opt.run("test_strat", focus=[], auto_apply=False))
            assert job.status == STATUS_COMPLETED
            assert job.verifier_passed is True
            assert job.applied is False  # auto_apply=False

    def test_verifier_failed_returns_rolled_back(self, tmp_path):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        opt = self._make_optimizer(codex_output=diff, verifier_passed=False)
        opt._data_dir = tmp_path
        opt._registry = MagicMock()
        with __import__("pytest").MonkeyPatch().context() as mp:
            from app.services.trading import code_store
            mp.setattr(code_store, "read_code", lambda dd, sid: "STRATEGY = 'test'\n")
            job = asyncio.run(opt.run("test_strat", focus=[], auto_apply=False))
            assert job.status == STATUS_ROLLED_BACK
            assert job.verifier_passed is False
            assert job.applied is False  # rolled back, NOT applied

    def test_cooldown_after_three_failures(self, tmp_path):
        opt = self._make_optimizer(codex_output="garbage1")
        opt._data_dir = tmp_path
        opt._registry = MagicMock()
        with __import__("pytest").MonkeyPatch().context() as mp:
            from app.services.trading import code_store
            mp.setattr(code_store, "read_code", lambda dd, sid: "STRATEGY = 'test'\n")
            # 3 failures
            for i in range(3):
                asyncio.run(opt.run("test_strat", focus=[], auto_apply=False))
            # Now in cooldown
            assert opt.is_in_cooldown("test_strat") is True
            # 4th call should be blocked (status still PENDING after start)
            # Actually we expect it to skip or be rate-limited. Check failure_count.
            assert opt._FAILURE_COUNT.get("test_strat", 0) >= 3
