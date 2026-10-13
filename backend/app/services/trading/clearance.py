"""17:00 清算 — 收盘后自动撤所有未完成委托.

聚宽行为:
  - A 股 15:00 收盘, 17:00 系统清算, 自动撤销所有未完成订单
  - 已成交 fill 不动, account cash 不动
  - 写 TradeLog(CANCEL, reason='17:00 自动清算')

实现:
  - run_daily_clearance_if_due(data_dir, engine) 由 _tick_once 每轮调用
  - 内部维护 _last_clearance_date 模块级状态, 每天 17:00~17:01 之间第一次调用时跑
  - 单笔 cancel 失败不中断, 写 ERROR 日志
  - 推到 SSE: kind='order_cleared'
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from datetime import time as _time

from app.services.trading import store
from app.services.trading.models import (
    OrderStatus,
    TradeLogEntry,
    TradeLogKind,
)

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))

# 默认清算时间 (用户决定: 写死 17:00, 17:00:00~17:00:59 之间第一轮 tick 触发)
DEFAULT_CLEARANCE_TIME = _time(17, 0)
# 清算窗口: 触发时间 ±30 秒
CLEARANCE_WINDOW_SECONDS = 30

# 模块级状态: 上次清算日期
_last_clearance_date: str | None = None


def _cn_now() -> datetime:
    return datetime.now(CN_TZ)


def _should_run_clearance(now: datetime, clearance_time: _time) -> bool:
    """是否该跑清算: now 的日期 != 上次清算日期 AND now 在 clearance_time 窗口内."""
    global _last_clearance_date
    today = now.strftime("%Y-%m-%d")
    if _last_clearance_date == today:
        return False
    target_dt = now.replace(
        hour=clearance_time.hour, minute=clearance_time.minute,
        second=0, microsecond=0,
    )
    delta = abs((now - target_dt).total_seconds())
    return delta <= CLEARANCE_WINDOW_SECONDS


def run_daily_clearance_if_due(data_dir, engine) -> int:
    """由 _tick_once 每轮调用; 命中 17:00 窗口时跑一次清算.

    Args:
        data_dir: 用户数据目录.
        engine: TradingEngine 实例, 用于 cancel() + SSE 推送.

    Returns:
        实际撤销的委托数 (0 表示本轮未触发或无 pending).
    """
    now = _cn_now()
    clearance_time = DEFAULT_CLEARANCE_TIME
    if not _should_run_clearance(now, clearance_time):
        return 0
    return _do_clearance(data_dir, engine, now)


def _do_clearance(data_dir, engine, now: datetime) -> int:
    global _last_clearance_date
    today = now.strftime("%Y-%m-%d")

    pending = store.pending_orders(data_dir)
    if not pending:
        # 记录日期, 避免后续 tick 重复判断
        _last_clearance_date = today
        logger.info("17:00 清算: 当日无 pending 委托, 跳过")
        return 0

    canceled_count = 0
    error_count = 0
    for order in pending:
        try:
            order.status = OrderStatus.CANCELED
            order.canceled_at = store.now_iso()
            order.reject_reason = "17:00 自动清算"
            store.update_order(data_dir, order)
            # 写日志
            entry = TradeLogEntry(
                id=store._new_id("lg"),
                kind=TradeLogKind.CANCEL,
                ts=store.now_iso(),
                symbol=order.symbol, order_id=order.id,
                message="17:00 自动清算撤单",
            )
            store.append_log(data_dir, entry)
            # SSE
            try:
                engine._broadcast_event({
                    "type": "order_cleared",
                    "order": order.model_dump(),
                    "reason": "clearance",
                })
            except Exception as e:
                logger.debug("17:00 清算 SSE 推送失败: %s", e)
            canceled_count += 1
        except Exception as e:
            error_count += 1
            logger.error("17:00 清算撤单失败: order_id=%s err=%s", order.id, e)

    _last_clearance_date = today
    logger.info(
        "17:00 清算完成: 撤单 %d, 失败 %d, 日期 %s",
        canceled_count, error_count, today,
    )
    return canceled_count


def reset_for_tests() -> None:
    """测试用: 清空模块级状态."""
    global _last_clearance_date
    _last_clearance_date = None


__all__ = [
    "DEFAULT_CLEARANCE_TIME",
    "reset_for_tests",
    "run_daily_clearance_if_due",
]
