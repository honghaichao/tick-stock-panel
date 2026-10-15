"""scripts/cleanup_stale_pnl_snapshots.py 的复现 + 回归测试.

只 import 纯逻辑 (derive_initial_cash / is_double_counted / collect_stats /
filter_rows), 避开 REPO_ROOT 和 argparse 入口. 不写文件, 不用临时目录.

历史脏快照两类:
  rule 1 (mv == 0): engine 启动瞬间 quote_service 未就绪, mv=0 落盘
  rule 2 (双计):    sell 后 cash 已加 proceeds, mv 还没刷卖出份额
                    → cash + mv - realized - unrealized ≈ 2 × initial_cash
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# 把 scripts/ 加进 sys.path, 直接 module-spec 加载避免污染
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "cleanup_stale_pnl_snapshots",
    _SCRIPTS_DIR / "cleanup_stale_pnl_snapshots.py",
)
assert _SPEC and _SPEC.loader, "cleanup_stale_pnl_snapshots.py 加载失败"
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

derive_initial_cash = _mod.derive_initial_cash
is_double_counted = _mod.is_double_counted
collect_stats = _mod.collect_stats
filter_rows = _mod.filter_rows


INITIAL_CASH = 1_000_000.0


def _snap(
    ts: str,
    *,
    trade_date: str,
    total_equity: float,
    cash: float,
    market_value: float,
    realized: float = 0.0,
    unrealized: float = 0.0,
    total_pnl: float | None = None,
    trigger: str = "tick",
) -> dict:
    """构造一条干净口径快照 (total_equity = cash + mv + realized + unrealized)."""
    if total_pnl is None:
        total_pnl = realized + unrealized
    return {
        "ts": ts,
        "trade_date": trade_date,
        "total_equity": total_equity,
        "cash": cash,
        "market_value": market_value,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": total_pnl,
        "trigger": trigger,
    }


# ---------- derive_initial_cash ----------


def test_derive_initial_cash_from_clean_snapshots():
    """干净段中位数 = initial_cash (≈ 1M), 不被单点脏快照污染."""
    rows = [
        # 干净段: 持仓状态, cash + mv + pnl ≈ initial, cash+mv-pnl ≈ initial
        _snap("2026-10-13T10:00:00+08:00", trade_date="2026-10-13",
              total_equity=999_983, cash=997_661, market_value=2_322,
              unrealized=-2),
        _snap("2026-10-14T10:00:00+08:00", trade_date="2026-10-14",
              total_equity=1_002_123, cash=2_603, market_value=999_520,
              unrealized=2_000),
    ]
    assert derive_initial_cash(rows) == pytest.approx(INITIAL_CASH, rel=1e-3)


def test_derive_initial_cash_excludes_double_counted():
    """双计快照 (cash+mv ≈ 2 × initial) 应被中位数忽略, 不污染 initial_cash 估计."""
    rows = [
        _snap("2026-10-13T10:00:00+08:00", trade_date="2026-10-13",
              total_equity=1_000_000, cash=500_000, market_value=500_000),
        _snap("2026-10-13T10:01:00+08:00", trade_date="2026-10-13",
              total_equity=1_000_000, cash=500_000, market_value=500_000),
        _snap("2026-10-13T10:02:00+08:00", trade_date="2026-10-13",
              total_equity=1_000_000, cash=500_000, market_value=500_000),
        # 1 条双计 (cash=1M + mv=1M, equity=2M, pnl=0)
        _snap("2026-10-13T10:03:00+08:00", trade_date="2026-10-13",
              total_equity=2_000_000, cash=1_000_000, market_value=1_000_000),
    ]
    # 中位数 = 1M (5 个候选里第 3 个), 双计在末尾不污染
    assert derive_initial_cash(rows) == pytest.approx(INITIAL_CASH, rel=1e-3)


def test_derive_initial_cash_empty_falls_back_to_default():
    """无 mv>0 快照 → 退回 1M 默认值, 不抛异常."""
    assert derive_initial_cash([]) == 1_000_000.0


def test_derive_initial_cash_excludes_mv_zero_snapshots():
    """mv=0 快照不应参与 initial_cash 估计 (cash 残留不反映真实投入)."""
    rows = [
        # mv==0 段 (rule 1 命中的脏快照)
        _snap("2026-10-13T09:00:00+08:00", trade_date="2026-10-13",
              total_equity=2_603, cash=2_603, market_value=0),
        # 干净段
        _snap("2026-10-13T10:00:00+08:00", trade_date="2026-10-13",
              total_equity=999_983, cash=997_661, market_value=2_322,
              unrealized=-2),
    ]
    # mv==0 不进候选, 中位数 = 999983 ≈ initial
    assert derive_initial_cash(rows) == pytest.approx(999_983, rel=1e-3)


# ---------- is_double_counted ----------


def test_is_double_counted_normal_state_false():
    """干净段: cash + mv ≈ initial, 双计判定为 False."""
    snap = _snap("2026-10-13T10:00:00+08:00", trade_date="2026-10-13",
                 total_equity=1_002_123, cash=2_603, market_value=999_520,
                 unrealized=2_000)
    assert is_double_counted(snap, INITIAL_CASH) is False


def test_is_double_counted_sell_mv_unrefreshed_true():
    """复现 2026-10-15 10:24~10:37 bug: sell 后 cash+=1M (proceeds), mv 还没刷
    卖出份额 → cash=1M + mv=1M = 2M, equity=2M, total_pnl≈0. 双计判定为 True.
    """
    snap = _snap("2026-10-15T10:24:45+08:00", trade_date="2026-10-15",
                 total_equity=2_008_220, cash=1_005_230.15,
                 market_value=1_002_990, unrealized=5_420)
    assert is_double_counted(snap, INITIAL_CASH) is True


def test_is_double_counted_mv_zero_returns_false():
    """mv=0 的快照双计判定为 False (归 rule 1 处理, 不要双重计数)."""
    snap = _snap("2026-10-13T09:00:00+08:00", trade_date="2026-10-13",
                 total_equity=2_603, cash=2_603, market_value=0)
    assert is_double_counted(snap, INITIAL_CASH) is False


def test_is_double_counted_legitimate_large_gain_false():
    """合法大涨: cash + mv - pnl 仍 ≈ initial (不管总盈亏多大), 双计判定为 False.
    
    即使 realized=+500K (赚了一笔大的), cash + mv - pnl = cash + mv - realized = initial
    (因 equity = initial + total_pnl = cash + mv + total_pnl ⇒ cash + mv = initial + pnl - pnl = initial).
    """
    snap = _snap("2026-10-15T15:00:00+08:00", trade_date="2026-10-15",
                 total_equity=1_500_000, cash=500_000, market_value=500_000,
                 realized=500_000)
    assert is_double_counted(snap, INITIAL_CASH) is False


# ---------- filter_rows (集成) ----------


def test_filter_rows_keeps_clean_drops_both_dirty_types():
    """两类脏快照同时命中, filter_rows 只保留干净段."""
    rows = [
        # 干净 1 (早期): cash=997k, mv=2k, equity=999983
        _snap("2026-10-13T10:00:00+08:00", trade_date="2026-10-13",
              total_equity=999_983, cash=997_661, market_value=2_322,
              unrealized=-2),
        # 脏 1 (mv=0 半态)
        _snap("2026-10-13T09:00:00+08:00", trade_date="2026-10-13",
              total_equity=2_603, cash=2_603, market_value=0),
        # 脏 2 (双计)
        _snap("2026-10-15T10:24:45+08:00", trade_date="2026-10-15",
              total_equity=2_008_220, cash=1_005_230,
              market_value=1_002_990, unrealized=5_420),
        # 干净 2 (今日稳定段): cash=7k, mv=1M, equity≈1M+7k
        _snap("2026-10-15T11:00:00+08:00", trade_date="2026-10-15",
              total_equity=1_011_948, cash=7_360, market_value=1_004_588),
    ]
    kept = filter_rows(rows, INITIAL_CASH)
    ts_kept = {r["ts"] for r in kept}
    assert ts_kept == {
        "2026-10-13T10:00:00+08:00",
        "2026-10-15T11:00:00+08:00",
    }
    assert len(kept) == 2


def test_filter_rows_all_clean_returns_all():
    """全干净: filter_rows 返回全部."""
    rows = [
        _snap("2026-10-13T10:00:00+08:00", trade_date="2026-10-13",
              total_equity=999_983, cash=997_661, market_value=2_322,
              unrealized=-2),
        _snap("2026-10-13T10:01:00+08:00", trade_date="2026-10-13",
              total_equity=1_002_123, cash=2_603, market_value=999_520,
              unrealized=2_000),
    ]
    assert len(filter_rows(rows, INITIAL_CASH)) == 2


def test_filter_rows_all_dirty_returns_empty():
    """全脏 (mv=0 或 双计): filter_rows 返回空."""
    rows = [
        _snap("2026-10-13T09:00:00+08:00", trade_date="2026-10-13",
              total_equity=2_603, cash=2_603, market_value=0),
        _snap("2026-10-15T10:24:45+08:00", trade_date="2026-10-15",
              total_equity=2_008_220, cash=1_005_230, market_value=1_002_990),
    ]
    assert filter_rows(rows, INITIAL_CASH) == []


def test_filter_rows_derived_initial_cash_used():
    """不传 initial_cash 时用 derive_initial_cash 自动从数据反推."""
    # 构造一组快照, 中位数能反映真实 initial_cash=500_000
    rows = [
        _snap("2026-10-13T10:00:00+08:00", trade_date="2026-10-13",
              total_equity=500_000, cash=250_000, market_value=250_000),
        _snap("2026-10-13T10:01:00+08:00", trade_date="2026-10-13",
              total_equity=500_000, cash=250_000, market_value=250_000),
        _snap("2026-10-13T10:02:00+08:00", trade_date="2026-10-13",
              total_equity=500_000, cash=250_000, market_value=250_000),
        # 双计: cash=500k + mv=500k = 1M
        _snap("2026-10-13T10:03:00+08:00", trade_date="2026-10-13",
              total_equity=1_000_000, cash=500_000, market_value=500_000),
    ]
    est = derive_initial_cash(rows)
    assert est == pytest.approx(500_000, rel=1e-3)
    # 自动用 derived initial_cash 过滤, 双计被拦下
    kept = filter_rows(rows, est)
    ts_kept = {r["ts"] for r in kept}
    assert "2026-10-13T10:03:00+08:00" not in ts_kept
    assert len(kept) == 3


# ---------- collect_stats ----------


def test_collect_stats_separates_two_rule_buckets():
    """stats 应分开记录 mv=0 / 双计 / 保留."""
    rows = [
        _snap("2026-10-13T10:00:00+08:00", trade_date="2026-10-13",
              total_equity=999_983, cash=997_661, market_value=2_322,
              unrealized=-2),
        _snap("2026-10-13T09:00:00+08:00", trade_date="2026-10-13",
              total_equity=2_603, cash=2_603, market_value=0),
        _snap("2026-10-15T10:24:45+08:00", trade_date="2026-10-15",
              total_equity=2_008_220, cash=1_005_230,
              market_value=1_002_990, unrealized=5_420),
    ]
    stats = collect_stats(rows, INITIAL_CASH)
    assert stats["total"] == 3
    assert stats["kept"] == 1
    assert stats["removed_mv_eq_0"] == 1
    assert stats["removed_double_counted"] == 1


def test_collect_stats_sums_equals_total():
    """kept + removed_mv_eq_0 + removed_double_counted == total."""
    rows = [
        _snap("2026-10-13T10:00:00+08:00", trade_date="2026-10-13",
              total_equity=999_983, cash=997_661, market_value=2_322,
              unrealized=-2),
        _snap("2026-10-13T09:00:00+08:00", trade_date="2026-10-13",
              total_equity=2_603, cash=2_603, market_value=0),
        _snap("2026-10-15T10:24:45+08:00", trade_date="2026-10-15",
              total_equity=2_008_220, cash=1_005_230, market_value=1_002_990),
    ]
    stats = collect_stats(rows, INITIAL_CASH)
    assert stats["kept"] + stats["removed_mv_eq_0"] + stats["removed_double_counted"] == stats["total"]
