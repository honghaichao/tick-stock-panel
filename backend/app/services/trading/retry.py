"""失败重试 — 按原 cron 触发时刻 (as_of) 撮合原 buys.

聚宽没有显式重试机制; 本 task 实现"补单"语义:
  - 用户调 POST /api/trading/runs/{run_id}/retry
  - 读 strategy_runs.jsonl 找 run_id 对应记录
  - 对每个 submitted order: 按 as_of 时刻撮合 (回放原 K 线)
  - 写新 fill, 落新 RunReport(retry_of=run_id)

不重跑策略 (避免重复决策); 仅撮合原订单, 跟"原 cron 时刻的撮合"对齐.
跨日不重试 (as_of 日期 != 当日 → 拒绝).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import polars as pl

from app.services.trading import executor as executor_mod
from app.services.trading import store
from app.services.trading.executor import RunReport
from app.services.trading.models import (
    Order,
    OrderSide,
    OrderStatus,
)
from app.services.trading.paper_broker import PaperBroker
from app.services.trading.slippage import SlippageConfig

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))


def _cn_today() -> date:
    return datetime.now(CN_TZ).date()


def _parse_iso_dt(s: str) -> datetime | None:
    """parse ISO 字符串, 支持 'Z' / '+08:00' / naive (假定北京时间)."""
    if not s:
        return None
    try:
        # 优先 fromisoformat; 处理 'Z'
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)
        return dt.astimezone(CN_TZ)
    except Exception as e:
        logger.warning("retry: 解析 ISO 时间失败: %s (%s)", s, e)
        return None


def _find_run_report(data_dir, run_id: str) -> dict | None:
    """从 strategy_runs.jsonl 找 run_id (用 started_at+event 拼接做 key)."""
    runs = executor_mod.list_runs(data_dir, limit=10_000)
    for r in runs:
        # 旧 schema 没 run_id 字段, 用 started_at + event + strategy_id 拼接做唯一 key
        rid = f"{r.get('strategy_id', '?')}::{r.get('started_at', '?')}::{r.get('event', '?')}"
        if rid == run_id or r.get("started_at", "") == run_id:
            return r
    return None


def _build_retry_key(run: dict) -> str:
    """构造 run_id (跟 _find_run_report 一致)."""
    return f"{run.get('strategy_id', '?')}::{run.get('started_at', '?')}::{run.get('event', '?')}"


def _get_bar_at_or_before(
    repo: Any, symbol: str, as_of: datetime, trade_date: date
) -> tuple[pl.DataFrame | None, bool]:
    """返回 as_of 时刻"已收盘"最后一根 K (close_time <= as_of - 60s) + 是否当日第一根.

    跟 engine._tick_once 的 K 选择语义一致.
    """
    try:
        df = repo.get_minute_batch([symbol], trade_date)
    except Exception as e:
        logger.warning("retry: 拉 %s 分钟 K 失败: %s", symbol, e)
        return None, False
    if df.is_empty():
        return None, False
    if "datetime" not in df.columns:
        return df.tail(1), False
    # 已收盘阈值: K close_time = datetime + 1m <= as_of - 60s
    threshold = as_of - timedelta(seconds=60)
    # K 线 datetime 是 naive (仓库惯例视为北京时间); threshold 是 aware,
    # polars pl.lit().cast(pl.Datetime) 会把 aware 转 UTC, 导致 closed 永远是空。
    # 提前去掉 tzinfo, 让两边都用 Beijing naive 比较 (数值一致)。
    threshold_naive = threshold.replace(tzinfo=None)
    try:
        closed = df.filter(
            (pl.col("datetime").cast(pl.Datetime) + pl.duration(minutes=1))
            <= pl.lit(threshold_naive).cast(pl.Datetime)
        ).sort("datetime")
    except Exception as e:
        logger.warning("retry: K 过滤失败: %s", e)
        return None, False
    if closed.is_empty():
        return None, False
    last = closed.tail(1)
    # 是否当日第一根 K (9:30)
    first_dt = closed.row(0, named=True).get("datetime")
    is_first = False
    if first_dt is not None and hasattr(first_dt, "hour"):
        is_first = (first_dt.hour == 9 and first_dt.minute == 30)
    return last, is_first


def retry_failed_run(
    data_dir,
    run_id: str,
    repo: Any | None = None,
    broker: PaperBroker | None = None,
    slippage: SlippageConfig | None = None,
    *,
    now_iso: str | None = None,
) -> dict:
    """重试一个失败的 run: 按原 as_of 撮合原 submitted orders.

    Args:
        data_dir: 用户数据目录.
        run_id: run 标识 (策略id::started_at::event).
        repo: TickflowRepository 实例 (用于拉 K 线); None 时从 _APP_STATE 拿.
        broker: Broker 实例; None = 新建 PaperBroker.
        slippage: 撮合滑点; None = 不滑.
        now_iso: 重试触发时间; 测试用.

    Returns:
        新 RunReport dict. status 可能是 'success' / 'no_match' / 'error'.
    """
    now_iso = now_iso or store.now_iso()
    report = RunReport(
        strategy_id="?",
        event="retry",
        started_at=now_iso,
        trigger="retry",
        status="error",
    )
    try:
        # 1) 找原 run
        run = _find_run_report(data_dir, run_id)
        if run is None:
            report.error = f"run not found: {run_id}"
            return report.to_dict()
        report.strategy_id = run.get("strategy_id", "?")
        report.event = f"retry:{run.get('event', '?')}"

        # 2) 校验 as_of 是否当日 (跨日拒绝)
        as_of_str = run.get("started_at", "")
        as_of = _parse_iso_dt(as_of_str)
        if as_of is None:
            report.error = f"as_of 解析失败: {as_of_str}"
            return report.to_dict()
        if as_of.date() != _cn_today():
            report.error = f"跨日不重试: as_of={as_of.date()}, today={_cn_today()}"
            report.status = "no_match"
            return report.to_dict()

        # 3) 拿 repo / broker
        if repo is None:
            from app.services.trading import engine as engine_mod
            app_state = engine_mod._APP_STATE
            repo = getattr(app_state, "repo", None) if app_state else None
        if repo is None:
            report.error = "无法访问 repo (请确认 app.state 已注入)"
            return report.to_dict()
        if broker is None:
            broker = PaperBroker()
        slippage = slippage or SlippageConfig()

        # 4) 找原 submitted order_ids, 读 order
        submitted_ids = run.get("submitted", []) or []
        if not submitted_ids:
            report.error = "原 run 无 submitted orders, 无需重试"
            report.status = "no_match"
            return report.to_dict()

        orders: list[Order] = []
        for oid in submitted_ids:
            o = store.get_order(data_dir, oid)
            if o is None:
                logger.warning("retry: order %s 已不在, 跳过", oid)
                continue
            if o.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED):
                logger.info("retry: order %s 已是终态 (%s), 跳过", oid, o.status.value)
                continue
            orders.append(o)
        if not orders:
            report.error = "原 submitted orders 全部已终结, 无需重试"
            report.status = "no_match"
            return report.to_dict()

        # 5) 按 as_of 撮合: 拉 K → broker.match_tick
        bar_by_symbol: dict[str, pl.DataFrame] = {}
        first_bar: dict[str, bool] = {}
        for o in orders:
            bar, is_first = _get_bar_at_or_before(repo, o.symbol, as_of, as_of.date())
            if bar is None:
                logger.info("retry: %s 在 as_of=%s 时刻无已收盘 K, 跳过", o.symbol, as_of)
                continue
            bar_by_symbol[o.symbol] = bar
            first_bar[o.symbol] = is_first

        if not bar_by_symbol:
            report.error = "所有订单在 as_of 时刻都无 K 线数据"
            report.status = "no_match"
            return report.to_dict()

        decisions = broker.match_tick(
            orders, bar_by_symbol,
            slippage=slippage,
            order_volume_ratio=1.0,    # retry 不限成交量
            first_bar_of_day=first_bar,
        )
        # 6) 落 fill + 更新 order 状态 + 更新 account
        from app.services.trading import engine as engine_mod
        eng = getattr(engine_mod, "_APP_STATE", None)
        trading_engine = getattr(eng, "trading_engine", None) if eng else None

        any_filled = False
        for d in decisions:
            if d.matched:
                _settle_retry_fill(data_dir, orders, d, as_of)
                any_filled = True
            else:
                logger.info("retry: order 未撮合 (%s) reason=%s", d.order_id, d.reason)

        if any_filled:
            report.status = "success"
        else:
            report.status = "no_match"
            report.error = "K 线满足条件但 broker 未撮合 (限价未触及 / 涨跌停)"

        # 7) 写新 RunReport (retry_of 字段标记原 run)
        d = report.to_dict()
        d["retry_of"] = _build_retry_key(run)
        _append_retry_report(data_dir, d)
        return d
    except Exception as e:
        logger.exception("retry 异常: %s", e)
        report.error = f"{type(e).__name__}: {e}"
        d = report.to_dict()
        d["retry_of"] = run_id
        _append_retry_report(data_dir, d)
        return d
    finally:
        report.finished_at = now_iso
        # 落 report 已在 finally 之外的 try/except 路径处理


def _settle_retry_fill(
    data_dir, orders: list[Order], decision, as_of: datetime
) -> None:
    """把 retry 撮合的 fill 落到 fills.jsonl + 更新 order 状态 + 更新 account."""
    # 找原 order
    order = next((o for o in orders if o.id == decision.order_id), None)
    if order is None:
        return
    if decision.matched and decision.fill_price is not None and decision.fill_qty is not None:
        # 落 fill
        from app.services.trading.models import Fill
        fee = max(5.0, decision.fill_qty * decision.fill_price * 0.0003)
        tax = decision.fill_qty * decision.fill_price * 0.0005 if order.side == OrderSide.SELL else 0.0
        amount = decision.fill_qty * decision.fill_price
        fill = Fill(
            id=store._new_id("fl"),
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=decision.fill_qty,
            price=decision.fill_price,
            fee=fee, tax=tax,
            amount=amount,
            filled_at=store.now_iso(),
            trade_date=as_of.date(),
        )
        store.append_fill(data_dir, fill)
        # 更新 order 状态
        order.filled_qty = decision.fill_qty
        order.avg_fill_price = decision.fill_price
        order.filled_at = store.now_iso()
        order.status = OrderStatus.FILLED
        store.update_order(data_dir, order)
        # 更新 account (简化: 不引 engine, 手动算 cash 影响)
        from app.services.trading import preferences as prefs
        account = store.load_account(data_dir, initial_cash=prefs.initial_cash())
        if order.side == OrderSide.BUY:
            account.cash -= (amount + fee)
        else:
            account.cash += (amount - fee - tax)
        store.save_account(data_dir, account)
        logger.info(
            "retry fill: order=%s symbol=%s side=%s qty=%d price=%.3f",
            order.id, order.symbol, order.side.value,
            decision.fill_qty, decision.fill_price,
        )


def _append_retry_report(data_dir, report_dict: dict) -> None:
    """retry 报告落 strategy_runs.jsonl (跟 executor 一致)."""
    import json
    p = data_dir / "user_data" / "trading" / "strategy_runs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(report_dict, ensure_ascii=False) + "\n")


__all__ = ["retry_failed_run"]
