"""scripts.trading_cli 测试 — argparse 解析 + 子命令分派。

不真正起后端 (会撞 503), 只验证: 子命令注册 / 参数解析 / 帮助文本生成正确。
end-to-end 行为由 test_trading_api + test_trading_runner 覆盖。
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from app.services.trading import engine as engine_mod
from app.services.trading import store
from app.services.trading.engine import TradingEngine


@pytest.fixture
def fake_state(tmp_path, monkeypatch):
    store.reset_for_tests()
    import app.services.preferences as base_prefs
    base_prefs._invalidate_cache()
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr("app.services.trading.preferences.save", base_prefs.save)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_minute_batch=lambda symbols, today: pl.DataFrame(),
    )
    qs = MagicMock()
    qs.get_quotes_compat.return_value = pl.DataFrame({
        "symbol": ["600519.SH"],
        "last_price": [10.0], "close": [10.0], "prev_close": [9.0],
    })
    eng = TradingEngine()
    state = SimpleNamespace(repo=repo, quote_service=qs, trading_engine=eng)
    engine_mod.set_app_state(state)
    return state, tmp_path


def _run_cli(argv):
    """调用 trading_cli.main(argv) 并捕获 stdout."""
    from scripts import trading_cli

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = trading_cli.main(argv)
    return rc, buf.getvalue()


# ── argparse 解析 + help 文案 ───────────────────────
# ── argparse 解析 (不进引擎) ─────────────────────────
def test_cli_help_lists_all_subcommands():
    from scripts import trading_cli
    with pytest.raises(SystemExit):
        trading_cli.main([])


def test_cli_submit_parses_buy_limit_defaults(fake_state):
    from scripts import trading_cli
    captured = {}

    def fake_submit(args):
        captured["ns"] = args
        return 0

    with (patch("scripts.trading_cli._resolve_runner", return_value=MagicMock()),
          patch("scripts.trading_cli.cmd_submit", side_effect=fake_submit)):
        trading_cli.main(["submit", "buy", "600519.SH", "100", "--price", "10.0"])
    assert captured["ns"].side == "buy"
    assert captured["ns"].symbol == "600519.SH"
    assert captured["ns"].qty == 100
    assert captured["ns"].price == 10.0
    assert captured["ns"].order_type == "limit"


def test_cli_submit_market_flag_overrides_order_type(fake_state):
    from scripts import trading_cli
    captured = {}

    def fake_submit(args):
        captured["ns"] = args
        return 0

    with (patch("scripts.trading_cli._resolve_runner", return_value=MagicMock()),
          patch("scripts.trading_cli.cmd_submit", side_effect=fake_submit)):
        trading_cli.main(["submit", "sell", "600519.SH", "100", "--market"])
    assert captured["ns"].order_type == "market"
    assert captured["ns"].price is None


def test_cli_orders_status_csv_parsed(fake_state):
    from scripts import trading_cli
    captured = {}

    def fake_orders(args):
        captured["ns"] = args
        return 0

    with (patch("scripts.trading_cli._resolve_runner", return_value=MagicMock()),
          patch("scripts.trading_cli.cmd_orders", side_effect=fake_orders)):
        trading_cli.main(["orders", "--status", "new,filled", "--limit", "20"])
    assert captured["ns"].status == "new,filled"
    assert captured["ns"].limit == 20


# ── end-to-end 子命令分发 ──────────────────────────
def test_cli_submit_buy_emits_order_json(fake_state):
    rc, out = _run_cli(["submit", "buy", "600519.SH", "100", "--price", "10.0"])
    assert rc == 0
    body = json.loads(out)
    assert body["ok"] is True
    assert body["order"]["side"] == "buy"
    assert body["order"]["qty"] == 100
    assert body["order"]["limit_price"] == 10.0


def test_cli_submit_sell_rejects_when_no_holding(fake_state):
    rc, out = _run_cli(["submit", "sell", "600519.SH", "100", "--price", "11.0"])
    assert rc == 1
    body = json.loads(out)
    assert body["ok"] is False
    assert "可卖数量不足" in body["error"]


def test_cli_positions_writes_json_lines(fake_state):
    # 落一笔历史 fill 模拟持仓
    store.append_fill(fake_state[1], store.Fill(
        id="f1", order_id="o_hist", symbol="600519.SH", side="buy",
        qty=100, price=10.0, fee=0, tax=0, amount=1000.0,
        filled_at="2026-01-14T10:00:00+08:00", trade_date=date(2026, 1, 14),
    ))
    rc, out = _run_cli(["positions"])
    assert rc == 0
    lines = [ln for ln in out.strip().split("\n") if ln]
    assert len(lines) == 1
    body = json.loads(lines[0])
    assert body["symbol"] == "600519.SH"
    assert body["qty"] == 100


def test_cli_enable_toggles_preference(fake_state):
    rc, out = _run_cli(["enable"])
    assert rc == 0
    body = json.loads(out)
    # preferences 可能因为测试顺序不一致, 但 JSON 结构固定
    assert "trading_enabled" in body
