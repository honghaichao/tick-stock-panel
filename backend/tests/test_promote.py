"""F1-1: promote 晋级 API 测试."""
from __future__ import annotations

from app.services.trading import code_store, promote


def _write_ai_strategy(tmp_path, strategy_id: str) -> None:
    out = tmp_path / "strategies" / "ai"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{strategy_id}.py").write_text(
        f"""from app.services.trading.live_strategy import LiveStrategy, StrategySchedule

class _S(LiveStrategy):
    id = '{strategy_id}'
    name = 'promoted test'
    enabled_default = False
    schedules = [StrategySchedule(name='rebalance', hour=9, minute=35)]
    def on_event(self, event, ctx):
        return {{}}

STRATEGY = _S()
""",
        encoding="utf-8",
    )


def _write_custom_strategy(tmp_path, strategy_id: str) -> None:
    out = tmp_path / "strategies" / "custom"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{strategy_id}.py").write_text(
        f"""from app.services.trading.live_strategy import LiveStrategy, StrategySchedule

class _S(LiveStrategy):
    id = '{strategy_id}'
    name = 'custom promoted test'
    enabled_default = False
    schedules = [StrategySchedule(name='rebalance', hour=9, minute=35)]
    def on_event(self, event, ctx):
        return {{}}

STRATEGY = _S()
""",
        encoding="utf-8",
    )


# ── promote_to_live 直接测试 ──────────────────────────────────────
def test_promote_ai_to_live_success(tmp_path):
    _write_ai_strategy(tmp_path, "ai_test")
    r = promote.promote_to_live(tmp_path, "ai_test", "ai", verify=False)
    assert r.error is None
    assert r.strategy_id == "ai_test"
    assert r.source == "ai"
    assert r.target_path.exists()
    assert "STRATEGY" in r.target_path.read_text(encoding="utf-8")
    # changelog 也写了
    items = code_store.list_changelog(tmp_path)
    assert any(item["strategy_id"] == "ai_test" for item in items)


def test_promote_custom_to_live_success(tmp_path):
    _write_custom_strategy(tmp_path, "custom_test")
    r = promote.promote_to_live(tmp_path, "custom_test", "custom", verify=False)
    assert r.error is None
    assert r.target_path.exists()


def test_promote_rejects_unknown_source(tmp_path):
    r = promote.promote_to_live(tmp_path, "x", "malicious_dir", verify=False)
    assert r.error is not None
    assert "不支持" in r.error


def test_promote_rejects_missing_source_file(tmp_path):
    r = promote.promote_to_live(tmp_path, "nonexistent", "ai", verify=False)
    assert r.error is not None
    assert "源文件不存在" in r.error


def test_promote_rejects_overwrite_when_disabled(tmp_path):
    """目标 live/{id}.py 已存在 + overwrite=False → 拒绝."""
    _write_ai_strategy(tmp_path, "dupe")
    # 先正常 promote 一次
    promote.promote_to_live(tmp_path, "dupe", "ai", verify=False)
    # 第二次 (overwrite=False) → 拒绝
    r = promote.promote_to_live(tmp_path, "dupe", "ai", verify=False, overwrite=False)
    assert r.error is not None
    assert "目标已存在" in r.error


def test_promote_overwrite_when_enabled(tmp_path):
    """目标已存在 + overwrite=True → 覆盖."""
    _write_ai_strategy(tmp_path, "ow")
    promote.promote_to_live(tmp_path, "ow", "ai", verify=False)
    r = promote.promote_to_live(tmp_path, "ow", "ai", verify=False, overwrite=True)
    assert r.error is None


def test_promote_with_verify_blocks_bad_strategy(tmp_path):
    """verify=True + 源策略 on_event 抛错 → 失败, 不落盘."""
    bad = tmp_path / "strategies" / "ai"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "broken.py").write_text(
        """from app.services.trading.live_strategy import LiveStrategy, StrategySchedule

class _S(LiveStrategy):
    id = 'broken'
    name = 'broken'
    enabled_default = False
    schedules = [StrategySchedule(name='rebalance', hour=9, minute=35)]
    def on_event(self, event, ctx):
        raise ValueError('boom')

STRATEGY = _S()
""",
        encoding="utf-8",
    )
    r = promote.promote_to_live(tmp_path, "broken", "ai", verify=True)
    assert r.error is not None
    assert "RuntimeError" in r.error
    # 不落盘
    assert not (tmp_path / "user_data" / "strategies" / "live" / "broken.py").exists()


# ── discover_live_after_promote ──────────────────────────────────────
def test_discover_live_after_promote_registers(tmp_path):
    """晋级后调 discover → 策略进入 Registry."""
    from app.services.trading.live_strategy import LiveStrategyRegistry

    _write_ai_strategy(tmp_path, "reg_test")
    promote.promote_to_live(tmp_path, "reg_test", "ai", verify=False)

    reg = LiveStrategyRegistry()
    n = promote.discover_live_after_promote(reg, tmp_path)
    assert n >= 1
    assert reg.get("reg_test") is not None
