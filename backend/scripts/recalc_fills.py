"""重算历史 fill — 用新撮合语义 (市价单按 close + 滑点, 限价单逻辑保持) 重算旧 fill.

行为:
  1. 读 fills.jsonl + orders.jsonl, 按时间排序
  2. 对每个 fill, 找对应 order 的 created_at 作为 as_of
  3. 拉 data/kline/minute/{symbol}/{date}.parquet, 找 as_of 时刻已收盘 K
  4. 用新 broker (聚宽式市价单语义 + 默认 NONE 滑点) 撮合
  5. 若新成交价/量 != 旧: 写新 fill (id=fl_recalc_xxx), 旧 fill 保留
  6. account.json 重写, 旧值备份 account.json.bak.<ts>
  7. 输出报告 data/user_data/trading/recalc_report.json

不修改 orders.jsonl (order 状态保留为原 fill 时刻的快照).
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import polars as pl

# 允许从项目根 / backend 直接跑
ROOT = Path(__file__).resolve().parents[2]
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trading.models import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.services.trading import store
from app.services.trading.paper_broker import PaperBroker
from app.services.trading.slippage import SlippageConfig
from app.services.trading import preferences as prefs

CN_TZ = timezone(timedelta(hours=8))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("recalc_fills")


def _parse_iso_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)
        return dt.astimezone(CN_TZ)
    except Exception:
        return None


def _find_minute_k_file(repo_data_dir: Path, symbol: str, trade_date) -> Path | None:
    """找当日分钟 K parquet. 路径: {data_dir}/kline_minute/date=YYYY-MM-DD/part.parquet.

    按日期分区, 整个市场 symbol 都在一个文件里. 实际列: symbol, datetime, open, high, low,
    close, volume, amount. 路径基于仓库惯例 (跟 backend/app/tickflow/repository.py 一致).
    trade_date 接受 date 或 str (ISO 格式).
    """
    if isinstance(trade_date, str):
        td = date.fromisoformat(trade_date)
    else:
        td = trade_date
    p = repo_data_dir / "kline_minute" / f"date={td.isoformat()}" / "part.parquet"
    return p if p.exists() else None


def _load_kline(p: Path, symbol: str | None = None) -> pl.DataFrame:
    try:
        df = pl.read_parquet(p)
        if symbol and "symbol" in df.columns:
            df = df.filter(pl.col("symbol") == symbol)
        return df
    except Exception as e:
        logger.warning("读 K 线失败 %s: %s", p, e)
        return pl.DataFrame()


def _select_closed_bar(df: pl.DataFrame, as_of: datetime) -> tuple[pl.DataFrame | None, bool]:
    """返回 as_of 时刻已收盘最后一根 K + 是否当日第一根 (9:30).

    polars datetime[μs] 是 naive (按仓库惯例视为北京时间), 所以 as_of 也用 naive.
    """
    if df.is_empty() or "datetime" not in df.columns:
        return None, False
    # 去掉 tz, 用 naive datetime (polars 默认 UTC, 但仓库实际是北京时间, 数值相同)
    as_of_naive = as_of.replace(tzinfo=None) if as_of.tzinfo is not None else as_of
    threshold = as_of_naive - timedelta(seconds=60)
    try:
        closed = df.filter(
            (pl.col("datetime").cast(pl.Datetime) + pl.duration(minutes=1))
            <= pl.lit(threshold)
        ).sort("datetime")
    except Exception as e:
        logger.warning("K 线过滤失败: %s", e)
        return None, False
    if closed.is_empty():
        return None, False
    last = closed.tail(1)
    first_dt = closed.row(0, named=True).get("datetime")
    is_first = False
    if first_dt is not None and hasattr(first_dt, "hour"):
        is_first = (first_dt.hour == 9 and first_dt.minute == 30)
    return last, is_first


def _new_fill_id() -> str:
    return f"fl_recalc_{datetime.now().strftime('%H%M%S%f')}_{secrets_token_hex(4)}"


def secrets_token_hex(n: int) -> str:
    import secrets
    return secrets.token_hex(n)


def main():
    ap = argparse.ArgumentParser(description="重算历史 fill (聚宽式撮合语义)")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data",
                    help="仓库 data/ 目录 (含 kline/ 与 user_data/)")
    ap.add_argument("--user-data-dir", type=Path, default=None,
                    help="user_data/ 目录; 默认 data-dir/user_data/")
    ap.add_argument("--slippage-kind", type=str, default="none",
                    help="撮合滑点类型 (none/price_related/tick_related/fixed)")
    ap.add_argument("--slippage-value", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    args = ap.parse_args()

    user_data = args.user_data_dir or (args.data_dir / "user_data")
    fills_path = user_data / "trading" / "fills.jsonl"
    orders_path = user_data / "trading" / "orders.jsonl"
    account_path = user_data / "trading" / "account.json"

    if not fills_path.exists():
        logger.error("fills.jsonl 不存在: %s", fills_path)
        sys.exit(1)
    if not orders_path.exists():
        logger.error("orders.jsonl 不存在: %s", orders_path)
        sys.exit(1)

    # 读 orders 索引
    orders: dict[str, Order] = {}
    for line in orders_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = Order.model_validate_json(line)
            orders[o.id] = o
        except Exception as e:
            logger.warning("orders.jsonl 损坏行跳过: %s", e)

    # 读 fills
    fills: list[Fill] = []
    for line in fills_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            fills.append(Fill.model_validate_json(line))
        except Exception as e:
            logger.warning("fills.jsonl 损坏行跳过: %s", e)

    logger.info("读 orders=%d, fills=%d", len(orders), len(fills))

    # 准备 broker
    from app.services.trading.slippage import SlippageKind
    try:
        skind = SlippageKind(args.slippage_kind)
    except ValueError:
        skind = SlippageKind.NONE
    slippage = SlippageConfig(kind=skind, value=args.slippage_value)
    broker = PaperBroker()

    # 重算
    new_fills: list[Fill] = []
    mismatched: list[dict] = []
    skipped: list[dict] = []
    account_delta = 0.0
    for f in fills:
        order = orders.get(f.order_id)
        if order is None:
            skipped.append({"fill": f.id, "reason": "order not found"})
            continue
        # as_of = fill.filled_at (实际撮合时点); K 文件用 fill.trade_date
        # (历史 fill 里 trade_date != order.created_at 是因为 9:35 提交的订单可能
        # 等 K 数据齐了才撮合, 实际撮合可能在几天后 11:47; 不能用 order.created_at)
        as_of = _parse_iso_dt(f.filled_at)
        if as_of is None:
            skipped.append({"fill": f.id, "reason": f"filled_at 解析失败: {f.filled_at}"})
            continue
        # 拉 K 线
        kp = _find_minute_k_file(args.data_dir, f.symbol, f.trade_date)
        if kp is None:
            skipped.append({"fill": f.id, "reason": f"K 线不存在: {f.symbol} {f.trade_date}"})
            continue
        df = _load_kline(kp, symbol=f.symbol)
        bar, is_first = _select_closed_bar(df, as_of)
        if bar is None:
            skipped.append({"fill": f.id, "reason": f"as_of={as_of} 时刻无已收盘 K"})
            continue
        # 撮合 — 用临时 Order (filled_qty=0) 模拟"新提交", 避免旧 fill 干扰 fill_qty 计算
        fresh_order = order.model_copy(update={"filled_qty": 0, "status": OrderStatus.NEW})
        decisions = broker.match_tick(
            [fresh_order], {order.symbol: bar},
            slippage=slippage,
            order_volume_ratio=1.0,
            first_bar_of_day={order.symbol: is_first},
        )
        d = decisions[0]
        if not d.matched:
            skipped.append({"fill": f.id, "reason": f"broker 不撮合: {d.reason}"})
            continue
        new_price = d.fill_price
        new_qty = d.fill_qty
        if new_price is None:
            skipped.append({"fill": f.id, "reason": "fill_price=None"})
            continue
        # 对比新旧
        if abs(new_price - f.price) < 0.0001 and new_qty == f.qty:
            # 完全一致, 不写新 fill
            continue
        # 写新 fill
        amount = new_qty * new_price
        # 重算 fee / tax
        fee_rate = prefs.fee_rate()
        fee_min = prefs.fee_min()
        fee = max(fee_min, new_qty * new_price * fee_rate)
        tax = new_qty * new_price * prefs.stamp_tax_rate() if order.side == OrderSide.SELL else 0.0
        new_fill = Fill(
            id=_new_fill_id(),
            order_id=order.id,
            symbol=f.symbol,
            side=f.side,
            qty=new_qty,
            price=new_price,
            fee=fee, tax=tax,
            amount=amount,
            filled_at=store.now_iso(),  # 用当前时间 (重算发生时刻)
            trade_date=f.trade_date,
        )
        new_fills.append(new_fill)
        # 算 account 影响 (重置账户, 后续统一重算)
        if order.side == OrderSide.SELL:
            account_delta += (amount - fee - tax) - (f.amount - f.fee - f.tax)
        else:
            account_delta += -((amount + fee) - (f.amount + f.fee))

        mismatched.append({
            "old_fill_id": f.id,
            "new_fill_id": new_fill.id,
            "order_id": order.id,
            "symbol": f.symbol,
            "side": f.side.value,
            "old_price": f.price, "new_price": new_price,
            "old_qty": f.qty, "new_qty": new_qty,
            "as_of": order.created_at,
        })

    logger.info("重算结果: 新增 %d 个 fill, 跳过 %d, 差异 %d",
                len(new_fills), len(skipped), len(mismatched))

    # 写报告
    report = {
        "ran_at": store.now_iso(),
        "slippage": slippage.to_dict(),
        "summary": {
            "total_fills": len(fills),
            "matched": len(fills) - len(skipped) - len(mismatched),
            "mismatched": len(mismatched),
            "skipped": len(skipped),
            "new_fills_added": len(new_fills),
            "account_delta": account_delta,
        },
        "mismatched": mismatched,
        "skipped": skipped,
    }
    report_path = user_data / "trading" / "recalc_report.json"
    if not args.dry_run:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("报告落: %s", report_path)

        # 追加新 fill 到 fills.jsonl
        if new_fills:
            with fills_path.open("a", encoding="utf-8") as fp:
                for nf in new_fills:
                    fp.write(nf.model_dump_json() + "\n")
            logger.info("追加 %d 个新 fill 到 %s", len(new_fills), fills_path)

        # 备份并重写 account.json
        if account_path.exists():
            ts = datetime.now().strftime("%Y%m%d%H%M%S")
            backup = account_path.with_suffix(f".json.bak.{ts}")
            shutil.copy(account_path, backup)
            logger.info("account.json 备份到 %s", backup)
            # 加载当前 account, 应用 delta
            try:
                account = json.loads(account_path.read_text(encoding="utf-8"))
                account["cash"] = account.get("cash", 0.0) + account_delta
                account_path.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info("account.json 重写: cash += %.2f", account_delta)
            except Exception as e:
                logger.error("account.json 重写失败: %s", e)

    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if mismatched:
        print(f"\n前 {min(5, len(mismatched))} 个差异 fill:")
        for m in mismatched[:5]:
            print(f"  {m['symbol']} {m['side']} 旧价={m['old_price']:.3f} 新价={m['new_price']:.3f} 旧量={m['old_qty']} 新量={m['new_qty']}")


if __name__ == "__main__":
    main()
