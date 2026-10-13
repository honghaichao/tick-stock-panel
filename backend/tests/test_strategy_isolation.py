"""Tests for live-trading-strategy-isolation.md — strategy_id as a first-class field.

Covers:
  - list_orders/fills/logs filter by Order/Fill/TradeLogEntry.strategy_id (not note)
  - manual submit with non-"strategy:<sid>:<event>" note is NOT mis-classified
  - Order/Fill/TradeLogEntry schema accepts strategy_id with default None
  - migrate script extracts strategy_id from old note format

Approach: exercise the store layer directly (pure functions; no FastAPI / engine plumbing).
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


def _load(modname, filepath):
    """Load module without triggering trading/__init__.py heavy imports."""
    spec = importlib.util.spec_from_file_location(modname, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


# ── fixtures ─────────────────────────────────────────────────
@pytest.fixture
def store_mod():
    sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
    return _load(
        "store_test_isolated",
        str(Path(__file__).parent.parent / "app/services/trading/store.py"),
    )


@pytest.fixture
def models_mod():
    sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
    return _load(
        "models_test_isolated",
        str(Path(__file__).parent.parent / "app/services/trading/models.py"),
    )


def _fresh_data_dir(tmp_path) -> Path:
    p = tmp_path / "user_data"
    p.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _now_iso():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


# ── 1) Schema accepts strategy_id with default None ─────────────
class TestSchemas:
    def test_order_strategy_id_default_none(self, models_mod):
        o = models_mod.Order(
            id="ord1", symbol="600519.SH", side="buy", qty=100,
            limit_price=1500.0, created_at=_now_iso(),
        )
        assert o.strategy_id is None

    def test_fill_strategy_id_default_none(self, models_mod):
        from datetime import date
        f = models_mod.Fill(
            id="fl1", order_id="ord1", symbol="600519.SH", side="buy",
            qty=100, price=1500.0, fee=0.0, tax=0.0, amount=150000.0,
            filled_at=_now_iso(), trade_date=date.today(),
        )
        assert f.strategy_id is None

    def test_trade_log_strategy_id_default_none(self, models_mod):
        e = models_mod.TradeLogEntry(
            id="lg1", kind="submit", ts=_now_iso(), message="test",
        )
        assert e.strategy_id is None

    def test_order_with_strategy_id_round_trips(self, models_mod):
        """Field set explicitly + JSONL dump + reload preserves value."""
        o = models_mod.Order(
            id="ord1", symbol="600519.SH", side="buy", qty=100,
            limit_price=1500.0, created_at=_now_iso(),
            strategy_id="demo_a",
        )
        d = o.model_dump(mode="json")
        assert d["strategy_id"] == "demo_a"
        # Re-parse
        o2 = models_mod.Order(**d)
        assert o2.strategy_id == "demo_a"


# ── 2) Store layer: list_orders/fills/logs use strategy_id field, not note ─
class TestStoreFirstClassStrategyId:
    def test_list_orders_filters_by_strategy_id_field(self, store_mod, tmp_path):
        """Two orders: one strategy_id='A', one strategy_id='B', one manual (None).
        list_orders(strategy_id='A') must return only the 'A' one — NOT parse note."""
        dd = _fresh_data_dir(tmp_path)
        now = _now_iso()
        o_a = store_mod.Order(
            id="ord_a", symbol="600000.SH", side="buy", qty=100,
            limit_price=10.0, created_at=now, note="manual a",
            strategy_id="A",
        )
        o_b = store_mod.Order(
            id="ord_b", symbol="600000.SH", side="buy", qty=200,
            limit_price=11.0, created_at=now, note="manual b",
            strategy_id="B",
        )
        o_manual = store_mod.Order(
            id="ord_m", symbol="600000.SH", side="buy", qty=50,
            limit_price=12.0, created_at=now, note="突破追单",  # 无 strategy:<sid>:<event> 前缀
            strategy_id=None,
        )
        store_mod.append_order(dd, o_a)
        store_mod.append_order(dd, o_b)
        store_mod.append_order(dd, o_manual)

        a_list = store_mod.list_orders(dd, strategy_id="A")
        assert [o.id for o in a_list] == ["ord_a"]

        b_list = store_mod.list_orders(dd, strategy_id="B")
        assert [o.id for o in b_list] == ["ord_b"]

        all_list = store_mod.list_orders(dd)
        assert {o.id for o in all_list} == {"ord_a", "ord_b", "ord_m"}

    def test_list_orders_does_not_classify_by_note(self, store_mod, tmp_path):
        """An order with note='strategy:A:rebalance' but strategy_id=None
        must NOT show up in list_orders(strategy_id='A')."""
        dd = _fresh_data_dir(tmp_path)
        now = _now_iso()
        # User pasted historical note format but new write path leaves strategy_id=None
        o_pasted = store_mod.Order(
            id="ord_p", symbol="600000.SH", side="buy", qty=100,
            limit_price=10.0, created_at=now,
            note="strategy:A:rebalance",
            strategy_id=None,
        )
        store_mod.append_order(dd, o_pasted)

        a_list = store_mod.list_orders(dd, strategy_id="A")
        # list_orders 用第一性字段判断, 不该被 note 字符串带偏
        assert a_list == []

    def test_list_fills_filters_by_inherited_strategy_id(self, store_mod, tmp_path):
        """Fill should inherit Order.strategy_id (set by engine._settle_fill in production).
        Here we set Fill.strategy_id directly to simulate that inheritance has happened.
        list_fills(strategy_id='A') must return only Fills with strategy_id='A'."""
        from datetime import date
        dd = _fresh_data_dir(tmp_path)
        now = _now_iso()
        f_a = store_mod.Fill(
            id="fl_a", order_id="ord_a", symbol="600000.SH", side="buy",
            qty=100, price=10.0, fee=0.0, tax=0.0, amount=1000.0,
            filled_at=now, trade_date=date.today(),
            strategy_id="A",
        )
        f_b = store_mod.Fill(
            id="fl_b", order_id="ord_b", symbol="600000.SH", side="buy",
            qty=200, price=11.0, fee=0.0, tax=0.0, amount=2200.0,
            filled_at=now, trade_date=date.today(),
            strategy_id="B",
        )
        store_mod.append_fill(dd, f_a)
        store_mod.append_fill(dd, f_b)

        a_fills = store_mod.list_fills(dd, strategy_id="A")
        assert [f.id for f in a_fills] == ["fl_a"]
        b_fills = store_mod.list_fills(dd, strategy_id="B")
        assert [f.id for f in b_fills] == ["fl_b"]

    def test_list_logs_filters_by_inherited_strategy_id(self, store_mod, tmp_path):
        dd = _fresh_data_dir(tmp_path)
        now = _now_iso()
        e_a = store_mod.TradeLogEntry(
            id="lg_a", kind="submit", ts=now, order_id="ord_a",
            message="A submit", strategy_id="A",
        )
        e_b = store_mod.TradeLogEntry(
            id="lg_b", kind="submit", ts=now, order_id="ord_b",
            message="B submit", strategy_id="B",
        )
        store_mod.append_log(dd, e_a)
        store_mod.append_log(dd, e_b)

        a_logs = store_mod.list_logs(dd, strategy_id="A")
        assert [e.id for e in a_logs] == ["lg_a"]
        b_logs = store_mod.list_logs(dd, strategy_id="B")
        assert [e.id for e in b_logs] == ["lg_b"]

    def test_no_strategy_id_param_returns_all(self, store_mod, tmp_path):
        """Default behavior (strategy_id=None) returns everything — current contract."""
        from datetime import date
        dd = _fresh_data_dir(tmp_path)
        now = _now_iso()
        store_mod.append_order(dd, store_mod.Order(
            id="o1", symbol="X.SH", side="buy", qty=1, limit_price=1.0,
            created_at=now, strategy_id="A",
        ))
        store_mod.append_order(dd, store_mod.Order(
            id="o2", symbol="Y.SH", side="buy", qty=1, limit_price=1.0,
            created_at=now, strategy_id="B",
        ))
        assert {o.id for o in store_mod.list_orders(dd)} == {"o1", "o2"}
        assert {o.id for o in store_mod.list_orders(dd, strategy_id=None)} == {"o1", "o2"}


# ── 3) Static lint: list_* no longer calls extract_strategy_id ──
class TestStaticLint:
    def test_extract_strategy_id_not_in_list_bodies(self):
        """Per docs §6 risk row, list_* must use first-class field, not parse note."""
        store_path = (
            Path(__file__).parent.parent / "app/services/trading/store.py"
        )
        src = store_path.read_text()
        # Find list_orders body (until next top-level def or empty line)
        import re
        for name in ("list_orders", "list_fills", "list_logs"):
            m = re.search(
                rf"^def {name}\b.*?(?=^def |\Z)", src, re.MULTILINE | re.DOTALL
            )
            assert m, f"{name} not found"
            body = m.group(0)
            assert "extract_strategy_id" not in body, (
                f"{name} still calls extract_strategy_id — must use first-class field"
            )

    def test_extract_strategy_id_function_still_defined(self):
        """Function must remain for migrate script + diagnostics."""
        store_path = (
            Path(__file__).parent.parent / "app/services/trading/store.py"
        )
        assert "def extract_strategy_id" in store_path.read_text()


# ── 4) Migrate script dry-run test ───────────────────────────
class TestMigrateScript:
    def test_dry_run_no_writes(self, tmp_path):
        """Migrate on synthetic orders.jsonl extracts strategy_id from note (dry-run)."""
        dd = tmp_path
        (dd / "user_data" / "trading").mkdir(parents=True)
        orders_path = dd / "user_data" / "trading" / "orders.jsonl"
        old_order = {
            "id": "ord_legacy",
            "symbol": "600519.SH",
            "side": "buy",
            "order_type": "limit",
            "qty": 100,
            "limit_price": 1500.0,
            "status": "filled",
            "filled_qty": 100,
            "avg_fill_price": 1500.0,
            "created_at": _now_iso(),
            "submitted_at": _now_iso(),
            "filled_at": _now_iso(),
            "canceled_at": None,
            "reject_reason": None,
            "note": "strategy:demo_a:rebalance",
            # strategy_id 缺省 (Pydantic 默认 None)
        }
        orders_path.write_text(json.dumps(old_order, ensure_ascii=False) + "\n", encoding="utf-8")

        # Import + invoke dry-run
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.migrate_strategy_id_to_firstclass import migrate_orders
        filled, unmatched, errors = migrate_orders(orders_path, dry_run=True)

        assert errors == 0
        assert filled == 1, f"expected 1 fill (note→strategy_id), got {filled}"
        assert unmatched == 0

        # File unchanged (dry-run)
        on_disk = json.loads(orders_path.read_text().strip())
        assert "strategy_id" not in on_disk or on_disk.get("strategy_id") is None

    def test_confirm_mode_writes_strategy_id(self, tmp_path):
        """With dry_run=False, the file gets overwritten with strategy_id filled in."""
        dd = tmp_path
        (dd / "user_data" / "trading").mkdir(parents=True)
        orders_path = dd / "user_data" / "trading" / "orders.jsonl"
        old_order = {
            "id": "ord_legacy",
            "symbol": "600519.SH",
            "side": "buy",
            "order_type": "limit",
            "qty": 100,
            "limit_price": 1500.0,
            "status": "filled",
            "filled_qty": 100,
            "avg_fill_price": 1500.0,
            "created_at": _now_iso(),
            "submitted_at": _now_iso(),
            "filled_at": _now_iso(),
            "canceled_at": None,
            "reject_reason": None,
            "note": "strategy:demo_b:rebalance",
        }
        orders_path.write_text(json.dumps(old_order, ensure_ascii=False) + "\n", encoding="utf-8")

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.migrate_strategy_id_to_firstclass import migrate_orders
        filled, unmatched, errors = migrate_orders(orders_path, dry_run=False)
        assert errors == 0 and filled == 1

        on_disk = json.loads(orders_path.read_text().strip())
        assert on_disk["strategy_id"] == "demo_b", (
            f"expected strategy_id='demo_b', got {on_disk.get('strategy_id')!r}"
        )

    def test_unrecognized_note_leaves_strategy_id_none(self, tmp_path):
        """Manual note '突破追单' must NOT become strategy_id — unmatched count."""
        dd = tmp_path
        (dd / "user_data" / "trading").mkdir(parents=True)
        orders_path = dd / "user_data" / "trading" / "orders.jsonl"
        old_order = {
            "id": "ord_m",
            "symbol": "X.SH",
            "side": "buy",
            "order_type": "limit",
            "qty": 1,
            "limit_price": 1.0,
            "status": "new",
            "created_at": _now_iso(),
            "note": "突破追单",  # 自由文本, 不应被识别
        }
        orders_path.write_text(json.dumps(old_order, ensure_ascii=False) + "\n", encoding="utf-8")

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.migrate_strategy_id_to_firstclass import migrate_orders
        filled, unmatched, errors = migrate_orders(orders_path, dry_run=False)
        assert errors == 0
        assert filled == 0
        assert unmatched == 1, f"expected 1 unmatched, got {unmatched}"
