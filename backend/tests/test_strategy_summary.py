"""PR-1 (live-trading-strategy-summary.md) 后端改动单测.

覆盖:
- pnl.write_strategy_snapshot + compute_strategy_summary
- preferences notify_wechat toggle + delete_strategy_keys
- code_store move_to_trash + restore_from_trash
- LiveStrategyRegistry started_at_of + unregister

不依赖 FastAPI / router; 都是模块级纯函数 + 内存 registry 测试.
"""
from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path


import pytest


# ── 动态加载 (避免 trading 包顶层 import 触发重型依赖 (polars etc)) ────────
def _load(modname: str, filepath: str):
    spec = importlib.util.spec_from_file_location(modname, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


# 复用 trading 模块, 但只 import 子模块 (pnl/registry/prefs/code_store)
@pytest.fixture
def pnl_mod(tmp_path):
    sys.path.insert(0, str(Path(__file__).parent.parent))
    base = tmp_path / "user_data"
    base.mkdir(parents=True, exist_ok=True)
    return _load("pnl_test_isolated", "app/services/trading/pnl.py")


# ─── pnl.compute_strategy_summary ───
class TestComputeStrategySummary:
    def test_empty_returns_none(self, pnl_mod, tmp_path):
        # 没文件 / 没 records → None
        result = pnl_mod.compute_strategy_summary(tmp_path, "any_id")
        assert result is None

    def test_single_record_returns_full_summary(self, pnl_mod, tmp_path):
        # 写 1 条 → 7 字段非空, total_pnl_pct 接近 0 (刚启动后)
        snap = pnl_mod.StrategyPnLSnapshot(
            ts="2026-10-12T10:00:00+08:00",
            strategy_id="dividend_auntie",
            event="rebalance",
            total_pnl=100.0,
            cash_delta=100.0,
        )
        pnl_mod.write_strategy_snapshot(tmp_path, snap)
        result = pnl_mod.compute_strategy_summary(tmp_path, "dividend_auntie")
        assert result is not None
        assert result["total_pnl"] == 100.0
        assert "total_pnl_pct" in result
        assert "annual_return" in result
        assert "today_pnl" in result
        assert "today_pnl_pct" in result
        assert "max_drawdown" in result
        assert "max_drawdown_date" in result

    def test_filters_by_strategy_id(self, pnl_mod, tmp_path):
        # 不同 strategy_id 的 record 不计入该 id 的 summary
        pnl_mod.write_strategy_snapshot(tmp_path, pnl_mod.StrategyPnLSnapshot(
            ts="2026-10-12T10:00:00+08:00",
            strategy_id="strategy_a",
            event="rebalance",
            total_pnl=50.0,
        ))
        pnl_mod.write_strategy_snapshot(tmp_path, pnl_mod.StrategyPnLSnapshot(
            ts="2026-10-12T10:01:00+08:00",
            strategy_id="strategy_b",
            event="rebalance",
            total_pnl=9999.0,
        ))
        result_a = pnl_mod.compute_strategy_summary(tmp_path, "strategy_a")
        assert result_a is not None
        assert result_a["total_pnl"] == 50.0
        result_b = pnl_mod.compute_strategy_summary(tmp_path, "strategy_b")
        assert result_b is not None
        assert result_b["total_pnl"] == 9999.0

    def test_handles_malformed_lines(self, pnl_mod, tmp_path):
        # 写文件时混入坏行, 优雅跳过
        p = tmp_path / "user_data" / "trading" / "pnl_strategy_snapshots.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write(json.dumps({
                "ts": "2026-10-12T10:00:00+08:00",
                "strategy_id": "good",
                "event": "e",
                "total_pnl": 1.0,
                "cash_delta": 1.0,
            }) + "\n")
            f.write("{[broken json\n")
        result = pnl_mod.compute_strategy_summary(tmp_path, "good")
        assert result is not None
        assert result["total_pnl"] == 1.0


class TestComputeStrategySummaryAnnualGuard:
    """annual_return 仅在 days >= 30 时计算; 不足时返回 None, 避免 (1+r)^(365/d) 爆炸."""

    def test_single_record_returns_none_for_annual(self, pnl_mod, tmp_path):
        # Days = 0 → annual_return 应为 None (而不是爆炸数字)
        snap = pnl_mod.StrategyPnLSnapshot(
            ts="2026-10-12T10:00:00+08:00",
            strategy_id="s1", event="pre_market",
            total_pnl=100.0, cash_delta=100.0,
        )
        pnl_mod.write_strategy_snapshot(tmp_path, snap)
        result = pnl_mod.compute_strategy_summary(tmp_path, "s1")
        assert result is not None
        assert result["annual_return"] is None
        # 其他字段仍正常
        assert result["total_pnl"] == 100.0
        assert result["max_drawdown"] == 0.0

    def test_two_records_within_29_days_annual_none(self, pnl_mod, tmp_path):
        # 跨 29 天 → 仍 None
        from datetime import datetime, timezone, timedelta
        CN = timezone(timedelta(hours=8))
        for d in (1, 15, 29):
            ts = (datetime(2026, 1, 1, tzinfo=CN) + timedelta(days=d)).isoformat(timespec="seconds")
            pnl_mod.write_strategy_snapshot(tmp_path, pnl_mod.StrategyPnLSnapshot(
                ts=ts, strategy_id="s2", event="rebalance",
                total_pnl=10.0 * d, cash_delta=10.0,
            ))
        result = pnl_mod.compute_strategy_summary(tmp_path, "s2")
        assert result is not None
        assert result["annual_return"] is None

    def test_records_30_days_apart_annual_numeric(self, pnl_mod, tmp_path):
        # 跨 30 天 → annual_return 应是数字 (不再 None)
        from datetime import datetime, timezone, timedelta
        CN = timezone(timedelta(hours=8))
        for d in (0, 30):
            ts = (datetime(2026, 1, 1, tzinfo=CN) + timedelta(days=d)).isoformat(timespec="seconds")
            pnl_mod.write_strategy_snapshot(tmp_path, pnl_mod.StrategyPnLSnapshot(
                ts=ts, strategy_id="s3", event="rebalance",
                total_pnl=10000.0, cash_delta=10000.0,
            ))
        result = pnl_mod.compute_strategy_summary(tmp_path, "s3")
        assert result is not None
        assert isinstance(result["annual_return"], (int, float))
        assert result["annual_return"] > 0  # 赚了 → 年化正


class TestTrash:
    def test_move_to_trash_creates_file(self, tmp_path):
        # 动态加载 code_store
        cs = _load("cs_test_isolated", "app/services/trading/code_store.py")
        # 创建源文件
        (tmp_path / "user_data" / "strategies" / "live").mkdir(parents=True, exist_ok=True)
        src = tmp_path / "user_data" / "strategies" / "live" / "my_strategy.py"
        src.write_text("STRATEGY = None\n", encoding="utf-8")
        # 移到 trash
        dst = cs.move_to_trash(tmp_path, "my_strategy")
        assert dst.exists()
        assert dst.name.startswith("my_strategy_")
        assert ".trash" in str(dst)
        assert not src.exists()

    def test_move_to_trash_missing_raises(self, tmp_path):
        cs = _load("cs_test_isolated2", "app/services/trading/code_store.py")
        (tmp_path / "user_data" / "strategies" / "live").mkdir(parents=True, exist_ok=True)
        with pytest.raises(FileNotFoundError):
            cs.move_to_trash(tmp_path, "nonexistent")

    def test_restore_from_trash_round_trip(self, tmp_path):
        cs = _load("cs_test_isolated3", "app/services/trading/code_store.py")
        live = tmp_path / "user_data" / "strategies" / "live"
        live.mkdir(parents=True, exist_ok=True)
        src = live / "my_strategy.py"
        src.write_text("STRATEGY = None\n", encoding="utf-8")
        dst = cs.move_to_trash(tmp_path, "my_strategy")
        assert not src.exists()
        # 还原
        restored = cs.restore_from_trash(tmp_path, dst.name)
        assert restored == src
        assert restored.exists()
        assert restored.read_text(encoding="utf-8") == "STRATEGY = None\n"


# ─── preferences notify_wechat + delete ───
class TestPreferencesHelpers:
    def test_notify_wechat_toggle_default_false(self, tmp_path, monkeypatch):
        # 通过隔离 data_dir 让 prefs 走我们临时目录
        from app.services import preferences as base_prefs
        # patch base_prefs._path
        fake = tmp_path / "preferences.json"
        monkeypatch.setattr(base_prefs, "_path", lambda: fake)

        from app.services.trading import preferences as tp
        # 缺省 → False (key 不存在)
        assert tp.get_notify_wechat("any") is False
        # 写入
        tp.set_notify_wechat("any", True)
        # 重读
        assert tp.get_notify_wechat("any") is True
        # 验证 prefs.json 内容
        raw = json.loads(fake.read_text(encoding="utf-8"))
        assert raw["trading_strategies.any.notify_wechat"] is True

    def test_delete_strategy_keys_removes_all(self, tmp_path, monkeypatch):
        from app.services import preferences as base_prefs
        fake = tmp_path / "preferences.json"
        monkeypatch.setattr(base_prefs, "_path", lambda: fake)

        from app.services.trading import preferences as tp
        # 写多个 key
        tp.set_strategy_schedules("a", [{"name": "s", "hour": 9, "minute": 30, "enabled": True}])
        tp.set_notify_wechat("a", True)
        # 写另一个策略的 key, 验证不影响
        tp.set_notify_wechat("b", True)
        # 删 a
        deleted = tp.delete_strategy_keys("a")
        assert len(deleted) >= 2  # schedules + notify_wechat
        # a 的 keys 都没了
        assert tp.get_notify_wechat("a") is False
        # b 的 keys 还在
        assert tp.get_notify_wechat("b") is True


# ─── Registry started_at ───
class TestRegistryStartedAt:
    def test_started_at_set_on_register(self):
        reglib = _load("lr_test_isolated", "app/services/trading/live_strategy.py")
        reg = reglib.LiveStrategyRegistry()

        class StubStrategy:
            id = "s1"
            name = "S1"
            enabled_default = True
            schedules = []
            def on_event(self, *a, **kw): return {}

        s = StubStrategy()
        reg.register(s)
        ts = reg.started_at_of("s1")
        assert ts is not None
        # ISO ts 形如 '2026-10-12T...'
        assert "T" in ts

    def test_started_at_persistent_across_re_register(self):
        reglib = _load("lr_test_isolated2", "app/services/trading/live_strategy.py")
        reg = reglib.LiveStrategyRegistry()
        class StubStrategy:
            id = "s1"
            name = "S1"
            enabled_default = True
            schedules = []
            def on_event(self, *a, **kw): return {}

        s = StubStrategy()
        reg.register(s)
        first_ts = reg.started_at_of("s1")
        # 短暂延迟再 register (overwrite=True), started_at 不变
        time.sleep(0.01)
        reg.register(s)
        second_ts = reg.started_at_of("s1")
        assert first_ts == second_ts  # 不更新, 保留首次注册时间

    def test_unregister_clears(self):
        reglib = _load("lr_test_isolated3", "app/services/trading/live_strategy.py")
        reg = reglib.LiveStrategyRegistry()
        class StubStrategy:
            id = "s1"
            name = "S1"
            enabled_default = True
            schedules = []
            def on_event(self, *a, **kw): return {}
        s = StubStrategy()
        reg.register(s)
        reg.unregister("s1")
        assert reg.started_at_of("s1") is None
