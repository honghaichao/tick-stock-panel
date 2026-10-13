"""失败重试测试 — 按原 cron 时刻 (as_of) 撮合原 buys."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.services.trading import engine as engine_mod
from app.services.trading import executor as executor_mod
from app.services.trading import retry as retry_mod
from app.services.trading import store
from app.services.trading.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)


def _make_bar(open_, high, low, close, vol=1000.0, dt=None):
    if dt is None:
        dt = datetime(2026, 1, 15, 9, 31, 0)
    return pl.DataFrame({
        "symbol": ["600519.SH"],
        "datetime": [dt],
        "open": [open_], "high": [high], "low": [low], "close": [close], "volume": [vol],
    })


def _mock_repo(today: date, bar_by_symbol: dict[str, pl.DataFrame]):
    """Mock repo: get_minute_batch 按 symbol 返对应 K."""
    def get_minute_batch(symbols, trade_date, asset_type="stock"):
        out = []
        for s in symbols:
            if s in bar_by_symbol:
                out.append(bar_by_symbol[s])
        if not out:
            return pl.DataFrame()
        return pl.concat(out, how="vertical_relaxed")
    return SimpleNamespace(get_minute_batch=get_minute_batch)


# ── 跨日不重试 ──────────────────────────────────
def test_retry_rejects_cross_day(tmp_path):
    """as_of 日期 != 当日: 拒绝, status='no_match'."""
    store.reset_for_tests()
    # 写一个跨日的 run
    runs = tmp_path / "user_data" / "trading"
    runs.mkdir(parents=True, exist_ok=True)
    past = (datetime.now() - timedelta(days=2)).isoformat()
    old_run = {
        "strategy_id": "x", "event": "rebalance",
        "started_at": past, "finished_at": past, "duration_ms": 100,
        "trigger": "cron", "status": "error",
        "target": {"600519.SH": 100}, "submitted": ["ord_x"],
        "error": "old failure",
    }
    (runs / "strategy_runs.jsonl").write_text(
        json.dumps(old_run, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    run_id = f"x::{past}::rebalance"
    result = retry_mod.retry_failed_run(
        tmp_path, run_id,
        repo=MagicMock(), broker=MagicMock(),
        now_iso=datetime.now().isoformat(),
    )
    assert result["status"] == "no_match"
    assert "跨日" in result["error"]


# ── run 不存在 ───────────────────────────────────
def test_retry_run_not_found(tmp_path):
    store.reset_for_tests()
    result = retry_mod.retry_failed_run(
        tmp_path, "missing::now::rebalance",
        repo=MagicMock(), broker=MagicMock(),
    )
    assert result["status"] == "error"
    assert "not found" in result["error"]


# ── 成功按 as_of 撮合 ────────────────────────────
def test_retry_fills_at_as_of_bar(tmp_path):
    """as_of=09:33, 撮合用 09:31 K (已收盘), 限价单 bar.low=9.5 <= 10 → 撮合价=min(open, 10)."""
    store.reset_for_tests()
    today = date.today()
    # 准备 run + 1 个 buy 委托 (limit=10, qty=100, status=NEW)
    as_of_str = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=33).isoformat()
    runs_dir = tmp_path / "user_data" / "trading"
    runs_dir.mkdir(parents=True, exist_ok=True)
    old_run = {
        "strategy_id": "auntie", "event": "rebalance",
        "started_at": as_of_str, "finished_at": as_of_str, "duration_ms": 100,
        "trigger": "cron", "status": "error",
        "target": {"600519.SH": 100}, "submitted": ["ord_aaa"],
        "error": "fund short",
    }
    (runs_dir / "strategy_runs.jsonl").write_text(
        json.dumps(old_run, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    o = Order(
        id="ord_aaa", symbol="600519.SH", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        qty=100, limit_price=10.0,
        created_at=as_of_str, submitted_at=as_of_str,
    )
    store.append_order(tmp_path, o)
    # 准备 account
    import json as _json
    (runs_dir / "account.json").write_text(_json.dumps({
        "initial_cash": 100000.0, "cash": 100000.0,
        "market_value": 0.0, "total_equity": 100000.0,
        "realized_pnl": 0.0, "unrealized_pnl": 0.0, "total_pnl": 0.0,
    }), encoding="utf-8")
    store.reset_for_tests()

    # 准备 K 线: 09:31 已收盘 K, open=10.5, high=11, low=9.5, close=10.8
    bar_931 = _make_bar(10.5, 11.0, 9.5, 10.8, vol=1000.0, dt=datetime.combine(today, datetime.min.time()).replace(hour=9, minute=31))
    repo = _mock_repo(today, {"600519.SH": bar_931})

    run_id = f"auntie::{as_of_str}::rebalance"
    result = retry_mod.retry_failed_run(
        tmp_path, run_id,
        repo=repo, broker=retry_mod.PaperBroker(),
        now_iso=datetime.now().isoformat(),
    )
    assert result["status"] == "success", f"expected success, got {result}"
    assert result["retry_of"] == run_id

    # 验证 fill 落账
    fills = store.list_fills(tmp_path, symbol="600519.SH")
    assert len(fills) == 1
    f = fills[0]
    assert f.side == OrderSide.BUY
    assert f.qty == 100
    # 撮合价 = min(open=10.5, limit=10) = 10.0
    assert abs(f.price - 10.0) < 0.001
    assert f.amount == 1000.0
    # Fill 模型没 note 字段, 跳过错错

    # 验证 order 状态
    o2 = store.get_order(tmp_path, "ord_aaa")
    assert o2.status == OrderStatus.FILLED
    assert o2.filled_qty == 100

    # 验证 account cash 减少
    account = store.load_account(tmp_path, initial_cash=100000.0)
    assert account.cash < 100000.0

    # 验证新 RunReport 落 strategy_runs.jsonl
    runs = list(runs_dir.glob("strategy_runs.jsonl"))
    lines = runs[0].read_text(encoding="utf-8").strip().splitlines()
    new_run = json.loads(lines[-1])
    assert new_run["retry_of"] == run_id
    assert new_run["status"] == "success"


# ── 限价单 bar.low > 委托价 → 不撮合 ────────────
def test_retry_no_fill_when_limit_not_crossed(tmp_path):
    store.reset_for_tests()
    today = date.today()
    as_of_str = datetime.now().isoformat()
    runs_dir = tmp_path / "user_data" / "trading"
    runs_dir.mkdir(parents=True, exist_ok=True)
    old_run = {
        "strategy_id": "auntie", "event": "rebalance",
        "started_at": as_of_str, "finished_at": as_of_str,
        "trigger": "cron", "status": "error",
        "target": {}, "submitted": ["ord_bbb"], "error": "",
    }
    (runs_dir / "strategy_runs.jsonl").write_text(
        json.dumps(old_run, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    o = Order(
        id="ord_bbb", symbol="600519.SH", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        qty=100, limit_price=10.0,
        created_at=as_of_str, submitted_at=as_of_str,
    )
    store.append_order(tmp_path, o)
    # bar.low=10.5 > 委托价 10: 不成交
    bar_931 = _make_bar(11.0, 12.0, 10.5, 11.5, vol=1000.0, dt=datetime.combine(today, datetime.min.time()).replace(hour=9, minute=31))
    repo = _mock_repo(today, {"600519.SH": bar_931})

    run_id = f"auntie::{as_of_str}::rebalance"
    result = retry_mod.retry_failed_run(
        tmp_path, run_id,
        repo=repo, broker=retry_mod.PaperBroker(),
    )
    assert result["status"] == "no_match"
    fills = store.list_fills(tmp_path, symbol="600519.SH")
    assert len(fills) == 0


# ── 无 K 线 → no_match ────────────────────────────
def test_retry_no_match_when_no_bar(tmp_path):
    store.reset_for_tests()
    today = date.today()
    as_of_str = datetime.now().isoformat()
    runs_dir = tmp_path / "user_data" / "trading"
    runs_dir.mkdir(parents=True, exist_ok=True)
    old_run = {
        "strategy_id": "auntie", "event": "rebalance",
        "started_at": as_of_str, "finished_at": as_of_str,
        "trigger": "cron", "status": "error",
        "target": {}, "submitted": ["ord_ccc"], "error": "",
    }
    (runs_dir / "strategy_runs.jsonl").write_text(
        json.dumps(old_run, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    o = Order(
        id="ord_ccc", symbol="600519.SH", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        qty=100, limit_price=10.0,
        created_at=as_of_str, submitted_at=as_of_str,
    )
    store.append_order(tmp_path, o)
    # K 线 empty
    repo = _mock_repo(today, {})
    run_id = f"auntie::{as_of_str}::rebalance"
    result = retry_mod.retry_failed_run(
        tmp_path, run_id,
        repo=repo, broker=retry_mod.PaperBroker(),
    )
    assert result["status"] == "no_match"
    assert "无 K 线" in result["error"] or "无 K" in result["error"] or "K 线满足" in result["error"]
