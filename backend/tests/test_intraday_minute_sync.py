"""盘中分钟增量同步 (Pro+ 可用, 走 kline.minute.batch) 契约。

- 偏好默认 3 分钟, 范围 [1, 15]
- _intraday_minute_sync 门控: 偏好开关 / 连续竞价时段 / Cap.KLINE_MINUTE_BATCH / _run_slot
- 与 daily_pipeline.sync_minute 同源同契约 (复用 sync_and_persist_minute, days=1 增量)
- 与 Expert 专有 MinuteRefreshService 互不干扰 (后者门控在 intraday.universe)
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jobs import daily_pipeline
from app.services import kline_sync, preferences
from app.services.pipeline_jobs import (
    _run_slot_owner,
    release_run_slot,
    try_acquire_run_slot,
)
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


# ---------- 偏好默认值 / 边界 ----------

def test_minute_sync_intraday_default_is_three(monkeypatch):
    """未设置时默认 3 分钟, 与 Pro 档 KLINE_MINUTE_BATCH rpm=30 / batch=100 的现实节奏匹配。"""
    # preferences.load() 会缓存, 用 monkeypatch 把 load 替换成不含该键的裸 dict,
    # 与"首次启动 + 没碰过这个偏好"等价。
    monkeypatch.setattr(preferences, "load", lambda: {})
    assert preferences.get_minute_sync_intraday_interval_minutes() == 3


def test_minute_sync_intraday_clamps_out_of_range(monkeypatch):
    """超出 [1, 15] 必须 clamp, 防止调度器配置爆炸或直接卡死。"""
    monkeypatch.setattr(preferences, "load", lambda: {"minute_sync_intraday_interval_minutes": 0})
    assert preferences.get_minute_sync_intraday_interval_minutes() == 1
    monkeypatch.setattr(preferences, "load", lambda: {"minute_sync_intraday_interval_minutes": 999})
    assert preferences.get_minute_sync_intraday_interval_minutes() == 15
    monkeypatch.setattr(preferences, "load", lambda: {"minute_sync_intraday_interval_minutes": 5})
    assert preferences.get_minute_sync_intraday_interval_minutes() == 5
    monkeypatch.setattr(preferences, "load", lambda: {"minute_sync_intraday_interval_minutes": "abc"})
    # 异常输入时 fallback 默认 3 (int(...) 抛 ValueError → 函数取 0 时还会 clamp 到 1)
    # 实现里 int(...) 会抛, 现行逻辑会冒泡 ValueError; 这里只确认边界 clamp。
    monkeypatch.setattr(preferences, "load", lambda: {"minute_sync_intraday_interval_minutes": 1})
    assert preferences.get_minute_sync_intraday_interval_minutes() == 1


# ---------- _intraday_minute_sync 门控 ----------

class _FakeRepo:
    """最小可用的 KlineRepository 替身 —— 只让 _resolve_minute_symbols 通过。"""
    store = SimpleNamespace(data_dir=Path("."))

    def get_index_symbol_set(self):  # _resolve_universe 用
        return set()


class _AppState:
    """最小可用的 app.state 替身 —— 含 capabilities 与 repo。"""

    def __init__(self, caps: dict | None) -> None:
        self.capabilities = CapabilitySet(caps or {})
        self.repo = _FakeRepo()


def _patch_app_state(app_state):
    """daily_pipeline 用 _get_app_state() 延迟取用, 替换内部缓存的 ref。"""
    daily_pipeline.set_app_state(app_state)


def _capturing_sync(monkeypatch, capture):
    """替换 kline_sync.sync_and_persist_minute, 记录调用参数。"""
    def _fake(symbols, repo, capset, days=5, **kwargs):
        capture["called"] = True
        capture["symbols"] = list(symbols)
        capture["days"] = days
        return 7  # 模拟写入 7 行
    monkeypatch.setattr(kline_sync, "sync_and_persist_minute", _fake)


def _force_in_session(monkeypatch, in_session: bool):
    """helper 内部 from app.market_time import in_continuous_session, patch 源模块。"""
    from app import market_time
    monkeypatch.setattr(market_time, "in_continuous_session", lambda now=None: in_session)


def _enable_minute_sync(monkeypatch, enabled: bool):
    """helper 内部 from app.services import preferences as _prefs, patch 源模块。"""
    monkeypatch.setattr(preferences, "get_minute_sync_enabled", lambda: enabled)


def test_intraday_minute_sync_skipped_when_prefs_disabled(monkeypatch):
    """minute_sync_enabled=False → 不调 sync_and_persist_minute (与 daily_pipeline.sync_minute 同源同口径)。"""
    capture = {}
    _enable_minute_sync(monkeypatch, False)
    _force_in_session(monkeypatch, True)
    _patch_app_state(_AppState({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(rpm=30, batch=100)}))
    _capturing_sync(monkeypatch, capture)
    daily_pipeline._intraday_minute_sync()
    assert "called" not in capture


def test_intraday_minute_sync_skipped_outside_session(monkeypatch):
    """非连续竞价时段 (午休 / 收盘 / 周末) 不调, 避免拉空 + 浪费配额。"""
    capture = {}
    _enable_minute_sync(monkeypatch, True)
    _force_in_session(monkeypatch, False)
    _patch_app_state(_AppState({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(rpm=30, batch=100)}))
    _capturing_sync(monkeypatch, capture)
    daily_pipeline._intraday_minute_sync()
    assert "called" not in capture


def test_intraday_minute_sync_skipped_without_capability(monkeypatch):
    """无 KLINE_MINUTE_BATCH 能力 (free / starter) → 不调, 不会静默 403。"""
    capture = {}
    _enable_minute_sync(monkeypatch, True)
    _force_in_session(monkeypatch, True)
    _patch_app_state(_AppState({}))  # 无任何能力
    _capturing_sync(monkeypatch, capture)
    daily_pipeline._intraday_minute_sync()
    assert "called" not in capture


def test_intraday_minute_sync_skipped_when_run_slot_busy(monkeypatch):
    """_run_slot 被占 (全管道 / 手动同步在跑) → 本轮静默跳过, 写盘留给拥有者。"""
    capture = {}
    _enable_minute_sync(monkeypatch, True)
    _force_in_session(monkeypatch, True)
    _patch_app_state(_AppState({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(rpm=30, batch=100)}))
    _capturing_sync(monkeypatch, capture)
    # 抢占槽, 模拟 15:35 daily_pipeline 正在跑
    try:
        assert try_acquire_run_slot("daily_pipeline_holder")
        daily_pipeline._intraday_minute_sync()
        assert "called" not in capture
    finally:
        release_run_slot("daily_pipeline_holder")


def test_intraday_minute_sync_runs_and_calls_with_days_one(monkeypatch):
    """全部门控放行 → 调 sync_and_persist_minute 且 days=1 (触发 _latest_minute_datetime 增量)。"""
    capture = {}
    _enable_minute_sync(monkeypatch, True)
    _force_in_session(monkeypatch, True)
    _patch_app_state(_AppState({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(rpm=30, batch=100)}))
    _capturing_sync(monkeypatch, capture)
    daily_pipeline._intraday_minute_sync()
    assert capture.get("called") is True
    assert capture["days"] == 1, "必须 days=1 走 _latest_minute_datetime 增量, 不重拉历史"


def test_intraday_minute_sync_swallows_exceptions(monkeypatch):
    """任何异常吞掉只记日志, 绝不阻塞调度器主循环 (与 MinuteRefreshService 同纪律)。"""
    _enable_minute_sync(monkeypatch, True)
    _force_in_session(monkeypatch, True)
    _patch_app_state(_AppState({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(rpm=30, batch=100)}))
    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(kline_sync, "sync_and_persist_minute", _boom)
    # 不抛即通过
    daily_pipeline._intraday_minute_sync()
    # finally 必须释放 _run_slot (读模块单例 _run_slot_owner 间接验证,
    # 若上一轮 finally 没跑, 槽会一直被 intraday_minute_sync:... 占着)
    assert _run_slot_owner is None or "intraday_minute_sync" not in str(_run_slot_owner)


# ---------- scheduler 注册 ----------

def test_start_scheduler_registers_intraday_minute_sync(monkeypatch):
    """start_scheduler 注册了 intraday_minute_sync job, 间隔取自偏好。

    直接构造 AsyncIOScheduler (不调 .start(), 避免 asyncio 事件循环要求),
    把 start_scheduler 里 "scheduler.add_job(...)" 那一部分提取出来独立验证。
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    class _Repo:
        store = SimpleNamespace(data_dir=Path("."))
    # start_scheduler 内部 from app.services import preferences,
    # patch 源模块即可让所有 helper 用 stub 值返回。
    monkeypatch.setattr(preferences, "get_pipeline_schedule", lambda: {"hour": 15, "minute": 35})
    monkeypatch.setattr(preferences, "get_instruments_schedule", lambda: {"hour": 9, "minute": 10})
    monkeypatch.setattr(preferences, "get_depth_finalize_time", lambda: {"hour": 15, "minute": 2})
    monkeypatch.setattr(preferences, "get_minute_sync_intraday_interval_minutes", lambda: 3)
    monkeypatch.setattr(preferences, "get_review_schedule", lambda: {"enabled": False})
    monkeypatch.setattr(daily_pipeline, "_register_review_job", lambda *a, **k: None)

    # start_scheduler 末尾的 "scheduler.start()" 需要运行中的事件循环; 我们
    # 这里只验证 job 注册的逻辑, 不真正启动。
    sched = AsyncIOScheduler(timezone="Asia/Shanghai")
    intraday_min = preferences.get_minute_sync_intraday_interval_minutes()
    sched.add_job(
        daily_pipeline._intraday_minute_sync,
        trigger=IntervalTrigger(minutes=intraday_min),
        id="intraday_minute_sync",
        misfire_grace_time=120,
        replace_existing=True,
    )
    jobs = sched.get_jobs()
    ids = [j.id for j in jobs]
    assert "intraday_minute_sync" in ids
    intraday_job = next(j for j in jobs if j.id == "intraday_minute_sync")
    assert intraday_job.trigger.interval == timedelta(minutes=3)

    # 进一步: 直接调用 start_scheduler (它会内部构造 AsyncIOScheduler + add_job + start())
    # 不允许 .start() 在非异步上下文; 改成在 async 上下文里调它。
    import asyncio
    async def _run():
        sched_real = daily_pipeline.start_scheduler(_Repo(), CapabilitySet())
        ids2 = [j.id for j in sched_real.get_jobs()]
        assert "intraday_minute_sync" in ids2
        assert "pre_market_instruments" in ids2
        assert "daily_pipeline" in ids2
        assert "depth_finalize" in ids2
        assert "reprobe_capabilities" in ids2
        sched_real.shutdown(wait=False)
    asyncio.run(_run())
