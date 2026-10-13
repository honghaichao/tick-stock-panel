"""F1-4: drift 检测 (实盘 vs verify)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.services.trading import drift, store
from app.services.trading.models import Fill


def _write_fill(data_dir, sym, side="buy", qty=100, price=10.0, days_ago=0):
    from app.services.trading.store import _new_id
    when = datetime.now(timezone(timedelta(hours=8))) - timedelta(days=days_ago)
    f = Fill(
        id=_new_id("fl"),
        order_id=_new_id("ord"),
        symbol=sym,
        side=side,
        qty=qty,
        price=price,
        amount=price * qty,
        trade_date=(when - timedelta(days=1)).date(),
        filled_at=when.isoformat(),
        strategy_id="st",
    )
    store.append_fill(data_dir, f)


def _write_verify(data_dir, run_id, target_symbols):
    d = data_dir / "user_data" / "trading" / "verifications" / "st"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps({
        "run_id": run_id,
        "strategy_id": "st",
        "started_at": datetime.now().isoformat(),
        "last_rebalance_target": {s: 100 for s in target_symbols},
        "target_symbols": sorted(target_symbols),
    }, ensure_ascii=False), encoding="utf-8")


# ── 端到端: 各种 drift 场景 ──────────────────────────────────────
def test_drift_no_verify_returns_empty_overlap(tmp_path):
    _write_fill(tmp_path, "600000.SH")
    r = drift.compute_drift(tmp_path, "st")
    assert r.verify_symbols == []
    assert r.live_symbols == ["600000.SH"]
    assert r.only_verify == []
    assert r.only_live == ["600000.SH"]
    assert r.verify_run_id is None


def test_drift_no_live_returns_full_verify_drift(tmp_path):
    _write_verify(tmp_path, "r1", ["600000.SH", "600001.SH"])
    r = drift.compute_drift(tmp_path, "st")
    assert r.live_symbols == []
    assert r.verify_symbols == ["600000.SH", "600001.SH"]
    assert r.only_verify == ["600000.SH", "600001.SH"]
    assert r.only_live == []
    assert r.verify_run_id == "r1"


def test_drift_perfect_overlap(tmp_path):
    for s in ["600000.SH", "600001.SH"]:
        _write_fill(tmp_path, s)
    _write_verify(tmp_path, "r1", ["600000.SH", "600001.SH"])
    r = drift.compute_drift(tmp_path, "st")
    assert set(r.overlap) == {"600000.SH", "600001.SH"}
    assert r.only_live == []
    assert r.only_verify == []
    assert r.overlap_pct == 1.0
    assert r.fill_count == 2


def test_drift_complete_mismatch(tmp_path):
    _write_fill(tmp_path, "999999.SH")  # only live
    _write_verify(tmp_path, "r1", ["888888.SH"])  # only verify
    r = drift.compute_drift(tmp_path, "st")
    assert r.overlap == []
    assert r.only_live == ["999999.SH"]
    assert r.only_verify == ["888888.SH"]
    assert r.overlap_pct == 0.0


def test_drift_uses_latest_verify_run(tmp_path):
    """多份 verify 报告时, 用最新的 (lexicographic sort 由 list_verifications 倒序保证)."""
    _write_verify(tmp_path, "r1", ["600000.SH"])
    _write_verify(tmp_path, "r2", ["600001.SH"])  # 更新
    r = drift.compute_drift(tmp_path, "st")
    assert r.verify_symbols == ["600001.SH"]
    assert r.verify_run_id == "r2"


def test_drift_window_excludes_old_fills(tmp_path):
    """window_days=7 → 8 天前的 fill 不计入."""
    _write_fill(tmp_path, "OLD.SH", days_ago=10)
    _write_fill(tmp_path, "NEW.SH", days_ago=1)
    r = drift.compute_drift(tmp_path, "st", window_days=7)
    assert "OLD.SH" not in r.live_symbols
    assert "NEW.SH" in r.live_symbols
