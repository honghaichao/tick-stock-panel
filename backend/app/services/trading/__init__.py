"""模拟实盘交易模块。

按 L3 (新增核心模块) 接入:

  - 行情 (K线/快照) 走仓库与 quote_service 的 enriched 缓存, 不再开数据通道。
  - 撮合走后台线程; 委托/成交/持仓/日志 全部 append-only JSONL 落盘,
    内存缓存 + 锁, 满足 CONTRIBUTING §6 并发安全要求。
  - 当前默认实现 = PaperBroker (按分钟撮合), 真实券商适配走
    `BrokerAdapter` Protocol 二开, 不在主线默认启用。
  - 订单仅来自手动 (用户在前端表单提交); 策略信号自动下单不在本期范围。

公共导出见下: `engine`, `router`, `preferences` 子模块按需 import。
"""
from __future__ import annotations

from app.services.trading import (
    broker,
    engine,
    models,
    paper_broker,
    preferences,
    router,
    store,
)

__all__ = [
    "broker",
    "engine",
    "models",
    "paper_broker",
    "preferences",
    "router",
    "store",
]
