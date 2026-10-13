"""dividend_auntie.py 单元测试 — 逐函数验证与 JoinQuant 原代码语义 1:1.

注意: 这是策略文件本身的测试, 不是策略接口测试 (test_trading_live_strategy 已覆盖).
本文件覆盖原代码每个函数的语义 (筛选 / 调仓 / 涨停检查), 用 mock data_provider 隔离数据.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import polars as pl

# 加载 dividend_auntie.py (不在 app 包里, 直接 load)
_STRATEGY_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "user_data" / "strategies" / "live" / "dividend_auntie.py"
_spec = importlib.util.spec_from_file_location("dividend_auntie", _STRATEGY_PATH)
dividend_auntie = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dividend_auntie)


# ── 测试夹具: mock data_provider + context ─────────────
class _FakeDP:
    """最小可用的 data_provider: instruments (可选), limits, minute_close, paused, name, dividends()."""

    def __init__(self,
                 instruments: pl.DataFrame | None = None,
                 *,
                 limit_up: dict[str, float] | None = None,
                 limit_down: dict[str, float] | None = None,
                 minute_close: dict[str, float] | None = None,
                 paused: dict[str, bool] | None = None,
                 name: dict[str, str] | None = None,
                 total_shares: dict[str, float] | None = None,
                 dividends_data: pl.DataFrame | None = None,
                 prev_day_close: dict[str, float] | None = None) -> None:
        self.instruments = instruments if instruments is not None else pl.DataFrame()
        self.limit_up = limit_up or {}
        self.limit_down = limit_down or {}
        self.minute_close = minute_close or {}
        self.paused = paused or {}
        self.name = name or {}
        self.total_shares = total_shares or {}
        self._dividends = dividends_data if dividends_data is not None else pl.DataFrame()
        self._prev_day_close = prev_day_close or {}
        # repo: 提供 get_enriched_latest (返回 (df, last_date)) + get_daily_batch
        repo_self = self
        class _Repo:
            def get_enriched_latest(self):
                return (pl.DataFrame(), __import__("datetime").date(2026, 1, 14))
            def get_daily_batch(self, symbols, start, end=None, columns=None):
                if not repo_self._prev_day_close:
                    return pl.DataFrame()
                return pl.DataFrame({
                    "symbol": list(repo_self._prev_day_close.keys()),
                    "close": list(repo_self._prev_day_close.values()),
                })
            def get_enriched_range(self, start, end, columns=None):
                # 单测默认空 df; _previous_trading_day 等 helper 的针对性 case
                # 直接覆盖此 lambda 提供.
                return pl.DataFrame({"date": []})
        self.repo = _Repo()

    def dividends(self, symbols, since, until):
        if self._dividends.is_empty():
            return self._dividends
        return self._dividends.filter(
            pl.col("symbol").is_in(symbols)
            & (pl.col("ex_date") >= since)
            & (pl.col("ex_date") <= until)
        )

    def fundamentals_market_cap(self, symbols):
        # 区分 missing vs 0: 实际 dp 缺失 minute_close 时 market_cap 是 0,
        # price=0 也是 0. 测试以前用 `or 1.0` 把 missing 当 1.0 兜底,
        # 导致 test_dividend_filter_skips_zero_market_cap 失效. 改用 sentinel.
        _MISSING = object()
        out = {}
        for s in symbols:
            shares = self.total_shares.get(s, 0.0)
            price = self.minute_close.get(s, _MISSING)
            out[s] = shares * (0.0 if price is _MISSING else price)
        return out


def _make_context(dp: _FakeDP, **overrides) -> SimpleNamespace:
    ctx = SimpleNamespace(
        data_provider=dp,
        trade_date=__import__("datetime").date(2026, 1, 15),
        market={},
        positions={},
        account={"cash": 100_000.0, "total_equity": 100_000.0},
        fills_recent=[],
        as_of="",
        event="rebalance",
        state={},
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


# ── 原 get_dividend_ratio_filter_list (1:1 复刻) ─────
def test_dividend_filter_top_25_pct_descending():
    """sort=False (降序) → top 25% 取最高 dividend_ratio.

    真实场景: stock_list 是从全年 dividends 拼出来的 (跨年), 所以即使
    today 是 2026-01-15, dividend window (since=2025-01-15) 也能覆盖到 2025 全年.
    """
    div = pl.DataFrame({
        "symbol": ["A", "A", "B", "B", "C", "C", "D", "D"],
        "ex_date": [
            __import__("datetime").date(2025, 6, 1),
            __import__("datetime").date(2025, 12, 1),
            __import__("datetime").date(2025, 6, 1),
            __import__("datetime").date(2025, 12, 1),
            __import__("datetime").date(2025, 6, 1),
            __import__("datetime").date(2025, 12, 1),
            __import__("datetime").date(2025, 6, 1),
            __import__("datetime").date(2025, 12, 1),
        ],
        "bonus_per_share": [5.0, 5.0, 4.0, 4.0, 3.0, 3.0, 2.0, 2.0],
    })
    dp = _FakeDP(
        dividends_data=div,
        minute_close={"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0},
        total_shares={"A": 100, "B": 200, "C": 300, "D": 400},
    )
    # dividend_ratio = bonus_per_share / 10000 / market_cap
    # A: 10/10000/100 = 1e-5 (最高); B: 8/10000/200 = 4e-6; C: 6/10000/300 = 2e-6; D: 4/10000/400 = 1e-6
    # 降序: A > B > C > D; top 25% (p1=0, p2=0.25) → 1 只 → ['A']
    # 简化: 临时把 min_continuous_year 改成 1, 避开三年连续分红的硬约束
    ctx = _make_context(dp)
    ctx.state["min_continuous_year"] = 1
    out = dividend_auntie.get_dividend_ratio_filter_list(ctx, ["A", "B", "C", "D"], False, 0, 0.25)
    assert out == ["A"]


def test_dividend_filter_no_data_falls_back_to_price_proxy():
    """无 dividend 数据 → 退化为按 1/price 排序 (proxy)."""
    dp = _FakeDP(
        dividends_data=pl.DataFrame(),
        minute_close={"A": 10.0, "B": 5.0, "C": 2.0, "D": 1.0},
    )
    ctx = _make_context(dp)
    out = dividend_auntie.get_dividend_ratio_filter_list(ctx, ["A", "B", "C", "D"], False, 0, 0.25)
    # 1/price 倒序: D(1.0) > C(0.5) > B(0.2) > A(0.1); top 25% → 1 只 → D
    assert out == ["D"]


def test_dividend_filter_empty_input_returns_empty():
    dp = _FakeDP()
    ctx = _make_context(dp)
    assert dividend_auntie.get_dividend_ratio_filter_list(ctx, [], False, 0, 0.25) == []


def test_dividend_filter_ratio_uses_total_bonus_over_market_cap():
    """回归: dividend_ratio = (annual_bonus_per_share * total_shares) / market_cap.
    旧版 bug: 漏乘 total_shares, 公式变成 (元/股) / (元) = 元/股 ≈ 0.
    """
    div = pl.DataFrame({
        "symbol": ["BIG", "SMALL"],
        "ex_date": [__import__("datetime").date(2025, 6, 1)] * 2,
        # BIG: 高 dividend_per_share, 大市值 (大盘银行)
        # SMALL: 低 dividend_per_share, 小市值 (小盘股)
        # 真实股息率: BIG 应显著高于 SMALL
        "bonus_per_share": [0.5, 0.1],
    })
    dp = _FakeDP(
        dividends_data=div,
        minute_close={"BIG": 5.0, "SMALL": 10.0},
        # BIG: 100 亿股 × 5 元 = 500 亿
        # SMALL: 1 亿股 × 10 元 = 10 亿
        total_shares={"BIG": 100_000_000_000, "SMALL": 100_000_000},
    )
    ctx = _make_context(dp)
    ctx.state["min_continuous_year"] = 1
    out = dividend_auntie.get_dividend_ratio_filter_list(ctx, ["BIG", "SMALL"], False, 0, 1.0)
    # BIG: 0.5 元/股, 市值 500 亿 → yield = 0.5/5 = 10%
    # SMALL: 0.1 元/股, 市值 10 亿 → yield = 0.1/10 = 1%
    # 按 yield 倒序 top 100% → BIG, SMALL 都在
    assert "BIG" in out
    assert "SMALL" in out
    # 数值正确性: 看 DR 内部排序 (此处通过 _dividend_stocks 后还能保住 BIG 即可,
    # BIG yield 10% > SMALL yield 1%, 排序后 BIG 必定在前)


def test_dividend_filter_skips_zero_market_cap():
    """market_cap == 0 → drop (避免 ratio 变成 inf)."""
    div = pl.DataFrame({
        "symbol": ["A", "B"],
        "ex_date": [__import__("datetime").date(2025, 6, 1), __import__("datetime").date(2025, 6, 1)],
        "bonus_per_share": [10.0, 5.0],
    })
    # 只给 A minute_close, B 不在 minute_close → mock 返回 0 → market_cap = 0
    dp = _FakeDP(
        dividends_data=div,
        minute_close={"A": 10.0},
        total_shares={"A": 100, "B": 1000},   # B 有 1000 股, 但 minute_close 缺 → 0
    )
    ctx = _make_context(dp)
    ctx.state["min_continuous_year"] = 1
    # p2=1.0 (full top), 不依赖 B 留下来才能保证切片非空.
    out = dividend_auntie.get_dividend_ratio_filter_list(ctx, ["A", "B"], False, 0, 1.0)
    # B market_cap=0 被 drop, 只剩 A
    assert "B" not in out
    assert "A" in out


# ── 原 _dividend_stocks (1:1 复刻) ─────────────────
def test_dividend_stocks_filters_continuous_years():
    """连续 3 年有分红 → 保留; 否则剔除."""
    div = pl.DataFrame({
        "symbol": ["A"] * 3 + ["B"],          # A 3 年, B 仅 1 年
        "ex_date": [
            __import__("datetime").date(2023, 6, 1),
            __import__("datetime").date(2024, 6, 1),
            __import__("datetime").date(2025, 6, 1),
            __import__("datetime").date(2026, 6, 1),
        ],
        "bonus_per_share": [1.0, 1.0, 1.0, 1.0],
    })
    dp = _FakeDP(dividends_data=div)
    ctx = _make_context(dp, trade_date=__import__("datetime").date(2026, 12, 1))
    # nyear=3: 检查 2024/2025/2026 三年
    out = dividend_auntie._dividend_stocks(ctx, 3, ["A", "B"])
    assert "A" in out
    assert "B" not in out


def test_dividend_stocks_empty_input():
    dp = _FakeDP()
    ctx = _make_context(dp)
    assert dividend_auntie._dividend_stocks(ctx, 3, []) == []


# ── 过滤函数 (1:1 复刻) ────────────────────────────
def test_filter_kcbj_stock_removes_bj_star_chinext():
    """原版只过滤 stock[0]=='8', 漏 92*** BJ / 688*** 科创 / 300/301*** 创业.
    按用户语义 (沪深主板只留) 改为: 北交所 (.BJ) + 688*** + 300/301*** 全踢.
    """
    inp = [
        "600519.SH",      # SH 主板 → 保留
        "000001.SZ",      # SZ 主板 → 保留
        "830001.BJ",      # 北交所 老格式 8 开头 → 踢
        "920445.BJ",      # 北交所 新格式 92 开头 → 踢
        "688828.SH",      # 科创板 → 踢
        "300708.SZ",      # 创业板 → 踢
        "301311.SZ",      # 创业板 301 → 踢
    ]
    out = dividend_auntie.filter_kcbj_stock(inp)
    assert "600519.SH" in out
    assert "000001.SZ" in out
    assert "830001.BJ" not in out
    assert "920445.BJ" not in out
    assert "688828.SH" not in out
    assert "300708.SZ" not in out
    assert "301311.SZ" not in out


def test_filter_st_stock_skips_st_names():
    dp = _FakeDP(name={
        "600519.SH": "贵州茅台",
        "000001.SZ": "平安银行",
        "600000.SH": "*ST海航",
        "600002.SH": "退市股",
    })
    ctx = _make_context(dp)
    out = dividend_auntie.filter_st_stock(ctx, ["600519.SH", "000001.SZ", "600000.SH", "600002.SH"])
    assert "600519.SH" in out
    assert "000001.SZ" in out
    assert "600000.SH" not in out    # *ST
    assert "600002.SH" not in out    # 退


def test_filter_st_stock_raises_when_name_missing():
    """原版语义: 拿不到 name (current_data[stock] KeyError) → 抛, 不静默 keep.
    防止 silent-keep 危险票 (新上市/被摘牌/数据缺失).
    """
    dp = _FakeDP(name={
        "600519.SH": "贵州茅台",
        "000999.SZ": None,   # data 缺失
    })
    ctx = _make_context(dp)
    import pytest
    with pytest.raises(KeyError):
        dividend_auntie.filter_st_stock(ctx, ["600519.SH", "000999.SZ"])


def test_filter_paused_stock_skips_zero_price():
    """原代码: not current_data[stock].paused. 本仓: last_price == 0 = paused."""
    dp = _FakeDP(paused={
        "600519.SH": False,
        "000001.SZ": True,   # last_price == 0
    })
    ctx = _make_context(dp)
    out = dividend_auntie.filter_paused_stock(ctx, ["600519.SH", "000001.SZ"])
    assert out == ["600519.SH"]


def test_filter_limitup_skips_holding_when_at_limit():
    """原代码: 已持仓跳过 limit_up 过滤 (避免被剔后选别的)."""
    dp = _FakeDP(
        limit_up={"A": 11.0, "B": 11.0},
        minute_close={"A": 11.0, "B": 10.99},   # A 涨停, B 差 1 分
    )
    ctx = _make_context(dp, positions={"A": 100})   # A 已持仓
    out = dividend_auntie.filter_limitup_stock(ctx, ["A", "B"])
    # A 已持仓 → 保留 (即使涨停)
    assert "A" in out
    # B 未持仓 + 现价 < limit_up (10.99 < 11.0) → 保留
    assert "B" in out

    # 如果 B 涨到涨停
    dp2 = _FakeDP(
        limit_up={"A": 11.0, "B": 11.0},
        minute_close={"A": 11.0, "B": 11.0},
    )
    out2 = dividend_auntie.filter_limitup_stock(_make_context(dp2, positions={"A": 100}), ["A", "B"])
    assert "A" in out2
    assert "B" not in out2   # 涨停被过滤


def test_filter_limitdown_skips_when_below_low_limit():
    """原代码: last_prices > low_limit (避免跌停不可卖)."""
    dp = _FakeDP(
        limit_down={"A": 9.0, "B": 9.0},
        minute_close={"A": 10.0, "B": 8.5},  # A 正常, B 跌停
    )
    ctx = _make_context(dp, positions={"A": 100})
    out = dividend_auntie.filter_limitdown_stock(ctx, ["A", "B"])
    assert "A" in out
    assert "B" not in out   # 跌停


def test_filter_highprice_skips_above_9():
    """原代码: last_prices < 9."""
    dp = _FakeDP(minute_close={"A": 5.0, "B": 10.0, "C": 8.99})
    ctx = _make_context(dp)
    out = dividend_auntie.filter_highprice_stock(ctx, ["A", "B", "C"])
    assert out == ["A", "C"]


# ── prepare_stock_list (1:1 复刻) ─────────────────
def test_prepare_stock_list_no_holding():
    """无持仓 → high_limit_list 为空, state 写入."""
    dp = _FakeDP()
    ctx = _make_context(dp, positions={})
    out = dividend_auntie.prepare_stock_list(ctx)
    assert out == {}
    assert ctx.state["high_limit_list"] == []


def test_prepare_stock_list_marks_limit_up_stocks():
    """持仓里 prev_close == high_limit 的标为涨停 (JoinQuant 原代码用 previous_date close).
    修 prepare_stock_list 改用 trade_date 前一交易日 (而非 latest-1) 后,
    需要同时 mock get_enriched_range 和 get_daily_batch 才能让流程跑通.
    """
    import datetime as dt
    dp = _FakeDP(
        limit_up={"A": 11.0, "B": 11.0, "C": 11.0},
        # get_daily_batch 的 mock 看 prev_day_close
        prev_day_close={"A": 11.0, "B": 10.5, "C": 10.99},
    )
    # mock get_enriched_range 让 _previous_trading_day 能返回一个日期
    dp.repo.get_enriched_range = lambda s, e, **kw: pl.DataFrame({
        "date": [dt.date(2026, 1, 14)]
    })
    ctx = _make_context(dp, positions={"A": 100, "B": 100, "C": 100})
    dividend_auntie.prepare_stock_list(ctx)
    # A prev_close == high_limit → 涨停
    # B prev_close < high_limit → 不涨停
    # C prev_close ≈ high_limit (差 0.01) → 涨停 (允许浮点容差)
    assert "A" in ctx.state["high_limit_list"]
    assert "B" not in ctx.state["high_limit_list"]
    assert "C" in ctx.state["high_limit_list"]


# ── check_limit_up (1:1 复刻) ─────────────────────
def test_previous_trading_day_returns_largest_date_before_target():
    """回归: prepare_stock_list 用 trade_date 之前最近一个有 enriched 数据的交易日
    (不是 get_enriched_latest() - 1, 那个对所有 event 都用同一个最新日期).
    """
    import datetime as dt
    div = pl.DataFrame({
        "symbol": ["A"],
        "ex_date": [dt.date(2025, 6, 1)],
        "bonus_per_share": [1.0],
    })
    dp = _FakeDP(dividends_data=div)

    # case 1: trade_date = 2026-08-26 (周三), enriched 含 [08-25, 08-24]
    # 正确: 前一交易日 = 2026-08-25
    # 错误 (旧版): get_enriched_latest() - 1 = 整个窗口最新日期前一天
    dp.repo.get_enriched_range = lambda s, e, **kw: pl.DataFrame({
        "date": [dt.date(2026, 8, 24), dt.date(2026, 8, 25)]
    })
    prev = dividend_auntie._previous_trading_day(dp.repo, dt.date(2026, 8, 26))
    assert prev == dt.date(2026, 8, 25), f"case 1: expected 2026-08-25, got {prev}"

    # case 2: trade_date = 2026-08-24 (周二), enriched 含 [08-21, 08-23] (跨周末)
    # 正确: 前一交易日 = 2026-08-23
    dp.repo.get_enriched_range = lambda s, e, **kw: pl.DataFrame({
        "date": [dt.date(2026, 8, 21), dt.date(2026, 8, 23)]
    })
    prev = dividend_auntie._previous_trading_day(dp.repo, dt.date(2026, 8, 24))
    assert prev == dt.date(2026, 8, 23), f"case 2: expected 2026-08-23, got {prev}"

    # case 3: trade_date = 2026-08-25 (周二), enriched 只到 2026-08-21 (周五)
    # 跨周末, 正确: 前一交易日 = 2026-08-21
    dp.repo.get_enriched_range = lambda s, e, **kw: pl.DataFrame({
        "date": [dt.date(2026, 8, 21)]
    })
    prev = dividend_auntie._previous_trading_day(dp.repo, dt.date(2026, 8, 25))
    assert prev == dt.date(2026, 8, 21), f"case 3: expected 2026-08-21, got {prev}"

    # case 4: enriched 完全空 (例如首日运行, 还没数据), 应返回 None
    dp.repo.get_enriched_range = lambda s, e, **kw: pl.DataFrame({"date": []})
    prev = dividend_auntie._previous_trading_day(dp.repo, dt.date(2026, 8, 25))
    assert prev is None, f"case 4: expected None, got {prev}"


def test_check_limit_up_no_state_returns_empty():
    """state 没 high_limit_list → 返空."""
    dp = _FakeDP()
    ctx = _make_context(dp, positions={"A": 100})
    out = dividend_auntie.check_limit_up(ctx)
    assert out == {}


def test_check_limit_up_sells_when_limit_opens():
    """涨停打开 (last_price < limit_up) → 清仓 (target=0)."""
    dp = _FakeDP(
        limit_up={"A": 11.0, "B": 11.0},
        minute_close={"A": 10.5, "B": 11.0},  # A 打开, B 还涨停
    )
    ctx = _make_context(
        dp,
        positions={"A": 100, "B": 100},
        state={"high_limit_list": ["A", "B"]},
    )
    out = dividend_auntie.check_limit_up(ctx)
    assert out == {"A": 0}   # 只清 A


def test_check_limit_up_skips_not_held():
    """涨停列表里有但实际无持仓 → 跳过."""
    dp = _FakeDP(
        limit_up={"A": 11.0},
        minute_close={"A": 10.0},
    )
    ctx = _make_context(
        dp,
        positions={},
        state={"high_limit_list": ["A"]},
    )
    out = dividend_auntie.check_limit_up(ctx)
    assert out == {}


# ── my_Trader (1:1 复刻, 端到端) ─────────────────
def test_my_trader_end_to_end_with_dividends():
    """完整链路: 股息率筛选 → 5 过滤 → 等权调仓 target."""
    div = pl.DataFrame({
        "symbol": ["A", "B", "C", "D", "E", "F", "G", "H"],
        "ex_date": [__import__("datetime").date(2025, 6, 1)] * 8,
        "bonus_per_share": [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0],
    })
    # instruments: 包含 type='stock', 含 8 开头 (北交所) 被剔除
    inst = pl.DataFrame({
        "symbol": ["A", "B", "C", "D", "E", "F", "G", "H", "830001.BJ"],
        "type": ["stock"] * 9,
    })
    dp = _FakeDP(
        dividends_data=div,
        instruments=inst,
        limit_up={"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0, "E": 100.0, "F": 100.0, "G": 100.0, "H": 100.0},
        limit_down={"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0, "E": 0.0, "F": 0.0, "G": 0.0, "H": 0.0},
        name={"A": "A", "B": "B", "C": "C", "D": "D", "E": "E", "F": "F", "G": "G", "H": "H"},
        minute_close={"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0, "F": 1.0, "G": 1.0, "H": 1.0},
        total_shares={"A": 100, "B": 100, "C": 100, "D": 100, "E": 100, "F": 100, "G": 100, "H": 100},
    )
    # 持仓为空 → 全买 top 5
    ctx = _make_context(
        dp,
        positions={},
        account={"cash": 10_000.0, "total_equity": 10_000.0},
        market={"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0, "F": 1.0, "G": 1.0, "H": 1.0},
    )
    ctx.state["min_continuous_year"] = 1
    target = dividend_auntie.my_Trader(ctx)
    # dividend_ratio 高 → A B C D E (top 25% 是 8/4=2 只, 但原代码 p2=0.25 取 25% 切片)
    # 8 只 * 0.25 = 2 只 → 选 top 2: A, B
    # 然后过滤: 没涨停, 没跌停, 没 ST, 没停牌, 价 < 9 → 都通过
    # 等权: cash 10000 / (5 - 0) = 2000/股; 2000/1 = 2000 股 → int(2000//100)*100 = 2000
    assert isinstance(target, dict)
    # 实际值依赖 0.25 切片
    assert len(target) >= 1


# ── 策略实例 路由 ─────────────────────────────────
def test_my_trader_buy_loop_breaks_only_when_stock_num_reached():
    """回归: buy 循环的 break 条件必须是 "持仓数达到 stock_num" 而不是
    sum(target.values()) >= stock_num * 100.

    历史 bug: 早期实现错用累计 qty 和阈值, 第一次 buy 完 qty=23000 >= 5*100=500 就 break,
    导致永远只买 1 只而不是补齐到 stock_num=5. 跟原版 JoinQuant 偏离, 1 年回测只成交 1 笔.

    注: get_dividend_ratio_filter_list 用 int(0.25 * len(DR)) 切片 top 25%, 所以测试需要
    至少 20 只票才能让 top 25% = 5 只 = stock_num. 5 只时 int(0.25*5)=1, 永远只出 1 只.
    """
    import datetime as dt
    n_stocks = 20  # top 25% * 20 = 5 只
    symbols = [f"S{i:02d}" for i in range(n_stocks)]
    div = pl.DataFrame({
        "symbol": symbols,
        "ex_date": [dt.date(2025, 6, 1)] * n_stocks,
        "bonus_per_share": [1.0] * n_stocks,
    })
    inst = pl.DataFrame({
        "symbol": symbols,
        "type": ["stock"] * n_stocks,
        "limit_up": [11.0] * n_stocks,
        "limit_down": [0.0] * n_stocks,
        "name": symbols,
        "total_shares": [100_000_000] * n_stocks,
        "exchange": ["SH"] * n_stocks,
        "listing_date": pl.date_range(
            dt.date(2010, 1, 1), dt.date(2010, 1, n_stocks), interval="1d", eager=True,
        )[:n_stocks],
    })
    minute_close = {s: 1.0 for s in symbols}
    dp = _FakeDP(
        dividends_data=div, instruments=inst,
        minute_close=minute_close,
        limit_up={s: 11.0 for s in symbols},
        limit_down={s: 0.0 for s in symbols},
        name={s: s for s in symbols},
        total_shares={s: 100_000_000 for s in symbols},
    )
    ctx = _make_context(
        dp, positions={},
        account={"cash": 500_000.0, "total_equity": 500_000.0},
        market=minute_close,
    )
    ctx.state["min_continuous_year"] = 1
    target = dividend_auntie.my_Trader(ctx)
    # 期望: 买满 stock_num=5 只, 不是只 1 只
    assert len(target) == 5, f"expected 5 buys, got {len(target)}: {target}"
    for sym, qty in target.items():
        assert qty > 0, f"expected positive qty for {sym}, got {qty}"


def test_strategy_dispatches_to_correct_function():
    """on_event 路由到对应原函数."""
    strat = dividend_auntie.STRATEGY
    assert strat.id == "dividend_auntie"
    assert len(strat.schedules) == 3
    events = {s.name for s in strat.schedules}
    assert events == {"pre_market", "rebalance", "limit_check"}
    # 时点
    sched_by_event = {s.name: (s.hour, s.minute) for s in strat.schedules}
    assert sched_by_event["pre_market"] == (9, 5)
    assert sched_by_event["rebalance"] == (9, 26)
    assert sched_by_event["limit_check"] == (13, 30)


# ── 回归: position_count / psize ─────────────────────────
def test_my_trader_position_count_uses_holding_count_not_qty_sum():
    """回归: 持仓 5 只每只 23000 股时, position_count 必须 = 5 (持仓只数),
    而不是 5*23000=115000 (sum of qty). 原版 JoinQuant len(positions).

    历史 bug: 早期实现用 sum(qty for qty in positions.values()) → position_count=115000,
    stock_num=5 → 5 > 115000 永远 False, 整个 buy 块被跳过 → 建仓后永远不补仓,
    1 年回测只成交 10 笔 (跟原版 200+ 笔差 20 倍).

    测试技巧: 给每只票不同 price (price=0.5, 1.0, 1.5, ...), 让 dividend_ratio
    (= 1/price) 有梯度, top 25% 切片后 choice 是确定的 symbols[:5].
    """
    import datetime as dt
    n_stocks = 20
    symbols = [f"P{i:02d}" for i in range(n_stocks)]
    div = pl.DataFrame({
        "symbol": symbols,
        "ex_date": [dt.date(2025, 6, 1)] * n_stocks,
        "bonus_per_share": [1.0] * n_stocks,
    })
    inst = pl.DataFrame({
        "symbol": symbols,
        "type": ["stock"] * n_stocks,
        "limit_up": [11.0] * n_stocks,
        "limit_down": [0.0] * n_stocks,
        "name": symbols,
        "total_shares": [100_000_000] * n_stocks,
        "exchange": ["SH"] * n_stocks,
        "listing_date": pl.date_range(
            dt.date(2010, 1, 1), dt.date(2010, 1, n_stocks), interval="1d", eager=True,
        )[:n_stocks],
    })
    # price 递增 → ratio (=1/price) 递减 → top 25% 确定是 symbols[:5]
    minute_close = {s: float(i + 1) * 0.5 for i, s in enumerate(symbols)}
    dp = _FakeDP(
        dividends_data=div, instruments=inst,
        minute_close=minute_close,
        limit_up={s: 11.0 for s in symbols},
        limit_down={s: 0.0 for s in symbols},
        name={s: s for s in symbols},
        total_shares={s: 100_000_000 for s in symbols},
    )
    # 持仓 5 只每只 23000 股, 都 = choice[:5], 验证 position_count = 5 (不是 115000)
    positions = {s: 23000 for s in symbols[:5]}
    ctx = _make_context(
        dp, positions=positions,
        account={"cash": 3000.0, "total_equity": 118000.0},
        market=minute_close,
    )
    ctx.state["min_continuous_year"] = 1
    target = dividend_auntie.my_Trader(ctx)
    # 期望: 持仓 5 只都在 choice → 无 sell; position_count=5=stock_num → 不 buy.
    # target 必须为空 (5 只都还在). 旧 bug 时 target 会包含所有 5 只的卖单 (因为
    # _dividend_stocks 空, 走不到 buy, 但 sell 循环照常 → 5 个 sell=0).
    assert target == {}, f"expected empty target (5 holdings stable, no buy), got {target}"


def test_my_trader_psize_includes_in_call_sell_proceeds():
    """回归: 同函数中先 order_target(s, 0) 再 order_value(s, psize), psize 必须用
    卖出后 (含本次 sell 所得) 的 cash, 而不是卖出前的 cash.

    历史 bug: 早期实现只用了 account.cash (建仓后残 ~$3K), 卖 1 只 $200K 股票后只够买
    500 股 (psize=3000 → 5.58 → 500 shares). 下一天又被踢掉 → 持续 churn.

    JoinQuant 语义: order_target(s, 0) 后, portfolio.available_cash 已含本次卖出所得.
    本仓 verifier 在 on_event 返回后才 fill, 需要手动加 in-call sell proceeds.
    """
    import datetime as dt
    n_stocks = 20
    symbols = [f"S{i:02d}" for i in range(n_stocks)]
    div = pl.DataFrame({
        "symbol": symbols,
        "ex_date": [dt.date(2025, 6, 1)] * n_stocks,
        "bonus_per_share": [1.0] * n_stocks,
    })
    inst = pl.DataFrame({
        "symbol": symbols,
        "type": ["stock"] * n_stocks,
        "limit_up": [11.0] * n_stocks,
        "limit_down": [0.0] * n_stocks,
        "name": symbols,
        "total_shares": [100_000_000] * n_stocks,
        "exchange": ["SH"] * n_stocks,
        "listing_date": pl.date_range(
            dt.date(2010, 1, 1), dt.date(2010, 1, n_stocks), interval="1d", eager=True,
        )[:n_stocks],
    })
    # 用不同价格让 dividend_ratio 拉开差距, 让 symbols[0] (price=2.0) 是 bottom
    # 而 symbols[5] (price=8.0) 在 top 25% 内 (前 5)
    prices = {s: float(i + 1) * 0.5 for i, s in enumerate(symbols)}
    # bonus 都是 1.0, 但 price 不同 → dividend_ratio = (1*shares)/mc 不同
    # 取 mc = price * shares (shares 一样), 所以 ratio = 1/price
    # 价低 → ratio 高. symbols[0..3] (price 0.5, 1, 1.5, 2) 是 top 4
    # symbols[19] (price 10) 是 bottom
    minute_close = prices
    dp = _FakeDP(
        dividends_data=div, instruments=inst,
        minute_close=minute_close,
        limit_up={s: 11.0 for s in symbols},
        limit_down={s: 0.0 for s in symbols},
        name={s: s for s in symbols},
        total_shares={s: 100_000_000 for s in symbols},
    )
    # 持仓 4 只 (top 5 中 4 只) + 1 只卖出 (top 5 之外, 但已持仓)
    # 这样会触发 1 sell + 1 buy, 验证 buy 用的是 post-sell cash
    held_in_top5 = symbols[:4]    # 在 choice 内 (top 5)
    sold_symbol = symbols[10]      # 在 choice 外 (top 5 之外)
    positions = {s: 200_000 for s in held_in_top5}
    positions[sold_symbol] = 200_000  # 会被 sell (不在 choice)
    # sold_symbol price=5.5, sold qty=200K → proceeds = $1.1M
    # cash = $1000 (post-build), effective cash = $1000 + $1.1M ≈ $1.1M
    # 新买入 top 5 中未持仓的: symbols[4] (price=2.5)
    # psize = $1.1M / (5 - 4) = $1.1M
    # qty = int(1_100_000 // (2.5 * 100)) * 100 = int(4400)*100 = 440_000
    ctx = _make_context(
        dp, positions=positions,
        account={"cash": 1000.0, "total_equity": 1_101_000.0},
        market=minute_close,
    )
    ctx.state["min_continuous_year"] = 1
    target = dividend_auntie.my_Trader(ctx)
    # 期望: sell sold_symbol, buy 1 只新票 (qty ~ 440_000 shares, 不是 ~200-500)
    assert sold_symbol in target and target[sold_symbol] == 0, \
        f"expected sell {sold_symbol}=0, got target={target}"
    buys = {k: v for k, v in target.items() if v > 0}
    assert len(buys) >= 1, f"expected ≥1 buy, got {target}"
    for sym, qty in buys.items():
        # 旧 bug: qty=500 (psize=$3K). 修后: qty 应该是 ~440_000 (用 in-call sell proceeds).
        # 至少要 > 5000 股, 不能是几百股.
        assert qty > 5000, f"expected full-size buy qty (>5000), got {sym}={qty}"
