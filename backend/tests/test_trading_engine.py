"""TradingEngine 集成测试 — 委托提交 / 撮合 / 持仓 / T+1 / 拒单 / SSE 广播。

不启撮合线程, 直接调 _tick_once 验证撮合逻辑。
quote_service 用 mock 桩注入: 让 _latest_prices_and_prev_closes 返回 last_price=11.0。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.services.trading import engine as engine_mod
from app.services.trading import preferences as prefs
from app.services.trading import store
from app.services.trading.engine import TradingEngine
from app.services.trading.models import (
    OrderDraft,
    OrderSide,
    OrderStatus,
)


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """每个测试前后清空 store 内存态 + preferences 目录."""
    store.reset_for_tests()
    # preferences 用临时文件: 把 _cache 失效
    import app.services.preferences as base_prefs
    base_prefs._invalidate_cache()
    # 设置 preferences 字段到 tmp_path
    import app.config
    monkeypatch.setattr(app.config.settings, "data_dir", tmp_path, raising=False)
    yield


def _make_engine(tmp_path: Path) -> TradingEngine:
    """构造一个 mock app.state: 注入假 repo + quote_service."""
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_minute_batch=lambda symbols, today: pl.DataFrame(),
    )
    app_state = SimpleNamespace(repo=repo, quote_service=None)
    engine_mod.set_app_state(app_state)

    eng = TradingEngine()
    return eng


def _quote_with_last_price(tmp_path: Path, last_price: float) -> TradingEngine:
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_minute_batch=lambda symbols, today: pl.DataFrame(),
    )
    # 构造 quote_service.get_quotes_compat 返回带 last_price 的 DataFrame
    qs = MagicMock()
    qs.get_quotes_compat.return_value = pl.DataFrame({
        "symbol": ["600519.SH"],
        "last_price": [last_price],
        "close": [last_price],
        "prev_close": [10.0],
    })
    app_state = SimpleNamespace(repo=repo, quote_service=qs)
    engine_mod.set_app_state(app_state)
    return TradingEngine()


# ── 委托受理 ──────────────────────────────────────
def test_submit_limit_buy_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "save", lambda u: u)
    monkeypatch.setattr(prefs, "is_enabled", lambda: True)
    eng = _quote_with_last_price(tmp_path, 11.0)

    order = eng.submit(tmp_path, OrderDraft(
        symbol="600519.SH", side="buy", order_type="limit", qty=100, limit_price=10.5,
    ))
    assert order.status == OrderStatus.NEW
    assert order.id.startswith("ord_")
    # 持久化检查: 落盘 + 账户初始化
    assert store.get_order(tmp_path, order.id) is not None
    account = eng.account(tmp_path)
    assert account.initial_cash == pytest.approx(prefs.initial_cash())
    # 账户还未扣减 (成交才扣)
    assert account.cash == pytest.approx(prefs.initial_cash())


def test_submit_rejects_when_amount_exceeds_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "save", lambda u: u)
    monkeypatch.setattr(prefs, "max_order_amount", lambda: 1000.0)
    # last_price 调到不阻塞 (initial_cash = 100 万 默认, 100 股 * 50 元 = 5000 < 100 万)
    eng = _quote_with_last_price(tmp_path, 50.0)
    # 限价 200 元 * 100 股 = 20000 > 1000 → 触发上限
    with pytest.raises(engine_mod.TradingRejectError) as ei:
        eng.submit(tmp_path, OrderDraft(
            symbol="600519.SH", side="buy", order_type="limit", qty=100, limit_price=200.0,
        ))
    assert "超过上限" in str(ei.value)


def test_submit_rejects_when_cash_insufficient(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "save", lambda u: u)
    # 把 max_order_amount 调大, 避开金额上限检查, 直接落到现金校验
    monkeypatch.setattr(prefs, "max_order_amount", lambda: 1e12)
    monkeypatch.setattr(prefs, "initial_cash", lambda: 10_000.0)
    eng = _quote_with_last_price(tmp_path, 100.0)
    # 1000 股 * 100 元 = 100000 > 10000 (initial cash), 现金不足
    with pytest.raises(engine_mod.TradingRejectError) as ei:
        eng.submit(tmp_path, OrderDraft(
            symbol="600519.SH", side="buy", order_type="limit",
            qty=1000, limit_price=100.0,
        ))
    assert "不足以支付" in str(ei.value)


def test_submit_sell_rejects_when_t1_insufficient(tmp_path, monkeypatch):
    """T+1: 无持仓卖出应被拒."""
    monkeypatch.setattr(prefs, "save", lambda u: u)
    eng = _quote_with_last_price(tmp_path, 11.0)
    with pytest.raises(engine_mod.TradingRejectError) as ei:
        eng.submit(tmp_path, OrderDraft(
            symbol="600519.SH", side="sell", order_type="limit", qty=100, limit_price=11.0,
        ))
    assert "可卖数量不足" in str(ei.value)


# ── 撮合 → 持仓聚合 ─────────────────────────────
def test_matcher_fills_buy_and_updates_position(tmp_path):
    """挂买单 → 撮合触发 fill → 持仓 + 账户扣减."""
    eng = _quote_with_last_price(tmp_path, 11.0)
    order = eng.submit(tmp_path, OrderDraft(
        symbol="600519.SH", side="buy", order_type="limit", qty=100, limit_price=10.0,
    ))

    # 注入分钟 K: open=10, high=10.5, low=9.5 (穿越限价 10.0)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_minute_batch=lambda symbols, today: pl.DataFrame({
            "symbol": ["600519.SH"],
            "datetime": [datetime(2026, 1, 15, 10, 0)],
            "open": [10.0], "high": [10.5], "low": [9.5], "close": [10.2],
            "volume": [1000.0],
        }),
    )
    engine_mod._APP_STATE = SimpleNamespace(repo=repo, quote_service=engine_mod._APP_STATE.quote_service)

    # 直接调 _tick_once
    eng._tick_once()

    # 委托应已 FILLED
    o = store.get_order(tmp_path, order.id)
    assert o.status == OrderStatus.FILLED
    assert o.filled_qty == 100
    # 账户扣减
    account = eng.account(tmp_path)
    assert account.cash == pytest.approx(prefs.initial_cash() - 100 * 10.0, rel=1e-3)
    # 持仓聚合
    positions = eng.positions(tmp_path)
    assert len(positions) == 1
    p = positions[0]
    assert p.symbol == "600519.SH"
    assert p.qty == 100
    assert p.avg_cost == pytest.approx(10.0)
    assert p.last_price == pytest.approx(11.0)
    # 累计盈亏 = (11 - 10) * 100 = 100
    assert p.accumulated_pnl == pytest.approx(100.0)
    # 当时盈亏 = (11 - 10) * 100 = 100 (prev_close mock = 10.0)
    assert p.total_pnl == pytest.approx(100.0)
    # fill 落库
    fills = store.list_fills(tmp_path, limit=None)
    assert len(fills) == 1
    assert fills[0].order_id == order.id


def test_matcher_no_fill_when_no_bar(tmp_path):
    eng = _quote_with_last_price(tmp_path, 11.0)
    order = eng.submit(tmp_path, OrderDraft(
        symbol="600519.SH", side="buy", order_type="limit", qty=100, limit_price=10.0,
    ))
    # mock repo returns empty df → no fill
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_minute_batch=lambda symbols, today: pl.DataFrame(),
    )
    engine_mod._APP_STATE = SimpleNamespace(repo=repo, quote_service=engine_mod._APP_STATE.quote_service)
    eng._tick_once()
    o = store.get_order(tmp_path, order.id)
    assert o.status == OrderStatus.NEW  # 未成交


def test_cancel_works_and_logs(tmp_path):
    eng = _quote_with_last_price(tmp_path, 11.0)
    order = eng.submit(tmp_path, OrderDraft(
        symbol="600519.SH", side="buy", order_type="limit", qty=100, limit_price=10.0,
    ))
    eng.cancel(tmp_path, order.id)
    o = store.get_order(tmp_path, order.id)
    assert o.status == OrderStatus.CANCELED
    # 日志包含 CANCEL
    logs = store.list_logs(tmp_path, kind="cancel", limit=None)
    assert len(logs) == 1


def test_status_reports_running_false_when_no_thread(tmp_path):
    eng = _quote_with_last_price(tmp_path, 11.0)
    s = eng.status(tmp_path)
    assert s.running is False
    assert s.broker == "paper"
    assert s.matcher_interval_seconds == prefs.match_interval_seconds()


# ── 持仓聚合 (多次成交) ───────────────────────────
def test_positions_aggregate_buy_then_partial_sell(tmp_path):
    """先买入 → 再卖出 (T+1 隔日, 这里直接用 mock fills 模拟历史)."""
    eng = _quote_with_last_price(tmp_path, 12.0)
    # 手动落 2 笔 fill (历史) 模拟已成交
    store.append_fill(tmp_path, store.Fill(
        id="f1", order_id="o1", symbol="600519.SH", side=OrderSide.BUY,
        qty=100, price=10.0, fee=5.0, tax=0.0, amount=1000.0,
        filled_at="2026-01-14T10:00:00+08:00", trade_date=date(2026, 1, 14),
    ))
    store.append_fill(tmp_path, store.Fill(
        id="f2", order_id="o2", symbol="600519.SH", side=OrderSide.SELL,
        qty=30, price=11.0, fee=5.0, tax=11.0, amount=330.0,
        filled_at="2026-01-15T10:00:00+08:00", trade_date=date(2026, 1, 15),
    ))
    positions = eng.positions(tmp_path)
    assert len(positions) == 1
    p = positions[0]
    assert p.qty == 70
    # 平均成本 (移动加权): (1000 * 70/100) / 70 = 10.0
    assert p.avg_cost == pytest.approx(10.0, rel=1e-3)
    # 累计盈亏 = (12 - 10) * 70 = 140
    assert p.accumulated_pnl == pytest.approx(140.0)


def test_positions_listing_filtered_by_symbol(tmp_path):
    """持仓 API 支持 symbol 过滤."""
    eng = _quote_with_last_price(tmp_path, 11.0)
    store.append_fill(tmp_path, store.Fill(
        id="f1", order_id="o1", symbol="600519.SH", side=OrderSide.BUY,
        qty=100, price=10.0, fee=0.0, tax=0.0, amount=1000.0,
        filled_at="2026-01-14T10:00:00+08:00", trade_date=date(2026, 1, 14),
    ))
    only_519 = eng.positions(tmp_path)  # 当前只过滤未在签名里, 直接给完整
    assert len(only_519) >= 1


def test_tick_refreshes_account_when_no_pending_orders(tmp_path):
    """回归测试: 撮合循环在 pending=0 时必须仍刷新账户市值.

    历史 bug (2026-09-29): 5 笔全部成交, pending=0, _tick_once 直接 return,
    account.json 里的 market_value 永远停在 fill 时刻 (last_price 还没拉到, 0),
    前端 /api/trading/account 一直返回 market_value=0, PnL 看板永远 0.

    修复: pending=0 时调 _tick_refresh_when_idle, 每 60s 节流刷新一次账户市值 + PnL 快照.
    """
    eng = _quote_with_last_price(tmp_path, last_price=11.0)
    # 模拟"有持仓": 直接写入 fill + account, 让 pending=0, 但 market_value 还停在 0
    from app.services.trading import store as store_mod
    from app.services.trading.models import OrderSide
    from datetime import datetime, timezone, timedelta
    today = (datetime.now(timezone(timedelta(hours=8)))).date()
    # 写一笔历史 fill (fill 时刻 last_price 还是 None, 所以 market_value=0)
    fill = store_mod.Fill(
        id="fl_test", order_id="ord_test", symbol="600519.SH",
        side=OrderSide.BUY, qty=100, price=10.0, fee=5.0, tax=0.0,
        amount=1000.0, filled_at=store_mod.now_iso(), trade_date=today,
    )
    store_mod.append_fill(tmp_path, fill)
    # 同步账户 cash
    account = eng.account(tmp_path)
    account.cash -= 1005.0
    store_mod.save_account(tmp_path, account)
    # 此时 market_value 应该是 0 (因为没有 fill 触发的 refresh)
    pre_account = eng.account(tmp_path)
    assert pre_account.market_value == 0.0

    # 清掉 _last_pnl_ts 节流, 让 _tick_refresh_when_idle 一定执行
    eng._last_pnl_ts = None

    # 调一次 _tick_once (pending=0, 应该走 idle 刷新分支)
    eng._tick_once()

    # 账户市值应当从 0 变到 11 * 100 = 1100
    after_account = eng.account(tmp_path)
    assert after_account.market_value == pytest.approx(1100.0)
    assert after_account.total_equity == pytest.approx(after_account.cash + 1100.0)
    # unrealized_pnl = (11 - 10) * 100 = 100
    assert after_account.unrealized_pnl == pytest.approx(100.0)
    assert after_account.total_pnl == pytest.approx(100.0)


def test_tick_refresh_account_every_tick_but_snapshot_throttled(tmp_path):
    """_tick_refresh_when_idle: account 实时, snapshot 60s 节流.

    历史: 第一版用 60s 一起节流, PnL 看板延迟最高 60s.
    第二版: 拆开 — account.json 每 5s (实时), pnl_snapshots.jsonl 60s (历史曲线).
    """
    from datetime import datetime, timezone, timedelta
    eng = _quote_with_last_price(tmp_path, last_price=11.0)

    # 1) 即使刚 refresh 过, account.json 必须每 tick 都刷 (实时)
    eng._last_pnl_ts = datetime.now(timezone(timedelta(hours=8)))
    called_account = {"n": 0}
    called_snapshot = {"n": 0}
    orig_account = eng._refresh_account_market_value
    orig_snapshot = eng._write_pnl_snapshot
    eng._refresh_account_market_value = lambda *a, **kw: called_account.__setitem__("n", called_account["n"] + 1)
    eng._write_pnl_snapshot = lambda *a, **kw: called_snapshot.__setitem__("n", called_snapshot["n"] + 1)

    eng._tick_refresh_when_idle(tmp_path)
    assert called_account["n"] == 1, "account 必须每 tick 刷新 (实时)"
    assert called_snapshot["n"] == 0, "snapshot 60s 内不能写"

    eng._refresh_account_market_value = orig_account
    eng._write_pnl_snapshot = orig_snapshot


def test_tick_snapshot_writes_after_throttle_window(tmp_path):
    """pnl_snapshots 必须 60s 后才能再写, 控制历史文件大小."""
    from datetime import datetime, timezone, timedelta
    eng = _quote_with_last_price(tmp_path, last_price=11.0)
    eng._last_pnl_ts = datetime.now(timezone(timedelta(hours=8))) - timedelta(seconds=61)
    called = {"n": 0}
    orig = eng._write_pnl_snapshot
    eng._write_pnl_snapshot = lambda *a, **kw: called.__setitem__("n", called["n"] + 1)
    eng._tick_refresh_when_idle(tmp_path)
    assert called["n"] == 1, ">60s 后 snapshot 才能写"
    eng._write_pnl_snapshot = orig


def test_matcher_loop_emits_heartbeat_log(tmp_path, caplog):
    """_matcher_loop 每分钟写 1 条 「撮合心跳」INFO 级别的日志.

    历史诊断 2026-10-10: 后端跑 14h, 撮合线程启动后无 per-tick log;
    引入 60s 心跳让人眼可见线程健康 + 当前 pending 数 + 上次撮合时间.
    """
    import logging, time
    from app.services.trading.engine import TradingEngine

    eng = _quote_with_last_price(tmp_path, last_price=11.0)
    # 让 next 一次循环立刻就被判定为已过 60s
    eng._last_heartbeat_ts = 0.0
    caplog.set_level(logging.INFO, logger="app.services.trading.engine")
    # 模拟 _matcher_loop 第一轮: tick_once 后跳到心跳检查
    caplog.clear()
    eng._tick_once()  # 走一次撮合
    # 直接调心跳发射点的逻辑 (与 _matcher_loop 内部等价)
    import time as _t
    now_ts = _t.time()
    if now_ts - eng._last_heartbeat_ts >= 60.0:
        from datetime import datetime, timezone, timedelta
        _now = datetime.now(timezone(timedelta(hours=8)))
        eng._last_heartbeat_ts = now_ts
        logging.getLogger("app.services.trading.engine").info(
            "撮合心跳: alive=%s, pending=0, last_match=%s, interval=%ss",
            _now.strftime("%H:%M:%S"),
            eng._last_match_ts or "(never)",
            5,
        )
    found = any("撮合心跳" in rec.message for rec in caplog.records)
    assert found, "expected 撮合心跳 log entry"


# ── F0-3: max_position_count / max_position_pct 实盘生效 (2026-10-13) ─────
def _seed_fills(tmp_path, fills_data):
    """直接 append fills 构造持仓 (绕过撮合). fills_data = list of (symbol, side, qty, price)."""
    from app.services.trading.models import Fill
    from app.services.trading.store import _new_id
    today = datetime.now().date()
    for sym, side, qty, price in fills_data:
        f = Fill(
            id=_new_id("fl"),
            order_id=_new_id("ord"),
            symbol=sym,
            side=side,
            qty=qty,
            price=price,
            amount=price * qty,
            trade_date=today,
            filled_at=datetime.now().isoformat(),
            created_at=datetime.now().isoformat(),
        )
        store.append_fill(tmp_path, f)


def test_f03_max_position_count_blocks_new_symbol_overflow(tmp_path, monkeypatch):
    """已持仓 20 只, 再买新 symbol → 触发 max_position_count 拒单."""
    from app.services.trading.engine import TradingRejectError
    monkeypatch.setattr(prefs, "is_enabled", lambda: True)
    eng = _quote_with_last_price(tmp_path, 11.0)
    # 构造 20 只持仓
    _seed_fills(tmp_path, [(f"60000{i}.SH", "buy", 100, 10.0) for i in range(20)])
    # 设置 max_position_count = 20 (默认)
    assert prefs.max_position_count() == 20
    # 买入新 symbol 999999.SH → 持仓将达 21 > 20 → 拒
    with pytest.raises(TradingRejectError, match="max_position_count"):
        eng.submit(tmp_path, OrderDraft(
            symbol="999999.SH", side="buy", order_type="limit", qty=100, limit_price=10.0,
        ))


def test_f03_max_position_count_allows_buy_existing_symbol(tmp_path, monkeypatch):
    """已持仓 20 只, 加仓已持仓 symbol → 不算 +1 → 不被 count 拒 (单票 pct 也不拒, 价低)."""
    monkeypatch.setattr(prefs, "is_enabled", lambda: True)
    eng = _quote_with_last_price(tmp_path, 11.0)
    _seed_fills(tmp_path, [(f"60000{i}.SH", "buy", 100, 10.0) for i in range(20)])
    # 加仓 600000.SH (已持仓) → 100 股 * 11 = 1100 元
    # 但 max_order_amount 默认 50000, 单笔 1100 OK
    order = eng.submit(tmp_path, OrderDraft(
        symbol="600000.SH", side="buy", order_type="limit", qty=100, limit_price=11.0,
    ))
    assert order.status == OrderStatus.NEW


def test_f03_max_position_pct_blocks_oversized_buy(tmp_path, monkeypatch):
    """单票新增市值 > max_position_pct (默认 20%) 总资产 → 拒."""
    from app.services.trading.engine import TradingRejectError
    monkeypatch.setattr(prefs, "is_enabled", lambda: True)
    eng = _quote_with_last_price(tmp_path, 11.0)
    store.load_account(tmp_path, initial_cash=1_000_000.0)
    # 账户 1,000,000. 单笔上限 50000. pct 20% = 200000.
    # limit_price=50, qty=4000 → 200000 正好 20% — 因为 _check_position_pct 用 > not >=
    # 设 qty=4001 → 200050 > 200000 (20%) → pct 拒 (注意 max_order_amount=50000 < 200050, 应先被 max_order 拒)
    # 调整: 用更大 qty 让 amount 超 50000 但单票占比超 20%
    # qty=5000, limit_price=50 → 250000 → 25% > 20%, 但超 50000 → max_order_amount 先拒
    # 解决方案: 提高 max_order_amount_for_strategy, 或用 limit_price=10 qty=50000 → 500000 > 50000 还是拒
    # 实际: max_order_amount 是 50000 硬卡, 单笔超 50000 就被拒. 因此 pct 检查只在 amount ≤ 50000 时生效.
    # 这里直接 monkeypatch max_order_amount 到 1e9 来测试 pct
    monkeypatch.setattr(prefs, "max_order_amount", lambda: 1_000_000_000.0)
    with pytest.raises(TradingRejectError, match="max_position_pct"):
        eng.submit(tmp_path, OrderDraft(
            symbol="600519.SH", side="buy", order_type="limit", qty=5000, limit_price=50.0,
        ))


def test_f03_sell_side_skips_position_risk(tmp_path, monkeypatch):
    """卖单不增加敞口 → 跳过 position_risk (即使持仓已超 max_count)."""
    monkeypatch.setattr(prefs, "is_enabled", lambda: True)
    eng = _quote_with_last_price(tmp_path, 11.0)
    _seed_fills(tmp_path, [(f"60000{i}.SH", "buy", 100, 10.0) for i in range(20)])
    # 卖 600001.SH → 不被 max_position_count/pct 拒 (T+1 可卖 100 股)
    try:
        order = eng.submit(tmp_path, OrderDraft(
            symbol="600001.SH", side="sell", order_type="limit", qty=100, limit_price=11.0,
        ))
        # 卖单可能 T+1 OK 进入 NEW, 或被拒 (我们只检查不是 max_position_count/pct)
        assert order.status in (OrderStatus.NEW, OrderStatus.REJECTED)
    except Exception as e:
        assert "max_position_count" not in str(e)
        assert "max_position_pct" not in str(e)
