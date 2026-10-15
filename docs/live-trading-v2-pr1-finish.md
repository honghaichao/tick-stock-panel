# live-trading-v2-pr1-finish — PR1+PR2+PR3 实施状态 (全部落地, 待 commit)

> 状态: 已落地, 待 commit + 补 spec
> 范围: working tree 中 90+ frontend modified + 4 untracked 组件文件
> 不在范围: 修剩余 2 个测试 fail (matrix limit_up_locked + rps_rotation 延伸)

---

## 1. 目标

`docs/live-trading-v2-pr1.md` (PR1 7 项) + `docs/live-trading-v2-pr2-pr3.md` (PR2 5 项 + PR3 3 项)
共 15 项改动实际全部落地, 但 working tree 仍 dirty 90+ 文件, 没 commit + 没补 spec 文档.
本 task 把 frontend 改动按 commit 切干净 + 写 PR2+PR3 spec 修订文档 (标"已落地").

成功标准:
- frontend 改动按依赖关系切 N 个 commit (每个单独可 revert)
- 现有 frontend tsc / vitest 不破
- spec 文档完整 (PR1 已有, 补 PR2+PR3 落地版)
- 浏览器实测: 实盘页 5 tab + 持仓卡片网格 + OrderDialog 预览 + 撤单 timeout + bell 抽屉 + PageHeader 策略名 全部 OK

## 2. 实际落地状态

### 2.1 PR1 (7 项, 全部 ✅)

| # | 改动 | 位置 | 状态 |
|---|---|---|---|
| 1 | TopBar 重写 (hero 数字 + 救命三件套) | `components/live/TopBar.tsx` (78 行 diff) | ✅ 已 import EmergencyControls + BellNotifications |
| 2 | Sidebar 5→3 tab + 策略配置 ▼ 抽屉 | `components/live/Sidebar.tsx` (115 行 diff) | ✅ |
| 3 | CenterTabs 删/降级 | `pages/live/LiveTrading.tsx` (99 行 shim) | ✅ 已改用 Sidebar.tsx tab 状态 |
| 4 | OverviewContent 重写 (4 段式) | `pages/live/LiveTradingOverview.tsx` (1036 行) | ✅ 拆成 9 个 view 子组件 |
| 5 | PositionCard 新建 | `components/live/PositionCard.tsx` | ✅ 已 import 进 Overview |
| 6 | EmergencyControls 新建 | `components/live/EmergencyControls.tsx` | ✅ 已 import 进 TopBar |
| 7 | 后端 cancel-all + flatten | `backend/app/services/trading/router.py` (commit 6e0322f5 § B) | ✅ |

### 2.2 PR2 (5 项, 全部 ✅)

| # | 改动 | 位置 | 状态 |
|---|---|---|---|
| 1 | MessagesStream → bell 抽屉 | `components/live/BellNotifications.tsx` (新建, 已 import 进 TopBar line 101) | ✅ |
| 2 | StrategyTable 12→6 列 (名称/频率/状态/今日 PnL/开始时间/进入) | `components/live/StrategyTable.tsx` line 86-91 | ✅ |
| 3 | OrderDialog 资金占用 + 手续费预览 | `pages/live/LiveTradingOverview.tsx` line 384-410 (feeRate + lastPrice props) | ✅ |
| 4 | RiskStats "今日/历史" toggle | `components/live/RiskStats.tsx` line 22-26 (usePersistedState scope) | ✅ |
| 5 | 持仓卡片快捷按钮接 OrderDialog (卖 1/3 / 1/2 / 全) | `PositionCard.tsx` line 119-130 onQuickTrade + `LiveTradingOverview.tsx` line 990 onQuickTrade 实现 + 顶层 OrderDialog 状态提升 | ✅ |

### 2.3 PR3 (3 项, 全部 ✅)

| # | 改动 | 位置 | 状态 |
|---|---|---|---|
| 1 | usePersistedState hook 抽 | `lib/usePersistedState.ts` (新建) + RiskStats/Sidebar 已用 | ✅ |
| 2 | 撤单 4s timeout | `LiveTradingOverview.tsx` line 120-131 setTimeout + onSettled | ✅ |
| 3 | PageHeader 标题加策略名 | `pages/LiveTrading.tsx` line 47-48 `strategyName` | ✅ |

**结论**: PR1+PR2+PR3 15 项 100% 落地, working tree dirty 90+ files + 4 untracked 都是这些实施.

## 3. 范围

### 3.1 In — commit 计划

按依赖关系拆 commit, 每个 commit 单独可 revert:

1. **commit 1**: 4 个新组件 (`BellNotifications` / `EmergencyControls` / `PositionCard` / `usePersistedState` + shared.tsx) — 不引用现有文件, 独立
2. **commit 2**: `TopBar.tsx` + `Sidebar.tsx` 重写 (PR1.1 + PR1.2 + PR2.1 bell 抽屉) — 引用 commit 1
3. **commit 3**: `LiveTrading.tsx` 99 行 shim (PR1.3 CenterTabs 删 + PR3.3 PageHeader 策略名) — 引用 commit 1
4. **commit 4**: `pages/live/*.tsx` 5 个文件 (LiveTradingOverview 1036 行 / Code / Logs / Settings / Stats) — 引用 commit 2
5. **commit 5**: `RiskStats.tsx` + `StrategyTable.tsx` + `OrderDialog` 集成 (PR2.2-5 + PR3.1-2) — 引用 commit 4

实际拆 commit 数等执行时按 git diff 边界定, 上表是目标。

### 3.2 Out

- ❌ 修 2 个剩余测试 fail (matrix limit_up_locked + rps_rotation 延伸) → 独立 spec
- ❌ PR1 后端 cancel-all / flatten 端点 spec → `docs/live-trading-v2-pr1.md` 已有, 不补
- ❌ BellNotifications 前端 UI 视觉调整 (目前是基本版, 可能要加 v2 视觉) → 留后续

## 4. 数据契约

### 4.1 新组件 props 契约

| 组件 | Props | 行为 |
|---|---|---|
| `BellNotifications` | (无) | 顶栏右上, 5s 轮询 `/api/trading/notify`, 未读数红徽, 点击展开 320px 抽屉 |
| `EmergencyControls` | `strategyId: string` | 救命三件套 (撤单黄/停止蓝/平仓红), 2 段确认弹窗 |
| `PositionCard` | `position`, `onQuickTrade`, `sparkData`, `pnl*` 等 | 持仓卡片 (1 行 1 票), 底部快捷按钮触发 onQuickTrade |
| `usePersistedState<T>` | `(key: string, defaultValue: T)` | 跨 tab 持久化 localStorage |

### 4.2 LiveTradingOverview state 提升

```ts
const [dialog, setDialog] = useState<{
  side: 'buy' | 'sell'
  symbol?: string
  qty?: number
} | null>(null)
```

3 个触发源共用一个 OrderDialog:
- `openBuy()` / `openSell()`: 顶部按钮
- `onQuickTrade(side, ratio, symbol)`: PositionCard 快捷按钮 (卖 1/3 / 1/2 / 全)

## 5. API 契约

无变化。后端 (commit 6e0322f5 § B + § C) 已经在 Task 1 落地, 前端接入本 task 仅是 UI 集成。

## 6. 行为口径

- **PR2.5 持仓卡片快捷按钮**: 点击 PositionCard "卖 1/3" → 算 qty = floor(pos.qty / 3) → setDialog({side:'sell', symbol, qty}) → OrderDialog 弹, qty 已预填, feeRate + lastPrice 传进去 → 用户改 price 后确认
- **PR2.4 RiskStats toggle**: 默认 "今日" (trader 4 小时盯盘主要看今天); 切 "历史" 走 risk_metrics 端点 60 天 Alpha/Beta/Sharpe
- **PR3.2 撤单 timeout**: 失败后 4 秒自动 reset confirmCancelId; 前端兜底, 后端已幂等
- **PR3.3 PageHeader 标题**: `实盘交易 · ${strategyName}` (e.g. "实盘交易 · dividend_auntie"), 复用 tradingStrategies query (QK.tradingStrategies) 缓存

## 7. 风险与边界

- **commit 切分依赖**: TopBar 引用 BellNotifications + EmergencyControls, 必须先 commit 新组件再 commit TopBar。LiveTradingOverview 引用 PositionCard, 同样要先 commit PositionCard。
- **LiveTradingOverview 1036 行**: 单文件大, 但拆 9 个子组件 (OrdersTable / TradesTable / PositionsGrid / PnLPanel / OrderDialog / StrategyRunHistoryPanel / StrategiesPanel / LogsList / ScheduleRow) 后单 view 不超过 200 行, OK。
- **PR2.5 qty pre-fill**: `Math.floor(pos.qty / 3)` 在 qty < 3 时返回 0, 已 toast '无可卖数量' 阻止; 不阻断用户手动改 qty 输入。
- **PR2.1 BellNotifications 5s polling**: 用户离开页面后停止 poll (componentWillUnmount clear interval); SSE 改造成本大留 PR4。

## 8. 测试矩阵

| 维度 | 命令 | 通过条件 |
|---|---|---|
| 1. tsc | `cd frontend && pnpm exec tsc -b` | 0 error |
| 2. vitest | `cd frontend && pnpm exec vitest run` | 全绿 |
| 3. 后端测试不破 | `cd backend && uv run --no-project pytest tests/test_trading_*.py` | ≥ 79 passed |
| 4. 浏览器视觉 | 打开 `/live-trading/<策略>`, 13" + 27" 都试 | 1) TopBar 1 行不碎 2) 持仓卡片网格 3) 救命按钮 4) bell 抽屉 5) OrderDialog 预览 |
| 5. 撤单 timeout | OrdersTable 行内点撤单失败 | 4 秒后 confirmCancelId 自动复位 |
| 6. PageHeader 标题 | 任意策略 | 标题含策略 name + id |
| 7. PositionCard 快捷 | 持仓卡片点 "卖 1/3" | OrderDialog 弹, qty = floor(qty/3) |

## 9. 验收标准

1. 5+ 个 commit 切干净, 每个 commit message 含 PR1/PR2/PR3 + 具体子项
2. working tree 在 commit 后, frontend dirty 减到 0 (PR2+PR3 集成落地完成)
3. tsc + vitest 全绿
4. 后端 79+ 测试不破
5. 浏览器视觉 1-7 项全部通过

## 10. 不在本轮 (留 follow-up)

- 修 2 个剩余 fail (matrix limit_up_locked + rps_rotation 延伸) → `docs/fix-remaining-test-fails.md`
- PR4 SSE 推送改造 → 留未来
- BellNotifications 视觉调整 → 留未来
