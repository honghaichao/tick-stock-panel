# live-trading-v2-pr1-nav-and-wire — 实盘侧栏入口 + v2 PR1 概览集成

> 状态: 待用户 review
> 前置: `docs/live-trading-v2-design-conclusion.md` § 4 PR1 设计定稿
> 同源任务: `docs/live-trading-restore-404.md` 已修路由, 本轮补 UI 入口
> 独立组件已就绪 (TopBar / PositionCard / BellNotifications / EmergencyControls /
> usePersistedState / CurrentStrategyPill), 无需新写

## 1. 目标

把"实盘交易哪去了"这个问题一次性解决:

1. **侧栏入口恢复**: 在 Layout 全局导航加 `实盘` 项, 一键到 `/live-trading`,
   `RootRedirectGate` 自动跳首个启用的策略详情 (D4)。
   - 仅恢复侧栏入口, 持久化逻辑 (`nav_order` / `nav_hidden`) 沿用现有机制,
     不动 `lib/navOrder.ts` 之外的排序语义。
2. **v2 PR1 概览集成**: 把 `LiveTradingOverview.tsx` (969 行) 里残留的旧版结构
   (10 列持仓表 + 4-tab CenterTabs + 内联 VerifyAndDriftSection + 内联
   MessagesStream) 替换为 v2 终态:
   - **持仓**: `<PositionCard>` 卡片网格 (默认 sm=1 col / md=2 / xl=3 / 2xl=4)
   - **VerifyAndDriftSection**: 移到 `?view=verify` query, 通过 Sidebar
     「策略配置 ▼ → 打开回测验证 (VerifyAndDrift)」入口触发
   - **CenterTabs**: 删, 它的 4 个子 view 全部直接 inline 在 Overview
   - **MessagesStream**: 删, 已由 TopBar 的 `<BellNotifications>` 接管
     (PR2 已就绪, 集成未被引用)

成功标准:
- 任何用户 (`/login` 已登录), 全局侧栏能看到 `实盘` 项 (8 项 → 9 项),
  点击进入 `/live-trading`, D4 守卫推送到首个策略详情,
  Overview 页第一行是持仓卡片网格 (1 行 1 票, hero 是 PnL) + 顶栏 hero 今日盈亏 + 救命三件套
- VerifyAndDriftSection 不再 inline 在 Overview, 只通过 ?view=verify 渲染
- CenterTabs + MessagesStream 这两个文件无 import 者 → 可安全删除
- pnpm tsc / vitest / 现有 14 个 LiveTrading 测试全过, 无新增 ruff/type 错

## 2. 范围

### In (本轮)

A. **侧栏入口** (`frontend/src/components/Layout.tsx` + `frontend/src/components/BrandIcons.tsx` + `frontend/src/lib/navOrder.ts`)
   - 加 `IconLive` 导出 (Brand 图标族, 24 网格, 等宽 2px, 直角语言)
   - `Layout.tsx` nav 数组顺序: 在 `/backtest` (idx 4) 后插一项 `/live-trading -> 实盘 -> IconLive`
   - `lib/navOrder.ts` 的 `DEFAULT_NAV_PATHS` 在 `'/backtest'` (idx 4) 后插入 `'/live-trading'`, 长度 19 -> 20
   - 同步 `lib/navOrder.test.ts` 的位置断言

B. **Overview 集成** (`frontend/src/pages/live/LiveTradingOverview.tsx`):
   - 删 `PositionsTable` -> 替换成 `<PositionCard>` 卡片网格
   - 删 `<VerifyAndDriftSection strategyId={strategyId} />` 内联段
   - 删 `<CenterTabs>` + 把 4 个子 view (PnLPanel / Orders+Trades /
     StrategyRunHistoryPanel-or-StrategiesPanel / LogsList) 直接 flat 渲染
   - 删 `<MessagesStream />` 段
   - 加 `?view=verify` 的条件渲染: `searchParams.get('view') === 'verify'` 时只显示 `<VerifyAndDriftSection>`, 否则不显示

C. **Sidebar 接线** (`frontend/src/pages/LiveTrading.tsx`):
   - 加 `useSearchParams`, 把 `setSearchParams({ view: 'verify' })` 传给 `<LiveSidebar onOpenVerify>`
   - `LiveSidebar` 的 `onOpenVerify` 是已存在的 prop, 不用改 Sidebar.tsx

D. **清理**:
   - `frontend/src/components/live/CenterTabs.tsx` — 若全仓 grep 0 个引用者, 删
   - `frontend/src/components/live/MessagesStream.tsx` — 若全仓 grep 0 个引用者, 删
   - 上述两个组件 import 在 `LiveTradingOverview.tsx` 删掉
   - `LiveTrading.tsx` PageHeader title 改用策略名 (PR3 残留), 调 `CurrentStrategyPill` 已有的 query cache (避免重复请求)

### Out (本轮不动, 留给 PR2/PR3/follow-up)

- 不动 `LiveTrading.tsx` 外壳 (60 行, 已稳定)
- 不动 `Sidebar.tsx` (3 tab + 配置抽屉, 已 PR1 完成)
- 不动 `TopBar.tsx` (hero + 救命 + Bell, 已完成)
- 不动 8 个独立 v2 组件 (PositionCard / BellNotifications / EmergencyControls / CurrentStrategyPill / usePersistedState / shared.tsx / CodeView / StrategySettings)
- 不动 `LiveTradingLayout.tsx` 的 D4 守卫逻辑
- 不动后端 (本轮纯前端)
- 不动 `LiveStrategyApi` 等前端类型契约
- 不引入新依赖

## 3. 数据契约

### 3.1 PositionCard 数据源 (不变)

直接复用现有 `QK.tradingPositions(strategyId ?? null)` -> `api.tradingPositions()` ->
`PositionApi[]`。原 PositionsTable 的 `lots` 合并逻辑 (plan/live 对齐展示) **不在本轮范围**:

- v1 (当前): PositionsTable 用表格左列 (实盘持仓 + 同 symbol 的 lots plan 并排)
- v2 PR1 (本轮): PositionCard 只显实盘持仓 (`positionsQ.data?.positions ?? []`),
  **临时丢 plan/lots 对齐展示**; PR2 (follow-up) 再 plan/lots 整合进 PositionCard
- 沟通: 现有 lots 仍有 `lotsQ` 查询在 `StrategiesPanel` 引用, 不动

### 3.2 `?view=verify` query 语义

- URL: `/live-trading/<id>?view=verify`
- 行为: `LiveTradingOverview` 全屏只渲染 `<VerifyAndDriftSection>`,
  持仓网格 / 委托 / 成交 / PnL 全隐
- 不在 URL 里持久 (不写 localStorage, 不影响浏览器后退逻辑)
- 与 `RootRedirectGate` (LiveTradingLayout line 33-67) 的 `?view=list` 两套独立 query, 不冲突

### 3.3 navOrder 顺序变更 (用户可见侧栏改动)

无 saved `prefs.nav_order` 的用户 (新装 / 从未调过菜单) 侧栏从 8 项 -> 9 项,
`实盘` 出现在 `回测` 与 `市场环境` 之间 (idx 5)。

**已有 saved nav_order 的用户**: 若他们的 saved 顺序里没 `/live-trading`,
现有 `Layout.tsx` 合并逻辑会按默认基准把它插到 `/backtest` 之后、
`/stock-analysis` 之前, 不破坏他们已保存的相对次序。

## 4. API 契约

不变。

## 5. 行为口径

### 5.1 图标 (IconLive)

- 含义: K 线 + 向上三角 (聚宽式"实盘信号"意象)
- 设计语言: 与 IconBacktest 同框 (24 网格, 等宽线 2px, butt 笔帽, miter 转角)
- SVG: 实 K 线主体 (rect, fill=currentColor) + 向上三角入场箭头 (path, 上箭头)
- 实现: `BrandIcons.tsx` 内紧跟 IconStrategy 后追加 (跟 trade-domain icon 集中), export 同形状

### 5.2 持仓卡片网格布局

- 容器: `<div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-3">`
- 卡片: 直接复用 `<PositionCard position={p} name={names[p.symbol]} onSelect={...} onQuickTrade={...} />`
- `onSelect`: 当前 Phase 不接 (遗留以后接策略详情页跳转), 传 `() => {}`
- `onQuickTrade`: 当前 Phase 不接 PR2 快捷下单 (`卖 1/3` 等按钮渲染但点击时弹 toast "v2 PR2 上线"), 不引新依赖

### 5.3 VerifyAndDriftSection 的 query 触发

- 在 `LiveTrading.tsx` 加 `const [searchParams, setSearchParams] = useSearchParams()`
- `<LiveSidebar active={tab} onChange={setTab} strategyId={strategyId} onOpenVerify={() => setSearchParams({ view: 'verify' })} />`
- `LiveTradingOverview` 接 `view` prop (`view: 'verify' | undefined`),
  `view === 'verify'` 时只返回 `<VerifyAndDriftSection strategyId={strategyId!} />`
- `?view=verify` 在 Overview 内部判, **不** 经 `LiveTradingLayout`, 避免影响列表页守卫

### 5.4 PageHeader 策略名 (PR3 一并)

- 当前 `LiveTrading.tsx` title 是 `实盘交易 · ${strategyId}` 显示 ID
- 改: 在 PageHeader 内部用 `useQuery` (key 与 `CurrentStrategyPill` 共享) 拿策略名, fallback 到 ID
- 实现在 `LiveTrading.tsx` 本地 (避免为 PageHeader 加新 prop), `CurrentStrategyPill` 也用同 query (已是)

### 5.5 navOrder 测试同步 (`lib/navOrder.test.ts`)

| 断言 | 旧 | 新 |
|---|---|---|
| `toHaveLength(19)` | 19 | **20** |
| `toBe('/')` @ idx 0 | ✓ | ✓ |
| `toBe('/stock-analysis')` @ idx 5 | ✓ | **(改成 idx 6)** |
| `toBe('/monitor')` @ idx 10 | ✓ | **(改成 idx 11)** |
| `toBe('/regime')` @ idx 11 | ✓ | **(改成 idx 12)** |
| `toBe('/review')` @ idx 16 | ✓ | **(改成 idx 17)** |
| `toBe('/data')` @ idx 18 | ✓ | **(改成 idx 19)** |
| `Set.size` 唯一性 | 19 | **20** |
| `defaultNavIndex('/data').toBe(18)` | 18 | **19** |

排序比较器那个测试 (6 路径相对顺序) 输出断言 **不变** — 这些路径相对顺序在新基础里也保持一致。

## 6. 风险与边界

- **R1** `LiveTradingOverview.tsx` 仍 969 行, 集成改动集中在一处 -> 文件预计降到 ~700 行
- **R2** `lotsQ` 数据只用于 StrategiesPanel, 不被 PositionCard 引用
- **R3** `LiveTradingOverview` 内部引用 `lotsQ` 的唯一位置是 PositionsTable — 删
  PositionsTable 后, lotsQ 不再被 Overview 引用
- **R4** 侧栏新增项在 1024px 以下屏幕 (`xl:` 断点之前) 会触发折叠 -> Layout 处理 rail mode 已就绪
- **R5** 当前 Layout 有 124 个 uncommitted backend 改动 -> 本轮只动前端, 不冲突
- **R6** `LiveTrading.tsx` 改 title 用 query, query 失败时 fallback 显示 strategyId (不退化)

## 7. 测试矩阵

| # | 维度 | 命令 / 操作 | 通过条件 |
|---|------|-----------|---------|
| 1 | 类型 | `pnpm exec tsc -b --pretty` | 0 error |
| 2 | 单测 (策略拆分) | `pnpm exec vitest run src/pages/LiveTrading` | 14 passed |
| 3 | 单测 (navOrder) | `pnpm exec vitest run src/lib/navOrder` | 全部通过 (位置更新见 § 5.5) |
| 4 | 单测 (full) | `pnpm exec vitest run` | 既有测试全过, 无新增失败 |
| 5 | 构建 | `pnpm exec vite build` | ✓ |
| 6 | 路由 | `curl -sI http://127.0.0.1:3011/live-trading` | HTTP/1.1 200 |
| 7 | 路由 (详情) | `curl -sI http://127.0.0.1:3011/live-trading/dividend_auntie` | HTTP/1.1 200 |
| 8 | 业务行为 (侧栏) | 浏览器 `/` 加载 | 看到 9 项菜单, 第 6 项 `实盘`, 点击进 `/live-trading` |
| 9 | 业务行为 (D4 守卫) | 浏览器 `/live-trading` | 自动 redirect 到 `/live-trading/dividend_auntie` |
| 10 | 业务行为 (持仓卡片) | 浏览器 `/live-trading/dividend_auntie` | 持仓行变成 1 行 1 票卡片网格 |
| 11 | 业务行为 (Verify 出口) | 浏览器 `/live-trading/<id>?view=verify` | 只显示 VerifyAndDriftSection |
| 12 | 业务行为 (顶栏) | 浏览器 `/live-trading/<id>` | TopBar 1 行: hero 今日盈亏 + 总资产/累计/状态 + Bell + 救命三件套 |
| 13 | 业务行为 (PageHeader) | 浏览器 `/live-trading/dividend_auntie` | title 显示 `实盘交易 · 菜场大妈活跃股版 (...)` 而非 `实盘交易 · dividend_auntie` |

## 8. 验收标准 (人工可跑)

```bash
cd /Volumes/quant-ssd/projects/tickflow-stock-panel/frontend
pnpm exec tsc -b --pretty              # 期望: 0 error
pnpm exec vitest run src/lib/navOrder  # 期望: 6 全过
pnpm exec vitest run src/pages/LiveTrading  # 期望: 14 全过
pnpm exec vitest run                   # 期望: 既有全过

pnpm exec vite build                   # 期望: ✓ built

cd /Volumes/quant-ssd/projects/tickflow-stock-panel
./dev.sh start                         # 期望: backend 3018 + frontend 3011 都起
# 浏览器跑 8 / 9 / 10 / 11 / 12 / 13
```

## 9. 改动文件预估

### 新增 (0 个)

无。`IconLive` 加在 `BrandIcons.tsx` 内。

### 修改 (5 个)

| 文件 | 改动 | 预估行数变化 |
|------|------|--------------|
| `frontend/src/components/BrandIcons.tsx` | + IconLive | +10 |
| `frontend/src/components/Layout.tsx` | + IconLive import + nav 数组 +1 项 | +3 |
| `frontend/src/lib/navOrder.ts` | DEFAULT_NAV_PATHS 插入 +1 项 | +1 |
| `frontend/src/lib/navOrder.test.ts` | 6 处位置断言 | ~+5 |
| `frontend/src/pages/LiveTrading.tsx` | 加 useSearchParams, 接 onOpenVerify, 改 title | +12 |
| `frontend/src/pages/live/LiveTradingOverview.tsx` | 删 PositionsTable / CenterTabs / MessagesStream 内联, 加 Card grid, 加 view==='verify' 分支 | -250 ~ -280 |

### 删除 (2 个文件, 待 grep 0 引用后)

| 文件 | 原因 |
|------|------|
| `frontend/src/components/live/CenterTabs.tsx` | 集成后无引用 |
| `frontend/src/components/live/MessagesStream.tsx` | 集成后无引用 |

## 10. 不在范围 (显式, 防 scope creep)

- 不动 LiveTrading.tsx 外壳的 5 tab 路由, 不重新设计 Sidebar
- 不改 PositionCard 内部 (PR2 快捷接 OrderDialog 是 PR2 的事)
- 不改 BellNotifications 内部 (PR2 已经定型)
- 不接 ?view=verify 之外的 query (?view=positions / ?view=orders 留给后续)
- 不动后端任何文件
- 不写新后端 endpoint
- 不做 plan/lots 数据对齐 (PR2 范围)
- 不改 nav_order / nav_hidden 持久化机制
- 不写新 i18n key (现有菜单项全是中文硬编码, 沿用)

## 11. 关联文档

- 设计结论: `docs/live-trading-v2-design-conclusion.md` (PR1 详细设计)
- 拆分: `docs/live-trading-v2-pr2-decompose-and-wire.md` (本轮基于该拆分结果)
- 路由恢复: `docs/live-trading-restore-404.md` (前置, 路径已通)
- PR1 原文: `docs/live-trading-v2-pr1.md` (本次实现的对象)
- PR2/PR3 设计: `docs/live-trading-v2-pr2-pr3.md` (本轮不动, 留给后续)
- navOrder 设计: `frontend/src/lib/navOrder.ts` (修改 DEFAULT_NAV_PATHS)
