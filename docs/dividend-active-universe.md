# dividend_auntie 活跃股版 + 全量股版 + filter 修复 + 数据源统一

> 状态：定稿（用户拍板 3 件事）
> 范围：实盘改用活跃股策略 + 保留全量股版 + 修 .BJ 漏过滤 + 回测/实盘 dividend 数据源统一

---

## 1. 任务拆分

### Task 1: 实盘拆 2 个策略
- **dividend_auntie_universe.py (新)**：原 dividend_auntie 复制改名 = 全量股版（保留回测/对比）
- **dividend_auntie.py (改)**：加 `universe="active"` 默认参数 = 活跃股版（top 500 活跃股）
- 实盘 preferences.json: 改 dividend_auntie 的 schedules 切到活跃股版（kind + new field）
- 加新策略 dividend_auntie_universe 的 schedules（默认 disabled）

### Task 2: 修 filter_kcbj_stock bug
- 当前: `if stock[0] == "8":` 只过滤 8xxx
- 聚宽原版（[FIXED] 偏差标注后）: 应过滤 `stock.endswith(".BJ")` (北交所) + `stock.startswith("688")` (科创板) + `stock.startswith("300")` 或 `"301"` (创业板)
- 修 dividend_auntie.py 的 filter_kcbj_stock 函数
- 保留 ACCEPTED 注释：原版只过滤 8，本仓扩展为 3 类

### Task 3: 统一 dividend 数据源
- 当前回测 compute_signals 用聚合的 `dividend_total` 字段（从 dividends.csv）
- 实盘 my_Trader 用 `data_provider.dividends(symbols, since, until)` (TushareDividendSource)
- 修：compute_signals 也走 data_provider
  - 通过 data_provider.dividends 实时算 365 天累计 (跟 my_Trader 一样)
  - data_provider 在 matrix 算子里是 market.fields["data_provider"] (运行时注入)
  - 写测试 + 跑回测确认数字一致

---

## 2. 数据契约

### 2.1 活跃股 universe
- 数据源: `data/kline_minute` 或 `kline_daily` 最近 60 天 amount 累计
- top 500 by total amount (聚宽 1:1 精神: "成交活跃")
- 在 compute_signals 里用 `market.fields["active_universe"]` 注入 (list of symbol)
- 优先于 my_Trader 自己的 universe 过滤 (减少 universe 数量提升性能)

### 2.2 preferences.json
```json
"trading_strategies.dividend_auntie_universe.schedules": [
  {"name": "pre_market", "hour": 9, "minute": 5, "enabled": false},
  {"name": "rebalance", "hour": 9, "minute": 35, "enabled": false},
  {"name": "limit_check", "hour": 13, "minute": 30, "enabled": false}
]
```
dividend_auntie (活跃股版) 的 enabled 改 true.

---

## 3. 行为口径

### 3.1 活跃股版
- universe = top 500 by recent avg amount
- 其他逻辑跟原版 1:1
- 实盘 cron 9:35 触发
- 选股数比全量股版少 90%, 撮合更快

### 3.2 全量股版
- universe = all instruments (5500+ 只)
- 保留作为回测/对比基准
- 实盘 schedules enabled=false (默认不跑)

### 3.3 filter_kcbj_stock 修复
```python
def filter_kcbj_stock(stock_list):
    """聚宽原版 1:1 扩展: 8xxx + .BJ + 688 + 300/301"""
    for stock in stock_list[:]:
        if (stock[0] == "8"                              # 北交所老格式 8xxxx
            or stock.endswith(".BJ")                     # 北交所新格式
            or stock.startswith("688")                   # 科创板
            or stock.startswith("300") or stock.startswith("301")):  # 创业板
            stock_list.remove(stock)
    return stock_list
```

### 3.4 compute_signals 用 data_provider
- matrix 算子里需要 data_provider (matrix 现在没有)
- 方案: market.fields["data_provider"] 注入, compute_signals 优先用这个; 缺则 fallback 到 dividend_total
- 保证回测和实盘用同一套数据源

---

## 4. 测试矩阵

| 用例 | 期望 |
|---|---|
| dividend_auntie_universe.py 可 import | ✓ |
| dividend_auntie.py 加 universe="active" 参数 | ✓ |
| filter_kcbj_stock 过滤 .BJ / 688 / 300 / 301 | ✓ |
| 实盘 enabled=true 在 dividend_auntie (活跃股) | ✓ |
| 活跃股 universe 注入 compute_signals | ✓ |
| compute_signals 用 data_provider 算 dividend | ✓ |
| 回测 3 年数字变化 | 收益略降 (universe 缩小) |

---

## 5. 验收

```bash
# 后端
cd backend
uv run pytest tests/test_trading_*.py tests/test_strategy_unified.py tests/test_trading_notify.py -q
# 期望 210+ 通过

# 前端
cd ../frontend && pnpm build
# 期望 build 通过

# 实盘触发
curl -X POST localhost:3018/api/trading/strategies/dividend_auntie/run-now -d '{"event":"rebalance"}'
# 期望 target 用活跃股 (数量比全量股少)
```

---

## 6. 改动文件

### Task 1
- `data/user_data/strategies/live/dividend_auntie_universe.py` (新)
- `data/user_data/strategies/live/dividend_auntie.py` (改: 加 universe 参数, 修 filter)
- `data/user_data/preferences.json` (改: dividend_auntie_universe schedules)

### Task 2
- `data/user_data/strategies/live/dividend_auntie.py` (改: filter_kcbj_stock)

### Task 3
- `data/user_data/strategies/live/dividend_auntie.py` (改: compute_signals 用 data_provider)
- `data/user_data/strategies/live/dividend_auntie_universe.py` (同上)
- `tests/test_trading_universe.py` (新)

### 文档
- `docs/dividend-active-universe.md` (本文)
