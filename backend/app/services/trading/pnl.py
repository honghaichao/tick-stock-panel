"""PnL 历史与指标 — 净值快照 + 收益/回撤/胜率计算。

数据流:
  - 每次 fill 落账后 (engine._settle_fill) 调用 append_snapshot → 写 equity 快照.
  - 撮合线程每轮撮合结束 (engine._apply_decisions) 也写一次 → 实时跟踪.
  - 前端 GET /api/trading/pnl-history → 时间序列 (ECharts 曲线).
  - 前端 GET /api/trading/pnl-metrics → 关键指标 (今日/总/最大回撤/胜率).

口径 (CONTRIBUTING §3.1):
  - realized_pnl = sum(卖出成交额 - 卖出时点持仓成本) - 卖出费税.
  - unrealized_pnl = sum((现价 - 持仓成本) * 持仓数量).
  - total_pnl = realized + unrealized.
  - 百分比一律小数制 (0.0366 = 3.66%).
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.trading import store
from app.services.trading.models import OrderSide

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))

# 保留上限: 一年约 240 个交易日, 每 5 秒一撮合轮 ≈ 28800 笔/日; 7 天 200k,
# 我们只保留日级 + 关键事件 (fill), 实际远低于此上限.
PNL_MAX_RECORDS = 100_000


# ── 快照数据 ───────────────────────────────────────
@dataclass(frozen=True)
class PnLSnapshot:
    ts: str           # ISO8601 北京时区
    trade_date: str   # YYYY-MM-DD
    total_equity: float
    cash: float
    market_value: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    trigger: str      # 'fill' | 'tick' | 'session_start' | 'session_end' | 'manual'

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "trade_date": self.trade_date,
            "total_equity": self.total_equity,
            "cash": self.cash,
            "market_value": self.market_value,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_pnl": self.total_pnl,
            "trigger": self.trigger,
        }


# ── 路径 ─────────────────────────────────────────
def _path(data_dir: Path) -> Path:
    p = data_dir / "user_data" / "trading" / "pnl_snapshots.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ── 写 ────────────────────────────────────────────
def append_snapshot(data_dir: Path, snapshot: PnLSnapshot) -> None:
    """追加一条快照 (append-only JSONL); 满上限后 prune."""
    line = json.dumps(snapshot.to_dict(), ensure_ascii=False)
    p = _path(data_dir)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    _maybe_prune(p)


def _maybe_prune(p: Path) -> None:
    try:
        count = sum(1 for _ in p.open("r", encoding="utf-8") if _.strip())
    except OSError:
        return
    if count <= PNL_MAX_RECORDS:
        return
    # 保留尾部 PNL_MAX_RECORDS
    lines = p.read_text(encoding="utf-8").splitlines()
    keep = lines[-PNL_MAX_RECORDS:]
    from app.services.fs_utils import atomic_write_text
    atomic_write_text(p, "\n".join(keep) + "\n")


# ── 读 ────────────────────────────────────────────
def list_history(
    data_dir: Path,
    *,
    start: str | None = None,    # YYYY-MM-DD
    end: str | None = None,
    trigger: str | None = None,
    limit: int | None = None,
    strategy_id: str | None = None,
) -> list[dict]:
    """读取快照 (时间正序). 支持日期范围 / trigger / strategy_id 过滤."""
    items: list[dict] = []
    p = _path(data_dir)
    if not p.exists():
        return items
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if start and rec.get("trade_date", "") < start:
            continue
        if end and rec.get("trade_date", "") > end:
            continue
        if trigger and rec.get("trigger") != trigger:
            continue
        if strategy_id and rec.get("strategy_id") and rec.get("strategy_id") != strategy_id:
            continue
        items.append(rec)
    if limit:
        items = items[-limit:]
    return items


# ── 指标 ──────────────────────────────────────────
@dataclass(frozen=True)
class PnLMetrics:
    """单点指标 (GET /api/trading/pnl-metrics 返回)."""
    total_pnl: float
    total_pnl_pct: float              # 总收益 / 初始资金
    today_pnl: float
    today_pnl_pct: float             # 今日收益 / 昨日结算权益
    realized_pnl: float
    unrealized_pnl: float
    initial_cash: float
    current_equity: float
    max_drawdown: float              # 最大回撤比例 (正数, 例如 0.15 = 15%)
    max_drawdown_peak: float         # 回撤峰值时的权益
    max_drawdown_trough: float       # 回撤谷值时的权益
    max_drawdown_date: str | None    # 回撤发生日期 (YYYY-MM-DD)
    total_trades: int                # 总成交笔数
    winning_trades: int              # 盈利笔数 (卖出实现盈利)
    losing_trades: int               # 亏损笔数
    win_rate: float                  # 胜率 (0~1)
    avg_pnl_per_trade: float          # 每笔平均盈亏 (元)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def compute_metrics(
    data_dir: Path,
    *,
    initial_cash: float | None = None,
    strategy_id: str | None = None,
) -> PnLMetrics:
    """从历史快照 + fills 计算当前指标.

    Args:
        data_dir: 用户数据目录.
        initial_cash: 初始资金, 不传则从账户当前态读 (首次运行无 history 时取账户 cash).
        strategy_id: per-strategy 视图 — 只统计该 strategy 的 fills, 重算 realized/unrealized/total_pnl
            (不读 account.total_pnl 等全局值, 避免混入其他 strategy 的成交).
    """
    account = store.load_account(data_dir, initial_cash=initial_cash or 1_000_000.0)
    # per-strategy 时只取该 strategy 的 fills, 不用 account 总指标
    fills = store.list_fills(data_dir, limit=None, strategy_id=strategy_id)
    snapshots = list_history(data_dir, strategy_id=strategy_id)

    # ── 胜率: 卖出成交按 FIFO 配对买入, 实现盈亏 = 卖出价 - 对应买入价 - 费税 ──
    # fills 按时间正序处理; 维护各 symbol 的买入成本 (价格, qty).
    buy_lots: dict[str, list[tuple[float, int]]] = defaultdict(list)  # (price, qty)
    realized_by_trade: list[float] = []
    for f in reversed(fills):  # reversed → 时间正序
        if f.side == OrderSide.BUY:
            buy_lots[f.symbol].append((f.price, f.qty))
        else:
            remaining = f.qty
            sell_proceeds = f.price * f.qty - f.fee - f.tax
            matched_qty = 0
            matched_cost = 0.0
            while remaining > 0 and buy_lots[f.symbol]:
                bp, bq = buy_lots[f.symbol][0]
                if bq <= remaining:
                    matched_cost += bp * bq
                    matched_qty += bq
                    remaining -= bq
                    buy_lots[f.symbol].pop(0)
                else:
                    matched_cost += bp * remaining
                    matched_qty += remaining
                    buy_lots[f.symbol][0] = (bp, bq - remaining)
                    remaining = 0
            if matched_qty > 0:
                realized_by_trade.append(sell_proceeds - matched_cost)

    total_trades = len(realized_by_trade)
    winning = sum(1 for p in realized_by_trade if p > 0)
    losing = sum(1 for p in realized_by_trade if p < 0)
    win_rate = (winning / total_trades) if total_trades > 0 else 0.0
    avg_pnl = (sum(realized_by_trade) / total_trades) if total_trades > 0 else 0.0

    # ── 今日 / 总 PnL ──
    today_str = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    yesterday_str = (datetime.now(CN_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    initial_cash_eff = initial_cash or 1_000_000.0

    if strategy_id:
        # ── per-strategy 视图: 从该 strategy 的 fills 重算 ──
        # 累计买入/卖出现金流 (含 fee/tax, 跟账户 cash 口径一致)
        #   buy:  account.cash -= (amount + fee)
        #   sell: account.cash += (amount - fee - tax)
        # cash_proxy = initial_cash - sum_buy_net + sum_sell_net
        total_buy_net = 0.0
        total_sell_net = 0.0
        for f in fills:
            if f.side == OrderSide.BUY:
                total_buy_net += f.amount + f.fee
            else:
                total_sell_net += f.amount - f.fee - f.tax
        # 持仓: 按 symbol FIFO 累计 buy - sell
        # fills 必须按时间正序遍历 (旧 -> 新); store.list_fills 默认倒序, 这里反转.
        lots: dict[str, dict[str, float]] = {}
        for f in reversed(fills):  # reversed → 时间正序
            sym = f.symbol
            lot = lots.setdefault(sym, {"qty": 0.0, "cost_basis": 0.0})
            if f.side == OrderSide.BUY:
                lot["cost_basis"] += f.price * f.qty
                lot["qty"] += f.qty
            else:
                if lot["qty"] > 0:
                    ratio = max(0.0, (lot["qty"] - f.qty) / lot["qty"])
                    lot["cost_basis"] *= ratio
                    lot["qty"] -= f.qty
                if lot["qty"] <= 0:
                    lot["qty"] = 0.0
                    lot["cost_basis"] = 0.0
        # realized_pnl = FIFO 配对累计
        realized_pnl = sum(realized_by_trade)
        # unrealized_pnl: 读 quote_service 取该 strategy 持仓的 last_price - avg_cost
        from app.services.trading.engine import _latest_prices_and_prev_closes as _lpc
        last_prices, _ = _lpc(data_dir)
        unrealized_pnl = 0.0
        market_value_total = 0.0
        for sym, lot in lots.items():
            if lot["qty"] > 0 and sym in last_prices and lot["cost_basis"] > 0:
                avg_cost = lot["cost_basis"] / lot["qty"]
                lp = last_prices[sym]
                unrealized_pnl += (lp - avg_cost) * lot["qty"]
                market_value_total += lp * lot["qty"]
        # per-strategy equity: cash_proxy + sum(market_value)
        # cash_proxy = initial_cash + sell_net_proceeds - buy_net_spent
        #         (含 fee/tax, 跟实际账户 cash 口径一致)
        cash_proxy = initial_cash_eff + total_sell_net - total_buy_net
        current_equity = cash_proxy + market_value_total
        total_pnl = realized_pnl + unrealized_pnl
        today_baseline = _equity_at_start_of_day(snapshots, today_str, yesterday_str, initial_cash=initial_cash_eff)
        today_pnl = current_equity - today_baseline
        total_pnl_pct = total_pnl / initial_cash_eff if initial_cash_eff > 0 else 0.0
        max_dd, peak, trough, dd_date = _compute_max_drawdown(snapshots, initial_cash_eff, current_equity)
    else:
        # ── 账户级视图 (原逻辑): 用 account 实时值 ──
        total_pnl = account.total_pnl
        realized_pnl = account.realized_pnl
        unrealized_pnl = account.unrealized_pnl
        today_baseline = _equity_at_start_of_day(snapshots, today_str, yesterday_str, initial_cash=account.initial_cash)
        today_pnl = account.total_equity - today_baseline
        today_pnl_pct = today_pnl / today_baseline if today_baseline > 0 else 0.0
        total_pnl_pct = total_pnl / account.initial_cash if account.initial_cash > 0 else 0.0
        max_dd, peak, trough, dd_date = _compute_max_drawdown(snapshots, account.initial_cash, account.total_equity)
        current_equity = account.total_equity

    today_pnl_pct = today_pnl / today_baseline if today_baseline > 0 else 0.0

    return PnLMetrics(
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        today_pnl=today_pnl,
        today_pnl_pct=today_pnl_pct,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        initial_cash=initial_cash_eff,
        current_equity=current_equity,
        max_drawdown=max_dd,
        max_drawdown_peak=peak,
        max_drawdown_trough=trough,
        max_drawdown_date=dd_date,
        total_trades=total_trades,
        winning_trades=winning,
        losing_trades=losing,
        win_rate=win_rate,
        avg_pnl_per_trade=avg_pnl,
    )


def _equity_at_start_of_day(snapshots: list[dict], today: str, yesterday: str, *, initial_cash: float) -> float:
    """取今日开始时的权益基准. 今日首笔快照 total_equity, 否则昨日末笔, 否则 initial_cash."""
    for s in snapshots:
        if s.get("trade_date", "") == today:
            return float(s.get("total_equity", initial_cash))
    # 没有今日快照 → 倒找昨日最后一笔
    yesterday_last: float | None = None
    for s in snapshots:
        if s.get("trade_date", "") == yesterday:
            yesterday_last = float(s.get("total_equity", initial_cash))
    if yesterday_last is not None:
        return yesterday_last
    return initial_cash


def _compute_max_drawdown(
    snapshots: list[dict],
    initial_cash: float,
    current_equity: float,
) -> tuple[float, float, float, str | None]:
    """最大回撤 = (peak - trough) / peak** from equity curve.

    Returns: (max_drawdown_pct, peak, trough, peak_date).

    `peak_date` 取 peak 触达时的日期 (回撤起点).
    """
    if not snapshots:
        # 无快照, 用 initial + current 估一个
        peak = max(initial_cash, current_equity)
        trough = min(initial_cash, current_equity)
        dd = (peak - trough) / peak if peak > 0 else 0.0
        return dd, peak, trough, None

    equity_series: list[tuple[str, float]] = [(s["trade_date"], float(s["total_equity"])) for s in snapshots]
    if equity_series and equity_series[0][1] != initial_cash:
        equity_series.insert(0, ("__init__", initial_cash))
    if equity_series[-1][1] != current_equity:
        equity_series.append(("__now", current_equity))

    peak_val = -math.inf
    peak_date: str | None = None
    trough_val = math.inf
    max_dd = 0.0
    final_peak = initial_cash
    final_trough = current_equity
    final_peak_date: str | None = None

    for date, eq in equity_series:
        if eq > peak_val:
            peak_val = eq
            peak_date = date
        if eq < trough_val:
            trough_val = eq
        dd = (peak_val - eq) / peak_val if peak_val > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            final_peak = peak_val
            final_peak_date = peak_date
            final_trough = eq

    return max_dd, final_peak, final_trough, final_peak_date


__all__ = [
    "PnLMetrics",
    "PnLSnapshot",
    "append_snapshot",
    "compute_metrics",
    "list_history",
]



# ── Per-strategy PnL 流 (PR-1 子任务 §5.1; B2=A) ────────────────────────────
# 独立文件 pnl_strategy_snapshots.jsonl, 不动现有 pnl_snapshots.jsonl.
# 增量写: 策略 event 完成 / attribution 的 fill 落盘后, 由调用方写一条.
# 表计算 per-strategy summary 时只读这一个文件, 不再做 fills × orders JOIN.

STRATEGY_PNL_FILENAME = "pnl_strategy_snapshots.jsonl"
MAX_RECORDS = 50_000


class StrategyPnLSnapshot:
    """per-strategy 一条快照. schema 增加 strategy_id / event / ts; total_pnl 是当前累计."""

    __slots__ = ("cash_delta", "event", "filled_qty_by_symbol", "strategy_id", "total_pnl", "ts")

    def __init__(
        self,
        *,
        ts: str,
        strategy_id: str,
        event: str,
        total_pnl: float,
        cash_delta: float = 0.0,
        filled_qty_by_symbol: dict[str, int] | None = None,
    ) -> None:
        self.ts = ts
        self.strategy_id = strategy_id
        self.event = event
        self.total_pnl = float(total_pnl)
        self.cash_delta = float(cash_delta)
        self.filled_qty_by_symbol = filled_qty_by_symbol or {}

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "strategy_id": self.strategy_id,
            "event": self.event,
            "total_pnl": self.total_pnl,
            "cash_delta": self.cash_delta,
            "filled_qty_by_symbol": dict(self.filled_qty_by_symbol),
        }


def _strategy_path(data_dir: Path) -> Path:
    p = data_dir / "user_data" / "trading" / STRATEGY_PNL_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def write_strategy_snapshot(data_dir: Path, snapshot: StrategyPnLSnapshot) -> None:
    """追加一条 per-strategy 快照. 满上限后 prune 尾部."""
    p = _strategy_path(data_dir)
    line = json.dumps(snapshot.to_dict(), ensure_ascii=False)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    _maybe_prune_strategy(p)


def _maybe_prune_strategy(p: Path) -> None:
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_RECORDS:
        return
    keep = lines[-MAX_RECORDS:]
    from app.services.fs_utils import atomic_write_text
    atomic_write_text(p, "\n".join(keep) + "\n")


def _initial_cash_for_summary(data_dir: Path) -> float:
    try:
        from app.services.trading import preferences as trading_prefs
        v = trading_prefs.initial_cash()
        return float(v) if v else 1_000_000.0
    except Exception:
        return 1_000_000.0


def _read_strategy_pnl_records(data_dir: Path, strategy_id: str) -> list[dict]:
    """读该 strategy 全部快照 (append-only file, 量级小)."""
    p = _strategy_path(data_dir)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if r.get("strategy_id") == strategy_id:
            out.append(r)
    return out


# 年化 (annual_return) 至少需要 30 天窗口才不至于数字荒谬
# (单条记录 days=1 时 (1+0.037)^365 ≈ 66M% — 毫无意义).
# 不足 30 天 → annual_return = None, 前端以 "—" 显示.
_ANNUAL_MIN_DAYS = 30


def compute_strategy_summary(data_dir: Path, strategy_id: str) -> dict | None:
    """从 pnl_strategy_snapshots.jsonl 派生 7 字段 summary. records 空 → None.

    annual_return: 当跨度 < 30 天时为 None (前端显示 —); 避免 (1+r)^(365/days)-1 爆炸.
    """
    records = _read_strategy_pnl_records(data_dir, strategy_id)
    if not records:
        return None
    records.sort(key=lambda r: r.get("ts", ""))
    initial_cash = _initial_cash_for_summary(data_dir)
    final_total = float(records[-1].get("total_pnl", 0.0))
    total_pnl_pct = (final_total / initial_cash) if initial_cash else 0.0
    first_ts = records[0].get("ts", "")
    last_ts = records[-1].get("ts", "")
    days = 1
    if first_ts and last_ts:
        try:
            from datetime import datetime
            a = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            b = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            diff = (b - a).days
            days = max(diff, 1)
        except Exception:
            pass

    if days >= _ANNUAL_MIN_DAYS and total_pnl_pct > -1:
        annual_return = (1 + total_pnl_pct) ** (365.0 / days) - 1
        annual_return_pct = round(annual_return * 100, 4)
    else:
        # 数据不足 / 全亏光: 不年化, 前端以 — 显示
        annual_return_pct = None
    today_pnl = float(records[-1].get("cash_delta", 0.0))
    today_pnl_pct = (today_pnl / initial_cash) if initial_cash else 0.0
    mdd = 0.0
    mdd_date = last_ts[:10] if last_ts else ""
    peak = 0.0
    cum = 0.0
    for r in records:
        cum += float(r.get("cash_delta", 0.0))
        if cum > peak:
            peak = cum
        dd = (peak - cum) / initial_cash if initial_cash else 0.0
        if dd > mdd:
            mdd = dd
            mdd_date = r.get("ts", "")[:10]
    return {
        "total_pnl": round(final_total, 2),
        "total_pnl_pct": round(total_pnl_pct * 100, 4),
        "annual_return": annual_return_pct,  # None if days < 30
        "today_pnl": round(today_pnl, 2),
        "today_pnl_pct": round(today_pnl_pct * 100, 4),
        "max_drawdown": round(mdd * 100, 4),
        "max_drawdown_date": mdd_date,
    }
