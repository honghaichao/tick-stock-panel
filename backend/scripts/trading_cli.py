#!/usr/bin/env python
"""实盘交易 CLI — 不依赖 UI / HTTP 即可下单 / 查持仓 / 撤单。

仅做薄层包装, 落库 / 风控 / 撮合都走 TradingEngine; 与 UI 提交路径完全一致。

用法 (从 backend/ 目录运行):
  # 启用实盘 (默认关闭, 改 preferences.trading_enabled)
  .venv/bin/python -m scripts.trading_cli enable

  # 提交一笔限价买单
  .venv/bin/python -m scripts.trading_cli submit buy 600519.SH 100 --price 1500.5

  # 提交一笔市价卖单
  .venv/bin/python -m scripts.trading_cli submit sell 600519.SH 100 --market

  # 撤单
  .venv/bin/python -m scripts.trading_cli cancel ord_abc123

  # 查询
  .venv/bin/python -m scripts.trading_cli status          # 账户 + 引擎
  .venv/bin/python -m scripts.trading_cli positions       # 当前持仓
  .venv/bin/python -m scripts.trading_cli orders         # 委托列表 (默认 NEW/FILLED)
  .venv/bin/python -m scripts.trading_cli trades         # 历史成交

注意:
  - 默认模拟盘, 不连接真实券商 (按 docs/secondary-development.md §"按需扩展").
  - 启动实盘引擎需要后端进程已经跑起来 (engine.set_app_state 由 main.py 注入).
    本 CLI 不能独立启动后端, 仅作为运行中后端的"外部客户端"。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

logger = logging.getLogger(__name__)


def _resolve_runner():
    """拿到已在运行的后端进程的 engine + data_dir.

    通过全局 app.state 单例; 如果不在主进程上下文, 直接抛错, 引导用户去后端
    内嵌的策略脚本里调用 TradingRunner (而不是从 shell 启本 CLI)。
    """
    from app.services.trading import engine as engine_mod
    from app.services.trading.runner import TradingRunner

    state = engine_mod._APP_STATE  # type: ignore[attr-defined]
    if state is None or not hasattr(state, "repo"):
        raise SystemExit(
            "未找到后端 app.state。本 CLI 必须在后端进程内运行,"
            " 或作为已运行服务的接口脚本使用. "
            "若要做一次性脚本下单, 请用 `from app.services.trading.runner "
            "import TradingRunner` 启动一个独立后端实例."
        )
    eng = getattr(state, "trading_engine", None)
    if eng is None:
        raise SystemExit(
            "后端 app.state.trading_engine 未初始化. 请确认 main.py 启动完成."
        )
    return TradingRunner(eng, state.repo.store.data_dir)


def cmd_enable(args: argparse.Namespace) -> int:
    from app.services.trading import preferences as prefs

    r = _resolve_runner()
    enabled = not prefs.is_enabled()
    if args.disable:
        enabled = False
    prefs.save({prefs.KEY_ENABLED: enabled})
    if enabled:
        r.engine.start()
    else:
        r.engine.stop()
    print(json.dumps({"trading_enabled": prefs.is_enabled()}, ensure_ascii=False, indent=2))
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    r = _resolve_runner()
    note = args.note
    try:
        if args.side == "buy":
            order = r.buy(args.symbol, args.qty,
                           limit_price=args.price, order_type=args.order_type, note=note)
        else:
            order = r.sell(args.symbol, args.qty,
                            limit_price=args.price, order_type=args.order_type, note=note)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "order": order.model_dump(mode="json")},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    from app.services.trading import engine as engine_mod

    r = _resolve_runner()
    try:
        order = r.cancel(args.order_id)
    except engine_mod.TradingRejectError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "order": order.model_dump(mode="json")},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from app.services.trading import preferences as prefs

    r = _resolve_runner()
    s = r.engine.status(r.data_dir)
    a = r.account()
    print(json.dumps({
        "engine": s.model_dump(mode="json"),
        "account": a.model_dump(mode="json"),
        "preferences": {
            "trading_enabled": prefs.is_enabled(),
            "trading_initial_cash": prefs.initial_cash(),
            "trading_max_order_amount": prefs.max_order_amount(),
        },
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    r = _resolve_runner()
    for p in r.positions():
        print(json.dumps(p.model_dump(mode="json"), ensure_ascii=False))
    return 0


def cmd_orders(args: argparse.Namespace) -> int:
    from app.services.trading.models import OrderStatus

    r = _resolve_runner()
    statuses: list[OrderStatus] | None = None
    if args.status:
        try:
            statuses = [OrderStatus(s) for s in args.status.split(",")]
        except ValueError as e:
            raise SystemExit(f"非法 status: {e}") from e
    orders = r.list_orders(statuses=statuses, symbol=args.symbol, limit=args.limit)
    for o in orders:
        print(json.dumps(o.model_dump(mode="json"), ensure_ascii=False))
    return 0


def cmd_trades(args: argparse.Namespace) -> int:
    r = _resolve_runner()
    for f in r.list_fills(symbol=args.symbol, limit=args.limit):
        print(json.dumps(f.model_dump(mode="json"), ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="实盘交易 CLI (PaperBroker; 与 UI 走同一引擎)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enable = sub.add_parser("enable", help="切换 trading_enabled 偏好")
    p_enable.add_argument("--disable", action="store_true", help="禁用撮合线程")
    p_enable.set_defaults(func=cmd_enable)

    p_submit = sub.add_parser("submit", help="提交委托 (buy / sell)")
    p_submit.add_argument("side", choices=["buy", "sell"])
    p_submit.add_argument("symbol")
    p_submit.add_argument("qty", type=int)
    p_submit.add_argument("--price", type=float, default=None,
                          help="限价; --market 时忽略")
    p_submit.add_argument("--market", dest="order_type", action="store_const",
                          const="market", default="limit",
                          help="市价单 (成交按下一根 K 线开盘价)")
    p_submit.add_argument("--note", default=None)
    p_submit.set_defaults(func=cmd_submit)

    p_cancel = sub.add_parser("cancel", help="撤单")
    p_cancel.add_argument("order_id")
    p_cancel.set_defaults(func=cmd_cancel)

    p_status = sub.add_parser("status", help="引擎 + 账户快照")
    p_status.set_defaults(func=cmd_status)

    p_pos = sub.add_parser("positions", help="当前持仓 (逐行 JSON)")
    p_pos.set_defaults(func=cmd_positions)

    p_ord = sub.add_parser("orders", help="委托列表 (逐行 JSON)")
    p_ord.add_argument("--status", default=None,
                       help="逗号分隔, 例: new,filled / partial,canceled")
    p_ord.add_argument("--symbol", default=None)
    p_ord.add_argument("--limit", type=int, default=50)
    p_ord.set_defaults(func=cmd_orders)

    p_trd = sub.add_parser("trades", help="历史成交 (逐行 JSON)")
    p_trd.add_argument("--symbol", default=None)
    p_trd.add_argument("--limit", type=int, default=50)
    p_trd.set_defaults(func=cmd_trades)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
