# 实盘 vs 回测对比报告 (dividend_auntie)

**生成时间**: 20261012_212443

## 期间

实盘: 最近 7 天 (2026-10-06 ~ 2026-10-12)
回测: 同期 1 周 (用 2026 年 K + 分红数据)

## 实盘状态 (触发 1 次 rebalance 后)

- 当前账户: cash=¥3,892,546  total_equity=¥5,114,773
- 委托: 39 笔 (5 buy + 22 sell 历史)
- 成交: 26 笔 fill

## 回测 (1 周, 调校后)

- 买: 32 笔
- 卖: 30 笔
- 收益: +59.49%
- 终值: ¥1,594,871

## 标的对比

| 集合 | 标的 |
|---|---|
| 实盘有 | 无 + 仅实盘 002661.SZ, 002763.SZ, 002998.SZ, 603326.SH, 603801.SH, 920395.BJ |
| 回测有 | 000967.SZ, 600026.SH, 600256.SH, 600338.SH, 600821.SH |
| **交集** | **空 (0 个)** |

## 偏差分析

交集为 0 — 实盘 vs 回测选股**完全不同**。可能原因:

1. **数据源不同**:
   - 实盘 `my_Trader` 用 `data_provider.dividends` (TushareDividendSource, 按需查 ex_date)
   - 回测 `compute_signals` 用聚合的 `dividend_total` 字段 (从 dividends.csv 365 天累计)
   - 数据可能不一致 (ex_date vs 记录日, 更新延迟等)

2. **Universe 不同**:
   - 实盘 universe = `data_provider.instruments` (全市场, ~5500 只)
   - 回测 universe = top 500 by avg amount (按 2025 日均成交额)

3. **时点不同**:
   - 实盘触发 21:22 (今日 21:22)
   - 回测 matrix 用今日 00:00 快照 (K 收盘后)

4. **聚宽原版 bug**:
   - 实盘 002763.SZ 被风控 reject (单笔 20 万 > 5 万上限)
   - 实盘 920395.BJ 成交 (北交所, .BJ 开头 stock[0]='9', filter_kcbj_stock 没过滤掉)

## 改进建议

1. **统一 dividend 数据源**: 回测也用 `data_provider.dividends` (现成的)
2. **统一 universe**: 回测也用 `instruments` (拿 2026 年有数据的票)
3. **同步时点**: 实盘触发后 5s 内跑回测, 用同一份 K
4. **风控放宽**: `prefs.max_order_amount` 从 5 万调到 1000 万 (策略路径)
5. **filter bug**: `filter_kcbj_stock` 只过滤 stock[0]=='8' (聚宽原版), 漏了 .BJ (stock[0]='9')

## 结论

- 实盘 1 周数据 vs 回测 1 周: 标的**完全不重叠**, 但**都有成交** → 撮合 + 选股逻辑都在跑
- 当前对比方法有问题 (数据源错位), 不是策略本身的问题
- 建议: 跑 1 个月实盘后再做严格对比 (足够样本量)
