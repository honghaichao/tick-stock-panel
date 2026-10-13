"""实盘偏好设置 — preferences.json 上的少量字段。

字段 (全部可缺省, 缺省即取保守默认):

  trading_enabled                bool   默认 False — 显式开启才会启动撮合
  trading_initial_cash           float  默认 1_000_000 (100 万模拟资金)
  trading_max_order_amount       float  默认 50_000 — 单笔委托金额上限 (元)
  trading_max_position_count     int    默认 20 — 同时持仓标的数上限
  trading_max_position_pct       float  默认 0.20 — 单标的市值占账户比上限 (0.20 = 20%)
  trading_match_interval_seconds int    默认 5 — 撮合扫描间隔 (3~60)
  trading_fee_rate               float  默认 0.0003 — 佣金率 (万 3 单边)
  trading_fee_min                float  默认 5.0 — 最低佣金 (元)
  trading_stamp_tax_rate         float  默认 0.001 — 印花税率 (千 1, 仅卖出)

说明:
  - 这些是"风控 + 模拟账户参数", 不是真实券商的强制约束; 用户可按需调整。
  - 不持久化敏感信息 (真实券商的 token / 账号) — 见 docs/secondary-development.md
    BrokerAdapter 的二开接入要求。
"""
from __future__ import annotations

from app.services import preferences as base

# 字段名集中导出, 避免拼写漂移
KEY_ENABLED = "trading_enabled"
KEY_INITIAL_CASH = "trading_initial_cash"
KEY_MAX_ORDER_AMOUNT = "trading_max_order_amount"
KEY_MAX_ORDER_AMOUNT_FOR_STRATEGY = "trading_max_order_amount_for_strategy"
KEY_MAX_POSITION_COUNT = "trading_max_position_count"
KEY_MAX_POSITION_PCT = "trading_max_position_pct"
KEY_MATCH_INTERVAL = "trading_match_interval_seconds"
KEY_FEE_RATE = "trading_fee_rate"
KEY_FEE_MIN = "trading_fee_min"
KEY_STAMP_TAX_RATE = "trading_stamp_tax_rate"
KEY_SLIPPAGE_KIND = "trading_slippage_kind"
KEY_SLIPPAGE_VALUE = "trading_slippage_value"
KEY_ORDER_VOLUME_RATIO = "trading_order_volume_ratio"

DEFAULTS = {
    KEY_ENABLED: False,
    KEY_INITIAL_CASH: 1_000_000.0,
    KEY_MAX_ORDER_AMOUNT: 50_000.0,
    KEY_MAX_ORDER_AMOUNT_FOR_STRATEGY: 10_000_000.0,
    KEY_MAX_POSITION_COUNT: 20,
    KEY_MAX_POSITION_PCT: 0.20,
    KEY_MATCH_INTERVAL: 5,
    KEY_FEE_RATE: 0.0003,
    KEY_FEE_MIN: 5.0,
    KEY_STAMP_TAX_RATE: 0.001,
    KEY_SLIPPAGE_KIND: "none",
    KEY_SLIPPAGE_VALUE: 0.0,
    KEY_ORDER_VOLUME_RATIO: 1.0,
}


# ── 基础偏好 getters (clamp + 类型校验) ────────────
def _clamp_int(key: str, lo: int, hi: int) -> int:
    raw = base.load().get(key, DEFAULTS[key])
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = int(DEFAULTS[key])
    return max(lo, min(hi, v))


def _clamp_float(key: str, lo: float, hi: float) -> float:
    raw = base.load().get(key, DEFAULTS[key])
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = float(DEFAULTS[key])
    return max(lo, min(hi, v))


def is_enabled() -> bool:
    return bool(base.load().get(KEY_ENABLED, DEFAULTS[KEY_ENABLED]))


def initial_cash() -> float:
    return _clamp_float(KEY_INITIAL_CASH, 0.0, 1e12)


def max_order_amount() -> float:
    return _clamp_float(KEY_MAX_ORDER_AMOUNT, 0.0, 1e9)


def max_order_amount_for_strategy() -> float:
    """策略调用路径 (rebalance_to) 的单笔委托金额上限。

    菜场大妈类 JoinQuant 策略原版设计 = 按账户余额等权分仓 (5 票 × 200k 等),
    不带单笔金额概念。TSP fork 加的 trading_max_order_amount 主要是给手动下单护栏;
    强制套到策略上等于废了原版设计, 留单独额度让策略按原意跑。

    默认 1000 万 (覆盖菜场大妈在 1M-10M 账户都能等权), 0 = 完全不限。
    """
    return _clamp_float(KEY_MAX_ORDER_AMOUNT_FOR_STRATEGY, 0.0, 1e9)


def max_position_count() -> int:
    return _clamp_int(KEY_MAX_POSITION_COUNT, 1, 1000)


def max_position_pct() -> float:
    return _clamp_float(KEY_MAX_POSITION_PCT, 0.0, 1.0)


def match_interval_seconds() -> int:
    return _clamp_int(KEY_MATCH_INTERVAL, 3, 60)


def fee_rate() -> float:
    return _clamp_float(KEY_FEE_RATE, 0.0, 0.01)


def fee_min() -> float:
    return _clamp_float(KEY_FEE_MIN, 0.0, 1000.0)


def stamp_tax_rate() -> float:
    return _clamp_float(KEY_STAMP_TAX_RATE, 0.0, 0.1)


def slippage_kind() -> str:
    """撮合滑点类型. 默认 'none' (向后兼容)."""
    return str(base.load().get(KEY_SLIPPAGE_KIND, DEFAULTS[KEY_SLIPPAGE_KIND]))


def slippage_value() -> float:
    """撮合滑点值."""
    return _clamp_float(KEY_SLIPPAGE_VALUE, 0.0, 1.0)


def order_volume_ratio() -> float:
    """单笔最大成交量比例 (1.0=不限制)."""
    return _clamp_float(KEY_ORDER_VOLUME_RATIO, 0.0, 1.0)


def save(updates: dict) -> dict:
    """合并写入 — 调用方负责白名单字段, 这里不做 schema 校验."""
    return base.save(updates)


# ── 实盘策略调度 (按策略 ID 分键存储) ──────────────
# 偏好中以 `trading_strategies.{strategy_id}.{field}` 三层命名空间存储;
# ── 实盘策略调度 (per-strategy schedules 列表) ─────────
# 偏好 schema:
#   trading_strategies.{strategy_id}.schedules = [
#       {name: "pre_market", hour: 9, minute: 5, enabled: true},
#       {name: "rebalance",  hour: 9, minute: 26, enabled: true},
#       ...
#   ]
# 多 cron 时点 → 同一 strategy 不同 event 各挂一个 job.


def strategy_schedules_key(strategy_id: str) -> str:
    """生成 preferences.json 中 schedules 字段的 key."""
    return f"trading_strategies.{strategy_id}.schedules"


def get_strategy_schedules(strategy_id: str) -> list[dict]:
    """读取指定策略的所有 schedule; 没有则返回空列表."""
    raw = base.load()
    schedules = raw.get(strategy_schedules_key(strategy_id), [])
    if not isinstance(schedules, list):
        return []
    return [s for s in schedules if isinstance(s, dict)]


def list_all_strategy_schedules() -> dict[str, list[dict]]:
    """列出所有已配置的策略调度."""
    raw = base.load()
    out: dict[str, list[dict]] = {}
    for k, v in raw.items():
        if not k.startswith("trading_strategies.") or not k.endswith(".schedules"):
            continue
        sid = k[len("trading_strategies."): -len(".schedules")]
        if isinstance(v, list):
            out[sid] = [s for s in v if isinstance(s, dict)]
    return out


def is_strategy_schedule_enabled(strategy_id: str, event: str) -> bool:
    """查询指定策略的指定 event 是否启用 (没配 → False)."""
    for s in get_strategy_schedules(strategy_id):
        if s.get("name") == event:
            return bool(s.get("enabled", True))
    return False


def set_strategy_schedules(strategy_id: str, schedules: list[dict]) -> list[dict]:
    """整体覆写指定策略的 schedules.

    校验: hour 0..23, minute 0..59, name 非空, 同 strategy 内 name 唯一.
    """
    seen: set[str] = set()
    cleaned: list[dict] = []
    for s in schedules:
        if not isinstance(s, dict):
            raise ValueError("schedule 必须是 dict")
        name = str(s.get("name", "")).strip()
        if not name:
            raise ValueError("schedule.name 不能为空")
        if name in seen:
            raise ValueError(f"strategy {strategy_id} 内 schedule.name 重复: {name}")
        seen.add(name)
        try:
            hour = int(s.get("hour", 9))
            minute = int(s.get("minute", 30))
        except (TypeError, ValueError) as e:
            raise ValueError(f"hour/minute 必须是整数: {e}") from e
        if not (0 <= hour <= 23):
            raise ValueError(f"hour 必须在 0..23; 收到 {hour}")
        if not (0 <= minute <= 59):
            raise ValueError(f"minute 必须在 0..59; 收到 {minute}")
        cleaned.append({
            "name": name,
            "hour": hour,
            "minute": minute,
            "enabled": bool(s.get("enabled", True)),
            "day_of_week": s.get("day_of_week", "mon-fri"),
        })
    base.save({strategy_schedules_key(strategy_id): cleaned})
    return cleaned


def update_strategy_schedule(
    strategy_id: str,
    event: str,
    *,
    enabled: bool | None = None,
    hour: int | None = None,
    minute: int | None = None,
    day_of_week: str | None = None,
) -> list[dict]:
    """更新单个 schedule (按 event 名定位); 不存在则 append."""
    cur = list(get_strategy_schedules(strategy_id))
    target = next((s for s in cur if s.get("name") == event), None)
    if target is None:
        target = {"name": event, "hour": hour or 9, "minute": minute or 30,
                  "enabled": enabled if enabled is not None else True,
                  "day_of_week": day_of_week or "mon-fri"}
        cur.append(target)
    if enabled is not None:
        target["enabled"] = enabled
    if hour is not None:
        target["hour"] = hour
    if minute is not None:
        target["minute"] = minute
    if day_of_week is not None:
        target["day_of_week"] = day_of_week
    return set_strategy_schedules(strategy_id, cur)



# ── 既有 endpoint stub (router.py 调用, 跟 settings/preferences/trading 兼容) ─
def get_all_preferences() -> dict:
    """返所有 trading preferences (按 settings/preferences/trading 公开 schema)."""
    import app.services.preferences as base
    cfg = base.load()
    return {
        "trading_enabled": bool(cfg.get(KEY_ENABLED, DEFAULTS[KEY_ENABLED])),
        "trading_initial_cash": float(cfg.get(KEY_INITIAL_CASH, DEFAULTS[KEY_INITIAL_CASH])),
        "trading_max_order_amount": float(cfg.get(KEY_MAX_ORDER_AMOUNT, DEFAULTS[KEY_MAX_ORDER_AMOUNT])),
        "trading_max_order_amount_for_strategy": float(cfg.get(KEY_MAX_ORDER_AMOUNT_FOR_STRATEGY, DEFAULTS[KEY_MAX_ORDER_AMOUNT_FOR_STRATEGY])),
        "trading_max_position_count": int(cfg.get(KEY_MAX_POSITION_COUNT, DEFAULTS[KEY_MAX_POSITION_COUNT])),
        "trading_max_position_pct": float(cfg.get(KEY_MAX_POSITION_PCT, DEFAULTS[KEY_MAX_POSITION_PCT])),
        "trading_match_interval": int(cfg.get(KEY_MATCH_INTERVAL, DEFAULTS[KEY_MATCH_INTERVAL])),
        "trading_fee_rate": float(cfg.get(KEY_FEE_RATE, DEFAULTS[KEY_FEE_RATE])),
        "trading_fee_min": float(cfg.get(KEY_FEE_MIN, DEFAULTS[KEY_FEE_MIN])),
        "trading_stamp_tax_rate": float(cfg.get(KEY_STAMP_TAX_RATE, DEFAULTS[KEY_STAMP_TAX_RATE])),
        "trading_slippage_kind": str(cfg.get(KEY_SLIPPAGE_KIND, DEFAULTS[KEY_SLIPPAGE_KIND])),
        "trading_slippage_value": float(cfg.get(KEY_SLIPPAGE_VALUE, DEFAULTS[KEY_SLIPPAGE_VALUE])),
        "trading_order_volume_ratio": float(cfg.get(KEY_ORDER_VOLUME_RATIO, DEFAULTS[KEY_ORDER_VOLUME_RATIO])),
    }


def update_preferences(updates: dict) -> dict:
    """更新 trading preferences. 走 save() 落盘."""
    return save(updates)


# ── F0-2 (2026-10-13): push 通道 keys ─
KEY_TRADING_NOTIFY_WECOM_WEBHOOK = "trading_notify_wecom_webhook"
KEY_TRADING_NOTIFY_FEISHU_WEBHOOK = "trading_notify_feishu_webhook"
KEY_TRADING_NOTIFY_PUSH_ENABLED = "trading_notify_push_enabled"

__all__ = [
    "KEY_ENABLED",
    "KEY_FEE_MIN",
    "KEY_FEE_RATE",
    "KEY_INITIAL_CASH",
    "KEY_MATCH_INTERVAL",
    "KEY_MAX_ORDER_AMOUNT",
    "KEY_MAX_ORDER_AMOUNT_FOR_STRATEGY",
    "KEY_MAX_POSITION_COUNT",
    "KEY_MAX_POSITION_PCT",
    "KEY_STAMP_TAX_RATE",
    "KEY_TRADING_NOTIFY_FEISHU_WEBHOOK",
    "KEY_TRADING_NOTIFY_PUSH_ENABLED",
    "KEY_TRADING_NOTIFY_WECOM_WEBHOOK",
    "fee_min",
    "fee_rate",
    "get_all_preferences",
    "get_strategy_schedules",
    "initial_cash",
    "is_enabled",
    "is_strategy_schedule_enabled",
    "list_all_strategy_schedules",
    "match_interval_seconds",
    "max_order_amount",
    "max_position_count",
    "max_position_pct",
    "save",
    "set_strategy_schedules",
    "stamp_tax_rate",
    "strategy_schedules_key",
    "update_preferences",
    "update_strategy_schedule",
]


# ── per-strategy notify_wechat (PR-1 子任务 §5.3) ────────────────────────────
def notify_wechat_key(strategy_id: str) -> str:
    """生成 preferences.json 中 notify_wechat 字段的 key."""
    return f"trading_strategies.{strategy_id}.notify_wechat"


def get_notify_wechat(strategy_id: str) -> bool:
    """读取指定策略 notify_wechat; 缺省 False (不打扰)."""
    raw = base.load()
    val = raw.get(notify_wechat_key(strategy_id))
    return bool(val) if val is not None else False


def set_notify_wechat(strategy_id: str, enabled: bool) -> bool:
    """写入指定策略 notify_wechat. 返回写入后的新值."""
    base.save({notify_wechat_key(strategy_id): bool(enabled)})
    return bool(enabled)


def delete_strategy_keys(strategy_id: str) -> list[str]:
    """删除 prefs.json 中所有 trading_strategies.<strategy_id>.* 键 (schedules, notify_wechat, ...).

    返回删除掉的 key 列表, 便于审计 / 测试.
    注: 写新文件而非 del, 保持与 base.save 同样的 atomic_write_text 风格.
    """
    import json as _json

    from app.services.fs_utils import atomic_write_text as _atomic
    p = base._path()
    try:
        raw = _json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, Exception):
        return []
    prefix = f"trading_strategies.{strategy_id}."
    deleted: list[str] = []
    for k in list(raw.keys()):
        if k.startswith(prefix):
            raw.pop(k, None)
            deleted.append(k)
    if deleted:
        _atomic(p, _json.dumps(raw, indent=2, ensure_ascii=False))
        base._invalidate_cache()
    return deleted
