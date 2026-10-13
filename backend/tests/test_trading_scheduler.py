"""StrategyScheduler._async_run 回归测试.

历史 bug: 2026-09-29 cron 全部报
  TypeError: StrategyExecutor.run_now() takes 2 positional arguments but 5 were given
根因: loop.run_in_executor 把 keyword 参数当成 positional 传给
StrategyExecutor.run_now (keyword-only), 触发 TypeError.

修复: _async_run 改用 functools.partial 显式绑定 keyword 参数, run_in_executor
只传 partial 自身。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.services.trading.scheduler import StrategyScheduler


@pytest.mark.asyncio
async def test_async_run_calls_executor_with_cron_trigger_and_event() -> None:
    """_async_run 必须把 trigger='cron' 和 event='<name>' 正确传给 run_now.

    回归原 TypeError: 修复前 run_in_executor 把 trigger= 和 event= 当 positional 传,
    因 run_now 是 keyword-only 签名而抛 TypeError.
    """
    executor = MagicMock()
    # run_now 返回任意值, 模拟真实调用
    executor.run_now.return_value = {"status": "ok"}

    scheduler = StrategyScheduler(scheduler=None, executor=executor)
    await scheduler._async_run("dividend_auntie", "rebalance")

    # 必须用关键字正确调用, 不能用 positional
    executor.run_now.assert_called_once_with(
        "dividend_auntie",
        trigger="cron",
        event="rebalance",
    )


@pytest.mark.asyncio
async def test_async_run_does_not_raise_type_error() -> None:
    """完整跑一遍 _async_run, 不能抛 TypeError.

    这条专门盯历史 bug: takes 2 positional arguments but N were given.
    """
    executor = MagicMock()
    scheduler = StrategyScheduler(scheduler=None, executor=executor)

    # 如果 _async_run 内部抛 TypeError, pytest 会失败
    try:
        await scheduler._async_run("dividend_auntie", "pre_market")
    except TypeError as e:
        pytest.fail(f"_async_run 抛 TypeError (历史 bug 回归): {e}")

    assert executor.run_now.called
    # 验证 trigger 是 cron, 不是 manual / api
    kwargs = executor.run_now.call_args.kwargs
    assert kwargs["trigger"] == "cron"
    assert kwargs["event"] == "pre_market"


@pytest.mark.asyncio
async def test_async_run_logs_and_swallows_executor_exception() -> None:
    """run_now 抛异常时, _async_run 必须 log 但不阻断.

    防止 scheduler 因为某次失败被 APScheduler 标记为 crashed 而停掉后续 job.
    """
    executor = MagicMock()
    executor.run_now.side_effect = RuntimeError("下游崩了")

    scheduler = StrategyScheduler(scheduler=None, executor=executor)

    # 不应向上抛
    await scheduler._async_run("dividend_auntie", "rebalance")


# ── 聚宽式调度优先级 (Task B1) ────────────────────
def test_kind_priority_ordering():
    """KIND_PRIORITY 决定同 (hour, minute) 多 schedule 的触发顺序: monthly < weekly < daily."""
    from app.services.trading.live_strategy import KIND_PRIORITY, StrategySchedule
    assert KIND_PRIORITY["monthly"] < KIND_PRIORITY["weekly"] < KIND_PRIORITY["daily"] < KIND_PRIORITY["manual"]


def test_schedule_kind_default_daily():
    """StrategySchedule 不传 kind: 默认 'daily'."""
    from app.services.trading.live_strategy import StrategySchedule
    s = StrategySchedule(name="x", hour=9, minute=35)
    assert s.kind == "daily"
    assert s.priority == 2


def test_schedule_kind_unknown_falls_back_daily():
    """未知 kind 被强制降级为 'daily' (防御性)."""
    from app.services.trading.live_strategy import StrategySchedule
    s = StrategySchedule(name="x", hour=9, minute=35, kind="nonsense")
    assert s.kind == "daily"
    assert s.priority == 2


def test_schedule_kind_to_from_dict():
    """kind 字段 round-trip via to_dict / from_dict."""
    from app.services.trading.live_strategy import StrategySchedule
    s = StrategySchedule(name="x", hour=9, minute=35, kind="weekly")
    d = s.to_dict()
    assert d["kind"] == "weekly"
    s2 = StrategySchedule.from_dict(d)
    assert s2.kind == "weekly"


def test_scheduler_apply_all_schedules_by_priority():
    """apply_all 把 (sid, sch) 按 (priority, hour, minute, sid, name) 排序后 add_job.

    用 mock scheduler 验证 add_job 调用顺序.
    """
    from unittest.mock import MagicMock
    from app.services.trading.live_strategy import StrategySchedule
    from app.services.trading.scheduler import StrategyScheduler

    mock_apscheduler = MagicMock()
    eng = MagicMock()
    sched = StrategyScheduler(mock_apscheduler, eng)
    prefs_mock = MagicMock()
    prefs_mock.is_strategy_schedule_enabled = lambda sid, ev: True

    import app.services.trading.scheduler as sched_mod
    sched_mod.prefs = prefs_mock

    # 3 个 schedule 同 (9, 35), kind 故意乱序
    s1 = MagicMock(); s1.id = "a"; s1.schedules = [StrategySchedule(name="n1", hour=9, minute=35, kind="daily")]
    s2 = MagicMock(); s2.id = "b"; s2.schedules = [StrategySchedule(name="n2", hour=9, minute=35, kind="monthly")]
    s3 = MagicMock(); s3.id = "c"; s3.schedules = [StrategySchedule(name="n3", hour=9, minute=35, kind="weekly")]

    sched.apply_all([s1, s2, s3])

    # add_job 顺序: monthly (b) → weekly (c) → daily (a)
    add_calls = mock_apscheduler.add_job.call_args_list
    assert len(add_calls) == 3
    # args[0] = strategy_id
    assert add_calls[0].kwargs["args"] == ["b", "n2"]
    assert add_calls[1].kwargs["args"] == ["c", "n3"]
    assert add_calls[2].kwargs["args"] == ["a", "n1"]


# ── after_code_changed 钩子 (Task B2) ─────────────
def test_after_code_changed_triggers_on_mtime_change(tmp_path):
    """策略文件 mtime 变化时, 调 on_after_code_changed 钩子."""
    from app.services.trading.scheduler import StrategyScheduler
    from app.services.trading.live_strategy import LiveStrategy, LiveStrategyContext, StrategySchedule

    strategy_file = tmp_path / "test_strat.py"
    strategy_file.write_text("# v1\nSTRATEGY = None\n", encoding="utf-8")

    class _MyStrat(LiveStrategy):
        id = "test_strat"
        name = "Test"
        enabled_default = False
        schedules = []  # no cron
        def on_event(self, event, context): return {}
        def on_after_code_changed(self, context): self.callback_called = True

    s = _MyStrat()
    sched = StrategyScheduler(None, None)  # no APScheduler needed for this test

    # 第一次调用: 文件存在, 触发 (首次)
    triggered = sched.check_code_changed([s], tmp_path)
    assert "test_strat" in triggered
    assert s.callback_called is True

    # 第二次调用: mtime 没变, 不触发
    s.callback_called = False
    triggered = sched.check_code_changed([s], tmp_path)
    assert triggered == []
    assert s.callback_called is False

    # 修改文件 mtime, 再次调用触发
    s.callback_called = False
    import os
    os.utime(strategy_file, (strategy_file.stat().st_atime, strategy_file.stat().st_mtime + 10))
    triggered = sched.check_code_changed([s], tmp_path)
    assert "test_strat" in triggered
    assert s.callback_called is True


def test_after_code_changed_silent_when_no_hook(tmp_path):
    """策略没实现 on_after_code_changed: 不抛, 跳过."""
    from app.services.trading.scheduler import StrategyScheduler
    from app.services.trading.live_strategy import LiveStrategy

    strategy_file = tmp_path / "no_hook.py"
    strategy_file.write_text("STRATEGY = None\n", encoding="utf-8")

    class _NoHook(LiveStrategy):
        id = "no_hook"
        name = "No Hook"
        enabled_default = False
        schedules = []
        def on_event(self, event, context): return {}

    s = _NoHook()
    sched = StrategyScheduler(None, None)
    triggered = sched.check_code_changed([s], tmp_path)
    # 触发列表返回空 (没回调)
    assert triggered == []


def test_after_code_changed_hook_exception_does_not_crash(tmp_path):
    """on_after_code_changed 抛异常: 不影响其他策略 + 不影响 cron jobs."""
    from app.services.trading.scheduler import StrategyScheduler
    from app.services.trading.live_strategy import LiveStrategy

    strategy_file = tmp_path / "boom.py"
    strategy_file.write_text("STRATEGY = None\n", encoding="utf-8")

    class _Boom(LiveStrategy):
        id = "boom"; name = "Boom"; enabled_default = False; schedules = []
        def on_event(self, event, context): return {}
        def on_after_code_changed(self, context): raise RuntimeError("boom!")

    s = _Boom()
    sched = StrategyScheduler(None, None)
    # 不应该抛
    triggered = sched.check_code_changed([s], tmp_path)
    assert triggered == []  # 失败时返回空, 不在 triggered 里
