"""实盘数据存储 — JSONL 追加 + 内存索引。

目录结构 (data_dir 之下, 全部归 user_data):

  user_data/trading/
    orders.jsonl         每行 = Order.model_dump_json()
    fills.jsonl          每行 = Fill.model_dump_json()
    logs.jsonl           每行 = TradeLogEntry.model_dump_json()
    account.json         Account 单值快照 (原子替换)

设计:
  - 写 = append-only JSONL (原子追加: open('a') + 一行 JSON); 不重写历史。
  - 读 = 启动时全量加载, 后续只在写时增量更新内存索引。
  - 锁 = 模块级 `Lock`, 把"读 + 追加 + 索引更新"作为一个临界区;
    不在持锁时做撮合计算, 撮合结果是已计算好的数据进来再持锁落库。
  - 清理: append N 次后 prune (按 mtime + 文件行数双重防爆)。

不依赖外部 parquet/duckdb: 当前数据集预计千行量级 (orders/fills/logs),
JSONL 足够, 后续若量大可平迁到 parquet 但本期不做。
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.fs_utils import atomic_write_text
from app.services.trading.models import (
    Account,
    Fill,
    Order,
    OrderStatus,
    TradeLogEntry,
)

logger = logging.getLogger(__name__)

# 写盘节流: 每 N 行 logs 触发一次 prune; orders/fills 极少变, 不主动 prune。
LOG_PRUNE_EVERY = 200
LOG_MAX_RECORDS = 5000

_lock = threading.Lock()
_write_counter = 0

_orders: dict[str, Order] = {}
_fills: list[Fill] = []                # 时间倒序时反序遍历
_logs: list[TradeLogEntry] = []
_account: Account | None = None
_loaded_dir: str | None = None


# ── 路径 ──────────────────────────────────────────────
def _root(data_dir: Path) -> Path:
    p = data_dir / "user_data" / "trading"
    p.mkdir(parents=True, exist_ok=True)
    return p



# ── ID 生成 (供 clearance / retry / engine 复用) ────────────────────
def _new_id(prefix: str) -> str:
    """生成全局唯一 id (同一前缀也确保唯一).

    secrets.token_hex(6) = 12 hex 字符 (48 bits) 近似 281 万亿分之一的碰撞概率 —
    进程内反复调用同一前缀也基本不可能重复 (vs. datetime-based 微秒精度
    在 17:00 清算同微秒撤 30 单会全部 id 冲突).

    放在 store.py 是因为这是数据层 utility: clearance / retry / engine 都生成
    Order/Fill/LogEntry 三类的 id, 共享一个函数避免各自实现散落造成的精度问题.
    """
    return f"{prefix}_{__import__('secrets').token_hex(6)}"


def _orders_path(data_dir: Path) -> Path:
    return _root(data_dir) / "orders.jsonl"


def _fills_path(data_dir: Path) -> Path:
    return _root(data_dir) / "fills.jsonl"


def _logs_path(data_dir: Path) -> Path:
    return _root(data_dir) / "logs.jsonl"


def _account_path(data_dir: Path) -> Path:
    return _root(data_dir) / "account.json"


# ── 加载 ──────────────────────────────────────────────
def _load_locked(data_dir: Path) -> None:
    """加载全量数据到内存索引 (持锁调用)."""
    global _orders, _fills, _logs, _account, _loaded_dir, _write_counter

    _orders = {}
    _fills = []
    _logs = []
    _account = None

    op = _orders_path(data_dir)
    if op.exists():
        for line in op.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = Order.model_validate_json(line)
                _orders[o.id] = o
            except Exception as e:
                logger.warning("orders.jsonl 损坏行已跳过: %s", e)

    fp = _fills_path(data_dir)
    if fp.exists():
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                _fills.append(Fill.model_validate_json(line))
            except Exception as e:
                logger.warning("fills.jsonl 损坏行已跳过: %s", e)

    lp = _logs_path(data_dir)
    if lp.exists():
        for line in lp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                _logs.append(TradeLogEntry.model_validate_json(line))
            except Exception as e:
                logger.warning("logs.jsonl 损坏行已跳过: %s", e)

    ap = _account_path(data_dir)
    if ap.exists():
        try:
            _account = Account.model_validate_json(ap.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("account.json 损坏, 已忽略: %s", e)

    _loaded_dir = str(data_dir)
    _write_counter = 0


def _ensure_loaded(data_dir: Path) -> None:
    """首次访问 / data_dir 变化时全量加载。持锁调用。"""
    global _loaded_dir
    if _loaded_dir != str(data_dir):
        _load_locked(data_dir)


# ── 委托 ──────────────────────────────────────────────
def append_order(data_dir: Path, order: Order) -> None:
    """新增一条委托 (append-only)。"""
    with _lock:
        _ensure_loaded(data_dir)
        _orders[order.id] = order
        with _orders_path(data_dir).open("a", encoding="utf-8") as f:
            f.write(order.model_dump_json() + "\n")


def update_order(data_dir: Path, order: Order) -> None:
    """更新委托状态 — 原子覆写整个 orders.jsonl。

    之前用 append, 每笔委托状态机走 NEW → FILLED/CANCELED 至少多 2 行历史态,
    文件无限增长; 加载侧 ``_orders[id] = o`` 只保留最后一行, 历史态实际无用。
    现改为原子覆写: 每个 id 只保留一行最新状态, 文件大小稳定 (千笔 ~ 200KB),
    全量写开销在毫秒级, 与 ``_lock`` 串行化配合下无并发问题。
    """
    with _lock:
        _ensure_loaded(data_dir)
        _orders[order.id] = order
        all_orders = sorted(
            _orders.values(), key=lambda o: o.created_at, reverse=True,
        )
        text = "\n".join(o.model_dump_json() for o in all_orders) + "\n"
        atomic_write_text(_orders_path(data_dir), text)


def get_order(data_dir: Path, order_id: str) -> Order | None:
    with _lock:
        _ensure_loaded(data_dir)
        return _orders.get(order_id)


_STRATEGY_NOTE_RE = re.compile(r"^strategy:([^:]+):(rebalance|pre_market|limit_check|verify)$")


def extract_strategy_id(note: str | None) -> str | None:
    """从 order.note 解析 strategy_id.

    note 格式: "strategy:<sid>:<event>" (rebalance/pre_market/limit_check/verify).
    其他格式 (历史脏数据如 "rebalance_to buy") 或 None → None.
    """
    if not note:
        return None
    m = _STRATEGY_NOTE_RE.match(note)
    return m.group(1) if m else None


def list_orders(
    data_dir: Path,
    *,
    statuses: Iterable[OrderStatus] | None = None,
    symbol: str | None = None,
    strategy_id: str | None = None,
    limit: int | None = None,
) -> list[Order]:
    """委托列表; 默认按 created_at 倒序。strategy_id 过滤走 Order.strategy_id 第一性字段."""
    with _lock:
        _ensure_loaded(data_dir)
        items = list(_orders.values())

    if statuses is not None:
        ss = set(statuses)
        items = [o for o in items if o.status in ss]
    if symbol:
        items = [o for o in items if o.symbol == symbol]
    if strategy_id is not None:
        # 第一性字段: 不再依赖 note 字符串解析; 历史手动单 note 写 "strategy:<sid>:<event>"
        # 也不能再误归类 (该条 strategy_id 字段为 None).
        items = [o for o in items if o.strategy_id == strategy_id]
    items.sort(key=lambda o: o.created_at, reverse=True)
    if limit is not None:
        items = items[:limit]
    return items


def pending_orders(data_dir: Path) -> list[Order]:
    """NEW + PARTIAL = 未完成委托。撮合器只看这一份。"""
    with _lock:
        _ensure_loaded(data_dir)
        return [o for o in _orders.values()
                if o.status in (OrderStatus.NEW, OrderStatus.PARTIAL)]


# ── 成交 ──────────────────────────────────────────────
def append_fill(data_dir: Path, fill: Fill) -> None:
    with _lock:
        _ensure_loaded(data_dir)
        _fills.append(fill)
        with _fills_path(data_dir).open("a", encoding="utf-8") as f:
            f.write(fill.model_dump_json() + "\n")


def list_fills(
    data_dir: Path,
    *,
    symbol: str | None = None,
    strategy_id: str | None = None,
    limit: int | None = None,
) -> list[Fill]:
    """成交列表 (按 filled_at 倒序); strategy_id 过滤走 Fill.strategy_id 第一性字段."""
    with _lock:
        _ensure_loaded(data_dir)
        items = list(reversed(_fills))   # 最新优先
    if symbol:
        items = [f for f in items if f.symbol == symbol]
    if strategy_id is not None:
        # 第一性字段: fill 撮合时已从关联 order 继承 strategy_id (engine._settle_fill).
        # 历史 fills (无 strategy_id) 留 None — migrate 脚本会补, 见 §6.
        items = [f for f in items if f.strategy_id == strategy_id]
    if limit is not None:
        items = items[:limit]
    return items


# ── 交易日志 ─────────────────────────────────────────
def append_log(data_dir: Path, entry: TradeLogEntry) -> None:
    with _lock:
        _ensure_loaded(data_dir)
        global _write_counter
        _logs.append(entry)
        _write_counter += 1
        with _logs_path(data_dir).open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
        if _write_counter >= LOG_PRUNE_EVERY:
            _write_counter = 0
            _prune_logs_locked(data_dir)


def _prune_logs_locked(data_dir: Path) -> None:
    """按保留上限裁剪 logs (按 ts 倒序保留尾部 N 条)。"""
    global _logs
    if len(_logs) <= LOG_MAX_RECORDS:
        return
    keep = sorted(_logs, key=lambda e: e.ts, reverse=True)[:LOG_MAX_RECORDS]
    keep.sort(key=lambda e: e.ts)
    _logs = keep
    text = "\n".join(e.model_dump_json() for e in _logs) + "\n"
    atomic_write_text(_logs_path(data_dir), text)


def list_logs(
    data_dir: Path,
    *,
    limit: int | None = None,
    kind: str | None = None,
    strategy_id: str | None = None,
) -> list[TradeLogEntry]:
    """日志列表; strategy_id 过滤走 TradeLogEntry.strategy_id 第一性字段."""
    with _lock:
        _ensure_loaded(data_dir)
        items = list(reversed(_logs))
    if kind:
        items = [e for e in items if e.kind.value == kind]
    if strategy_id is not None:
        # 第一性字段: 写日志时已从关联 order 继承 strategy_id (engine._log_*).
        # 历史日志 (无 strategy_id) 留 None — migrate 脚本会补.
        items = [e for e in items if e.strategy_id == strategy_id]
    if limit is not None:
        items = items[:limit]
    return items


# ── 账户 ──────────────────────────────────────────────
def load_account(data_dir: Path, *, initial_cash: float) -> Account:
    """读取账户; 无快照则用 initial_cash 初始化 (返回并落盘)。"""
    global _account
    with _lock:
        _ensure_loaded(data_dir)
        if _account is None:
            _account = Account(
                initial_cash=initial_cash,
                cash=initial_cash,
                market_value=0.0,
                total_equity=initial_cash,
            )
            _save_account_locked(data_dir)
        return _account.model_copy()


def save_account(data_dir: Path, account: Account) -> None:
    with _lock:
        _ensure_loaded(data_dir)
        global _account
        _account = account.model_copy()
        _save_account_locked(data_dir)


def _save_account_locked(data_dir: Path) -> None:
    if _account is None:
        return
    atomic_write_text(_account_path(data_dir), _account.model_dump_json(indent=2))


# ── 工具 ──────────────────────────────────────────────
def reset_for_tests() -> None:
    """测试用: 清空内存索引。"""
    global _orders, _fills, _logs, _account, _loaded_dir, _write_counter
    with _lock:
        _orders.clear()
        _fills.clear()
        _logs.clear()
        _account = None
        _loaded_dir = None
        _write_counter = 0


def now_iso() -> str:
    """统一使用带时区的 ISO8601 (北京时间, 仓库惯例)."""
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


