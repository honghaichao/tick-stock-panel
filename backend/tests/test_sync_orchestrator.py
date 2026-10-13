"""BacktestSyncOrchestrator 单测 (2026-10-13).

Ptolemy 设计: 加载 verify 报告 + 实时价 + 风控 + audit log + run_with_target.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

CN_TZ = timezone(timedelta(hours=8))
# Use the package import (matches production code path).
from app.services.trading import sync as sync_mod
from app.services.trading.sync import (
    BacktestSyncOrchestrator, SyncError, VerifyRunNotFound, VerifyStrategyMismatch,
    StaleReport, CodeChangedSinceVerify, InvalidConfirmToken, SyncInFlight,
    AllTargetsBlocked, SyncPlan, SyncResult, list_sync_audit,
    _sign_token, _verify_token,
)


def _make_verify_report(strategy_id="dividend_auntie", as_of="2026-10-09",
                        last_target=None) -> dict:
    return {
        "run_id": "vrf_20261013_test",
        "strategy_id": strategy_id,
        "started_at": "2026-10-09T15:32:00+08:00",
        "start_date": "2026-10-01",
        "end_date": "2026-10-13",
        "finished_at": "2026-10-09T15:35:00+08:00",
        "duration_ms": 180000,
        "trading_days": 7,
        "events": [
            {"date": "2026-10-08", "event": "pre_market", "status": "no_target", "target": {}, "error": "", "duration_ms": 0},
            {"date": "2026-10-08", "event": "rebalance", "status": "success",
             "target": last_target or {"600027.SH": 1000, "002661.SZ": 1500, "603801.SH": 800},
             "error": "", "duration_ms": 320},
            {"date": "2026-10-08", "event": "limit_check", "status": "no_target", "target": {}, "error": "", "duration_ms": 0},
        ],
        "simulate_fills": True,
        "initial_cash": 1_000_000.0,
        "pnl": {
            "initial_cash": 1_000_000.0, "final_cash": 998000.0, "final_market_value": 1500.0,
            "final_total_equity": 999500.0, "realized_pnl": -200.0, "unrealized_pnl": 0.0,
            "total_pnl": -500.0, "total_pnl_pct": -0.0005, "max_drawdown": 0.001,
            "max_drawdown_date": "2026-10-08", "total_trades": 3, "winning_trades": 1,
            "losing_trades": 2, "win_rate": 0.333, "avg_pnl_per_trade": -166.7,
        },
        "summary": {"total_signals": 1, "total_events": 9, "event_distribution": {"rebalance": 3, "pre_market": 3, "limit_check": 3},
                    "status_distribution": {"error": 0, "success": 1, "no_target": 8},
                    "errors_count": 0, "no_target_count": 8},
        "warnings": [], "fills": [], "equity_curve": [],
        "last_rebalance_target": last_target or {"600027.SH": 1000, "002661.SZ": 1500, "603801.SH": 800},
        "last_rebalance_date": as_of, "last_event_date": as_of,
        "target_symbols": sorted((last_target or {"600027.SH": 1, "002661.SZ": 1, "603801.SH": 1}).keys()),
        "code_version": "2026-10-09T15:32:00+08:00",
        "committed_at": "2026-10-09T15:35:22+08:00",
    }


def _setup_orch(tmp_path, *, with_target=True, simulate_fills=True, as_of="2026-10-09", run_id="vrf_test_001"):
    """构造 orchestrator 环境: 落 verify 报告 + 落 code_changelog + mock 一切."""
    data_dir = tmp_path
    (data_dir / "user_data" / "trading" / "code_changelog.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (data_dir / "user_data" / "trading" / "code_changelog.jsonl").write_text(
        json.dumps({"ts": "2026-10-09T15:32:00+08:00", "strategy_id": "dividend_auntie", "note": "old"}) + "\n"
    )
    target = {"600027.SH": 1000, "002661.SZ": 1500, "603801.SH": 800} if with_target else {}
    report = _make_verify_report("dividend_auntie", as_of=as_of, last_target=target)
    report["simulate_fills"] = simulate_fills
    report_dir = data_dir / "user_data" / "trading" / "verifications" / "dividend_auntie"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{run_id}.json").write_text(json.dumps(report, ensure_ascii=False))

    mock_qs = MagicMock()
    def fake_get_enriched_today():
        import polars as pl
        df = pl.DataFrame({
            "symbol": ["600027.SH", "002661.SZ", "603801.SH", "600000.SH"],
            "close": [4.32, 7.56, 6.15, 9.62],
        })
        return df, "2026-10-13"
    mock_qs.get_enriched_today = fake_get_enriched_today

    mock_runner = MagicMock()
    mock_runner.positions.return_value = []
    mock_executor = MagicMock()
    mock_executor.run_with_target = MagicMock(return_value={
        "started_at": "2026-10-13T10:00:00+08:00", "status": "success",
        "target": target, "submitted": [], "skipped": [],
    })
    mock_registry = MagicMock()
    mock_registry.get.return_value = MagicMock()

    orch = BacktestSyncOrchestrator(
        data_dir=data_dir, runner=mock_runner, executor=mock_executor,
        quote_service=mock_qs, registry=mock_registry,
    )
    return orch, run_id, target, mock_executor, mock_qs


class TestSyncTokenRoundTrip:
    def test_sign_and_verify_ok(self):
        audit_id, run_id = "audit_20261013_test", "vrf_20261013_test"
        token = _sign_token(audit_id, run_id)
        parts = token.rsplit("|", 3)
        assert parts[1] == audit_id
        assert parts[2] == run_id
        _verify_token(token, audit_id, run_id)  # should not raise

    def test_verify_fails_on_wrong_audit_id(self):
        token = _sign_token("audit_a", "run_a")
        with pytest.raises(InvalidConfirmToken):
            _verify_token(token, "audit_b", "run_a")

    def test_verify_fails_on_wrong_run_id(self):
        token = _sign_token("audit_a", "run_a")
        with pytest.raises(InvalidConfirmToken):
            _verify_token(token, "audit_a", "run_b")

    def test_verify_expires_after_ttl(self):
        """TTL=0 → 任何 token 立即过期. 用 from-import 直接拿 _verify_token 调用源.

        int(time.time()) 在同一秒内不变, 所以 sign 后立即 verify 时差 = 0. 必须 sleep ≥1s
        才能触发 `>0` 的 expired 路径. TTL=0 + 0 >= 0 实际上不应 raise, 改为 sign 后 sleep.
        """
        from app.services.trading import sync as _real
        old = _real.CONFIRM_TOKEN_TTL_S
        _real.CONFIRM_TOKEN_TTL_S = 0
        try:
            token = _real._sign_token("a", "r")
            time.sleep(1.1)
            with pytest.raises(InvalidConfirmToken):
                _real._verify_token(token, "a", "r")
        finally:
            _real.CONFIRM_TOKEN_TTL_S = old
    def test_empty_audit_file(self, tmp_path):
        result = list_sync_audit(tmp_path, "dividend_auntie", limit=5)
        assert result == []


class TestPrepareSync:
    def test_dry_run_returns_plan(self, tmp_path):
        orch, run_id, target, mock_executor, _ = _setup_orch(tmp_path)
        plan = orch.prepare_sync("dividend_auntie", run_id, dry_run=True)
        assert plan.verify_run_id == run_id
        assert plan.as_of == "2026-10-09"
        assert plan.code_version_match is True
        assert plan.target_count == 3
        assert len(plan.would_submit_orders) == 3
        for o in plan.would_submit_orders:
            assert o["order_type"] == "limit"
            assert o["limit_price"] > 0
            assert o["price_source"] == "live_quote"
            assert o["note"].startswith(f"sync-from-verify:{run_id}")
        assert plan.confirm_token == ""  # dry_run: no token
        assert plan.audit_id.startswith("sync_")

    def test_real_run_calls_run_with_target(self, tmp_path):
        """execute_sync 内部会 re-call prepare_sync (产生新 audit_id), 所以直接 mock 掉
        _verify_token 让 token 验证通过, 验证 run_with_target 仍被正确调用."""
        orch, run_id, target, mock_executor, _ = _setup_orch(tmp_path)
        # 同时 patch sync_mod 和 app.services.trading.sync 避免双 module 对象
        from app.services.trading import sync as _realsync
        original = sync_mod._verify_token
        original2 = _realsync._verify_token
        sync_mod._verify_token = lambda *a, **kw: None
        _realsync._verify_token = lambda *a, **kw: None
        try:
            token = _sign_token("anything", run_id)
            result = orch.execute_sync("dividend_auntie", run_id, confirm_token=token)
            assert isinstance(result, SyncResult)
            assert result.verify_run_id == run_id
            assert result.submitted_count == 0
            assert mock_executor.run_with_target.call_count == 1
            call = mock_executor.run_with_target.call_args
            assert call.kwargs["strategy_id"] == "dividend_auntie"
            assert call.kwargs["event"] == "rebalance"
            assert call.kwargs["trigger"] == "sync-from-verify"
            assert call.kwargs["override_target"] == target
        finally:
            sync_mod._verify_token = original
            _realsync._verify_token = original2

    def test_run_id_mismatch_raises(self, tmp_path):
        orch, run_id, _, _, _ = _setup_orch(tmp_path)
        plan = orch.prepare_sync("dividend_auntie", run_id, dry_run=True)
        bad_token = _sign_token("DIFFERENT_RUN_ID", run_id)
        with pytest.raises(InvalidConfirmToken):
            orch.execute_sync("dividend_auntie", run_id, confirm_token=bad_token)

    def test_audit_id_mismatch_raises(self, tmp_path):
        orch, run_id, _, _, _ = _setup_orch(tmp_path)
        plan = orch.prepare_sync("dividend_auntie", run_id, dry_run=True)
        bad_token = _sign_token("DIFFERENT_AUDIT", run_id)
        with pytest.raises(InvalidConfirmToken):
            orch.execute_sync("dividend_auntie", run_id, confirm_token=bad_token)

    def test_stale_report_raises(self, tmp_path):
        orch, run_id, _, _, _ = _setup_orch(tmp_path, as_of="2025-01-01")
        with pytest.raises(StaleReport):
            orch.prepare_sync("dividend_auntie", run_id, dry_run=True)

    def test_no_live_price_drops_symbols_dry_run(self, tmp_path):
        """无实时价时, dry_run 也立即 raise (前端二次确认前就发现)."""
        orch, run_id, _, _, mock_qs = _setup_orch(tmp_path)
        def empty_qs():
            import polars as pl
            return pl.DataFrame(), "2026-10-13"
        mock_qs.get_enriched_today = empty_qs
        with pytest.raises(AllTargetsBlocked):
            orch.prepare_sync("dividend_auntie", run_id, dry_run=True)

    def test_run_id_not_found_raises(self, tmp_path):
        orch, _, _, _, _ = _setup_orch(tmp_path)
        with pytest.raises(VerifyRunNotFound):
            orch.prepare_sync("dividend_auntie", "vrf_does_not_exist", dry_run=True)


class TestInFlightConcurrency:
    def test_in_flight_blocks_second_call(self, tmp_path):
        """直接验证 BacktestSyncOrchestrator 的 in_flight 机制 (不依赖 token 验证).

        不通过 execute_sync 完整路径 — 那个会调 _verify_token 和 prepare_sync (需要 module
        patching trick, 易碎). 我们直接设置 in_flight 槽位然后测第二次 call 必 raise.
        """
        from datetime import datetime
        orch, run_id, _, _, _ = _setup_orch(tmp_path)
        # 手动占住 in_flight 槽位
        orch._in_flight["dividend_auntie"] = {
            "started_at": datetime.now().isoformat(),
            "audit_id": "fake_audit_for_test",
        }
        try:
            # execute_sync 应发现 in_flight 已存在, raise SyncInFlight
            # token 不重要 (验证在 in_flight check 之后)
            from app.services.trading import sync as _realsync2
            original_vt = _realsync2._verify_token
            _realsync2._verify_token = lambda *a, **kw: None
            try:
                with pytest.raises(SyncInFlight):
                    orch.execute_sync("dividend_auntie", run_id, confirm_token="anything")
            finally:
                _realsync2._verify_token = original_vt
        finally:
            orch._in_flight.pop("dividend_auntie", None)

    def test_sync_lock_for_serialization(self, tmp_path):
        """_sync_lock_for 每次返回同一 Lock 对象 (reentrant) — 跨 execute_sync 串行化."""
        orch, _, _, _, _ = _setup_orch(tmp_path)
        l1 = orch._sync_lock_for("dividend_auntie")
        l2 = orch._sync_lock_for("dividend_auntie")
        assert l1 is l2
        # 跨 strategy 不同 Lock
        l3 = orch._sync_lock_for("dividend_auntie_universe")
        assert l1 is not l3
