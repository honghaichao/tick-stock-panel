# 实盘 5 Tab 聚宽式布局（Live Trading 5-Page Layout）

> 状态：定稿（用户指示"直接做"）
> 范围：实盘页改 5 tab（概览/统计/日志/代码/设置）+ 风险统计 + 代码编辑 + 策略设置
> 依赖：上一 task `live-trading-joinquant-parity.md`（撮合/调度/17:00 清算已完成）

---

## 1. 目标

实盘页从"4 tab 横向 + 三栏"改成"5 tab 左侧导航 + 单栏"，对齐聚宽模拟盘详情页的 5 个视图：

1. **概览**：顶部 8 KPI + 收益曲线 + 持仓表 + 下单表
2. **统计**：6 条风险曲线（Alpha/Beta/Sharpe/Sortino/IR/Volatility）
3. **日志**：策略 log 输出 + 日志表格
4. **代码**：在线编辑 + 替换/替换并运行
5. **设置**：策略元数据 + schedules 编辑

## 2. 范围

### In
- 5 tab 侧边导航（聚宽风格）
- 8 KPI 顶部状态条（聚宽 vs 当前 4）
- 风险统计后端（pnl_snapshots → alpha/beta/sharpe）
- 代码在线读 + 写（API）
- 策略设置（schedules 编辑已有，复用）

### Out
- 通知（独立 task live-trading-notification.md）
- 实盘 ↔ 回测切换（聚宽"对应回测"链接，本仓不实现）
- 链接分享

## 3. 数据契约

### 3.1 新增 API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/trading/strategies/{id}/code` | GET | 读 `data/user_data/strategies/live/{id}.py` |
| `/api/trading/strategies/{id}/code` | PUT | 写文件 + 触发 after_code_changed |
| `/api/trading/strategies/{id}/code/changelog` | GET | 读代码变更记录（新建 `code_changelog.jsonl`） |
| `/api/trading/pnl/risk_metrics` | GET | 返回 alpha/beta/sharpe/sortino/ir/volatility 序列 |

### 3.2 RiskMetrics 数据结构

```python
class RiskMetrics(BaseModel):
    series: list[dict]   # [{date: "2026-09-29", alpha, beta, sharpe, sortino, info_ratio, volatility}, ...]
    summary: dict        # {alpha: 0.05, beta: 0.8, sharpe: 1.2, sortino: 1.5, info_ratio: 0.3, volatility: 0.15}
```

### 3.3 CodeChangelog 结构

```python
class CodeChangelogEntry(BaseModel):
    ts: str               # ISO
    strategy_id: str
    old_size: int         # 旧文件大小
    new_size: int         # 新文件大小
    diff_summary: str     # "替换 123 行" / "新增 5 行"
    note: str             # 用户备注 (optional)
```

存盘: `data/user_data/trading/code_changelog.jsonl`

## 4. 行为口径

### 4.1 风险统计

- 用 `pnl_snapshots.jsonl` 的 total_equity 序列计算日收益率
- 基准: 沪深 300 (000300.SH) — 用 `kline_daily_enriched` 缓存
- 计算公式 (跟聚宽 + 业界一致):
  - `daily_return[t] = total_equity[t] / total_equity[t-1] - 1`
  - `alpha = annualized(strategy_return - beta * benchmark_return - risk_free)` (rf=0.03)
  - `beta = covariance(strategy, benchmark) / variance(benchmark)`
  - `sharpe = mean(strategy_return - rf_daily) / std(strategy_return) * sqrt(252)`
  - `sortino = mean(strategy_return - rf_daily) / std(downside_returns) * sqrt(252)`
  - `info_ratio = (cumulative_strategy - cumulative_benchmark) / tracking_error * sqrt(252)`
  - `volatility = std(strategy_return) * sqrt(252)`
- 序列: 滚动 60 天窗口
- 空数据时: 返回空 series + summary 全部 None

### 4.2 代码编辑

- `GET` 返 200 + 文本（UTF-8）+ 行号
- `PUT` 接收 `{content: str, note?: str}`：
  - 校验: 必须非空、必须以 `STRATEGY = ` 结尾
  - 写文件用 `atomic_write_text` (fs_utils 已有)
  - 触发 after_code_changed 钩子 (scheduler 已有)
  - 追加 code_changelog.jsonl
  - 失败: 500 + 错误信息
- 不做语法校验 (那是 python 解析的事，落到运行时)
- 不支持新建/删除策略 (只替换已有)

### 4.3 5 Tab 布局

- 左侧 fixed sidebar 200px: 5 tab 列表
- 主区域: 顶部 PageHeader + 当前 tab 内容
- 概览: 单列从上到下
- 统计: 单列多条折线
- 日志: 现有 LogsList + 完整 log 文本
- 代码: 行号 + 编辑 textarea + 操作按钮
- 设置: 复用现有 put_strategy_schedules

## 5. 风险与边界

- 风险统计需要 pnl_snapshots 数据；如果不满 60 天，summary 部分字段为 None，前端显示 "数据不足"
- 代码编辑后立即生效（reload 策略），但已注册的 cron jobs 不会自动 re-register（需要 apply_all）
- 替换代码时如果进程正在执行该策略，可能 race condition；先 cancel active run 再 reload
- 行号显示: 简单 split + index + 1，不做 syntax highlight (避免引入依赖)

## 6. 测试矩阵

| 用例 | 期望 |
|---|---|
| `GET /strategies/dividend_auntie/code` | 200 + 完整文件内容 |
| `PUT /strategies/dividend_auntie/code` 空 content | 400 |
| `PUT` 后原文件被覆盖 | 落盘新内容 |
| `PUT` 后 code_changelog.jsonl 多一条 | ✓ |
| `GET /pnl/risk_metrics` 空数据 | series=[], summary all None |
| `GET /pnl/risk_metrics` 30+ 天数据 | series 30+ 条, summary 6 个字段 |
| 前端 5 tab 切换 | localStorage 持久化当前 tab |
| 代码 tab "替换并运行" | PUT + 立即调 run_now |
| 设置 tab "保存" | 走现有 PUT /strategies/{id}/schedules |

## 7. 验收标准

```bash
# 后端
cd backend
uv run --frozen pytest tests/test_trading_*.py -v
# 期望: 原有 185 + 新增 8+ 通过

# 前端
cd frontend
pnpm build
pnpm exec vitest run src/pages/LiveTrading
# 期望: build 通过, LiveTrading 14+ 通过

# 端到端
./dev.sh start
# 打开 http://localhost:3011/live-trading
# - 默认进概览 tab, 8 KPI 显示
# - 切统计 tab: 6 条曲线 (有数据时)
# - 切日志 tab: 完整 log + 表格
# - 切代码 tab: dividend_auntie.py 文本 + 行号, 点"替换并运行"测试
# - 切设置 tab: 策略元数据 + 启停 schedules
```

## 8. 改动文件

### 后端
- 新增 `backend/app/services/trading/risk_metrics.py` (alpha/beta/sharpe 计算)
- 改 `backend/app/services/trading/router.py` (新增 4 个端点)
- 改 `backend/app/services/trading/code_store.py` (新) — 读/写/changelog
- 测试 `tests/test_trading_risk_metrics.py` (新)

### 前端
- 新增 `frontend/src/components/live/Sidebar.tsx` (5 tab 导航)
- 新增 `frontend/src/components/live/RiskStats.tsx` (6 曲线)
- 新增 `frontend/src/components/live/CodeView.tsx` (行号 + 编辑)
- 新增 `frontend/src/components/live/StrategySettings.tsx` (元数据 + schedules)
- 改 `frontend/src/pages/LiveTrading.tsx` (主页面用 Sidebar + 子组件)
- 改 `frontend/src/lib/api.ts` (4 个新方法)
