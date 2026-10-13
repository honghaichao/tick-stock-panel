# 回测偏差调校 + 5 Tab 通知 + 回测报告可视化

> 状态：定稿（用户指示"做这几个"）
> 范围：3 件事 — ① 回测偏差调校 ② 5 Tab 通知+监控中心接通 ③ 回测报告可视化

---

## 1. 任务拆分

### Task 1: 回测偏差调校
- 加手续费（fee_pct=0.0003, stamp_tax_pct=0.001 for sell）
- 加滑点（0.1%）
- 退出改"每日 9:35 rebalance 真实行为"：每天先按当日 entry 信号重选，不在新 entry 的清仓
- 重跑对比（调校前后差异）

### Task 3: 回测报告可视化
- 基于 Task 1 输出画 equity curve (matplotlib → PNG)
- 写报告到 `docs/live-trading-backtest-report.md`
- 含：收益曲线图 + 月度收益表 + 最大回撤 + 交易笔数 + 持仓时间分布

### Task 2: 5 Tab 通知+监控中心接通
- 后端 `notify_store.py`：落盘 + SSE 推 `notify_event`
- 策略 `send_message(text)` 接口（写到 LiveStrategyContext）
- 委托/拒单/异常自动入 notify
- 前端：Monitor.tsx 新增"实盘事件"卡片
- LiveTrading 右栏 MessagesStream 接 SSE

---

## 2. 数据契约

### 2.1 回测撮合契约
- entry price = close × (1 + slippage_bps/10000) for buy
- exit price = close × (1 - slippage_bps/10000) for sell
- fee = qty × price × fee_pct
- stamp_tax = qty × price × stamp_tax_pct (only for sell)
- cash: buy 时扣 price × qty + fee; sell 时加 price × qty - fee - stamp_tax

### 2.2 notify_store schema
- `data/user_data/trading/notify.jsonl` 每行 `{ts, kind, text, ...}`
- SSE event `notify_event` 前端消费

### 2.3 Backtest report md schema
- 标题 + 期间 + 参数 + KPI 表
- 月度收益表
- 收益曲线图（PNG 嵌入）

---

## 3. 行为口径

### 3.1 真实 rebalance 退出
- 每天 entry 时：先看持仓，**不在新 entry 里的清仓**（按当日 close 卖）
- 同一标的 entry 后保留到 rebalance 周期（不再 5 天强平）
- 资金不足时按 entry 顺序优先级买

### 3.2 notify 触发点
- 委托提交：`notify` kind=order_submitted
- 委托成交：`notify` kind=order_filled
- 委托拒单：`notify` kind=order_rejected
- 17:00 清算：`notify` kind=clearance
- 失败重试：`notify` kind=retry
- 策略 send_message：自定义 text

---

## 4. 测试矩阵

### Task 1
| 用例 | 期望 |
|---|---|
| 调校后收益 < 调校前（+73.51%） | 是（手续费+滑点 损耗 ~3-5%） |
| 调校后最大回撤接近 | 接近 |
| 调校后交易笔数 | 同 |
| 月度收益有正有负 | 一年数据应有分布 |

### Task 3
| 用例 | 期望 |
|---|---|
| 报告 md 生成 | `docs/live-trading-backtest-report.md` 存在 |
| equity curve PNG | 嵌入 md |
| 月度表 | 12 行 |
| KPI 表 | 5+ 行 |

### Task 2
| 用例 | 期望 |
|---|---|
| notify 落盘 | notify.jsonl 追加 |
| SSE 推送 | 浏览器收到 |
| 委托提交触发 notify | ✓ |
| 拒单触发 notify (kind=rejected) | ✓ |
| Monitor.tsx 显示"实盘事件"卡片 | ✓ |
| LiveTrading MessagesStream 接 SSE | ✓ |

---

## 5. 验收

### Task 1+3
```bash
cd /Volumes/quant-ssd/projects/tickflow-stock-panel/backend
uv run --frozen python scripts/backtest_dividend_auntie.py
# 期望输出调校后收益 + 月度表 + 报告路径
ls docs/live-trading-backtest-report.md
```

### Task 2
```bash
# 后端
uv run --frozen pytest tests/test_trading_*.py -q
# 期望 notify_store + notify_router 端点 + 测试通过

# 前端
cd ../frontend && pnpm build
# 期望 build 通过, Monitor.tsx 含实盘事件卡片
```

---

## 6. 改动文件

### Task 1
- `backend/scripts/backtest_dividend_auntie.py` — 加手续费/滑点/真实 rebalance 退出

### Task 3
- `backend/scripts/backtest_dividend_auntie.py` — 输出 reports/live-trading-backtest-{ts}/ 目录
- `docs/live-trading-backtest-report.md` (新) — 报告

### Task 2
- `backend/app/services/trading/notify_store.py` (新) — 落盘 + 读
- `backend/app/services/trading/router.py` — 4 个新端点
- `backend/app/services/trading/live_strategy.py` — LiveStrategyContext 加 `send_message`
- `backend/app/services/trading/executor.py` — on_event 完调 send_message 落盘
- `frontend/src/components/live/MessagesStream.tsx` — 接 SSE
- `frontend/src/pages/Monitor.tsx` — 加实盘事件卡片
- `frontend/src/lib/api.ts` — 新方法
