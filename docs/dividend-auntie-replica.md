# 菜场大妈 dividend_auntie.py 重写（聚宽 1:1 复刻）

> 状态：定稿（用户给了聚宽 ground truth）
> 范围：把 `data/user_data/strategies/live/dividend_auntie.py` 还原为聚宽 1:1 复刻
> 触发：上次端到端测试时被误覆盖成 32 字节占位

---

## 1. 目标

完全按聚宽文章《菜场大妈选股法》（开心果）1:1 复刻，包括：
- 3 个 cron 时点：9:05 / 9:26 / 13:30（聚宽原版时间，不再用 9:35）
- 6 个函数 + 1 个主循环 + DividendAuntieStrategy class
- 头部数据源映射注释（聚宽 API → 本仓 data_provider）

## 2. 范围

### In
- 重写 `data/user_data/strategies/live/dividend_auntie.py` 为聚宽 1:1 复刻
- schedules 改回 `9:05 / 9:26 / 13:30`（原版）

### Out
- 其他 dividend_* 策略（lowvol / quality / 等）不动
- 撮合/调度/17:00 清算不动
- preferences.json schedules 配置不动（用户可手动改）

## 3. 聚宽 → 本仓 API 映射

| 聚宽 | 本仓 |
|---|---|
| `get_all_securities('stock', dt_last).index.tolist()` | `dp.instruments` + `listing_date <= ctx.trade_date` |
| `finance.STK_XR_XD.code/a_registration_date/bonus_amount_rmb` | `dp.dividends(symbols, since, until)` |
| `valuation.market_cap` | `dp.fundamentals_market_cap(symbols)` |
| `context.previous_date` | `ctx.trade_date` |
| `context.current_dt.year` | `ctx.trade_date.year` |
| `context.portfolio.positions` | `ctx.positions` |
| `context.portfolio.available_cash` | `ctx.account['cash']` |
| `g.stock_num` | `ctx.state['stock_num']` |
| `g.high_limit_list` | `ctx.state['high_limit_list']` |
| `order_target(s, 0)` | `target[s] = 0` |
| `order_value(s, psize)` | `target[s] = int(psize/price/100)*100` |
| `history(1, '1m', 'close', security_list)` | `dp.minute_close[symbol]` |
| `current_data[stock].paused` | `dp.paused[symbol]` (last_price==0) |
| `current_data[stock].is_st / name` | `dp.name[symbol]` (ST/*/退) |
| `current_data[stock].high_limit / low_limit` | `dp.limit_up / limit_down[symbol]` |
| `get_price(..., end_date=context.previous_date, fields=['close','high_limit'], count=1)` | `repo.get_daily_batch(hold_list, prev_trade_day)` |

## 4. 行为口径

- `my_Trader` 在 9:26 触发，按 ctx.positions 算 in-frame sells proceeds 注入 buy
- `_dividend_stocks` 用 `ctx.trade_date.year` 算连续 N 年分红
- `filter_kcbj_stock` 按聚宽原版：`stock[0]=='8'` 过滤（北交所 8xxx 老格式；本仓有 `.BJ` 跟这不等价，但因为是 1:1 复刻，按原版写）
- `filter_st_stock` 用 `name` 含 `ST/*/退` 关键字（4 条件 AND）
- 涨跌停/高价 filter 持仓不踢（`stock in positions` 直接保留）
- `get_dividend_ratio_filter_list` dividend_ratio = (annual_bonus_per_share * total_shares) / market_cap

## 5. 时间

用户最终决定：`9:05 / 9:35 / 13:30`（不是聚宽原版的 9:26）。

- pre_market 9:05 — 跟聚宽原版一致
- rebalance 9:35 — 偏离聚宽原版 9:26，改为 9:35；理由：本仓撮合最早能撮合 9:34 那根 K（9:35 触发后 60s 内），跟原版撮合时机接近；9:26 离开盘太近，9:25 那根 K 还没生成，撮合效果差
- limit_check 13:30 — 跟聚宽原版一致

注：本仓撮合已支持"上一根已收盘 K"语义（见 live-trading-joinquant-parity.md），3 个时间点都能正常撮合。

## 6. 验收

```bash
cd /Volumes/quant-ssd/projects/tickflow-stock-panel/backend
uv run --frozen python -c "from app.services.trading.live_strategy import LiveStrategyRegistry; r=LiveStrategyRegistry(); from data.user_data.strategies.live.dividend_auntie import STRATEGY; r.register(STRATEGY); print('OK')"
# 期望: 打印 OK + 3 个 schedule (9:05/9:26/13:30)
```
