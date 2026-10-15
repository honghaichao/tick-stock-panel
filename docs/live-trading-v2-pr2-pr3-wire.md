# live-trading-v2-pr2-pr3-wire — 收尾 4 项 (OrderDialog 预览 / 持仓快捷接 / StrategyTable 减列 / 撤单 timeout)

> 状态: 待用户 review
> 前置: `live-trading-v2-pr1-nav-and-wire` 已合 (commit 1a91e926)
> 同源任务: `live-trading-v2-pr2-pr3.md` 原始 8 项 PR2+PR3 已大部分落地, 剩 4 项集中在
> `LiveTradingOverview.tsx` 的 OrdersTable + OrderDialog + PositionsGrid 接线.

## 0. 现状盘点 (2026-10-15)

| 项 | 原属 | 当前状态 |
|---|---|---|
| BellNotifications 抽屉 | PR2 #1 | ✅ 已集成到 TopBar (上一轮 PR1) |
| StrategyTable 减列 12 → 6 | PR2 #2 | ❌ 当前 7 列, 待本轮收尾 |
| OrderDialog 资金占用 / 手续费预览 | PR2 #3 | ❌ OrderDialog 当前没有任何金额预览 |
| RiskStats "今日 / 历史" toggle | PR2 #4 | ✅ RiskStats.tsx 已是 usePersistedState('liveRiskScope') + toggle UI |
| PositionCard 卖 1/3 / 1/2 / 全卖 接 OrderDialog | PR2 #5 | ❌ PositionCard 按钮已渲染但 onQuickTrade 未接 |
| usePersistedState hook namespace 统一 | PR3 #1 | ✅ 已写, Sidebar / RiskStats 已切换 |
| cancelMut 撤单 4 秒 timeout 兜底 | PR3 #2 | ❌ OrdersTable 的 confirmCancelId 只在 onSuccess 时清, 网络卡就粘住 |
| PageHeader 标题加策略名 | PR3 #3 | ✅ 上一轮 LiveTrading.tsx 已加 strategiesQ 共享 query |

剩 4 项本轮收尾: **#2, #3, #5, PR3 #2**. 全部是前端 UI 拼接, 不动后端.

## 1. 目标

把剩下 4 项 PR2 + PR3 改造一气收口:

1. **OrderDialog 资金占用 / 手续费预览**: 用户在买/卖时一眼看到 "这单 ¥多少钱 / 手续费 ¥多少", 防误触 (市价特别有意义 — trader 9:35 时常急着按一笔但不知道金额).
2. **持仓卡片快捷接 OrderDialog**: PositionCard 已渲染的 "卖 1/3 / 1/2 / 全卖" 按钮接成下单入口, 替代 "关掉详情页再点卖出" 的 4 次点击链路 (打开详情 -> 委托区点卖出 -> 选标 -> 输数量).
3. **StrategyTable 7 → 6 列**: 把 "时限" 列合并进 "开始时间" 一栏 (派生天数显示), 让 13 寸屏幕不再横向滚动.
4. **撤单 4 秒 timeout 兜底**: 后端 hang 时按钮 4 秒后自动复位, 不再粘住.

成功标准:
- OrderDialog 输入价格/数量时, 下方实时显示资金占用 + 手续费, 数字 tabular-nums, 跟随 input 重算
- PositionCard 上 "卖 1/3" 弹出预填 symbol='xxx', side='sell', qty=floor(position.qty/3) 的 OrderDialog; "全卖" 同 qty=position.qty
- StrategyTable 在 13 寸屏 1024-1280px 不再横向滚动 (sm:grid-cols 6 wrap 内容)
- 网络 hang 4 秒后撤单按钮自动从 "确认撤单" 复位回 trash 图标, 用户可重新选择
- tsc / vitest / vite build 全过

## 2. 范围

### In

A. **OrderDialog 扩展** (`LiveTradingOverview.tsx`, OrderDialog 函数 line 235+)
   - 加可选 prop `initial?: { symbol?: string; side?: 'buy'|'sell'; qty?: number }`
   - 在 lazy initializer (useState) 读 initial.*, 预填 symbol / qtyText
   - 加 `资金占用` 行: `qty * price` (priceText 取不到时退到 position.last_price, 都取不到就 `—`)
   - 加 `手续费` 行: `资金占用 * fee_rate` (fee_rate 来自 `QK.tradingPreferences` query; 用 tradingPreferences.trading_fee_rate 字段)
   - 涨跌停价行**不**加 (后端风控会精确算, 前端 ±10% 估算会被误以为是真值, 引起纠纷; spec 这里删了)
   - 字体 tabular-nums font-mono text-sm, 与已有 OrderDialog 风格一致

B. **OrderDialog 状态提升** (`LiveTradingOverview.tsx`, LiveTradingOverview 函数 + OrdersTable 函数)
   - 引入 `LiveTradingOverview` 顶层 state: `dialog: null | { side: 'buy'|'sell'; symbol?: string; qty?: number }`
   - 新增 imports + 顶层 hook 拿 preferences 和 account (fee_rate 在 preferences 里)
   - `OrdersTable` 改签名: `onOpenBuy / onOpenSell` 由父级注入, OrderDialog 拿 initial
   - `PositionsGrid` 改签名: 拿 onQuickTradeFromParent 由父级注入
   - `<OrderDialog side={dialog.side} initial={{...}} onClose={...} />` 渲染在 `LiveTradingOverview` 顶层 (删除 OrdersTable 内部的 OrderDialog 实例)

C. **PositionCard 接 quick-trade** (`LiveTradingOverview.tsx`, PositionsGrid 函数 + LiveTradingOverview 函数)
   - PositionsGrid 接受新 prop `onQuickTrade: (side: 'sell', ratio: 1 | 2 | 'all', symbol: string) => void`
   - 转给 PositionCard
   - 在 LiveTradingOverview 顶层实现 handler:
     ```
     const onQuickTrade = (side, ratio, symbol) => {
       const pos = positionsBySymbol[symbol]
       if (!pos) return
       const qty = ratio === 'all' ? pos.qty : ratio === 1 ? Math.floor(pos.qty / 3) : Math.floor(pos.qty / 2)
       setDialog({ side, symbol, qty })
     }
     ```
   - PositionsGrid 内部保留 `positionsQ`, 父级再单独拉一份共享 query (TanStack dedupe by key)

D. **StrategyTable 减列** (`StrategyTable.tsx`)
   - 删除 `<th>时限</th>` 和 StrategyRow 里对应的渲染
   - 在 "开始时间" 那个 cell 后面追加派生天数, 形式 `2026-10-01 · 7天`
   - `deriveRunDays` 是 lib/liveTradingTable 已 export 的函数 (见 line 1 imports)
   - 调整 `colSpan` 从 7 → 6 (在表格内)

E. **撤单 4 秒 timeout** (`LiveTradingOverview.tsx`, OrdersTable 函数)
   - `cancelMut` 加 `onSettled`: 4 秒后 `setConfirmCancelId(null)`, 同时 `cancelMut.reset()` 清错误
   - 计时器用 `useRef<number>` 记录, unmount 时 clear
   - 用户 4 秒内继续点 trash 切换其它单时, 清旧 timer
   - 不要 `onSuccess` 中清成功 — 网络 OK 后 4 秒也得用, 让 confirm 状态停留够长, 用户看清楚 "已成"

### Out

- 不动后端 (本轮纯前端)
- 不动 BellNotifications / EmergencyControls / TopBar / Sidebar 等已完成的 v2 组件
- 不动 RiskStats (本轮 status 已确认完成)
- 不改 OrderDialog 已有的 submit / 风控 / 拒单错误展示逻辑
- 不接买入快捷按钮 (PositionCard 只暴露 'sell'; 不为买加按钮, 防追高)
- 不动 StrategyTable 的 Tabs / 复选 / 频率汇总 — 只减列, 不重排
- 不做 PositionCard 的 onQuickTrade ratio='1/2' = qty/2 取整 + 余数留底仓的 "保留 100 股" 之类的精细逻辑 (整除就够; 余数归到下一笔或人工处理, spec 不在范围)

## 3. 数据契约

### 3.1 OrderDialog initial prop 类型 (前端, 新增)

```ts
interface OrderDialogInitial {
  /** 预填标的 (e.g. "600519.SH") */
  symbol?: string
  /** 预填方向; 'buy' | 'sell' */
  side?: 'buy' | 'sell'
  /** 预填数量 (股) */
  qty?: number
}
```

仅在 dialog 首次打开时被读取 (lazy useState initializer). Dialog 运行中改 initial 不变现有 draft (与现 OrderDialog 一致).

### 3.2 TradingPreferencesApi (后端, 不动)

`api.tradingPreferences()` -> `TradingPreferencesApi { trading_fee_rate: number, trading_fee_min: number, ... }`. OrderDialog 拉一次拿 fee_rate, staleTime 5min.

### 3.3 PositionCard onQuickTrade (已存在, 接线)

`onQuickTrade?: (side: 'sell', ratio: 1 | 2 | 'all', symbol: string) => void` — PositionCard 已定义, live PR1 留没接. 本轮补接.

### 3.4 StrategyTable 列变化 (用户可见)

| 现 (7 列) | 本轮 (6 列) |
|---|---|
| 名称 | 名称 |
| 频率 | 频率 |
| 状态 | 状态 |
| 今日 PnL | 今日 PnL |
| 开始时间 | 开始时间 (+ 派生天数 `deriveRunDays(s)` 拼接) |
| 时限 | (删) |
| 进入 (>) | 进入 (>) |

### 3.5 OrdersTable confirmCancelId 超时兜底 (新行为)

- 现状: `cancelMut.onSuccess` 立即 setConfirmCancelId(null)
- 本轮: `onSuccess` 不动, **新增** `onSettled` 4 秒后清状态
- 4 秒内网络错误: `onSettled` 也会触发, 清状态; 同时 `cancelMut.reset()` 把 isError / error 清掉, 用户可再次点 trash

## 4. API 契约

不变. `/api/trading/preferences` 是已有端点 (QK.tradingPreferences 在 queryKeys.ts line 120 已定义), OrderDialog 直接复用, 不新写.

## 5. 行为口径

### 5.1 OrderDialog 资金占用计算

- 限价委托 (`orderType === 'limit'`): `资金占用 = Math.max(qty, 0) * Math.max(price, 0)`. 其中 price = `priceText ? Number(priceText) : 0`. 任何一个为 0 时显示 `—`.
- 市价委托 (`orderType === 'market'`): `资金占用 = Math.max(qty, 0) * (last_price ?? 0)`. last_price 通过 `QK.tradingPositions` 缓存的同名 symbol 拿 (绝大多数场景下用户从 PositionCard 快捷进来, symbol 已经在 positions 里).
- 都不存在时显示 `—`, 不显示 NaN / Infinity.

### 5.2 OrderDialog 手续费计算

- `手续费 = 资金占用 * fee_rate`; 真实计算见 backend `services/trading/router.py:280` 风控, 这里只是前端估算, 标 "估算"
- fee_rate 0 (mock broker) 或 fee_rate 取不到时显示 `—`
- 写死 `fee_min` 透传? 不接 — 真成交后 backend 算出实际, 这里只是 preview, 简单 `资金占用 * fee_rate` 即可

### 5.3 PositionCard 快捷下单 qty 计算

- ratio='1/3' -> `Math.floor(position.qty / 3)` — 整除向下取整, 余数留底仓
- ratio='1/2' -> `Math.floor(position.qty / 2)`
- ratio='all' -> `position.qty` (全仓)
- 如果 qty === 0, 弹 toast "无可卖数量" 而不打开 dialog (UI 反馈)
- 如果 qty > 0 但 < 100, 仍打开 dialog, 用户可能想全部清掉 (允许 "0.x 手" 申报, 后端撮合层面按整百处理可; spec 不深究)

### 5.4 OrderDialog 状态提升

- 一份 dialog 实例, 多个触发源:
  - OrdersTable 的 "买入" / "卖出" 按钮: 传 `{ side: 'buy' }` / `{ side: 'sell' }` (无 symbol, 让用户输入)
  - PositionCard 的 卖 1/3 / 1/2 / 全卖: 传 `{ side: 'sell', symbol, qty }`
- 状态位置: 顶层 LiveTradingOverview, 与 PnLPanel / OrdersTable 同级
- 提交成功后: existing `submitMut.onSuccess` 调 `qc.invalidateQueries([...])` + `onClose()` — 改成 `onClose()` 关闭 dialog (`dialog` 设 null), 复用现有 onSuccess 不动

### 5.5 StrategyTable 列变更

- 删除 `<th>时限</th>` 和 StrategyRow 里对应 `<td>`
- 在 StrategyRow "开始时间" cell 里追加 ` · {n}天`, n > 0 才显示
- colSpan 7 → 6
- 同样在表格 colSpan 的 td 改 6

### 5.6 撤单 4 秒 timeout

```ts
const cancelTimerRef = useRef<number | undefined>(undefined)

const cancelMut = useMutation({
  mutationFn: (id: string) => api.tradingOrderCancel(id),
  onSettled: () => {
    window.clearTimeout(cancelTimerRef.current)
    cancelTimerRef.current = window.setTimeout(() => {
      setConfirmCancelId(null)
      cancelMut.reset()
    }, 4000)
  },
})

useEffect(() => {
  return () => window.clearTimeout(cancelTimerRef.current)
}, [])
```

- 用户 4 秒内切换 trash 到其它单: `setConfirmCancelId(otherId)` 触发 React 重渲染, 旧 timer 仍会跑 (会清掉 newId 设的 state). 解决: trash 按钮的 onClick 里先 clearTimeout(cancelTimerRef.current) 再 setConfirmCancelId(id)
- 用户 4 秒内连续点同一单的 trash (切换状态): 同样 clearTimeout 后再 setConfirmCancelId

### 5.7 navOrder / navOrder.test.ts

不动.

## 6. 风险与边界

- **R1** 取消 OrdersTable 内的 OrderDialog 实例可能影响向后兼容 (如果有测试 import OrdersTable 直接). 确认 grep 后无外部引用
- **R2** OrderDialog `initial` 改动是 additive (新可选 prop), 现有订单区按钮调用方不需要传, 行为不变
- **R3** `setSearchParams({ view: 'verify' })` 已经在 LiveTrading.tsx 完成, 与本轮无关. OrdersTable 里没有 searchParams 依赖
- **R4** StrategyTable 减列不影响 Sorting / Filtering / Tabs (它们都不依赖这个列). colSpan 减一: 用 colSpan=6 的 fallback 行 ("当前 tab 无策略") 仍能 cover 全宽
- **R5** 撤单 timeout 只在 UI 层兜底, 后端真实撤单行为不变. 用户 4 秒后看到 trash 图标并不意味着 cancel 失败 — invalidateQueries 仍在跑, 状态会刷新
- **R6** PositionCard 的 `sparkline data` 走 `QK['spark', symbol]`, 与快速下单无关, 不动

## 7. 测试矩阵

| # | 维度 | 命令 / 操作 | 通过条件 |
|---|------|-----------|---------|
| 1 | 类型 | `pnpm exec tsc -b --pretty` | 0 error |
| 2 | navOrder | `pnpm exec vitest run src/lib/navOrder` | 8 passed (不动) |
| 3 | LiveTrading | `pnpm exec vitest run src/pages/LiveTrading` | 14 passed (不动) |
| 4 | live 全组件 | `pnpm exec vitest run src/components/live` | 既有全过 (无新增) |
| 5 | 全量 | `pnpm exec vitest run` | 163 通过 (不引入新失败) |
| 6 | 构建 | `pnpm exec vite build` | ✓ |
| 7 | 路由 smoke | curl /live-trading/dividend_auntie | HTTP 200 |
| 8 | API smoke | curl /api/trading/preferences | 返回 trading_fee_rate: 数字 |
| 9 | 业务 (预览卡) | 浏览器: 委托区点 "买入" -> 输入 600519.SH / 价格 1500 / 数量 100 | OrderDialog 下方显示 "资金占用 ¥150,000" + "手续费 估算 ¥X (按 fee_rate)" |
| 10 | 业务 (持仓快捷) | 浏览器: 持仓卡片点 "卖 1/3" | OrderDialog 弹出, symbol 与数量预填, side='sell' |
| 11 | 业务 (StrategyTable 列) | 浏览器: /live-trading 列表页 | 不见 "时限" 列; "开始时间" 单元格后跟 `· 7天` |
| 12 | 业务 (撤单 timeout) | 浏览器: 委托区点 trash -> "确认撤单" -> 4 秒不动 | "确认撤单" 自动消失, 回到 trash 图标 |
| 13 | 业务 (撤单 error) | 浏览器 (devtools throttling 或后端 500 时) | 4 秒后按钮也复位 (不卡) |

## 8. 验收标准 (人工可跑)

```bash
cd /Volumes/quant-ssd/projects/tickflow-stock-panel/frontend
pnpm exec tsc -b --pretty
pnpm exec vitest run src/lib/navOrder src/pages/LiveTrading src/components/live
pnpm exec vite build

cd /Volumes/quant-ssd/projects/tickflow-stock-panel
./dev.sh start  # 已在跑, Vite HMR 自动应用

# 浏览器验收 §7 case 9-13
```

## 9. 改动文件预估

### 修改 (2 个)

| 文件 | 改动 | 预估行数变化 |
|------|------|--------------|
| `frontend/src/pages/live/LiveTradingOverview.tsx` | OrderDialog 加 initial + 预览卡 + 状态提升 + PositionCard 接 quick-trade + cancel timeout | +80 / -20 |
| `frontend/src/components/live/StrategyTable.tsx` | 删 "时限" 列, "开始时间" cell 拼派生天数, colSpan 7 → 6 | -15 / +5 |

### 不删不改

- BellNotifications, EmergencyControls, TopBar, Sidebar, PositionCard, RiskStats, CurrentStrategyPill, usePersistedState, VerifyAndDriftSection, CodeView, StrategySettings, shared

## 10. 不在范围 (防 scope creep)

- 不接买入快捷 (PositionCard 只暴露 'sell')
- 不做 OrderDialog "资金全部占用/可用余额不够" 硬拦截 (后端风控接, 不在前端 preview)
- 不做 "卖 1/2 + 保留 100 股" 的精确保留底仓逻辑 (整除即可)
- 不改 OrderDialog 拒单错误展示 / 风控字段渲染
- 不改 RiskStats (已完整)
- 不改 BellNotifications (已完整)
- 不动后端任何代码

## 11. 关联文档

- 上一轮: `docs/live-trading-v2-pr1-nav-and-wire.md` (commit 1a91e926)
- 设计结论: `docs/live-trading-v2-design-conclusion.md` §4 PR2 + PR3
- PR2+PR3 完整设计 (本次 4 项的源): `docs/live-trading-v2-pr2-pr3.md`
- 上轮拆分脚本: `docs/live-trading-v2-pr2-decompose-and-wire.md`
- 状态报告: `docs/live-trading-v2-pr2-pr3-status.md` (1a91e926 提交前)
