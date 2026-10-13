"""F0-4: dry-verify gate (保存策略前 importlib + on_event 跑一次)."""
from __future__ import annotations

import pytest

from app.services.trading import auto_verify, code_store


def _good_strategy_content() -> str:
    """一个合法策略: 1 个 schedule + on_event 返空 dict."""
    return (
        "from app.services.trading.live_strategy import LiveStrategy, StrategySchedule\n"
        "\n"
        "class _S(LiveStrategy):\n"
        "    id = 't'\n"
        "    name = 't'\n"
        "    enabled_default = False\n"
        "    schedules = [StrategySchedule(name='rebalance', hour=9, minute=35)]\n"
        "    def on_event(self, event, ctx):\n"
        "        return {}\n"
        "\n"
        "STRATEGY = _S()\n"
    )


def _broken_strategy_no_STRATEGY() -> str:
    return (
        "from app.services.trading.live_strategy import LiveStrategy, StrategySchedule\n"
        "class _S(LiveStrategy):\n"
        "    id = 't'\n"
        "    name = 't'\n"
        "    enabled_default = False\n"
        "    schedules = []\n"
        "    def on_event(self, event, ctx):\n"
        "        return {}\n"
    )


def _broken_strategy_on_event_raises() -> str:
    return (
        "from app.services.trading.live_strategy import LiveStrategy, StrategySchedule\n"
        "class _S(LiveStrategy):\n"
        "    id = 't'\n"
        "    name = 't'\n"
        "    enabled_default = False\n"
        "    schedules = [StrategySchedule(name='rebalance', hour=9, minute=35)]\n"
        "    def on_event(self, event, ctx):\n"
        "        raise ValueError('boom')\n"
        "\n"
        "STRATEGY = _S()\n"
    )


def _broken_strategy_import_error() -> str:
    return (
        "import os\n"   # 合法 import; 但下一步引用不存在的模块触发 ImportError? 改成 raise
        "raise ImportError('mock')\n"
        "STRATEGY = None\n"
    )


# ── 直接调 dry_verify_strategy_code ──────────────────────────────────────
def test_dry_verify_passes_for_good_strategy():
    r = auto_verify.dry_verify_strategy_code(_good_strategy_content())
    assert r.passed is True
    assert r.target_count == 0
    assert r.error_kind is None


def test_dry_verify_fails_when_no_STRATEGY():
    r = auto_verify.dry_verify_strategy_code(_broken_strategy_no_STRATEGY())
    assert r.passed is False
    assert r.error_kind == "no_strategy_instance"


def test_dry_verify_fails_when_on_event_raises():
    r = auto_verify.dry_verify_strategy_code(_broken_strategy_on_event_raises())
    assert r.passed is False
    assert r.error_kind == "on_event_error"
    assert "boom" in (r.error_msg or "")


def test_dry_verify_fails_when_import_raises():
    r = auto_verify.dry_verify_strategy_code(_broken_strategy_import_error())
    assert r.passed is False
    assert r.error_kind == "import_error"


# ── 集成: write_code(verify=True) ───────────────────────────────────────
def test_write_code_verify_true_blocks_bad_strategy(tmp_path):
    """verify=True + 坏策略 → RuntimeError('verify_failed:...') + 不落盘."""
    bad = _broken_strategy_on_event_raises()
    with pytest.raises(RuntimeError, match="verify_failed:on_event_error"):
        code_store.write_code(tmp_path, "t", bad, verify=True)
    # 不落盘
    p = code_store._strategy_file(tmp_path, "t")
    assert not p.exists()


def test_write_code_verify_false_keeps_backward_compat(tmp_path):
    """verify=False (默认) → 不跑 dry verify, 坏策略也能写入 (回归保护)."""
    bad = _broken_strategy_on_event_raises()
    entry = code_store.write_code(tmp_path, "t", bad, verify=False)
    assert entry["strategy_id"] == "t"
    p = code_store._strategy_file(tmp_path, "t")
    assert p.exists()


def test_write_code_verify_true_passes_for_good_strategy(tmp_path):
    """verify=True + 好策略 → 落盘."""
    good = _good_strategy_content()
    entry = code_store.write_code(tmp_path, "t", good, verify=True)
    assert entry["strategy_id"] == "t"
    p = code_store._strategy_file(tmp_path, "t")
    assert p.exists()
    assert "STRATEGY" in p.read_text(encoding="utf-8")
