"""StrategyScheduler — 一个策略多个 cron 时点, 各自触发 on_event(event, ctx).

设计:
  - 每个策略 x 每个 schedule 各挂 1 个 APScheduler job.
  - job 入口 = _async_run(strategy_id, event) → executor.run_now(strategy_id, event=event).
  - schedules 配置存在 preferences.json:
      trading_strategies.{id}.schedules = [{name, hour, minute, enabled}]
  - apply_all() 增量更新: 已存在的 job replace_existing=true, 已禁用的 schedule
    remove_job.

不做什么:
  - 不重新实现调度 — APScheduler 已经在仓库广泛使用, 复用 + 测试矩阵.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial
from typing import Any

from app.services.trading import preferences as prefs
from app.services.trading.live_strategy import LiveStrategy

logger = logging.getLogger(__name__)


class StrategyScheduler:
    """包装 APScheduler, 提供 per-strategy-per-schedule 的多 job 注册."""

    def __init__(self, scheduler: Any | None, executor: Any) -> None:
        self._scheduler = scheduler
        self._executor = executor
        # job_id 命名: live_strategy_{sid}__{event}; 双下划线分隔避免与 sid 内字符冲突
        self._job_ids: dict[tuple[str, str], str] = {}
        # after_code_changed mtime 缓存: {sid: 上次记录的 mtime}
        self._mtime_cache: dict[str, float] = {}

    @property
    def has_external_scheduler(self) -> bool:
        return self._scheduler is not None

    def _job_id(self, strategy_id: str, event: str) -> str:
        return f"live_strategy_{strategy_id}__{event}"

    def apply_all(
        self,
        strategies: list[LiveStrategy],
        enabled_schedule_filter: dict[tuple[str, str], bool] | None = None,
    ) -> None:
        """按策略 + schedules 批量注册 / 移除 jobs.

        Args:
            strategies: 已注册的 LiveStrategy 实例列表.
            enabled_schedule_filter: {(sid, event): enabled_bool}; None 表示全部按 preferences.
        """
        if self._scheduler is None:
            logger.warning("StrategyScheduler: 无外部 scheduler, 跳过定时配置")
            return
        from apscheduler.triggers.cron import CronTrigger

        # 1. 收集期望的 (sid, event, schedule) 列表 — 按聚宽优先级排序:
        #    同 (hour, minute) 多 schedule: monthly < weekly < daily < manual.
        #    APScheduler 同时间 job 的执行顺序由 add_job 顺序决定, 所以这里排序后再 add.
        expected: set[tuple[str, str]] = set()
        schedules_to_register: list[tuple[LiveStrategy, Any]] = []
        for s in strategies:
            schedules = s.schedules or []
            for sch in schedules:
                if enabled_schedule_filter and (s.id, sch.name) in enabled_schedule_filter:
                    en = enabled_schedule_filter[(s.id, sch.name)]
                else:
                    en = prefs.is_strategy_schedule_enabled(s.id, sch.name)
                if not en:
                    continue
                expected.add((s.id, sch.name))
                schedules_to_register.append((s, sch))
        # 按 (priority, hour, minute, sid, name) 排序 — APScheduler 触发顺序可预测
        schedules_to_register.sort(key=lambda t: (
            t[1].priority,
            t[1].hour,
            t[1].minute,
            t[0].id,
            t[1].name,
        ))
        for s, sch in schedules_to_register:
                try:
                    self._scheduler.add_job(
                        self._async_run,
                        trigger=CronTrigger(
                            hour=sch.hour, minute=sch.minute,
                            day_of_week=getattr(sch, "day_of_week", "mon-fri"),
                            timezone="Asia/Shanghai",
                        ),
                        args=[s.id, sch.name],
                        id=self._job_id(s.id, sch.name),
                        misfire_grace_time=3600,
                        replace_existing=True,
                    )
                    self._job_ids[(s.id, sch.name)] = self._job_id(s.id, sch.name)
                    logger.info(
                        "实盘策略 cron: %s :: %s @ %02d:%02d",
                        s.id, sch.name, sch.hour, sch.minute,
                    )
                except Exception as e:
                    logger.warning("注册策略 %s/%s 失败: %s", s.id, sch.name, e)

        # 2. 移除已不在期望集合里的旧 jobs
        for key, jid in list(self._job_ids.items()):
            if key not in expected:
                self._safe_remove(jid)
                del self._job_ids[key]
                logger.info("已移除: %s", jid)

    def _safe_remove(self, job_id: str) -> None:
        with contextlib.suppress(Exception):
            self._scheduler.remove_job(job_id)

    async def _async_run(self, strategy_id: str, event: str) -> None:
        """APScheduler 调度入口; executor 是同步的, 推到线程池执行."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                partial(
                    self._executor.run_now,
                    strategy_id,
                    trigger="cron",
                    event=event,
                ),
            )
        except Exception as e:
            logger.exception("cron 触发策略执行失败: %s/%s (%s)", strategy_id, event, e)

    def next_run(self, strategy_id: str, event: str | None = None) -> str | None:
        """返回该策略 (或计划)的下次执行时间 (ISO8601); 无 → None."""
        if self._scheduler is None:
            return None
        if event is not None:
            jid = self._job_ids.get((strategy_id, event))
            if jid is None:
                return None
            try:
                job = self._scheduler.get_job(jid)
                if job is None:
                    return None
                nxt = job.next_run_time
                return nxt.isoformat() if nxt else None
            except Exception:
                return None
        # event=None: 返回该策略所有 event 中最近的
        candidates = [k for k in self._job_ids if k[0] == strategy_id]
        if not candidates:
            return None
        times = []
        for sid, ev in candidates:
            t = self.next_run(sid, ev)
            if t:
                times.append(t)
        return min(times) if times else None

    def remove_strategy(self, strategy_id: str) -> None:
        """移除某策略的所有 jobs."""
        for (sid, ev), jid in list(self._job_ids.items()):
            if sid == strategy_id:
                self._safe_remove(jid)
                del self._job_ids[(sid, ev)]

    # ── after_code_changed 钩子 ────────────────────
    def check_code_changed(
        self,
        strategies: list[LiveStrategy],
        strategies_dir: Path,
        *,
        on_change=None,
    ) -> list[str]:
        """检查策略文件 mtime 是否变化, 变了调 strategy.on_after_code_changed(ctx).

        启动时也跑一次 (用户首次启动的代码即"变更"). 不影响 cron jobs, 仅调钩子.

        Args:
            strategies: 已注册的 LiveStrategy 实例列表.
            strategies_dir: 策略文件目录 (e.g. data/user_data/strategies/live/).
            on_change: 回调 (strategy, ctx) → None; 默认用 strategy.on_after_code_changed 兜底.

        Returns:
            触发了 after_code_changed 的 strategy id 列表.
        """
        if not strategies_dir.exists():
            return []
        triggered: list[str] = []
        for s in strategies:
            f = strategies_dir / f"{s.id}.py"
            if not f.exists():
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            key = f"_last_mtime:{s.id}"
            last = self._mtime_cache.get(key)
            if last is None or last != mtime:
                # 首次 / 变化: 跑钩子
                self._mtime_cache[key] = mtime
                cb = on_change or getattr(s, "on_after_code_changed", None)
                if cb is None:
                    # 默认空实现: 不做啥
                    logger.info("after_code_changed: %s 无自定义实现, 跳过", s.id)
                    continue
                try:
                    # 构造一个空 context (钩子不依赖实盘数据, 跟聚宽语义一致)
                    from app.services.trading.live_strategy import LiveStrategyContext
                    ctx = LiveStrategyContext()
                    cb(ctx)
                    triggered.append(s.id)
                    logger.info("after_code_changed 触发: %s", s.id)
                except Exception as e:
                    logger.exception("after_code_changed 失败: %s (%s)", s.id, e)
        return triggered


__all__ = ["StrategyScheduler"]
