"""实盘 preferences: schedules 列表 + 校验 + 兼容性."""
from __future__ import annotations

import pytest

import app.config
from app.services import preferences as base_prefs
from app.services.trading import preferences as t_prefs


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每个测试用 tmp_path 隔离 prefs.json."""
    base_prefs._invalidate_cache()
    monkeypatch.setattr(app.config.settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(t_prefs.base, "save", base_prefs.save)
    yield


def test_get_empty_returns_empty_list(tmp_path):
    assert t_prefs.get_strategy_schedules("nonexistent") == []


def test_set_and_get_schedules(tmp_path):
    out = t_prefs.set_strategy_schedules("x", [
        {"name": "pre_market", "hour": 9, "minute": 5, "enabled": True},
        {"name": "rebalance", "hour": 9, "minute": 26, "enabled": True},
    ])
    assert len(out) == 2
    # 再读
    cur = t_prefs.get_strategy_schedules("x")
    assert len(cur) == 2
    assert cur[0]["name"] == "pre_market"


def test_set_rejects_duplicate_names(tmp_path):
    with pytest.raises(ValueError) as ei:
        t_prefs.set_strategy_schedules("x", [
            {"name": "a", "hour": 9, "minute": 5},
            {"name": "a", "hour": 13, "minute": 30},
        ])
    assert "重复" in str(ei.value)


def test_set_rejects_invalid_hour(tmp_path):
    with pytest.raises(ValueError):
        t_prefs.set_strategy_schedules("x", [{"name": "a", "hour": 25, "minute": 0}])


def test_set_rejects_invalid_minute(tmp_path):
    with pytest.raises(ValueError):
        t_prefs.set_strategy_schedules("x", [{"name": "a", "hour": 9, "minute": 60}])


def test_set_rejects_missing_name(tmp_path):
    with pytest.raises(ValueError):
        t_prefs.set_strategy_schedules("x", [{"name": "", "hour": 9, "minute": 0}])


def test_is_strategy_schedule_enabled(tmp_path):
    t_prefs.set_strategy_schedules("x", [
        {"name": "a", "hour": 9, "minute": 0, "enabled": True},
        {"name": "b", "hour": 13, "minute": 0, "enabled": False},
    ])
    assert t_prefs.is_strategy_schedule_enabled("x", "a") is True
    assert t_prefs.is_strategy_schedule_enabled("x", "b") is False
    assert t_prefs.is_strategy_schedule_enabled("x", "c") is False
    assert t_prefs.is_strategy_schedule_enabled("nonexistent", "a") is False


def test_update_existing_schedule(tmp_path):
    t_prefs.set_strategy_schedules("x", [
        {"name": "a", "hour": 9, "minute": 0, "enabled": False},
    ])
    out = t_prefs.update_strategy_schedule("x", "a", enabled=True, hour=10)
    a = next(s for s in out if s["name"] == "a")
    assert a["enabled"] is True
    assert a["hour"] == 10


def test_update_appends_new_schedule(tmp_path):
    out = t_prefs.update_strategy_schedule("x", "new_one", enabled=True, hour=14, minute=15)
    assert len(out) == 1
    assert out[0]["name"] == "new_one"


def test_list_all_strategy_schedules(tmp_path):
    t_prefs.set_strategy_schedules("a", [{"name": "r", "hour": 9, "minute": 26}])
    t_prefs.set_strategy_schedules("b", [{"name": "p", "hour": 9, "minute": 5}])
    all_s = t_prefs.list_all_strategy_schedules()
    assert "a" in all_s and "b" in all_s
