"""实盘策略共用的数据就绪辅助 — 2026-10-13 引入.

背景:
  09:35 rebalance cron 触发时, quote_service 缓存没刷全, last_price 多为 0.
  build_data_provider() 用 last_price * total_shares 算 market_cap, 大量为 0.
  get_dividend_ratio_filter_list inner-join cap_df 后 filter(market_cap > 0) 把候选砍光,
  返回 [], my_Trader 提前 return {} → executor 报 no_target 不调仓.

修法:
  wait_for_quote_data 接受 quote_service, **直接 poll quote_service.get_quotes_compat()**
  看缓存里 last_price (close) > 0 的 row 数; 而不是看 dp.market_cap (frozen snapshot).
  
关键设计点 — 必须 BEFORE build_data_provider:
  build_data_provider 返回的 market_cap 是 **一次性 snapshot**, 后续 quote_service 数据
  更新不会回流. 所以 wait 必须发生在 _build_context 调 build_data_provider 之前,
  而不是策略的 on_event 入口. (放到策略里就是 Lock-H2 描述的假修复.)

API:
  wait_for_quote_data(qs=..., min_coverage=0.80, max_wait_s=60.0, poll_s=2.0,
                       expected_count=5882, event='rebalance')
"""
from __future__ import annotations

import logging
import time

import polars as pl

logger = logging.getLogger(__name__)


# 默认 5882 = 沪深 A 股股票活跃 universe 量级 (与 self.instruments.height 一致);
# 调用方应传 repo.get_instruments().height 以适配未来 universe 调整.
DEFAULT_EXPECTED_INSTRUMENT_COUNT = 5882
DEFAULT_MIN_COVERAGE = 0.80
DEFAULT_MAX_WAIT_S = 60.0
DEFAULT_POLL_S = 2.0


def _measure_coverage_qs(qs) -> int:
    """Poll quote_service live, count rows where close > 0 (= 已刷新的 last_price).

    Returns -1 (sentinel) when:
      - qs is None, OR
      - qs.get_quotes_compat() raised (让 caller 退化为 dp.market_cap)
    Returns 0 when qs is available but data is genuinely empty / no close column.
    """
    if qs is None:
        return -1
    try:
        df = qs.get_quotes_compat()
    except Exception as e:
        logger.debug("wait_for_quote_data: qs probe failed: %s", e)
        return -1
    if df is None or df.is_empty():
        return 0
    col = "close" if "close" in df.columns else "last_price"
    if col not in df.columns:
        return 0
    try:
        filled = df.filter(pl.col(col).cast(pl.Float64, strict=False).fill_null(0.0) > 0.0).height
    except Exception:
        filled = df.filter(pl.col(col).is_not_null()).height
    return int(filled)


def _measure_coverage_dp(context) -> int:
    """Legacy: poll the frozen dp.market_cap dict (only useful for legacy callers; not used in production now)."""
    try:
        dp = getattr(context, "data_provider", None) if context is not None else None
        if dp is not None and hasattr(dp, "market_cap"):
            return sum(1 for v in dp.market_cap.values() if v > 0)
    except Exception:
        return 0
    return 0


def wait_for_quote_data(
    quote_service=None,
    *,
    context=None,                 # legacy / tests: dp.market_cap fallback
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    max_wait_s: float = DEFAULT_MAX_WAIT_S,
    poll_s: float = DEFAULT_POLL_S,
    expected_count: int = DEFAULT_EXPECTED_INSTRUMENT_COUNT,
    event: str = "?",
) -> int:
    """阻塞直到 quote cache 至少 min_coverage × expected_count 行有 close>0, 或 max_wait_s 超时.

    Args:
        quote_service: 首选. 传它就 poll live (修复 H2 的关键).
        context: 旧 API 保留. 没传 quote_service 时退化为看 dp.market_cap (frozen).
        min_coverage: 0-1. 默认 0.80 (实测完整时 94.6%).
        max_wait_s: 最长阻塞秒数. 默认 60s.
        poll_s: 重试间隔. 默认 2s.
        expected_count: 数据 universe 期望行数 (来自 instruments).
        event: 写日志用的 event 名 ('rebalance', 'pre_market' 等).

    Returns:
        当前 filled row count. Returns -1 if no usable source (qs + context 都没有).
    """
    target = max(1, int(expected_count * min_coverage))
    deadline = time.time() + max_wait_s
    last_reported = -1

    while True:
        if quote_service is not None:
            cur = _measure_coverage_qs(quote_service)
            if cur < 0:
                # qs.get_quotes_compat 持续失败 → 退化为 dp
                cur = _measure_coverage_dp(context)
        else:
            cur = _measure_coverage_dp(context)

        if cur < 0:
            logger.warning(
                "[%s] wait_for_quote_data: no data source (qs=None, dp has no market_cap); "
                "proceeding without waiting",
                event,
            )
            return 0

        if cur >= target:
            if cur != last_reported:
                logger.info(
                    "[%s] wait_for_quote_data: READY count=%d (target=%d, expected=%d)",
                    event, cur, target, expected_count,
                )
            return cur

        if cur != last_reported:
            logger.info(
                "[%s] wait_for_quote_data: waiting (count=%d/%d, retry %.1fs, max %.0fs)",
                event, cur, target, poll_s, max_wait_s,
            )
            last_reported = cur

        if time.time() >= deadline:
            logger.warning(
                "[%s] wait_for_quote_data: TIMEOUT after %.0fs (count=%d/%d, target=%d, expected=%d) — proceeding with sparse data",
                event, max_wait_s, cur, target if expected_count else target, target, expected_count,
            )
            return cur

        time.sleep(poll_s)


__all__ = [
    "DEFAULT_EXPECTED_INSTRUMENT_COUNT",
    "DEFAULT_MAX_WAIT_S",
    "DEFAULT_MIN_COVERAGE",
    "DEFAULT_POLL_S",
    "wait_for_quote_data",
]
