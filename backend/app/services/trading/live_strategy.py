"""LiveStrategy 协议 + 自动加载器 — 用户面向的策略扩展点 (多事件 + 持久化状态).

设计要点:
  - 策略文件放在 `data/strategies/live/<name>.py`, 模块级 `STRATEGY` 实例被自动发现。
  - 策略可以声明多个 `schedules` (每个 = cron 时点 + 事件名),
    调度器按各自时点触发, 把 event 名传给 `on_event(event, ctx)`.
  - `LiveStrategyContext` 含 `state: dict[str, Any]` —— 跨事件共享的策略私有状态;
    executor 自动持久化到 `data/user_data/trading/live_strategy_state/<id>.json`,
    跨重启 + 跨日 (9:05 准备 → 13:30 检查 的状态链路) 不丢失.
  - 实现 `on_event(event, ctx) -> dict[str, int]`, 返回目标持仓 {symbol: qty}.
    qty=0 表示清仓; 返回 `{}` 表示不调仓 (用于"准备" / "检查" 等非交易事件).
    StrategyExecutor 调 `TradingRunner.rebalance_to(target)`, 走完整风控 + 撮合.

不是做什么:
  - 不是事件驱动的实时报价监听系统 (撮合按分钟 K, 不是逐笔).
  - 不是回测引擎 (回测走 strategy.lots.META + backtest.worker).
  - 不是 broker 适配器 (BrokerAdapter 在 broker.py 里, 不在策略层).
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ── 调度配置 ───────────────────────────────────────
# 聚宽式 priority: monthly (0) < weekly (1) < daily (2) < manual (3).
# 同 (hour, minute) 多 schedule: priority 数字越小越先触发.
KIND_PRIORITY = {"monthly": 0, "weekly": 1, "daily": 2, "manual": 3}


@dataclass(frozen=True)
class StrategySchedule:
    """一个 cron 时点配置.

    Attributes:
        name: 事件名 (传给 strategy.on_event); 策略按 name 分发.
        hour/minute: 工作日触发时间 (Asia/Shanghai).
        enabled: 是否启用; False 时 scheduler 不挂 job.
"""
    name: str
    hour: int
    minute: int
    enabled: bool = True
    day_of_week: str = "mon-fri"
    kind: str = "daily"  # 调度类型; 决定同 (hour, minute) 多 schedule 的优先级

    def __post_init__(self) -> None:
        # 未知 kind 强制降级为 'daily' (防御性, 不抛错)
        if self.kind not in KIND_PRIORITY:
            object.__setattr__(self, "kind", "daily")

    @property
    def priority(self) -> int:
        return KIND_PRIORITY.get(self.kind, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "hour": self.hour,
            "minute": self.minute,
            "enabled": self.enabled,
            "day_of_week": self.day_of_week,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategySchedule:
        # day_of_week 缺省走 "mon-fri" (与 dataclass 字段默认一致); 调用方传
        # 啥就接受啥 (preferences 已做过 0..23 / 0..59 的字段校验, day_of_week 是
        # APScheduler CronTrigger 接受的字符串, 不在白名单里就不传 — 见 preferences)。
        return cls(
            name=str(d["name"]),
            hour=int(d["hour"]),
            minute=int(d["minute"]),
            enabled=bool(d.get("enabled", True)),
            day_of_week=str(d.get("day_of_week", "mon-fri")),
            kind=str(d.get("kind", "daily")),
        )


# ── 上下文 ──────────────────────────────────────────
@dataclass(frozen=True)
class LiveStrategyContext:
    """每次策略执行的快照上下文 (只读 + 写 state).

    fields:
        positions: 当前实盘持仓聚合视图 (symbol → qty).
        account: 账户快照 (cash, market_value, total_equity 等).
        fills_recent: 最近 N 笔 fill (倒序), 默认 N=50.
        market: 全市场行情快照 (symbol → last_price).
        trade_date: 交易日 (北京时间).
        as_of: 触发时间 (ISO8601, 北京时区).
        event: 当前触发的事件名 (对应 schedules[].name).
        state: 策略私有持久化状态 (跨事件 + 跨重启). 策略可读可写.
        data_provider: StrategyDataProvider (含 instruments / minute_close / dividends 等).
        send_message: 策略通知接口 (聚宽 send_message 1:1). 调用即落 notify.jsonl + SSE 推送.
                     签名: send_message(text: str, kind: str = "user_message", extra: dict = None).
                     kind 选 user_message / info / warn / error.
    """

    positions: dict[str, int] = field(default_factory=dict)
    account: dict[str, float] = field(default_factory=dict)
    fills_recent: list[dict] = field(default_factory=list)
    market: dict[str, float] = field(default_factory=dict)
    trade_date: Any = None     # datetime.date
    as_of: str = ""
    event: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    data_provider: Any = None  # StrategyDataProvider
    send_message: Any = None   # Callable (text, kind, extra) -> None; executor 在 run_now 注入


# ── 协议 ──────────────────────────────────────────────
class LiveStrategy(Protocol):
    """用户实现的策略接口.

    必填属性:
        id: 唯一 ID.
        name: 中文显示名.
        enabled_default: 默认是否启用.
        schedules: 该策略的 cron 时点列表. 至少 1 个; 可以 0 个 (只手动触发).

    必填方法:
        on_event(event, context) -> dict[str, int]
            event: schedules 里某项的 name 字段 (如 'rebalance', 'mid_day_check').
            context: 含 state; 策略可读写 (策略结束后 executor 自动落盘).
            返回: 目标持仓 {symbol: qty}; 空字典 = 本次不调仓.
            抛异常 → executor 记 ERROR, run 失败, 不污染 fill / 不影响其他策略.

    可选方法 (回测用, 聚宽式统一 runtime):
        当 strategy 同时实现 on_event (实盘) + 下面 3 个方法 (回测), 同一份代码
        既能跑回测也能上实盘. 详见 docs/strategy-unified-runtime.md.

        META: dict — builtin 风格 META (params / scoring / order_by / limit). 不实现 → 回测走 builtin.
        EXECUTION_BACKEND: str — "matrix_native" 才算回测版. 默认 "" 表示无回测.
        required_fields() -> frozenset[str]: 回测需要的市场字段 (close / volume / etc).
        required_warmup_bars(params) -> int: 回测预热 bar 数 (如 365 天分红窗口).
        compute_signals(market, params) -> SignalMatrix: 日 K 全市场扫描, 返回 entry/exit 信号矩阵.
    """
    id: str
    name: str
    enabled_default: bool
    schedules: list[StrategySchedule] | list[dict[str, Any]]

    def on_event(self, event: str, context: LiveStrategyContext) -> dict[str, int]: ...

    # ── 可选: 回测 matrix 算子 (默认无, 即 "实盘-only" 策略) ──
    META: dict | None  # type: ignore[assignment]
    EXECUTION_BACKEND: str  # type: ignore[assignment]

    def required_fields(self) -> frozenset[str] | None: ...
    def required_warmup_bars(self, params: dict) -> int | None: ...
    def compute_signals(self, market: Any, params: dict) -> Any: ...  # SignalMatrix 避免循环 import


# ── 注册表 ──────────────────────────────────────────
class LiveStrategyRegistry:
    """内存中的策略注册中心."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._strategies: dict[str, LiveStrategy] = {}
        # PR-1 (B1=B): 首次注册时记录 ISO ts, 给 GET /strategies[].started_at 用.
        self._registered_at: dict[str, str] = {}

    def register(self, strategy, *, overwrite: bool = True) -> None:
        with self._lock:
            required = ("id", "name", "enabled_default", "schedules", "on_event")
            # matrix 算子 3 个方法全可选, 不强制要求
            missing = [a for a in required if not hasattr(strategy, a)]
            if missing:
                raise TypeError(
                    f"strategy 缺少必要属性 {missing}; 必须实现 LiveStrategy 接口."
                )
            # schedules 元素归一化: dict → StrategySchedule
            raw_schedules = strategy.schedules or []
            normalized: list[StrategySchedule] = []
            for s in raw_schedules:
                if isinstance(s, StrategySchedule):
                    normalized.append(s)
                elif isinstance(s, dict):
                    normalized.append(StrategySchedule.from_dict(s))
                else:
                    raise TypeError(
                        f"schedule 必须是 StrategySchedule 或 dict; 实际 {type(s).__name__}"
                    )
            # 替换到实例上 (让后续访问拿到 normalized 类型)
            with contextlib.suppress(Exception):
                # 协议 duck-typed, 部分实例可能不允许赋值; 跳过
                strategy.schedules = normalized

            if not overwrite and strategy.id in self._strategies:
                logger.info("strategy %s 已注册, 跳过 (overwrite=False)", strategy.id)
                return
            self._strategies[strategy.id] = strategy
            # 首次注册时记录 started_at; 重复 register (overwrite=True, e.g. 代码替换后重启)
            # 保留旧 ts, 不更新.
            if strategy.id not in self._registered_at:
                from datetime import datetime, timedelta, timezone
                _CN = timezone(timedelta(hours=8))
                self._registered_at[strategy.id] = datetime.now(_CN).isoformat(timespec="seconds")
            logger.info(
                "注册策略: %s (%s) — %d 个 schedule (started_at=%s)",
                strategy.id, getattr(strategy, "name", "?"), len(normalized),
                self._registered_at[strategy.id],
            )

    def started_at_of(self, strategy_id: str) -> str | None:
        """返回注册时间 ISO; 未注册返回 None. PR-1 B1=B 用."""
        with self._lock:
            return self._registered_at.get(strategy_id)

    def unregister(self, strategy_id: str) -> None:
        with self._lock:
            self._strategies.pop(strategy_id, None)
            self._registered_at.pop(strategy_id, None)  # 注销后清注册时间 (避免残留)
        logger.info("注销策略: %s", strategy_id)

    def get(self, strategy_id: str) -> LiveStrategy | None:
        return self._strategies.get(strategy_id)

    def list_all(self) -> list[LiveStrategy]:
        with self._lock:
            return list(self._strategies.values())

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._strategies.keys())

    def discover_from(self, live_dir: Path) -> int:
        """扫描目录下的 .py 文件, import 模块并拿 STRATEGY 实例注册."""
        if not live_dir.exists():
            live_dir.mkdir(parents=True, exist_ok=True)
        # 让策略可以用绝对包名 (data.user_data.strategies.live.xxx) 引用共享基类.
        # sys.path 需包含:
        #   1. live_dir 本身 (spec_from_file_location 用绝对路径加载时查找同级模块)
        #   2. data 包的祖先目录 (让 "import data" 能解析为顶层包)
        live_dir_resolved = live_dir.resolve()
        candidates = [live_dir_resolved, live_dir_resolved.parent.parent.parent.parent]
        for p in candidates:
            ps = str(p)
            if ps not in sys.path:
                sys.path.insert(0, ps)
        count = 0
        for py_file in sorted(live_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            mod_name = f"live_strategy_{py_file.stem}_{id(py_file)}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, py_file)
                if spec is None or spec.loader is None:
                    logger.warning("跳过策略文件 (无可加载 spec): %s", py_file)
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as e:
                logger.warning("策略文件加载失败 %s: %s", py_file, e)
                continue

            strategy = getattr(module, "STRATEGY", None)
            if strategy is None:
                logger.warning("策略文件未定义 STRATEGY 实例: %s", py_file)
                continue
            try:
                self.register(strategy, overwrite=True)
                count += 1
            except Exception as e:
                logger.warning("策略注册失败 %s: %s", py_file, e)
        return count


__all__ = [
    "KIND_PRIORITY",
    "LiveStrategy",
    "LiveStrategyContext",
    "LiveStrategyRegistry",
    "StrategySchedule",
]
