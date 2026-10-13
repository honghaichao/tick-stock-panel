# 代码二次开发与 AI 扩展指南

本文面向需要在当前仓库中二次开发的维护者、团队和 AI 编码代理。目标不是禁止修改源码，而是让新增页面、业务规则和定制逻辑尽量通过稳定边界接入，使后续合并上游版本时冲突更少、风险可验证。

修改任何代码前，仍须先完整阅读根目录的 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。金融数据口径、缓存、并发、数据源和测试要求以该文档为准。

## 1. 文档状态

本文同时描述现有能力和后续按需建设的代码扩展契约。两者不能混用：

| 状态 | 含义 |
| --- | --- |
| 已可用 | 当前仓库中已经存在，可以在确认调用链后直接复用 |
| 按需扩展 | 尚未实现；出现真实用例后才能增加，不能提前假设 API 存在 |

当前已可用的主要扩展能力：

- 自定义、AI 和叠加策略目录：`data/strategies/`。
- 数据源 Provider 与 `plugin.yaml` 机制：详见 [`plugin-development.md`](plugin-development.md)。
- 扩展数据与声明式分析页面：适合不需要自定义 React 交互的页面。
- 前端源码扩展注册：`frontend/src/custom/<namespace>/extension.tsx`，支持静态页面、导航和已开放插槽。
- 后端源码扩展注册：`backend/app/custom/<module>.py`，支持 FastAPI 路由、启动钩子和通知格式化器。
- 当前前端插槽：`layout.navigation.extra`、`stock-preview.footer`、`watchlist.toolbar`。
- 当前后端继承点：`NotificationFormatter`。
- 因子平台与策略线的既有桥接（已实现，二开时直接复用、勿重复实现）：因子库一键生成单因子排名策略（`GenerateFactorStrategyDialog` → `custom_factor_*`）；策略触发器引用因子条件信号（`AddFactorSignalDialog`，`csg_f_*`）；自定义信号 AI 提示词含因子分组（`custom_signals_ai.py`）；策略回测因子归因（`strategy.py` 的 `factor_attribution`，覆盖 `meta.scoring` 非空的策略）。

尚未实现、只能在真实需求出现后增加的能力：

- 更多页面局部插槽。
- 候选过滤、评分、仓位、风控和回测成本等后端业务策略接口。
- 配置 schema 迁移注册表。

实盘交易 Broker 适配（已实现，二开接入真实券商时使用）：

- 默认实现 `app.services.trading.paper_broker.PaperBroker`（按分钟 K 撮合，不连接真实券商）。
- 二开接入真实券商时实现 `app.services.trading.broker.BrokerAdapter` 协议：

  - `name` / `kind`：标识（`kind=BrokerKind.LIVE`）；
  - `submit(draft, order) -> OrderAck`：受理委托；
  - `cancel(order) -> (ok, reason)`：撤单；
  - `match_tick(pending, bar_by_symbol) -> list[MatchDecision]`：撮合计算（每轮由 engine 触发，纯函数，不写盘）。

  接入方式：在 `backend/app/custom/<name>.py` 中定义自己的 broker 实例；通过 `trading.engine.set_app_state(...)` 时替换默认 `engine._broker` 字段，或在更上层的启动钩子中调用 `engine._broker = MyBroker()`（后续 PR 会提供正式注册接口）。
- 真实券商的鉴权 / 下单请求必须在 worker 线程内完成，不能阻塞 quote 轮询线程；参考 CONTRIBUTING §5.2 / §6.2。
- 凭证（账号 / token）走 `app/secrets_store.py` 的 secrets.json（0600 权限），不写入 preferences.json / 代码 / 文档；UI 不应回显完整凭证。

程序化下单（已实现，策略 / 脚本 / CLI 接入入口）：

实盘引擎对上层提供两种程序化入口。所有路径都穿透到 `TradingEngine.submit / cancel / list_*`，享受同样的：风控校验（金额上限 / 资金 / T+1）、broker 受理、JSONL 落盘、SSE 推送、按分钟 K 撮合。本节接口**不**绕过任何校验，仅是语法糖层。

- **Python 脚本入口**：`app.services.trading.runner.TradingRunner`

  ```python
  from app.services.trading.runner import TradingRunner

  # 1. 默认: 必须在后端进程上下文 (main.py 已 set_app_state)
  runner = TradingRunner.make_default()

  # 2. 自定义 (测试 / 独立后端实例)
  from app.services.trading.engine import TradingEngine
  runner = TradingRunner(my_engine, my_data_dir)

  # 单笔委托
  order = runner.buy("600519.SH", qty=100, limit_price=10.5, note="突破追单")
  runner.sell("600519.SH", qty=100, limit_price=11.0)
  runner.cancel(order.id)

  # 调仓到目标持仓 (先卖后买, 差额调仓, 失败 WARN 不抛)
  runner.rebalance_to({"600519.SH": 100, "000858.SZ": 200})

  # 限价 + 单 symbol 价格表
  runner.rebalance_to(
      {"600519.SH": 100, "000858.SZ": 200},
      prices={"600519.SH": 10.5, "000858.SZ": 21.0},
      order_type="limit",
  )

  # 查询
  for p in runner.positions():
      print(p.symbol, p.qty, p.avg_cost)
  account = runner.account()
  pending = runner.pending_orders()
  ```

- **CLI 入口**：`backend/scripts/trading_cli.py`

  ```bash
  # 从 backend/ 目录运行; 必须在已启动后端进程内 (依赖 app.state.trading_engine)
  uv run python -m scripts.trading_cli enable                                       # 启用撮合线程
  uv run python -m scripts.trading_cli submit buy 600519.SH 100 --price 1500.5     # 限价买
  uv run python -m scripts.trading_cli submit sell 600519.SH 100 --market          # 市价卖
  uv run python -m scripts.trading_cli cancel ord_xxxxxxxx                         # 撤单
  uv run python -m scripts.trading_cli status                                       # 引擎 + 账户
  uv run python -m scripts.trading_cli positions                                    # 当前持仓
  uv run python -m scripts.trading_cli orders --status new --limit 50              # 委托
  uv run python -m scripts.trading_cli trades                                       # 历史成交
  ```

- **策略脚本模式**：在已运行的 FastAPI 进程内（`uvicorn` 已注入 `app.state.trading_engine`），策略可以直接通过 HTTP `/api/trading/orders` 提交（已可用）。**注意**：当前 UI 默认仅手动提交，策略信号 → 实盘的自动桥接**不在本期范围**（见 §1 实盘模块警示"策略信号自动下单不在本期范围"）。
- **何时调**：实盘引擎本身不调度、不监听时间。所有"何时调 buy / sell / rebalance_to"的决策由调用方（策略脚本 / cron / 用户手动）决定。本模块只保证：调进来一笔就走完同样的风控 + 撮合 + 落账全链路。
- **测试模式**：单测直接构造 `TradingEngine + TradingRunner` 即可（见 `backend/tests/test_trading_runner.py`），不需要 FastAPI / HTTP / 数据库。

实盘策略自动调仓（LiveStrategy + StrategyExecutor + StrategyScheduler，已实现，多 event）：

- 策略文件：`data/user_data/strategies/live/<name>.py`，模块级 `STRATEGY = MyStrategy()`，需实现 `LiveStrategy` 接口：

  ```python
  from app.services.trading.live_strategy import LiveStrategy, LiveStrategyContext, StrategySchedule

  class MyStrategy(LiveStrategy):
      id = "my_strategy"
      name = "我的策略"
      enabled_default = False
      # 多 schedule: 每个 cron 时点 + event 名 (JoinQuant run_daily 风格)
      schedules = [
          StrategySchedule(name="pre_market", hour=9,  minute=5),
          StrategySchedule(name="rebalance",  hour=9,  minute=26),
          StrategySchedule(name="limit_check", hour=13, minute=30),
      ]

      def on_event(self, event: str, context: LiveStrategyContext) -> dict[str, int]:
          # 跨事件共享 state: 9:05 写入, 13:30 读出 (持久化到 JSON)
          if event == "pre_market":
              context.state["high_limit_list"] = [...]
              return {}  # 空 target = 本次不下单
          if event == "rebalance":
              return {"600519.SH": 100}
          if event == "limit_check":
              held = context.state.get("high_limit_list", [])
              return {sym: 0 for sym in held}
  STRATEGY = MyStrategy()
  ```

- 启动时 `main.py` 调用 `LiveStrategyRegistry.discover_from(data_dir / "user_data/strategies/live")`，自动 import + 注册所有 `STRATEGY` 实例。
- 调度：`StrategyScheduler.apply_all(strategies)` 复用 main 现有的 `AsyncIOScheduler`（与盘后管道同一个实例），**每个 strategy × 每个 schedule 各挂 1 个 job**，按 `CronTrigger(day_of_week="mon-fri", hour=H, minute=M)` 触发，触发时调用 `executor.run_now(strategy_id, event=event)`。per-strategy 配置存在 `preferences.json`：

  ```
  trading_strategies.<id>.schedules = [
      {name: "pre_market", hour: 9, minute: 5, enabled: true},
      {name: "rebalance",  hour: 9, minute: 26, enabled: true},
      {name: "limit_check", hour: 13, minute: 30, enabled: false},
  ]
  ```

  每个 schedule 可单独启停（UI 上每个 event 一行带启停按钮）。

- **state 持久化**：`context.state: dict` 自动在每次 `on_event` 调用前从 `data/user_data/trading/live_strategy_state/<id>.json` 加载，调用后写回（原子替换）。跨重启 + 跨日 (9:05 → 13:30 → 次日 9:05) 保留。

- 执行链路（一次性调仓 / cron 触发均走同一路径）：
  `StrategyExecutor.run_now(id, event=event)` → 加载 state → 构造 `LiveStrategyContext`（含 state）→ 调 `strategy.on_event(event, ctx)` → 保存 state → 如果 target 非空：`TradingRunner.rebalance_to(target, prices, order_type="limit")` → 风控 + 撮合 + 落 fill。

- 异常隔离：策略抛异常 → 当前 run 状态 `error`，记 `strategy_runs.jsonl`，不影响其他策略 / 下次调度；state 仍尝试保存（部分写入保留）。
- UI 控制：实盘页「实盘策略」段每个 event 单独一行（启停按钮 + 立即跑按钮），展开看运行历史（含 event 名）。

Demo 策略 `data/user_data/strategies/live/dividend_auntie.py` 完整复刻 JoinQuant 风格 3 时点（9:05 pre_market / 9:26 rebalance / 13:30 limit_check）+ 跨事件 state 持久化。

**菜场大妈 = JoinQuant 模板 1:1 端口**: 文件中每个函数（`get_dividend_ratio_filter_list` / `_dividend_stocks` / `my_Trader` / `prepare_stock_list` / `check_limit_up` / `filter_st_stock` 等）都按用户提供的 JoinQuant 原代码 1:1 复刻，仅替换数据访问接口。函数签名 / 算法逻辑 / 默认参数全部忠实保留。**唯一缺口是 `finance.STK_XR_XD` 分红历史数据源**，需要从以下任一渠道接入：

- `data/user_data/dividends.csv` (列: symbol, ex_date, bonus_per_share) — 默认 CSVDividendSource 直接读
- 真生产环境：实现 `DividendSource` Protocol 接 fuyao 财报端点（参考 `app/services/trading/data_provider.py`）
- 无数据时：策略 WARN 一次并退化为按 1/price 作为 yield 代理（与原代码注释里的近似 fallback 等价）

**重要边界**：
- 策略**只计算**想要的目标持仓，**不直接下单**；真实下单走 `engine.submit` / `TradingRunner.rebalance_to`，享受所有风控（金额 / 资金 / T+1 / 涨跌停）。
- 不接真实券商：自动调仓 = PaperBroker 按分钟撮合，**资金不出账**。
- 风险敞口在用户控制下：每笔委托都过 `trading_max_order_amount` / `trading_max_position_pct` 等约束；策略异常 / 风控拒单只影响本笔，不连带其他。
- 多 event 时建议每个 event 处理一种语义（准备/调仓/检查），不要把多个逻辑揉到 on_event 里再内部判断——状态边界清晰，state 持久化更可靠。

AI 在开始任务前必须通过代码搜索确认能力是否已经实现。找不到定义和测试时，应把示例视为设计规范，不得虚构导入路径或调用结果。

## 2. 二次开发分级

按升级风险从低到高选择实现方式：

| 级别 | 实现方式 | 适用场景 | 升级风险 |
| --- | --- | --- | --- |
| L1 | 配置、策略文件、扩展数据 | 已有契约能够完成需求 | 最低 |
| L2 | 前端插槽、路由注册；后端策略接口、注册替换 | 新页面、局部 UI、可替换业务规则 | 较低 |
| L3 | 直接修改核心源码 | 核心流程本身必须变化，现有扩展点无法表达 | 最高 |

选择原则：

1. 先确认现有功能能否复用，禁止平行实现第二套数据、策略、缓存或请求逻辑。
2. 只在存在真实二开需求的位置增加扩展点，不为未来可能出现的需求预埋通用框架。
3. 插槽或接口无法表达核心行为变化时，可以修改源码；必须缩小改动范围并补回归测试。
4. 不为了避开一次冲突复制完整页面、服务或引擎。复制会把一次显式冲突变成长期的隐式分叉。

## 3. 前端扩展规范

### 3.1 何时使用插槽

插槽适合在既有页面中增加局部内容：

- 操作按钮或工具栏命令。
- 筛选条件或表单字段。
- 表格列和详情面板。
- 个股详情、策略详情中的附加标签页。
- 设置页面中的独立配置区。

新增完整页面时应使用路由和导航注册，不要把整页塞入某个插槽。改变核心页面的数据流、状态模型或主要布局时，应直接修改核心代码并按 L3 管理。

### 3.2 当前插槽契约

核心页面通过已经实现的 `ExtensionSlot` 提供受控上下文：

```tsx
<ExtensionSlot
  name="strategy.monitor.filters"
  context={{
    apiVersion: 1,
    rule: draft,
    updateRule,
    readOnly,
  }}
/>
```

二开模块通过默认导出的注册清单接入，不修改核心文件：

```tsx
import type { FrontendExtension } from '@/extensions/types'

function NavigationExtra({ collapsed }: { collapsed: boolean; pathname: string }) {
  return collapsed ? null : <div>二开内容</div>
}

const extension: FrontendExtension = {
  id: 'company.navigation',
  apiVersion: 1,
  slots: [{
    name: 'layout.navigation.extra',
    id: 'company-summary',
    order: 100,
    component: NavigationExtra,
  }],
}

export default extension
```

插槽设计必须满足：

- `name` 和注册项 `id` 全局稳定、唯一。
- `context` 使用明确的 TypeScript 类型，并包含契约版本。
- 只暴露完成该插槽职责所需的数据和操作，不传递整个页面状态。
- 插槽通过公开回调修改状态，不直接访问父组件内部 store 或缓存。
- 单个扩展渲染失败应由错误边界隔离，并显示可定位的扩展 ID。
- 注册顺序确定，使用 `order` 后再按 `id` 排序，避免加载顺序导致界面漂移。
- 插槽内容必须遵守项目现有设计系统、响应式和可访问性要求。

当前开放：

```text
layout.navigation.extra
stock-preview.footer
watchlist.toolbar
```

各插槽 context 契约（均要求 `apiVersion: 1`，定义见 `frontend/src/extensions/types.ts` 的 `FrontendSlotContextMap`）：

- `layout.navigation.extra`：`{ collapsed, pathname }`，侧边栏导航底部。
- `stock-preview.footer`：`{ symbol, name, view }`，个股详情对话框底部（日K/分时图表下方）；`view` 为 `'daily' | 'intraday'`。适合个股附加面板：龙虎榜、资金流、外部研究链接等。
- `watchlist.toolbar`：`{ symbols, viewMode, selectedGroup, refresh }`，自选页工具栏末尾；`symbols` 为当前筛选视图中的标的，`refresh` 在扩展修改数据后调用以刷新自选增强数据。适合批量操作入口：自定义分析、导出、组合计算等。

新增插槽前必须有真实用例，并同时定义 context 类型、异常隔离和测试；不能只在类型表中预留名字。

### 3.3 当前路由与导航契约

完整页面通过注册表接入：

```tsx
const extension: FrontendExtension = {
  id: 'company.risk',
  apiVersion: 1,
  routes: [
    { id: 'company-risk', path: '/company/risk', component: CompanyRiskPage },
  ],
  navigation: [
    {
      id: 'company-risk',
      routeId: 'company-risk',
      label: '风险分析',
      icon: ShieldCheck,
      order: 500,
    },
  ],
}
```

路由注册与菜单注册已经解耦：页面可以存在但不显示在菜单中；菜单只能引用同一扩展内的已注册路由。首版只支持静态绝对路径。扩展路由不得覆盖核心或其他扩展路径，冲突时只禁用该扩展并输出明确错误。

完整前端模板位于 [`frontend/src/custom/_template/extension.tsx.example`](../frontend/src/custom/_template/extension.tsx.example)。

### 3.4 前端禁止事项

- 不直接在多个组件中拼接后端 URL，统一使用 `frontend/src/lib/api.ts` 或未来公开客户端。
- 不自行创建与现有 TanStack Query 重复的缓存；查询键仍由 `queryKeys.ts` 集中管理。
- 不用插槽绕过权限、数据口径或表单校验。
- 不通过 DOM 查询、全局事件或 monkey patch 修改核心组件。
- 不把整个核心页面复制到二开目录后长期独立维护。

## 4. 后端扩展规范

### 4.1 使用小粒度继承

后端允许二开类继承稳定、职责单一的抽象基类，再通过注册表或依赖注入接入。不要继承并覆盖大型编排服务。

当前已经实现的继承点：

- `NotificationFormatter`：在监控规则完成评估后统一调整通知文案，不改变事件结构和触发语义。

下列是可能适合的小粒度接口，但目前没有实现，不能直接导入：

- `CandidateFilter`：候选池过滤。
- `ScoringPolicy`：评分计算。
- `PositionSizingPolicy`：仓位计算。
- `RiskPolicy`：风险约束。
- `NotificationFormatter`：通知文案。
- `StrategyProvider`：策略发现。
- `MonitorConditionEvaluator`：自定义监控条件。
- `BacktestCostModel`：手续费和滑点模型。

不适合作为公共继承点的核心类：

- `StrategyEngine`。
- `BacktestEngine`。
- `ScreenerService`。
- `StrategyMonitorService`。
- `DataStore` 和仓库实现。
- FastAPI 主应用及生命周期函数。

这些类管理流程、缓存、并发或生命周期。覆盖其中的内部方法会让上游调整执行顺序后产生难以发现的语义错误。

### 4.2 当前基类与注册契约

后端模块从 `app.extensions` 导入稳定契约：

```python
from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    NotificationFormatContext,
    NotificationFormatter,
)

EXTENSION_ID = 'company.notice'
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


class CompanyNotificationFormatter(NotificationFormatter):
    def format_message(self, event: dict, context: NotificationFormatContext) -> str:
        return f"[公司规则] {event.get('message', '')}".strip()


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.register_notification_formatter(
        'company.notification',
        CompanyNotificationFormatter(),
    )
```

同一个 `setup` 可以通过 `registrar.include_router(router)` 注册独立 FastAPI 路由。核心路由冲突、重复 ID 或契约版本不匹配时，该扩展整体不注册，不留下半注册状态。

核心数据层初始化完成后，可选的 `startup(context: ExtensionContext)` 会收到数据目录和只读仓库协议。启动钩子失败只记录错误，不阻止主程序启动。

完整后端模板位于 [`backend/app/custom/_template.py.example`](../backend/app/custom/_template.py.example)。

### 4.3 后端契约要求

- 上下文优先使用不可变 `dataclass` 或 `Protocol`，不向扩展暴露整个 `app.state`。
- 抽象方法参数、返回值、单位、空值和异常行为必须有文档及契约测试。
- 注册 ID 全局唯一；重复注册默认拒绝，不允许静默覆盖官方实现。
- 默认实现必须存在。没有启用二开实现时，核心行为与当前版本一致。
- 单个可选实现加载失败时应禁用自身；金融结果无法可靠计算时必须 fail-closed。
- 注册表在启动完成后冻结，实时线程中不得动态替换实现。
- 破坏性契约变化必须提升 `api_version`，旧版本至少保留一个大版本的兼容期。

### 4.4 继承与组合的边界

继承只用于表达稳定的“是一种策略实现”关系。需要同时组合过滤、评分、通知等能力时，分别注册多个小实现，不创建拥有大量可选方法的万能基类。

优先组合的场景：

- 一个服务需要多个独立规则。
- 行为需要按资产类型或运行上下文选择。
- 扩展只需要装饰默认结果，而不是完全替换算法。
- 依赖缓存、仓库或通知服务，需要通过明确构造参数注入。

## 5. 直接修改源码

扩展点不是限制。核心流程必须变化时允许直接修改源码，但要把升级成本显式管理。

### 5.1 修改要求

- 一个提交只包含一个二开目的，不混入格式化、依赖升级和无关重构。
- 优先新增独立模块，再对核心入口做最小接线。
- 修改公共契约时同时更新后端模型、前端类型、调用方和测试。
- 修改数据写入时列出持久化、内存缓存、版本、SSE 和前端查询失效链路。
- 保留旧配置和旧数据读取能力；必须迁移时提供幂等迁移和回滚说明。
- 在 PR 描述中标记被修改的核心热点及未来合并上游时的复核点。

### 5.2 高冲突热点

以下文件集中管理启动、路由或公共契约，直接修改时需要重点复核：

```text
backend/app/main.py
backend/app/strategy/engine.py
backend/app/backtest/engine.py
frontend/src/router.tsx
frontend/src/components/Layout.tsx
frontend/src/lib/api.ts
frontend/src/lib/queryKeys.ts
```

高冲突不代表禁止修改，而是要求改动更小、测试更完整。若多个二开需求反复修改同一热点，应把共同接线能力提升为正式插槽或策略接口。

## 6. AI 开发工作流

AI 必须按以下顺序工作：

1. 完整阅读 `AGENTS.md`、`CONTRIBUTING.md` 和本文。
2. 检查 `git status`，保留工作区已有修改。
3. 搜索目标调用链、相邻实现、现有扩展点和测试。
4. 明确需求属于 L1、L2 还是 L3，并说明选择依据。
5. 判断目标插槽或后端基类是否真实存在，不依据本文示例虚构代码。
6. 写出最小改动计划和完成标准。
7. 先补能证明行为的测试，再实施必要改动。
8. 执行对应验证矩阵，检查最终 diff 和兼容性。

### 6.1 可直接使用的任务模板

```text
请在 Tick Stock Panel 当前仓库中实现：[具体需求]。

开始前完整阅读 AGENTS.md、CONTRIBUTING.md 和
docs/secondary-development.md，并先检查 git status、真实调用链和现有测试。

约束：
1. 先判断现有功能能否复用，并将方案归类为 L1/L2/L3。
2. 前端优先使用已经存在的受控插槽、路由或导航注册；后端优先使用已经存在的
   小粒度抽象基类和注册机制。必须用搜索和测试证明接口真实存在，不能根据设计文档
   虚构 API。
3. 若扩展点尚未实现，先说明最小可行方案；只有该需求确实需要时才新增扩展点。
4. 允许直接修改源码，但保持改动最小，不复制完整页面或核心服务。
5. 复用现有 API、数据仓库、缓存、查询键、组件和领域口径，不创建第二套逻辑。
6. 保持历史配置和数据兼容，扩展失败不能破坏未启用扩展的主流程。
7. 不覆盖已有修改，不提交、不推送，除非我单独确认。

完成后请列出：
- 方案分级及原因；
- 修改文件和关键契约；
- 对缓存、数据、API 和升级兼容性的影响；
- 实际执行的测试、构建和结果；
- 仍需人工确认的风险。
```

### 6.2 让 AI 设计扩展点的模板

```text
请只设计并评审以下二次开发需求的扩展边界，暂不修改代码：[具体需求]。

请基于当前仓库真实调用链回答：
1. 现有能力是否已经可以实现；
2. 前端应使用局部插槽、路由注册还是直接改源码；
3. 后端应使用哪个小粒度策略接口，为什么不继承大型核心服务；
4. 最小 context/Protocol 应包含哪些字段；
5. 默认实现、失败隔离、契约版本和测试如何设计；
6. 哪些抽象属于当前不需要的过度设计。

不要假设本文中的目标 API 已经实现，请给出代码证据和文件位置。
```

## 7. 验证矩阵

除 `CONTRIBUTING.md` 的通用要求外，二开还应按接入方式验证：

| 改动 | 最低验证 |
| --- | --- |
| 前端插槽 | 无注册、单注册、多注册排序、异常隔离、窄屏、前端构建 |
| 路由/导航注册 | 路径冲突、隐藏页面、无权限、直接刷新、未知路由 |
| 后端策略实现 | 默认实现、二开实现、重复 ID、版本不兼容、加载失败隔离 |
| 配置或契约变化 | 旧字段缺失、未知字段、更高版本拒写、迁移幂等 |
| 直接修改核心 | 受影响模块完整回归、缓存失效、历史配置、前后端联调 |

常用命令：

```bash
cd backend
uv run --frozen pytest tests/path/to/test_x.py -q
uv run --frozen ruff check app/path.py tests/path.py

cd ../frontend
pnpm build

cd ..
git diff --check
git status --short --branch
```

不得把“扩展已加载”当作业务验证。测试必须断言真实过滤结果、评分、路由输出、界面状态或失败隔离行为。

## 8. 版本与升级约定

- 二开分支应记录开始开发时的上游 Git Tag 或 commit，不能只写“基于 v0.x”。
- 正式发布使用不可变 Tag；二开升级优先合并 Tag，而不是持续变化的开发分支头。
- 公共插槽和后端扩展接口使用独立的 `api_version`，不要直接等同应用版本。
- 同一 `api_version` 内只做向后兼容的字段新增；删除、改名或改变语义必须提升主版本。
- 废弃字段先标记并保留兼容读取，至少跨一个大版本后再移除。
- 升级后必须重新运行二开契约测试，不能只依赖 Git 显示“无冲突”。

直接修改核心源码的二开分支可在升级前运行只读预检：

```bash
python3 scripts/upgrade_check.py <目标Tag或分支>
```

脚本不会执行 merge、修改索引或工作区。它会报告共同基线、双方修改的同一文件以及 Git 三方预演可识别的文本冲突。未提交内容不会进入预演，因此正式评估前应先提交到临时二开分支。

## 9. 完成检查表

- [ ] 已确认需求属于 L1、L2 或 L3。
- [ ] 已证明使用的插槽、基类和注册 API 在当前代码中真实存在。
- [ ] 没有复制已有数据读取、缓存、API 客户端或完整核心页面。
- [ ] 前端扩展只获得必要 context，后端没有继承大型编排服务。
- [ ] 默认实现和未启用二开时的行为保持不变。
- [ ] 重复注册、加载失败、版本不兼容和空数据均有明确行为。
- [ ] 历史配置、策略和用户数据仍可读取。
- [ ] 已执行适用的测试、构建、Ruff 和 `git diff --check`。
- [ ] 最终说明包含升级风险和未来合并上游时的复核点。

## 10. 后续扩展原则

统一注册基础设施已经完成。后续只按真实业务需求增加能力：

1. 页面需要局部定制时，在真实位置增加一个类型化插槽及测试。
2. 后端业务规则需要替换时，从该调用链提取一个小粒度接口、默认实现和契约测试。
3. 不继承大型编排服务，不暴露整个 `app.state`，不复制核心流程。
4. 只有出现需要持久化的二开配置后，再增加 schema 迁移注册表。
5. 需要升级直接修改源码的分支时，使用 `scripts/upgrade_check.py` 预检，再执行真实合并和回归。

这种顺序遵循 KISS 和 YAGNI：先解决已经存在的升级冲突，不提前建设完整插件平台，也不阻止开发者在必要时直接修改源码。
