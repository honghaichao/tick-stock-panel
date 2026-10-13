"""/api/trading/* 路由测试 — FastAPI TestClient 覆盖关键路径."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.trading import engine as engine_mod
from app.services.trading import preferences as prefs
from app.services.trading import store
from app.services.trading.engine import TradingEngine
from app.services.trading.models import Fill, OrderSide
from app.services.trading.router import router as trading_router


@pytest.fixture
def client(tmp_path, monkeypatch):
    store.reset_for_tests()
    import app.services.preferences as base_prefs
    base_prefs._invalidate_cache()
    # 路由 prefs.save 到真实底层 (而非 mock), 但写 tmp_path (隔离真实 prefs).
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(prefs, "save", base_prefs.save)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_minute_batch=lambda symbols, today: pl.DataFrame(),
    )
    qs = MagicMock()
    qs.get_quotes_compat.return_value = pl.DataFrame({
        "symbol": ["600519.SH"],
        "last_price": [11.0],
        "close": [11.0],
        "prev_close": [10.0],
    })
    app_state = SimpleNamespace(repo=repo, quote_service=qs)

    eng = TradingEngine()
    app_state.trading_engine = eng
    engine_mod.set_app_state(app_state)

    app = FastAPI()
    app.include_router(trading_router)
    app.state = app_state
    return TestClient(app), eng, tmp_path


# ── 状态 / 账户 / 持仓 ────────────────────────────
def test_status_endpoint(client):
    c, _, _ = client
    r = c.get("/api/trading/status")
    assert r.status_code == 200
    body = r.json()
    assert body["broker"] == "paper"
    assert "pending_order_count" in body


def test_account_endpoint(client):
    c, _, _ = client
    r = c.get("/api/trading/account")
    assert r.status_code == 200
    body = r.json()
    assert body["initial_cash"] == pytest.approx(prefs.initial_cash())
    assert body["cash"] == pytest.approx(prefs.initial_cash())


def test_positions_empty(client):
    c, _, _ = client
    r = c.get("/api/trading/positions")
    assert r.status_code == 200
    assert r.json()["positions"] == []


# ── 委托 ──────────────────────────────────────────
def test_submit_order_limit_buy(client):
    c, _, _ = client
    r = c.post("/api/trading/orders", json={
        "symbol": "600519.SH",
        "side": "buy",
        "order_type": "limit",
        "qty": 100,
        "limit_price": 10.0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order"]["symbol"] == "600519.SH"
    assert body["order"]["status"] == "new"


def test_submit_order_rejects_invalid(client):
    c, _, _ = client
    # qty <= 0 should fail at pydantic layer
    r = c.post("/api/trading/orders", json={
        "symbol": "600519.SH", "side": "buy", "order_type": "limit",
        "qty": 0, "limit_price": 10.0,
    })
    assert r.status_code == 422


def test_submit_then_cancel(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setattr(prefs, "save", lambda u: u)
    r = c.post("/api/trading/orders", json={
        "symbol": "600519.SH", "side": "buy", "order_type": "limit",
        "qty": 100, "limit_price": 10.0,
    })
    oid = r.json()["order"]["id"]
    r2 = c.delete(f"/api/trading/orders/{oid}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["order"]["status"] == "canceled"


def test_list_orders_with_status_filter(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setattr(prefs, "save", lambda u: u)
    c.post("/api/trading/orders", json={
        "symbol": "600519.SH", "side": "buy", "order_type": "limit",
        "qty": 100, "limit_price": 10.0,
    })
    r = c.get("/api/trading/orders?status=new")
    assert r.status_code == 200
    items = r.json()["orders"]
    assert len(items) >= 1
    assert all(o["status"] == "new" for o in items)


def test_list_orders_invalid_status_returns_400(client):
    c, _, _ = client
    r = c.get("/api/trading/orders?status=foo")
    assert r.status_code == 400


# ── 历史成交 / 日志 ─────────────────────────────
def test_trades_endpoint_with_fills(client, tmp_path):
    c, _, _ = client
    # 直接 append 一笔 fill, 模拟历史
    f = Fill(
        id="f1", order_id="o1", symbol="600519.SH", side=OrderSide.BUY,
        qty=100, price=10.0, fee=5.0, tax=0.0, amount=1000.0,
        filled_at="2026-01-15T10:00:00+08:00", trade_date=date(2026, 1, 15),
    )
    store.append_fill(tmp_path, f)
    r = c.get("/api/trading/trades")
    assert r.status_code == 200
    assert len(r.json()["trades"]) == 1


def test_logs_endpoint_returns_list(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setattr(prefs, "save", lambda u: u)
    c.post("/api/trading/orders", json={
        "symbol": "600519.SH", "side": "buy", "order_type": "limit",
        "qty": 100, "limit_price": 10.0,
    })
    r = c.get("/api/trading/logs")
    assert r.status_code == 200
    items = r.json()["logs"]
    # 至少 1 条 accept 日志 (成功提交)
    assert len(items) >= 1
    assert items[0]["kind"] == "accept"


# ── 偏好 ────────────────────────────────────────
def test_preferences_get(client):
    c, _, _ = client
    r = c.get("/api/trading/preferences")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_enabled"] is False
    assert body["trading_initial_cash"] > 0


def test_preferences_update_enables_engine(client):
    c, eng, _ = client
    r = c.post("/api/trading/preferences", json={"trading_enabled": True})
    assert r.status_code == 200
    assert r.json()["trading_enabled"] is True
    # eng.start() 是异步启动线程, 不等线程, 只断言 enabled
    eng.stop()
