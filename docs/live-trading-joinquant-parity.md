# 实盘交易：聚宽式撮合与调度（Live Trading JoinQuant Parity）

> 状态：**定稿**（用户已 review 并拍板 3 个明确决定：tick_size=0.01 / 撮合轮询 5s 不调 / 17:00 清算放 StrategyScheduler）
> 范围：撮合层（Task A）+ 调度层（Task B）+ 页面布局（Task D）+ 历史 fill 重算 + 17:00 清算
> 不在范围：通知 / 监控中心接通（独立 task `live-trading-notification.md`）

---

## 1. 目标

把当前实盘（按分钟 K 撮合 + APScheduler cron + 单页垂直堆叠）改造成对齐聚宽模拟盘语义的版本。

成功标准：
- 市价单按"上一分钟最后价 + 滑点"撮合（聚宽语义），与 open 价解耦
- 限价单保持当前"bar.low <= 委托价 → 撮合价=委托价"语义，对齐聚宽
- 同 `(hour, minute)` 多 schedule 按 monthly > weekly > daily 优先级串行
- 策略代码 mtime 变化时跑 `after_code_changed` 钩子
- 策略运行失败时按 `as_of`（原 cron 触发时刻）补撮合原 buys，不重跑策略
- 17:00 清算自动撤所有 NEW+PARTIAL
- 历史 30 笔 fill 用新撮合逻辑重算（K 线 + broker 规则 + 滑点）
- 实盘页（`LiveTrading.tsx`）改成聚宽详情页风格：顶部状态条 + 左中右三栏 + 底部运行历史

---

## 2. 范围

### 2.1 In

- `PaperBroker` 改造：市价单语义、滑点、order_volume_ratio
- 新增 `Slippage` 模型（`PriceRelatedSlippage` / `TickRelatedSlippage` / `FixedSlippage`）
- 新增 `set_slippage` 配置（preferences + 运行时 override）
- 撮合轮询线程改造：从"按当前 K 撮合"改为"按上一根已收盘 K 撮合"
- 17:00 清算 cron：自动撤所有 NEW+PARTIAL，写 TradeLog（CANCEL）
- `StrategyScheduler`：同 `(hour, minute)` 多 schedule 优先级
- 策略文件 mtime 监听：`after_code_changed` 钩子
- 失败重试：`retry_failed_run(run_id)` 按 `as_of` 撮合原 buys
- 历史 fill 重算脚本：`backend/scripts/recalc_fills.py`
- `LiveTrading.tsx` 布局重构

### 2.2 Out（明确不做）

- 通知 / 微信 / 监控中心接通（独立 task）
- `g.*` 全局变量镜像（ctx.state 已有等价能力）
- 真实券商对接（`BrokerAdapter` Protocol 已留口）
- 修改撮合落盘 schema（保持 `Fill` 模型不变，仅改计算）

---

## 3. 数据契约

### 3.1 新增模型（`backend/app/services/trading/models.py`）

```python
class SlippageKind(str, Enum):
    NONE = "none"                  # 不滑点（默认）
    PRICE_RELATED = "price_related"  # PriceRelatedSlippage(pct)
    TICK_RELATED = "tick_related"    # TickRelatedSlippage(value)
    FIXED = "fixed"                  # FixedSlippage(value)


@dataclass(frozen=True)
class SlippageConfig:
    """撮合滑点配置. 跟聚宽 set_slippage 对齐."""
    kind: SlippageKind = SlippageKind.NONE
    value: float = 0.0  # pct (0.001 = 0.1%) or 价差 (元) or tick 倍数
```

`Fill` / `Order` / `Position` / `Account` 模型字段**不变**。

### 3.2 JSONL schema 变更

`orders.jsonl` / `fills.jsonl` / `logs.jsonl` / `account.json` 字段**不变**。重算 fill 写**新行**追加，旧行保留并加 `superseded_by` 字段指向新 fill（可选，默认不重写历史）。

**新行**：`recalc_fills.py` 写出时**追加**到 fills.jsonl，订单旧行保留。账户余额按"重算后"重写 account.json（备份旧值到 `account.json.bak`）。

`strategy_runs.jsonl` 新增 `retry_of` 字段（指向被重试的 run id）。

### 3.3 撮合 K 线源

`engine._matcher_loop` 当前每 5s 拉 quote_service 缓存，撮合时用 `bar_by_symbol`（实时分钟 K）。

**改造后**：
- 撮合用"上一根已收盘 K"（`bar.close_time <= now - 60s`），不是"当前 K"
- 拉 `repo.get_minute_batch(symbols, day=cn_today(), completed_only=True)`
- 开盘第一根（9:30 完整 K = 9:31 收盘后才有）撮合用 open 价

### 3.4 preferences 新增

```python
KEY_TRADING_SLIPPAGE_KIND = "trading_slippage_kind"  # default: "none"
KEY_TRADING_SLIPPAGE_VALUE = "trading_slippage_value"  # default: 0.0
KEY_TRADING_ORDER_VOLUME_RATIO = "trading_order_volume_ratio"  # default: 1.0
KEY_TRADING_CLEARANCE_TIME = "trading_clearance_time"  # default: "17:00" cron
```

---

## 4. API 契约

### 4.1 后端新增/修改

| 端点 | 变更 |
|---|---|
| `GET /api/trading/preferences` | 新增 `slippage_kind / slippage_value / order_volume_ratio / clearance_time` 字段 |
| `PATCH /api/trading/preferences` | 接受上述 4 字段 |
| `GET /api/trading/runs` | `RunReport` 新增 `retry_of: str \| None` 字段 |
| `POST /api/trading/runs/{run_id}/retry` | **新**：失败重试，按 `as_of` 撮合原 buys。返回新 `RunReport` |
| `GET /api/trading/klines/{symbol}/current_bar` | **新**：返回当前正在进行的分钟 K + 上一根已收盘 K（前端画 K 线用） |
| `GET /api/trading/status` | 新增 `slippage` / `next_clearance_at` 字段 |

### 4.2 前端

| 组件 | 变更 |
|---|---|
| `LiveTrading.tsx` | 整体重构为聚宽详情页布局（见 §6） |
| `QK` (`lib/queryKeys.ts`) | 新增 `tradingRunRetry` mutation key、`tradingClearance` query key |
| `api.ts` | 新增 `tradingRunRetry(run_id)`、`tradingKlineCurrentBar(symbol)` 方法 |

### 4.3 WebSocket / SSE

`trading_event` 事件新增 `kind: 'order_cleared' | 'run_retry' | 'after_code_changed' | 'schedule_dispatched'`，前端 `useQuoteStream` 透传。

---

## 5. 行为口径

### 5.1 撮合语义（聚宽式）

#### 5.1.1 市价单

```
最新价 L = (09:30 第一根) ? open : (上一分钟最后价 = 上一根 K 的 close)
买: 撮合价 = L * (1 + slippage)   # 滑点在买方向上加价
卖: 撮合价 = L * (1 - slippage)   # 卖方向减价
撮合量 = min(qty, order_volume_ratio * 上一分钟成交量)  # 0 = 无上限
```

- 开盘第一根（9:30）撮合：`L = bar.open`，9:30 ~ 9:31 之间也走 9:30 这根 K
- 9:31 之后撮合：`L = 9:30 那根 K 的 close`（上一根已收盘 K 的 close）
- 撮合 K 必须 `bar.close_time <= now - 60s`（避免撮合未收盘 K）

#### 5.1.2 限价单（保持当前）

```
撮合价 p (买): p = min(bar.open, limit_price)   if bar.low <= limit_price
撮合价 p (卖): p = max(bar.open, limit_price)   if bar.high >= limit_price
撮合量 = min(qty, order_volume_ratio * 上一分钟成交量)
```

- 不变，按"开 9:35 cron 触发 → 撮合线程 5s 后用 9:35 那根 K"逻辑

#### 5.1.3 滑点模型

| 类型 | 公式 |
|---|---|
| `PriceRelatedSlippage(pct)` | `price * (1 ± pct)` |
| `TickRelatedSlippage(value)` | `price + value * tick_size`（A 股 tick_size=0.01）|
| `FixedSlippage(value)` | `price ± value`（固定价差） |
| `NONE` | 不滑（默认，向后兼容） |

#### 5.1.4 order_volume_ratio

- 默认 1.0（不限制）
- 0.5 表示单笔最大成交量 = 上一分钟成交量的 50%
- 数据源：quote_service 缓存的 1m K 的 `volume` 字段

#### 5.1.5 tick_size（用户决定）

- A 股统一 `tick_size = 0.01`（用户决定，写死不分支）
- TickRelatedSlippage(value) 实际偏移 = `value * 0.01` 元
- 不为大票 / 港股 / 期货开特例（如未来需要，扩展为 `SlippageConfig.tick_size` 字段）

#### 5.1.6 撮合轮询频率（用户决定）

- `engine._matcher_loop` 保持 5s 间隔，不调整
- 一根 1m K 最多 12 次轮询撮合尝试（够用）

### 5.2 17:00 清算

**触发**：cron 每天 17:00 跑一次。**位置：`StrategyScheduler` 内**（用户决定，与策略 cron 共用 APScheduler 同一线程池，复用偏好与启停控制）。注册为内部 job `trading_clearance_daily`，不暴露给 `apply_all()` 的策略列表。

**行为**：
1. `store.pending_orders(data_dir)` 拿到所有 NEW+PARTIAL
2. 对每笔调 `engine.cancel()` → 状态置 CANCELED
3. 写 `TradeLog(kind=CANCEL, message="17:00 自动清算撤单")`
4. SSE 推 `trading_event {kind: 'order_cleared', order_id, reason: 'clearance'}`
5. **不影响**已成交 fill，不动 account cash

**边界**：
- 非交易日：仍跑（聚宽行为），但 pending 数为 0 通常 noop
- 失败：单笔 cancel 失败不中断后续，写 ERROR 日志

### 5.3 调度优先级

`StrategyScheduler.apply_all()` 注册时按 (schedule_kind, hour, minute, name) 排序：

```
priority = (
    0 if kind == 'monthly' else
    1 if kind == 'weekly' else
    2 if kind == 'daily' else
    3   # 'manual' / 其它
)
```

- 同 `(hour, minute)` 多 schedule：按 priority 串行触发，**前一 schedule 完成才触发下一**
- 跨 schedule 间隔 > 0：保持当前并行
- 优先级 metadata：新增 `kind` 字段到 `StrategySchedule`（默认 'daily'，向后兼容）

### 5.4 after_code_changed 钩子

- 监听 `data/user_data/strategies/live/*.py` 的 mtime
- 变化时（启动时也跑一次）调对应策略的 `on_after_code_changed(ctx)` 方法
- 默认 `LiveStrategy` 基类提供空实现，子类可覆盖
- 不影响 cron 已注册 jobs（重注册由 `apply_all` 周期任务做）

### 5.5 失败重试（按 cron 时间撮合）

**触发**：
- 用户手动 `POST /api/trading/runs/{run_id}/retry`
- 范围：仅 `status='error'` 或 `status='no_target'` 但 submitted 不为空的 run

**流程**：
1. 读 `strategy_runs.jsonl` 找 `run_id` 对应记录，得 `as_of`、`strategy_id`、`submitted` (order_ids)
2. 对每个 `submitted` order_id：从 `orders.jsonl` 读 `symbol / side / qty / limit_price / order_type`
3. **关键**：以 `as_of` 的"上一根已收盘 K"撮合（不是当前 K）
   - 即取 `as_of` 时刻 `bar.close_time <= as_of - 60s` 的最后一根 K
4. 调 `broker.match_tick` 撮合，落新 fill 写入 `fills.jsonl`
5. 新 `RunReport(retry_of=run_id, status='success'/'no_match', ...)` 落 `strategy_runs.jsonl`

**数据**：
- 旧 order 状态不变（保留为原 run 痕迹）
- 新 fill 独立 `id`，`note='retry:run_id'`
- account cash 同步更新（跟普通 fill 一样）

**与"重跑策略"区别**：
- 不调 `strategy.on_event()`（避免重复决策）
- 仅撮合原 submitted orders
- 聚宽没有显式重试；本 task 实现"补单"语义

### 5.6 历史 fill 重算

**触发**：
- 一次性脚本 `backend/scripts/recalc_fills.py`
- 重算后**追加**新 fill（不复用旧 id），旧 fill 保留

**流程**：
1. 读 `fills.jsonl` + `orders.jsonl`，按时间排序
2. 对每个 fill，找到对应 order 的 `created_at` 作为 `as_of`
3. 读 `data/kline/minute/{symbol}/{date}.parquet`，找 `as_of` 时刻的已收盘 K
4. 模拟当时撮合：调 `broker.match_tick([order], bar_by_symbol)`
5. 若新成交价 != 旧成交价或撮合量不同：写新 fill（`id=fl_recalc_xxx`, `note='recalc'`）
6. 更新 `account.json`（备份旧值到 `account.json.bak.<ts>`）
7. 输出报告：`data/user_data/trading/recalc_report.json`

**边界**：
- 旧 K 线缺失：跳过该 fill，记 WARN
- order_type='market' 重算时无滑点（旧实现无滑点）：新撮合也无滑点
- 当前 30 笔 fill 都是 9-29 / 9-30 / 10-02 / 10-05 / 10-06 / 10-07 触发，约 25 个 sell 5 个 buy

### 5.7 撮合 K 线"上一根"语义

```
09:30 整  → 撮合 K 用 09:30 K (开盘特殊, L=open)
09:30:30  → 撮合 K 用 09:30 K (开盘后, L=open)
09:31:00  → 撮合 K 用 09:30 K (close, 上一根)
09:31:30  → 撮合 K 用 09:30 K (close, 上一根)
09:32:00  → 撮合 K 用 09:31 K (close, 上一根)
```

撮合时检查 `bar.close_time <= now - 60s`，避免撮合未收盘 K。

---

## 6. 页面布局（Task D，聚宽详情页风格）

### 6.1 当前布局（要重构）

```
[AccountCards 4 卡] (4 行)
[PnLPanel 收益指标 + 净值曲线] (约 10 行)
[StrategiesPanel 策略列表] (按 strategy 展开, 变高)
[PositionsTable 当前持仓]
[OrdersTable 委托]
[TradesTable 成交]
[LogsList 日志]
```

### 6.2 目标布局（聚宽详情页）

```
+---------------------------------------------------------------+
| TopBar: 策略名 | 状态 | 总资产 | 持仓 | 今日 | 累计 | 下次 | 重跑/暂停 |
+--------+----------------------------------------------+--------+
|        |                                              |        |
| Left   |  Center (Tabs)                                | Right  |
| 持仓   |   [收益] [每日持仓] [交易明细] [日志]         | 消息流 |
|        |                                              |        |
|        |                                              |        |
+--------+----------------------------------------------+--------+
| Bottom: 策略运行历史 (runs 反序)                                    |
+----------------------------------------------------------------+
```

### 6.3 组件拆分

| 组件 | 内容 |
|---|---|
| `TopBar` | 策略选择下拉 + 状态指示 + KPI 卡片（总资产/持仓/今日/累计/下次运行） + 重跑/暂停按钮 |
| `LeftPanel` (持仓) | `PositionsTable` 简化版（实时刷新），无对照 lots 批次 |
| `CenterTabs` | 4 个 tab：① 收益曲线 ② 每日持仓（按日聚合） ③ 交易明细（orders + fills 合一） ④ 输出日志（run reports） |
| `RightPanel` (消息) | `MessagesStream` 接收 `trading_event` 推送（拒单/异常/自定义消息） |
| `BottomPanel` | `StrategyRunList` 展开版（每条 run 显示 target / error / submitted / traceback） |

### 6.4 布局响应式

- `>= 1280px`：4 列（左 25% + 中 50% + 右 25% + 底部 100%）
- `1024-1280px`：3 列（左右 + 中，右栏折叠到顶 bar）
- `< 1024px`：单列堆叠（左 → 中 → 右 → 底）

### 6.5 文件改动

- `frontend/src/pages/LiveTrading.tsx`：整体重构（1147 → ~800）
- `frontend/src/components/live/`：**新目录** 5 个子组件
  - `TopBar.tsx`
  - `PositionsSidebar.tsx`
  - `CenterTabs.tsx`
  - `MessagesStream.tsx`
  - `RunHistory.tsx`
- `frontend/src/lib/api.ts`：新增 `tradingRunRetry`, `tradingKlineCurrentBar`
- `frontend/src/lib/queryKeys.ts`：新增对应 key

### 6.6 测试

- 单元测试（vitest）：
  - `strategyIsExpanded`（已有，保留）
  - `splitStrategiesByStatus`（已有，保留）
  - **新**：`MessagesStream` 收到 `trading_event` 后追加消息
  - **新**：`TopBar` 状态色映射
  - **新**：`CenterTabs` 默认 tab 持久化（localStorage）
- e2e（已有 e2e harness 即可）：4 个 tab 切换 + 状态条刷新 + 重跑按钮

---

## 7. 风险与边界

### 7.1 撮合语义改的兼容

- 当前 30 笔 fill 用 `bar.open` 撮合；重算后用 `bar.close + slippage`
- 旧 fill 保留（`superseded_by` 可选字段，默认不写）
- `account.json` 重写，旧值备份 `account.json.bak.<ts>`

### 7.2 撮合 K 线"上一根"语义

- 当前撮合是"取当前 K"（未收盘也撮合）
- 改后"必须已收盘"，**会有 0~60s 撮合延迟**
- 9:35 cron 触发 → 9:35:00 ~ 9:36:00 撮合线程拿不到 9:35 K → 9:36 后撮合
- 用户接受这个延迟（聚宽也是 30s+ 延迟）

### 7.3 17:00 清算

- 当前没有 17:00 cron，新增需确认 quote_service 15:00 后还活着
- 测试：mock quote_service 不可用，确认清算 cron 仍能跑

### 7.4 失败重试的 K 线回放

- quote_service 缓存只有当前交易日，历史 K 线在 `data/kline/minute/{symbol}/{date}.parquet`
- 重试时如 `as_of` 当日 K 线不存在 → 报错给用户，不静默吞
- 跨日重试：只支持当日（T 日触发当日补撮），跨日不重试

### 7.5 after_code_changed 钩子

- mtime 监听用 `os.stat` 轮询，间隔 5s（跟撮合线程一致）
- 启动时也跑一次（用户首次启动的代码即"变更"）
- 避免热重载时无限循环：跑完后更新 mtime 基准

### 7.6 页面布局重构

- `LiveTrading.tsx` 1147 行整体改写风险大
- **分阶段**：先抽 5 个子组件到 `components/live/`，主页面 import；测试通过后再删旧逻辑
- 保持 `export function LiveTrading()` 签名不变，路由不动

---

## 8. 测试矩阵

### 8.1 撮合层（Task A）

| 用例 | 期望 |
|---|---|
| 市价单，9:31 撮合 | 用 9:30 K 的 close，无滑点 |
| 市价单 + PriceRelatedSlippage(0.001) 买 | 撮合价 = close * 1.001 |
| 市价单 + TickRelatedSlippage(1) | 撮合价 = close + 0.01 |
| 限价单 (买) limit=10, K high=11 low=9 | 撮合价 = min(open, 10) |
| 限价单 (买) limit=10, K high=11 low=10.5 | 不成交 |
| order_volume_ratio=0.5, 1m volume=10000 | 撮合量 = min(qty, 5000) |
| 撮合线程在 9:35:30 撮合 | 用 9:35 K（已收盘） |
| 撮合线程在 9:35:00 撮合 | 不撮合（K 未收盘），等下一轮 |
| 17:00 清算，2 个 NEW 1 个 FILLED | 2 个 NEW → CANCELED，1 个不动 |
| 17:00 清算，clearance cron 失败重试 3 次 | 重试 3 次后放弃，ERROR 日志 |

### 8.2 调度层（Task B）

| 用例 | 期望 |
|---|---|
| 1 个 strategy 有 3 个 schedule 同 9:35 | 串行触发，1→2→3 |
| daily + weekly 同 9:35 | weekly 先跑（priority=1 < 2） |
| monthly + daily 同 9:35 | monthly 先跑（priority=0 < 2） |
| 策略文件 mtime 变化 | 调 after_code_changed |
| after_code_changed 抛异常 | 不影响 cron jobs |
| 失败 run 重试 | 按 as_of 撮合原 buys，账户同步 |
| 重试时 K 线缺失 | 报错给用户，run status='no_match' |

### 8.3 页面布局（Task D）

| 用例 | 期望 |
|---|---|
| TopBar KPI 4 张卡片 | 实时刷新（refetchInterval=5s） |
| LeftPanel 持仓 0 | EmptyHint |
| CenterTabs 切换 4 tab | 状态保留（localStorage） |
| RightPanel 收到 trading_event | 追加消息流，最多 100 条 |
| BottomPanel run 列表 | 倒序，最近 50 条 |
| 1280px+ 4 列布局 | flex 三栏 + 底部 |
| < 1024px 单列堆叠 | 移动端可用 |

### 8.4 历史 fill 重算

| 用例 | 期望 |
|---|---|
| 30 笔 fill 重算 | 30 个新 fill 追加，旧保留 |
| account.json 重算 | 重写，旧值备份 |
| K 线缺失的 fill | 跳过，WARN 日志 |
| 重算后 fill 与旧 fill 不一致 | 报告 `recalc_report.json` |

---

## 9. 验收标准

### 9.1 后端

```bash
cd /Volumes/quant-ssd/projects/tickflow-stock-panel/backend
uv run --frozen pytest tests/test_trading_paper_broker.py tests/test_trading_engine.py tests/test_trading_scheduler.py tests/test_trading_executor.py -v
# 期望: 所有新旧测试通过, 含撮合语义/调度优先级/重试/清算

uv run --frozen ruff check app/services/trading/
# 期望: 无新增错误 (pre-existing 仍存在)
```

### 9.2 前端

```bash
cd /Volumes/quant-ssd/projects/tickflow-stock-panel/frontend
pnpm build
pnpm test  # vitest
# 期望: build 通过, vitest 通过
```

### 9.3 端到端

```bash
# 1. 启动后端
cd /Volumes/quant-ssd/projects/tickflow-stock-panel
./dev.sh start

# 2. 跑重算脚本
cd backend
uv run --frozen python scripts/recalc_fills.py
# 期望: 输出 recalc_report.json, account.json 有 backup

# 3. 跑一次策略 (manual trigger)
curl -X POST localhost:3018/api/trading/strategies/dividend_auntie/run \
  -H 'Content-Type: application/json' \
  -d '{"event": "rebalance"}'
# 期望: RunReport 落 strategy_runs.jsonl, orders 落 orders.jsonl

# 4. 撮合线程撮合 (等 5~60s)
sleep 60
curl localhost:3018/api/trading/orders?status=filled
# 期望: 部分 fill 落账

# 5. 失败重试
curl -X POST localhost:3018/api/trading/runs/{run_id}/retry
# 期望: 新 RunReport(retry_of=run_id), 按 as_of 撮合

# 6. 17:00 清算
# 改系统时间到 16:59 或 mock 触发
curl -X POST localhost:3018/api/trading/clearance/run
# 期望: 所有 NEW 变 CANCELED
```

### 9.4 视觉

- 打开 `http://localhost:3011/live-trading`
- 4 列布局：左 持仓 / 中 Tabs / 右 消息 / 底 运行历史
- 顶部状态条 4 张 KPI 卡片
- 切换 tab 状态保留
- 收到 trading_event 消息流立即追加

---

## 10. 不在本 task

- 通知 / 微信 / send_message（独立 task `live-trading-notification.md`）
- 监控中心页接通（独立 task，同上）
- 真实券商对接（`BrokerAdapter` Protocol 已留口）
- 回测引擎兼容（回测走 `backtest.worker`，独立路径）

---

## 11. 修改文件清单（预估）

### 后端（新增 4 / 改 6 / 测试 4）
- 新增：`backend/app/services/trading/slippage.py`、`backend/app/services/trading/clearance.py`、`backend/app/services/trading/retry.py`、`backend/scripts/recalc_fills.py`
- 改：`backend/app/services/trading/models.py`、`backend/app/services/trading/paper_broker.py`、`backend/app/services/trading/engine.py`、`backend/app/services/trading/scheduler.py`、`backend/app/services/trading/executor.py`、`backend/app/services/trading/preferences.py`、`backend/app/api/trading/router.py`
- 测试：`tests/test_trading_slippage.py`（新）、`tests/test_trading_clearance.py`（新）、`tests/test_trading_retry.py`（新）、`tests/test_trading_paper_broker.py`（改）、`tests/test_trading_engine.py`（改）、`tests/test_trading_scheduler.py`（改）、`tests/test_trading_executor.py`（改）

### 前端（新增 5 / 改 3）
- 新增：`frontend/src/components/live/TopBar.tsx`、`PositionsSidebar.tsx`、`CenterTabs.tsx`、`MessagesStream.tsx`、`RunHistory.tsx`
- 改：`frontend/src/pages/LiveTrading.tsx`、`frontend/src/lib/api.ts`、`frontend/src/lib/queryKeys.ts`
- 测试：`frontend/src/components/live/*.test.tsx`（新）

### 文档
- 新增：`docs/live-trading-joinquant-parity.md`（本文）
- 新增：`docs/live-trading-notification.md`（独立 task 范围）
- 改：`docs/secondary-development.md`（更新 trading 章节，标记新能力）
- 改：`AGENTS.md`（已加 "Task 前置规范"）
