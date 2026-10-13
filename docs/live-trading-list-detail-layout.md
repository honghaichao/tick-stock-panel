# 实盘 列表→详情 (Live Trading List→Detail Layout)

> 状态: **PR-1 (后端) + PR-2 (前端) + §5.2.2 (持仓独立) 全部落地** (user 在 round-4 确认都做; smoke test 全通过)
> 子任务 `docs/live-trading-strategy-summary.md` B1-B5 已锁定: B1=registry 启动时间 / B2=独立 pnl_strategy_snapshots.jsonl 流 / B3=不联通通知引擎 / B4=trash 回收站 / B5=立即 cancel 当前 run
> 关联: 复用 `live-trading-5page-layout.md` 已落地的 5 tab 详情视图
> PR 顺序: PR-2 (前端, 本任务) 先合; PR-1 (后端子任务) 后合; 后端上线后在前端代码解占位
> 本任务前端-only 范围: 列表 = 策略表格 (聚宽风格) + tabs + 行点击进详情 + 复选删除 UI; 详情 = 现 5 tab 搬到 `/live-trading/:strategyId` + Overview tab 持仓独立成 Section + 3 个全局 tab 加 `<GlobalAccountBadge />`; 表格 6 列数据先 `—` + tooltip
---

## 0. 待 user 确认的设计点 (默认值, 可调整)

| # | 决策点 | 默认方案 | 备选 |
|---|---|---|---|
| **D1** | 列表页主结构 | **策略表格 (JoinQuant 风格)**: PageHeader 含 [新建策略] [删除] 按钮 + Tabs (全部/进行中/已关闭) + 表头 + 行 (列见 §5.1); 行点击 → `navigate(/live-trading/<id>)` | A. 卡片网格; B. 单列简单列表; C. 旧 5 tab 直接拆分 |
| **D2** | 详情页内容 | **现有 5 tab 整体搬到 `/live-trading/:strategyId`**, 概览/统计/日志是全局 (账户共享), 代码/设置是 per-strategy; **概览/统计/日志 tab 内容区顶部固定显示 "全局账户视图" 标签** | A. 详情页只做 4 个 per-strategy 项; B. 加全局/策略切换器 |
| **D3** | 行密度 + 表格交互 | 表格行点击 = 直接进详情 (不再就地展开 schedules); 复选框勾选 + [删除] 实现批量删除 (后端需 `DELETE /strategies/{id}`, 待确认) | A. 行点击就地展开 schedules (保留旧 StrategyRow); B. 行点击进详情但就地弹 schedules |
| **D4** | 旧路由兼容 | `/live-trading` 自动 redirect 到 `/live-trading/<第一个启用的策略或首个策略>` | A. 列表页直接渲染 (旧书签失效) |

> 历史决策: round-2 写过"列表页 = 账户 KPI 行 + 持仓行 + 策略卡片主菜"三段式 (~/tmp/live-trading-list-detail-layout.round2.md 备份), 已被 round-3 推翻. 原因: user 给的聚宽参考图, 列表页是**纯策略表**而不是仪表盘组合.

---

## 1. 目标

```
/live-trading                                → 策略列表 (list page = 策略表格)
/live-trading/:strategyId                    → 策略详情 (detail page, 含原 5 tab)
```

- **列表页**: 一眼看到所有实盘策略 (含频率/状态/最近一次运行/开始时间 等元数据); 表格行可勾选批量删除; tabs 切换全部/进行中/已关闭; 行点击直接进对应策略的详情页
- **详情页**: 复用现有 5 tab; 概览/统计/日志是账户级数据 (顶部加 "全局账户视图" 标签), 代码/设置是 per-strategy; Overview 内部重新组织, 让"当前持仓"作为单独 row (而不是挤在 KPI 与曲线之间)

成功标准:
- `/live-trading` 渲染策略表格; 点击任意行 → URL 跳 `/live-trading/:strategyId`, 详情页加载对应策略
- tabs (全部/进行中/已关闭) 切换仅过滤前端渲染, 不重新拉接口
- 复选 + [删除] 触发批量删除请求, 成功后表格行减少
- 详情页 5 tab 与改造前等价; Overview tab 内持仓独立成 row (视觉上是一个 Section, 不混在曲线/委托里)
- 详情页深链可定位到 "策略 X 的代码 tab" (query `?tab=code`)
- 默认 `/live-trading` 直接进第一个启用的策略 (D4)

---

## 2. 范围

### 2.1 In

- **新增路由** `live-trading/:strategyId` + **list route** 改造 (列表页)
- **列表页结构改造** (取代当前 LiveTrading 主组件的 5 tab):
  - PageHeader 含 [新建策略] [刷新] [删除 (按选中数量变化)]
  - Tabs: 全部 / 进行中 / 已关闭 (前端 filter, 不新增 endpoint)
  - 表格列: ☑ checkbox / 名称 / 频率 / 状态 / 开始时间 / 累计收益 / 年化收益 / 今日收益 / 最大回撤 / 微信通知 / 时限 (后 6 列暂用占位 `—`, 见 §4)
  - 表头固定, 行 hover 高亮 + 鼠标变 pointer
  - 行整体可点击 (整行套 Link 或 onClick navigate), 复选框单独拦截 click
  - 删除按钮触发 `DELETE /strategies/{id}` (后端接口待确认, 见 §4)
- **详情页 5 tab 改造**:
  - 路径变为 `/live-trading/:strategyId`
  - 内部 `LiveSidebar` / `CodeView` / `StrategySettings` 等复用, 只改父组件取 strategyId 的方式
  - Overview tab 内部重组: `TopBar 8 KPI → 持仓 Section (独立 row) → 收益曲线 → 委托/下单` (持仓搬出来, 是 user 上轮反馈 "持仓单独一行" 的正确落点)
  - Overview / Stats / Logs tab 顶部加 `<GlobalAccountBadge />` (D2 默认)
  - Sidebar 加 "← 返回策略列表" 链接
- **新建策略**: 沿用现 EmptyHint 提示 ("把 .py 放到 `data/user_data/strategies/live/` 后刷新"), 不做对话框; 表格顶部 [新建策略] 按钮 = 刷新 + 显示 toast 提示路径

### 2.2 Out (明确不做)

- 表格 6 列 (开始时间 / 累计 / 年化 / 今日 / 最大回撤 / 微信通知) 的**后端实现**: 移到 `docs/live-trading-strategy-summary.md` 子任务 (已经按 user 选择 C 选项落地)
- 表格 6 列的**前端渲染**: 本任务代码先以 `—` 占位 + tooltip 渲染; 子任务 backend PR 合入后, 在本任务 PR 内解掉占位读新字段 (顺序见 §4.2)
- 通知引擎联通 (B3 = false; `live-trading-notification.md` 后续单独任务)
- 删除回收站 (B4 = false; 不实现 "被删除策略放到 trash" 流程, 直接 deregister)
- 详情页 per-strategy PnL / 收益曲线 (现有 pnl_snapshots 是账户级的, 不按策略拆)
- 通知中心 / 监控中心接通

---

## 3. 数据契约

### 3.1 现有 `LiveStrategyApi` 字段 (后端 `GET /strategies`)

```ts
export interface LiveStrategyApi {
  id: string                          // 来自目录名/类名, 例 'dividend_auntie'
  name: string                        // 显示名
  enabled_default: boolean            // 注册时的默认启停
  schedules: StrategyScheduleApi[]    // event 名 + cron 时间 + enabled
  next_run: string | null             // 调度器预算的下次运行时间
}
```

### 3.2 表格列与字段映射

| 列 | 数据来源 | 当前可用? | 后端缺什么 |
|---|---|---|---|
| ☑ checkbox | 客户端 state | ✓ | — |
| 名称 | `LiveStrategyApi.name` | ✓ | — |
| 频率 | 派生: `summarizeFrequency(s.schedules)` → `每日 N 次` / `每周 X @ HH:MM` / `自定义` | ✓ (前端派生) | — |
| 状态 | `enabledCount > 0 ? '运行中' : (schedules.length > 0 ? '已停用' : '占位')` | ✓ (前端派生) | — |
| 开始时间 | **缺** | ✗ | 需要 `LiveStrategyApi.started_at` 或扫 `strategy_runs.jsonl` 最早 run |
| 累计收益 | **缺** | ✗ | 需要 per-strategy PnL (现 `pnl_snapshots.jsonl` 无 `strategy_id`) |
| 年化收益 | **缺** | ✗ | 同上 |
| 今日收益 | **缺** | ✗ | 同上 |
| 最大回撤 | **缺** | ✗ | 同上 |
| 微信通知 | **缺** | ✗ | 需要 per-strategy 通知开关后端字段 |
| 时限 | 派生: `now - 最早 started_at` (需 started_at); 暂用 `started_at` 替 | 缺 | 同开始时间 |

**user 已在 round-3 选择 "C 选项 (扩 PR 到 backend)"**, 因此本任务中表格的 6 列数据来源移到子任务 `docs/live-trading-strategy-summary.md`. 在子任务完成前, 这 6 列先渲染 `—` + tooltip 解释 ("等待 `live-trading-strategy-summary.md` 落地, 见 §3.2/§4"). 子任务完成后, 字段映射更新为:

- 开始时间 ← `GET /strategies[].started_at` (派生自 `strategy_runs.jsonl` earliest)
- 累计 / 年化 / 今日 / 最大回撤 ← `GET /strategies[].summary.{...}` (JOIN fills × orders 按 strategy_id 聚合)
- 微信通知 ← `GET /strategies[].notify_wechat` (读 prefs.json)
- 时限 ← 用 `started_at` 派生 (now - started_at)

子任务落地后, 本任务 §3.2 / §4 / §5.1 同步去掉所有 `—` 占位与 fallback 措辞, 改为直接读新字段.

### 3.3 复用现有纯函数 (与 `splitStrategiesByStatus` 平级)

```ts
// 派生"频率" (聚合所有 enabled schedule)
// 例: [{h:9,m:30,enabled:true},{h:14,m:0,enabled:true}] → "每日 9:30, 14:00"
export function summarizeFrequency(schedules: StrategyScheduleApi[]): string

// tabs 过滤
export type StrategyTab = 'all' | 'active' | 'stopped'
export function filterStrategiesByTab(
  list: LiveStrategyApi[], tab: StrategyTab,
): LiveStrategyApi[]
```

---

## 4. API 契约

### 4.1 现有 (沿用)

- `GET /api/trading/strategies` — 表格数据源 (D1) + tabs 过滤
- `GET /api/trading/strategies/{id}/runs` — 详情页 5 tab (代码 tab 用不到 runs; 现保留)
- `GET /api/trading/account` — 详情页 TopBar / Overview (KPI)
- `GET /api/trading/positions` + `GET /api/trading/lots` — 详情页 Overview 持仓 Section
- `GET /api/trading/pnl/history` / `/pnl/metrics` / `/pnl/risk_metrics` — 详情页 收益曲线 + Stats
- `GET /api/trading/strategies/{id}/code` / `PUT` — 详情页 代码 tab

### 4.2 新增 (在子任务 `live-trading-strategy-summary.md` 落地)

| 接口 | 方法 | 用途 |
|---|---|---|
| `GET /api/trading/strategies` | GET (增强) | 增加 `started_at` / `notify_wechat` / `summary` 字段 |
| `POST /api/trading/strategies/{id}/notify` | POST | 微信通知列 ON/OFF 切换 |
| `DELETE /api/trading/strategies/{id}` | DELETE | 表格行勾选 + [删除] 按钮 |

**确定**: user 在 round-3 选 "C 选项 (扩 PR 到 backend)". 本任务 (§3.2 提到的 6 列) 依赖 `live-trading-strategy-summary.md` 完成才能填值. 子任务在那份文档里详细定义了 endpoint / 数据契约 / 算法 / 测试. PR 落地后, 本任务的:

- §3.2 表格列字段映射 (`—` 占位) 改为读新字段
- §5.1 表格渲染逻辑改为读 `summary` 7 字段, 通知列用 `notify_wechat` 切换
- §7 测试矩阵补充对应测试

**实施顺序** (user 在 round-4 选 "PR-2 先合"):

1. **PR-2 先合** (本任务, frontend only): 列表页表格 + 路由 + 详情页 5 tab 改造, 6 列渲染 `—` + tooltip, [删除] UI 调用 `DELETE` (后端未上线时 fallback 为前端软删除), 通知列 read-only 渲染
2. **PR-1 后合** (子任务 `live-trading-strategy-summary.md`, backend): 实现 `GET /strategies` 增强 + `POST /notify` + `DELETE` + B1-B5 算法 (trash 回收站, 立即 cancel, pnl_strategy_snapshots 流, registry started_at)
3. **PR-2 后续 patch** (在本任务代码里): 把 `—` 占位改为读新字段, [删除] 切真接口, 通知列加 toggle

任一 PR 单独可 review 可合. PR-2 + PR-2 patch 也可以合为一个 PR (把后端等前端一起上).

---

## 5. 行为口径

### 5.1 列表页 (策略表格)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ 实盘策略                         [+ 新建策略]  [刷新]   [删除 (n 选中)]      │  ← PageHeader
├──────────────────────────────────────────────────────────────────────────────┤
│ 全部 | 进行中 | 已关闭                                                       │  ← Tabs (默认 '全部')
├──────────────────────────────────────────────────────────────────────────────┤
│ ☐ │ 名称          │ 频率        │ 状态     │ 开始时间 │ 累计 │ 年化 │ ... │  ← thead
├──────────────────────────────────────────────────────────────────────────────┤
│ ☐ │ dividend_...  │ 每日 9:35   │ 运行中   │ —        │ —    │ —    │     │  ← row (click → detail)
│ ☐ │ momentum_v2   │ —           │ 已停用   │ —        │ —    │ —    │     │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **PageHeader**: 标题 "实盘策略"; 右侧操作 [新建策略] [刷新] [删除 (n)] (n=0 时禁用/隐藏)
- **[新建策略]**: 弹一个 toast `把 .py 放到 data/user_data/strategies/live/ 后点刷新` (沿用现 EmptyHint 文案)
- **Tabs**: `全部 | 进行中 | 已关闭`, 默认 '全部'; 切换只调前端 `filterStrategiesByTab`, 不重新请求
- **表头**: 列见 §3.2; 缺字段列 (`开始时间 / 累计 / 年化 / 今日 / 最大回撤 / 通知`) 表头标灰色 + tooltip "需要后端补 X"
- **行整体可点击** (cursor-pointer, hover 抬一格); 复选框单独拦截 click `stopPropagation`
- **复选 + 删除**:
  - 选中 → header [删除 (n)] 出现/计数
  - 点删除 → 二次确认 Modal (避免误删)
  - 确认后: 由于 §4.2 不新增 DELETE, 默认行为 = 把选中策略移到 "已关闭" tab (前端 filter 模拟); 若后端补 DELETE 接口再切到真实删除
- **0 策略**: 表格区显示 "还没有注册的策略. 把 .py 放到 `data/user_data/strategies/live/` 后点刷新."
- **路由**: 行点击 → `navigate(/live-trading/<id>)` 替换当前栈 (`replace: false`, 用户可用浏览器后退)

### 5.2 详情页 (`/live-trading/:strategyId`)

#### 5.2.1 整体框架

- 顶: `← 返回策略列表` (onClick → `navigate('/live-trading')`)
- 当前策略横条: `<策略名> · <id> · <启停状态>`
- `LiveTopBar` (8 KPI, 沿用)
- 主体: `LiveSidebar` (200px) + 右侧 tab 内容区
- 5 tab: overview / stats / logs / code / settings (与现 Sidebar TABS 完全一致)

#### 5.2.2 Overview tab 内部重组 (user 上轮反馈 "持仓单独一行" 落点)

```
┌── TopBar 8 KPI ─────────────────────────────────────┐  ← 沿用现 LiveTopBar (不变)

┌── 当前持仓 (独立 row, Section) ────────────────────────┐  ← 提升为一个 Section, 不与曲线混
│  标的  数量  成本  现价  市值  盈亏  ...             │
│  ...                                                  │
└────────────────────────────────────────────────────────┘

┌── 收益曲线 ─────────────────────────────────────────┐  ← 沿用
│  equity_curve                                         │
└────────────────────────────────────────────────────────┘

┌── 委托 / 下单 ──────────────────────────────────────┐  ← 沿用
│  ...                                                  │
└────────────────────────────────────────────────────────┘
```

视觉: 持仓 Section 与其他 Section 之间用 `space-y-6`, 边框/标题一致; Section 内部用现 § 4 (LiveTrading.tsx 第 123-235 行) `<Section>` 组件.

#### 5.2.3 Stats / Logs / Code / Settings tab

- 全局账户视图 (D2): Stats / Logs / Overview tab 内容区顶部插入 `<GlobalAccountBadge />`
  - 视觉: `<div className="text-xs text-muted-foreground mb-2">全局账户视图 (与策略无关)</div>`
  - 实现: 简单函数组件, 无 props, 不依赖 strategyId
- Code / Settings tab: 仍是 per-strategy (现 `CodeView` / `StrategySettings` 已接 strategyId)
- Sidebar 加 "← 返回策略列表" 链接 (置顶, fixed)

### 5.3 默认 Redirect (D4)

- 命中 `/live-trading` (无 strategyId): 等待 `/strategies` 加载后, `navigate(replace, '/live-trading/<firstId>')`
  - "首个启用的策略" = 调度器 active (有 schedules 且启用中); 若没有则 strategies 列表第一项
- 实现: 父组件 `LiveTradingLayout.tsx` 内 `useEffect([strategies])` 处理
- 加载中: 全屏 loading (避免 redirect 闪屏)
- 加载失败 / 0 策略: 仍渲染列表页 (不强制 redirect)

### 5.4 (删除原 § 5.4: 表格不需要 IntersectionObserver)

- 表格行不预拉 runs (详情页才拉), 无 N+1

---

## 6. 风险与边界

- **后端字段缺口**: 表格 6 列 (`开始时间 / 累计 / 年化 / 今日 / 最大回撤 / 微信通知`) 后端不存在. 默认渲染 `—`. user 决定是否扩 PR 到 backend (见 §4.2).
- **删除接口缺口**: 表格复选 + [删除] 没有 `DELETE /api/strategies/{id}`. 默认 = 前端软删除到 "已关闭" tab. user 决定是否扩 PR.
- **D2 "全局账户视图" 标签**: 必须出现在 Stats / Logs / Overview 三个全局 tab 顶部. 漏标 → 用户切策略时误读账户 PnL 为 "策略 PnL"
- **D4 redirect flash**: 用户首次进 `/live-trading` 看到列表页一帧 (带 row loading), 然后跳详情; 加载中态避免突兀 flash
- **历史 `StrategyRow` 组件 (`LiveTrading.tsx:968`)**: 现"展开 schedules"行为替换为表格行点击进详情. 该组件在 5 tab 上下文里仍可用, 但本次重构将表格组件独立 (`<StrategyTable />`), 不复用旧 StrategyRow 的展开 UI
- **`splitStrategiesByStatus` / `strategyIsExpanded` 纯函数 (§5.1 中由 tabs 过滤替代)**: 旧函数对应"运行时启用/停用"语义, 与新"全部/进行中/已关闭"语义重合, 但粒度不同 (旧 = schedules 启停; 新 = tab 过滤). 保留旧函数以防回滚; 表格新增 `filterStrategiesByTab`
- **Overview tab 持仓独立后**: 详情页首屏 (Overview tab) 高度可能比改造前稍高 (多一个 Section 头), 但视觉更清晰 (持仓不被曲线/委托挤掉)
- **表格行 hover 整行 + 复选框 click 拦截**: 实现细节, 整行套 onClick, 复选框套 onClick + stopPropagation. 无障碍: 复选框是 `<input type="checkbox">` 而不是 div

---

## 7. 测试矩阵

| 用例 | 期望 |
|---|---|
| `GET /live-trading` 0 策略 | 渲染表格 + 空状态文案 |
| `GET /live-trading` N 策略 | 表格 N 行; 自动 redirect 到 `/live-trading/<firstActive\|first>` (D4) |
| tabs 切换 全部→进行中 | 前端 filter, 不重新请求, 行数减少 |
| tabs 切换 全部→已关闭 | 前端 filter, 行数 = 已停用策略 |
| 行点击 | URL 变 `/live-trading/<id>`, 详情页加载 |
| 复选框 click | 不触发行点击 (stopPropagation); [删除 (n)] 计数 +1 |
| [删除] 点击 | 弹二次确认 Modal; 默认 = 软删除到 "已关闭" tab (前端 filter) |
| 详情页 `/live-trading/<id>` | Overview tab 默认激活 (沿用 `localStorage('liveTab5')`); 持仓 Section 独立成 row (不在曲线下面挤) |
| 详情页 `/live-trading/<id>?tab=code` | code tab 激活, localStorage 被覆盖为 code |
| 详情页 ← 返回策略列表 | URL 回 `/live-trading/<firstId>` (D4 自动 redirect) |
| 详情页 strategyId 不存在 | 行点击后, detail 页检测 id 不在 strategies → redirect `/live-trading` + toast "策略 <id> 已不存在" |
| 表格 6 个缺字段列 | 渲染 `—` + tooltip 说明; 不报错 |
| 性能测试: 表格 50 行 hover 流畅 | DOM 渲染 < 16ms / 行, 不卡顿 |

### 7.1 前端单元测试

**PR-2 落地**: `frontend/src/lib/liveTradingTable.test.ts` — 21 单测, 覆盖 `filterStrategiesByTab` / `summarizeFrequency` / `strategyTabCounts` / `selectDefaultStrategyId` / `deriveRunDays` (含边界: 0 schedules placeholder / 全停用 / mixed / 未来时间等).

**未做** (项目无 `@testing-library/react` 依赖, 不引入):
- 组件层 smoke test (因为 RTL 没装, 维持纯函数测试约定)
- LiveTradingList / StrategyTable 渲染交互测试 — PR-2 patch 阶段考虑加 (要么装 RTL, 要么保留单测 + manual smoke)

保留现 2 个 vitest: `LiveTrading.split.test.ts`, `LiveTrading.expand.test.ts`.

---

## 8. 验收标准

```bash
# 前端
cd frontend
pnpm build
pnpm exec vitest run src/pages/LiveTrading src/pages/LiveTradingList
# 期望: build 通过; vitest 5 个文件 (原 2 + 新 3 单测) 全绿

# 端到端
./dev.sh start
# 浏览器 http://localhost:3011/live-trading:
#   1. 表格展示 N 策略; tabs 全部/进行中/已关闭可切
#   2. 勾选 2 行 → header [删除 (2)] 出现
#   3. 点行 dividend_auntie → URL /live-trading/dividend_auntie, 详情页加载
#   4. 概览 tab 默认激活, 顶部 "← 返回策略列表" 可见
#   5. 持仓 Section 独立成 row (在曲线之上), 不混在委托区
#   6. 切 "代码" tab → 显示 dividend_auntie.py, 不串到其他策略
#   7. 点 ← 返回 → URL 回 /live-trading/<first>
#   8. 刷新 /live-trading/dividend_auntie?tab=code → 直接进 detail code tab
```

### 8.1 旧路由兼容

- 浏览器历史栈: `/live-trading` (redirect) → 详情 → 后退 → 回 `/live-trading` → 再次 redirect (react-router replace 处理, 不报错)
- 直接刷新 `/live-trading/<id>`: 仍渲染对应策略详情
- 直接刷新 `/live-trading`: 自动跳详情, 表格 (1 帧 loading) 然后跳走, 可接受

---

## 9. 改动文件

### 前端 (PR-2 已落地)

| 文件 | 状态 | 说明 |
|---|---|---|
| `frontend/src/router.tsx` | 改 | 加 `/live-trading/:strategyId` 路由; CORE_ROUTE_PATHS 加白名单 |
| `frontend/src/lib/api.ts` | 改 | `LiveStrategyApi` 加 3 optional 字段; 新增 `tradingDeleteStrategy` / `tradingSetStrategyNotify` 客户端 |
| `frontend/src/pages/LiveTradingLayout.tsx` | 新 | 路由层: D4 redirect 守卫 + 列表/详情切换 |
| `frontend/src/pages/LiveTradingList.tsx` | 新 | 列表页: PageHeader + 表格 + tabs + 复选 + 二次确认删除 |
| `frontend/src/components/live/StrategyTable.tsx` | 新 | 表格组件: tabs + header + row + status badge |
| `frontend/src/lib/liveTradingTable.ts` | 新 | 纯函数: `filterStrategiesByTab` / `summarizeFrequency` / `strategyTabCounts` / `selectDefaultStrategyId` / `deriveRunDays` |
| `frontend/src/lib/liveTradingTable.test.ts` | 新 | 21 单测覆盖纯函数 |
| `frontend/src/pages/LiveTrading.tsx` | 改 | 接收 `strategyId` prop (来自 `useParams`/Layout), `CodeContent`/`SettingsContent` 用之; `StatsContent`/`LogsContent` 加 `globalBadge` 渲染 `<GlobalAccountBadge />`; PageHeader 加 `<CurrentStrategyPill>` |
| `frontend/src/components/live/Sidebar.tsx` | 改 | 新增 `strategyId` prop; 提供时顶部显示 "← 返回策略列表" 链接 |
| `frontend/src/components/live/CodeView.tsx` / `StrategySettings.tsx` | 不动 | 已是 per-strategy |

#### §5.2.2 Overview tab 重组 (持仓单独一行) — **已落地** (在 round-4 顺势合并)

`OverviewContent` (`LiveTrading.tsx`) 改写为 `space-y-4` 包装: 顶 row `<PositionsTable />` (自携 `<Section title="当前持仓">`); 下方 12 列 grid 改成 `col-span-9 + col-span-3` (MessagesStream 横向收窄)

PR-2 范围是"列表→详情"的导航 + 路由 + 表格, 未触及 detail Overview 内部布局.
`<PositionsTable />` 在 Overview 3-column grid 内 (`LiveTrading.tsx:1112-1124`).

**建议后做** (PR-2 patch 或单独 PR):
- 抽出 `<PositionsTable />` 到独立 row
- 顶部加 "持仓独立 Section" 行
- 下方是 收益曲线 / 委托 / 下单

#### PR-1 落地 — `docs/live-trading-strategy-summary.md` 全栈 (B1-B5 已确认)

- `GET /api/trading/strategies` 增强 3 字段 (started_at / notify_wechat / summary)
- `POST /api/trading/strategies/{id}/notify` 新
- `DELETE /api/trading/strategies/{id}` 新 (B4 trash + B5 accept race)
- 后端 12 单测全绿 (`tests/test_strategy_summary.py`)
- 端到端 smoke: 增 → 验 → 删 → trash 全 OK

#### Round-5 跟进 (user 在上一轮确认 A)

A. **修 strategy 真实 bug + summary 流接入 executor 闭环**:
- `data/user_data/strategies/live/dividend_auntie.py` line 235: `pl.col("listing_date") <= context.trade_date` → `pl.col("listing_date") <= pl.lit(context.trade_date).cast(pl.Date)` (polars 1.x Series-vs-py-date 比较必须 inflate)
- `data/user_data/strategies/live/dividend_auntie_universe.py` 同位置同改
- `backend/app/services/trading/executor.py:_snapshot_after_run` 后追加 `pnl.write_strategy_snapshot(...)` — 每个策略每次 run 后写一条 pnl_strategy_snapshots.jsonl
- 验证: `verify` 0 errors; `run-now pre_market` 200 → `pnl_strategy_snapshots.jsonl` 真生成一条 → `GET /strategies` 的 summary 不再 None
- ✅ `annual_return` fallback 已加 (round-6): days < 30 时返回 `null`, 前端 StrategyTable 渲染 "—". 30 天后才计算 (1+r)^(365/days)-1. 实测 dividend_auntie summary.annual_return=null 而非 66M%. pytest 新加 `TestComputeStrategySummaryAnnualGuard` 3 个 case, 15/15 全绿.

#### PR-2 解占位 (round-4 已生效)

- 表格 6 列 `—` 占位自动读 `LiveStrategyApi` 新字段
- notify 列: 后端写入即生效, 当前前端保持 readOnly (待 `tradingSetStrategyNotify` UI 接通)
- 删除 button: 后端写入即生效 (前端软删 fallback 仍然保留, 防御性)

### 后端

无 (本任务前端 only). 后端 6 列的接口实现见 `docs/live-trading-strategy-summary.md` 子任务. PR 顺序: 先合子任务, 再合本任务解占位 (或先合本任务, 后端接口上线前前端先 `—`).

### 文档

- `docs/live-trading-list-detail-layout.md` (本文件, round-3 落地)
- 更新 `操作说明书.md` / README "实盘" 一节: 列表页是策略表格, 详情是 5 tab

---

## 10. 设计图

### 列表页 `/live-trading`

```
┌───────────────── PageHeader ─────────────────────────┐
│  实盘策略           [+ 新建策略] [刷新] [删除 (n)]   │
└─────────────────────────────────────────────────────┘

┌─────────── Tabs ───────────────────────────────────┐
│  ● 全部 (5)  │  进行中 (3)  │  已关闭 (2)          │
└─────────────────────────────────────────────────────┘

┌──────────────── 表格 ────────────────────────────────┐
│ ☐ │ 名称              │ 频率        │ 状态    │ ...  │
├───┼───────────────────┼─────────────┼─────────┼─────┤
│ ☐ │ dividend_auntie   │ 每日 9:35   │ 运行中  │ ...  │  ← hover 高亮 + click → detail
│ ☐ │ momentum_v2       │ 自定义      │ 占位    │ ...  │
│ ☐ │ etf_rotation      │ 每周一 10:00│ 已停用  │ ...  │
└──────────────────────────────────────────────────────┘

(empty state):
┌──────────────────────────────────────────────────────┐
│   📄 还没有注册的策略.                                │
│   把 .py 放到 data/user_data/strategies/live/ 后刷新.  │
└──────────────────────────────────────────────────────┘
```

### 详情页 `/live-trading/dividend_auntie`

```
← 返回策略列表
current: dividend_auntie · 运行中 · 下次 9:35

┌──── LiveTopBar 8 KPI (沿用) ───────────────────────────┐

┌Sidebar─┐ ┌───────────── main ──────────────────────────┐
│ 概览   │ │                                              │
│ 统计   │ │ (概览 tab 内)                                │
│ 日志   │ │                                              │
│ 代码   │ │  ┌─ 当前持仓 (独立 row, Section) ────────┐   │
│ 设置   │ │  │ 标的 数量 成本  现价  市值  盈亏 ... │   │  ← user "持仓单独一行" 落点
└────────┘ │  │ ...                                 │   │
           │  └────────────────────────────────────┘   │
           │                                              │
           │  ┌── 收益曲线 ──────────────────────────┐   │
           │  │ equity_curve                         │   │
           │  └──────────────────────────────────────┘  │
           │                                              │
           │  ┌── 委托 / 下单 ────────────────────────┐   │
           │  │ ...                                  │   │
           │  └──────────────────────────────────────┘  │
           │                                              │
           │ (stats tab) 🟦 全局账户视图                 │
           │   6 条风险曲线                               │
           │                                              │
           │ (logs tab) 🟦 全局账户视图                  │
           │   交易日志表格                               │
           │                                              │
           │ (code tab) 代码编辑器 + 替换/运行            │
           │                                              │
           │ (settings tab) 策略信息 + schedules 编辑    │
           └──────────────────────────────────────────────┘
```

### 关键视觉规则

1. 表格行 hover: 背景 `bg-elevated/30`, `cursor-pointer`; 复选框 click 拦截
2. tabs 选中态: 下划线 2px `border-accent`, 其他灰
3. 6 个缺字段列 (`开始时间 / 累计 / 年化 / 今日 / 最大回撤 / 微信通知`) 列头灰色 + tooltip
4. 表格 0 策略 → 空状态块; 不渲染表头 (避免误导)
5. 详情页 Overview tab 内 "持仓 Section" 与其它 Section 间距统一 (`space-y-6`); Section 标题沿用现 `<Section title="当前持仓" icon={Wallet} />`
6. 详情页 3 个全局 tab 顶部 `<GlobalAccountBadge />` 字号 `text-xs text-muted-foreground mb-2`, 不干扰主内容
```

