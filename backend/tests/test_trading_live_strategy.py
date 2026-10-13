"""LiveStrategy Protocol + 注册表 + 多 schedule 测试。"""
from __future__ import annotations

import pytest

from app.services.trading.live_strategy import (
    LiveStrategy,
    LiveStrategyContext,
    LiveStrategyRegistry,
    StrategySchedule,
)


class _DummyStrategy(LiveStrategy):
    def __init__(self, id_: str = "dummy", qty: int = 0,
                 schedules: list[StrategySchedule] | None = None) -> None:
        self.id = id_
        self.name = f"Dummy {id_}"
        self.enabled_default = False
        self.schedules = schedules or [
            StrategySchedule(name="rebalance", hour=9, minute=26),
        ]
        self._qty = qty

    def on_event(self, event, context) -> dict[str, int]:
        return {"600519.SH": self._qty}


class _BrokenStrategy:
    """缺 on_event 的 duck type."""
    id = "broken"
    name = "broken"
    enabled_default = False
    schedules = []  # noqa: RUF012


def test_strategy_schedule_round_trip():
    s = StrategySchedule(name="pre_market", hour=9, minute=5)
    d = s.to_dict()
    s2 = StrategySchedule.from_dict(d)
    assert s2.name == s.name
    assert s2.hour == s.hour
    assert s2.minute == s.minute
    assert s2.enabled == s.enabled


def test_registry_register_and_get():
    reg = LiveStrategyRegistry()
    s = _DummyStrategy(qty=100)
    reg.register(s)
    assert reg.get("dummy") is s
    assert reg.ids() == ["dummy"]


def test_registry_register_missing_attrs_raises():
    reg = LiveStrategyRegistry()
    with pytest.raises(TypeError) as ei:
        reg.register(_BrokenStrategy())
    assert "on_event" in str(ei.value)


def test_registry_overwrite_flag():
    reg = LiveStrategyRegistry()
    s1 = _DummyStrategy(qty=1)
    s2 = _DummyStrategy(qty=2)
    reg.register(s1, overwrite=False)
    reg.register(s2, overwrite=False)
    assert reg.get("dummy") is s1
    reg.register(s2, overwrite=True)
    assert reg.get("dummy") is s2


def test_registry_discover_from_directory(tmp_path):
    (tmp_path / "valid_one.py").write_text(
        "from app.services.trading.live_strategy import LiveStrategy, StrategySchedule\n"
        "class V1(LiveStrategy):\n"
        "    id='v1'\n"
        "    name='V1'\n"
        "    enabled_default=False\n"
        "    schedules = [StrategySchedule(name='r', hour=9, minute=26)]\n"
        "    def on_event(self, ctx): return {}\n"
        "STRATEGY = V1()\n",
        encoding="utf-8",
    )
    (tmp_path / "_skip_me.py").write_text("ignored=True\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("raise RuntimeError('oops')\n", encoding="utf-8")
    (tmp_path / "no_strategy.py").write_text("x = 1\n", encoding="utf-8")

    reg = LiveStrategyRegistry()
    n = reg.discover_from(tmp_path)
    assert n == 1
    assert "v1" in reg.ids()
    s = reg.get("v1")
    assert len(s.schedules) == 1
    assert s.schedules[0].name == "r"


def test_registry_normalizes_dict_schedules():
    """schedules 用 dict 列表声明时, register 后归一化为 StrategySchedule."""
    class S(LiveStrategy):
        id = "s"
        name = "S"
        enabled_default = False
        schedules = [{"name": "r", "hour": 9, "minute": 26}]  # type: ignore[assignment]  # noqa: RUF012

        def on_event(self, ctx):
            return {}

    reg = LiveStrategyRegistry()
    reg.register(S())
    s = reg.get("s")
    assert isinstance(s.schedules[0], StrategySchedule)


def test_strategy_routes_by_event():
    s = _DummyStrategy(
        schedules=[
            StrategySchedule(name="pre_market", hour=9, minute=5),
            StrategySchedule(name="rebalance", hour=9, minute=26),
        ],
        qty=200,
    )
    ctx = LiveStrategyContext()
    assert s.on_event("pre_market", ctx) == {"600519.SH": 200}
    assert s.on_event("rebalance", ctx) == {"600519.SH": 200}
    # 未知 event: 走策略的默认实现 (返回 target), 这里 qty=200
    # 实际策略应当处理 unknown event; 这里只验证路由存在
