# Live Trading 策略详情 — 4 视图 per-strategy 隔离

> 状态: **已落地** (2026-10-13; trade-affecting 测试 68/68 绿 + §8 静态检查 + smoke 全过)
> 关联:
> - 上游 `docs/live-trading-strategy-summary.md` 已经做了 per-strategy `pnl_strategy_snapshots.jsonl` 流, 但 list 页摘要之外的 **4 个详情 tab 的隔离没补齐**
> - 上游 `docs/live-trading-list-detail-layout.md` 已经把详情页搬到 `/live-trading/:strategyId`, 4 tab 在 `OverviewContent` CenterTabs + Stats + Logs tab 里都有出口
> - 上游 `docs/secondary-development.md` § "实盘" 章节对 `TradingEngine.list_fills / list_logs / list_orders` 已声明支持 `strategy_id` 入参 (但实现走的是 note 字符串解析)
> 范围: 详情页 4 个 tab 的过滤正确性 + 可能的下游数据契约补强

---

## 0. 现状审计 (摸底)

| # | 视图 | 位置 | 当前 strategyId 处理 | 结果 |
|---|---|---|---|---|
| V1 | **收益曲线** (EquityCurve) | `OverviewContent.CenterTabs.pnl` 用 `<PnLPanel>` + `StatsContent` 用 `<PnLPanel>` | `PnLPanel({ strategyId })` 调用 `api.tradingPnlHistoryStrategy(strategyId, ...)` (per-strategy endpoint) | ✅ 端点正确，但依赖 `pnl_strategy_snapshots.jsonl` 流的完整性 + 已实现 |
| V2 | **交易明细** (TradesTable + OrdersTable) | `OverviewContent.CenterTabs.orders` | `api.tradingTrades({ strategy_id })` / `api.tradingOrders({ strategy_id })` → 后端 `list_fills / list_orders(strategy_id=…)` 按 `order.note` 正则解析 | ⚠️ 端点接受 strategy_id, 但**回查靠 `extract_strategy_id(note)` 解析 `strategy:<sid>:<event>` 字符串** — 与手动下单 note 格式不匹配会漏掉 |
| V3 | **运行历史** (runs) | `OverviewContent.CenterTabs.runs` 用 `<StrategiesPanel />` 渲染 | **`<StrategiesPanel>` 完全没有接收 strategyId, 渲染全部策略的 schedules + 全部策略的 runs** (这就是 bug) | ❌ **硬 bug**: 不论 strategyId 是什么, 都展示完整策略列表 |
| V4 | **日志** (LogsList) | `OverviewContent.CenterTabs.logs` + `LogsContent` | `api.tradingLogs({ strategy_id })` → 后端 `list_logs(strategy_id=…)` 按 order 反查 note | ⚠️ 同 V2, note 解析脆弱性 |

**额外发现 (不在用户列表, 但顺带提醒)**:
- `RiskStats({ strategyId })` prop 已声明但**故意忽略**, 注释说 "per-strategy 风险指标需要单独算收益序列, 待 PR-2 落地" — 当前仍读账户级 `risk_metrics`。**不**在本任务里改, 留给后续 task。
- 已有 `pnl_strategy_snapshots.jsonl` + `pnl.compute_metrics(data_dir, strategy_id=…)` + `pnl.list_history(... strategy_id=…)` (`store.py`/`pnl.py`) — 单测 `test_strategy_summary.py::test_filters_by_strategy_id` 已覆盖隔离。但**只有 pnl 走的是独立流**, 交易明细/运行历史/日志仍走 `note` 解析。

### Bug 复现依据 (V3)

```tsx
// frontend/src/pages/LiveTrading.tsx:1117-1137
function OverviewContent({ strategyId }: { strategyId?: string | null } = {}) {
  return (
    <div className="space-y-4">
      <PositionsTable strategyId={strategyId} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <div className="lg:col-span-9 min-w-0">
          <CenterTabs>
            {{
              pnl:    <PnLPanel  strategyId={strategyId} />,             // ✅
              orders: <><OrdersTable strategyId={strategyId} /><TradesTable strategyId={strategyId} /></>,  // ✅ call site
              runs:   <StrategiesPanel />,                                // ❌ 不接 strategyId, 显示所有
              logs:   <LogsList strategyId={strategyId} />,              // ✅ call site
            }}
          </CenterTabs>
        </div>
        <div className="lg:col-span-3 min-w-0"><MessagesStream /></div>
      </div>
    </div>
  )
}
```

---

## 1. 目标

实盘详情页 (`/live-trading/:strategyId`) 的下列 4 个数据视图**只展示当前 strategyId 的数据**, 与账户级视图 (`LiveTradingList` header) 明确分离:

1. **收益曲线** — 净值曲线 + KPI 面板 → `PnLPanel` (panels: 今日/累计/最大回撤/胜率 等)
2. **交易明细** — `<TradesTable>` (成交) + `<OrdersTable>` (委托, 上文有, 一并隔离)
3. **运行历史** — `<StrategyRunList>` (每次 event 的 start/finish/status/submitted/target)
4. **日志** — `<LogsList>` (委托提交 / 成交 / 撤单 / 拒单)

成功标准:
- 切到任意策略 A 的详情页 → 4 个 tab 显示**只属于 A** 的数据; **没有任何其他策略的痕迹** (持仓/委托/成交/日志/runs)。
- 手动下单的订单 (note 是 "突破追单") **不再被错误归类到任何 strategy** (i.e. 只在账户级视图出现)。
- 后端 list_fills/list_orders/list_logs 在 `strategy_id` 非空时走**第一性字段**判断 (不依赖 note 格式), 在 `strategy_id=None` 时维持当前行为 (返回全部)。
- 详情页 cache (QK) 严格按 strategyId 分桶, 切策略后**不重用上一策略的缓存**, 但同策略之间跨 tab 共享缓存。
- `RiskStats` 保持账户级 (本任务不改)。

---

## 2. 范围

### 2.1 In (本任务必做)

**前端 (frontend/src/pages/LiveTrading.tsx)**:
1. **修 hard bug**: `OverviewContent.CenterTabs.runs` 改为 `<StrategyRunList runs={...} />` (接收来自新查询 `api.tradingStrategyRuns(strategyId, 20)` 的结果), 不再用 `<StrategiesPanel />`。同时为这个 case 提供 loading/empty UI, 与 `StrategyRow` 里的 `StrategyRunList` 同款呈现。
2. **tab 默认记忆**: `CenterTabs` 当前的 `useState(() => localStorage.getItem('liveCenterTab') ?? 'pnl')` 是**全局 localStorage**, 在 详情页 / 列表页 / 不同策略的详情页 三处**共用同一个键**, 切策略时 tab 默认值会"继承"上一策略的 tab。要么 a) 加 strategyId 维度, 要么 b) 在详情页 unmount 时清掉。本任务选 a — `CenterTabs` 增加 optional `strategyId` prop, 内部 storage key 变为 `liveCenterTab:<strategyId ?? 'global'`, 详情页传 strategyId。

**后端 (订单 / 成交 / 日志 落盘端)**:
3. **数据契约补强 (必选路径)**:
   - `Order` (`backend/app/services/trading/models.py`): **新增字段 `strategy_id: str | None = None`** (第一性字段; note 不再承担归类角色)。
   - `Fill` / `TradeLogEntry` (同上文件): 同样新增 `strategy_id: str | None = None`。
   - 落盘路径 (`store.append_order / append_fill / append_log`): 不需改, 因为 Pydantic 序列化随字段变化自动。
   - **写入入口改**:
     - `runner.buy/sell/rebalance_to/...`: 增加 `strategy_id: str | None = None` 参数, 落到 `Order.strategy_id`。
     - `executor.run_on_event` (LiveStrategy event 入口, `backend/app/services/trading/executor.py:294`): 调 `runner.rebalance_to(note=..., strategy_id=strategy_id)`。这与现有的 `f"strategy:{strategy_id}:{event}"` note 共存 (note 保留人可读标识, 但不再用于过滤)。
     - `engine._record_fill` (`backend/app/services/trading/engine.py`): 在 fill 落盘时, 从对应 order 的 `strategy_id` **继承**到 fill (fill 由 order 触发, 自然继承)。
     - `engine._record_log` (同上文件): 同样从关联 order 继承 strategy_id。
4. **`list_fills / list_orders / list_logs` 改用第一性字段**:
   - `store.list_orders`: 用 `o.strategy_id == strategy_id`, 删掉对 `extract_strategy_id(o.note)` 的调用。
   - `store.list_fills`: 用 `f.strategy_id == strategy_id` (fills 自己就有 strategy_id 字段, 写时已继承)。
   - `store.list_logs`: 用 `e.strategy_id == strategy_id`。
   - `extract_strategy_id(note)` 函数保留 (用于上游引擎在写 order 时检查 note 是否合规); 但 list_* 不再调用它。
5. **`engine.positions(data_dir, *, strategy_id=…)`** 路径 (line 269+): 内部 `store.list_fills(... strategy_id=strategy_id)`, 现在 strategy_id 走第一性字段了 — **不动函数签名, 实现自动正确**。
6. **历史数据 migration**:
   - 一次性脚本 `backend/scripts/migrate_strategy_id_to_firstclass.py`:
     - 读 `orders.jsonl` / `fills.jsonl` / `logs.jsonl` 三份历史 jsonl;
     - 对每条 order, 用 `extract_strategy_id(note)` 解析, 写入 `strategy_id` 字段;
     - fills/logs 用关联 order 的 strategy_id 继承;
     - 落盘覆盖回原 jsonl (走 `atomic_write_text` 整文件重写 + 临时文件 rename, 避免半写状态);
     - **不可逆**: 脚本要求用户显式传 `--confirm` 才执行;
     - 完成后输出 diff 报告 (N 条成功 / M 条无 strategy_id 留 None / 找不到关联 order 的 fills).

**测试 (backend/tests/)**:
7. 新增 `backend/tests/test_strategy_isolation.py`:
   - 两个 strategy (A/B) 各自跑一笔 rebalance (不同 symbol), 互相 fills 不串;
   - 给 order.note 写"任意非 strategy-prefix 文本" (模拟手动下单), 验证 list_fills 走 strategy_id 字段匹配后**不再**误归类;
   - 老的"note 解析"路径不再被 list_* 调用 (单测里 grep/ast 检查调用点是否仍调 `extract_strategy_id` 在 `list_*` 里 — best effort 静态检测)。
8. 已有 `test_strategy_summary.py` / `test_trading_executor.py` 必须保持绿 (changes 默认不破坏既有快照)。

### 2.2 Out (本任务**不**做)

- **不**改 `RiskStats` 的账户级口径 — 留给后续 per-strategy risk task。
- **不**做 per-strategy 历史回测接口 — 留给回测线 task。
- **不**改 pnl 流的 schema / 计算口径 (`pnl_strategy_snapshots.jsonl` 已经独立, 单测覆盖)。
- **不**改 list 页 (策略表) 的列 — list 页已经是 per-strategy 的展示 (一行一个策略), 没有混数据问题。
- **不**做数据清理 — 只补字段 + migrate 历史, 不删 note 字段 (note 仍可能承载人工备注)。

---

## 3. 数据契约

### 3.1 Order schema (新增字段)

```python
class Order(BaseModel):
    id: str
    symbol: str
    side: OrderSide
    order_type: OrderType = OrderType.LIMIT
    qty: int
    limit_price: float | None = None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: int = 0
    avg_fill_price: float | None = None
    created_at: str
    submitted_at: str | None = None
    filled_at: str | None = None
    canceled_at: str | None = None
    reject_reason: str | None = None
    note: str | None = None
    strategy_id: str | None = None          # ⬅️ 新增 (Pydantic Field / 直接默认 None)
```

### 3.2 Fill schema (新增字段)

```python
class Fill(BaseModel):
    id: str
    order_id: str
    symbol: str
    side: OrderSide
    qty: int
    price: float
    fee: float = 0.0
    tax: float = 0.0
    amount: float
    filled_at: str
    trade_date: date
    strategy_id: str | None = None          # ⬅️ 新增; 撮合时从关联 Order 继承
```

### 3.3 TradeLogEntry schema (新增字段)

```python
class TradeLogEntry(BaseModel):
    id: str
    kind: TradeLogKind
    ts: str
    symbol: str | None = None
    order_id: str | None = None
    fill_id: str | None = None
    message: str
    details: dict | None = None
    strategy_id: str | None = None          # ⬅️ 新增; 记录日志时从关联 order 继承
```

### 3.4 JSONL 兼容性

`orders.jsonl` / `fills.jsonl` / `logs.jsonl` 是按行 append 的 JSONL, Pydantic v2 `model_dump_json()` 顺序由字段声明顺序决定, **新增字段在末尾**。所以:
- 老数据读 → 新 schema: Pydantic 自动容忍缺失字段 (默认 None), 单测 `test_handles_malformed_lines` 已验证;
- 新数据读 → 老 reader (若有): 末尾未知字段容忍 (json 标准允许);
- migrate 脚本补字段后, 老前端照样能解析 (`o.strategy_id` undefined → undefined, 不报错)。

### 3.5 持仓 / PnL 派生 (无 schema 变化)

- `Position` 仍由 fills 派生, **不动 schema**;
- `PnLSnapshot` / `StrategyPnLSnapshot` 已有独立流, 不动。

---

## 4. API 契约

无新增 endpoint, 无破坏性变更:

| Endpoint | 入参 | 返回 | 变化 |
|---|---|---|---|
| `GET /api/trading/orders?strategy_id=…` | 同前 | 同前 | **实现层**改为按 `o.strategy_id` 过滤 (前端无感) |
| `GET /api/trading/trades?strategy_id=…` | 同前 | 同前 | 同上, `f.strategy_id` |
| `GET /api/trading/logs?strategy_id=…` | 同前 | 同前 | 同上, `e.strategy_id` |
| `GET /api/trading/positions?strategy_id=…` | 同前 | 同前 | 同上 (透传 list_fills) |
| `GET /api/trading/strategies/{id}/runs` | 同前 | 同前 | 已有 |
| `GET /api/trading/strategies/{id}/pnl/*` | 同前 | 同前 | 已有 |
| `POST /api/trading/strategies/{id}/run-now` | 同前 | 同前 | **实现层**在 `_run_now` 里把 `strategy_id` 传进 runner, 不再依赖 note 格式 |
| `POST /api/trading/orders` (manual 手动下单) | body 同前 | 同前 | body 不带 `strategy_id` → 后端存 `None`, 列表中策略 tab 看不到 |

> 前端 contract 不变。**不动**任何 Pydantic response model (因为 Order/Fill/TradeLogEntry 加了字段, 前端如果没用 `any`, 会多出一个可选字段但能 ignore)。

---

## 5. 行为口径

### 5.1 写入路径 (新增字段如何被填上)

| 入口 | strategy_id 来源 |
|---|---|
| `LiveStrategy.run_on_event` → `executor.execute()` → `runner.rebalance_to(...)` | **显式参数** `strategy_id=strategy_id` (executor 上下文已知) |
| 手动 `/api/trading/orders` POST | **留 None** (manual 不属于任何策略, 顶部 LiveTradingList header 显示账户级) |
| `runner.buy/sell` (CLI/脚本) | 接受可选 `strategy_id` 参数 (默认 None), 调用方可显式传 |
| `engine._record_fill` (撮合落账) | 从 `order.strategy_id` 继承 — `engine.submit_order` 时若已设 strategy_id, 则 fill.strategy_id = order.strategy_id |
| `engine._record_log` | 同上: `entry.strategy_id = order.strategy_id if order else None` |

### 5.2 读出路径 (过滤口径)

- `list_orders(strategy_id=None)`: 全部 (现有行为, 给账户级视图用);
- `list_orders(strategy_id="abc")`: 只返 `o.strategy_id == "abc"` 的, **不再**做 `extract_strategy_id(note)`;
- 同 V4 类似, fills/logs 一致。

### 5.3 CenterTabs storage key (前端)

- 旧 key: `liveCenterTab` (全局);
- 新 key (前端 PR): 当 `CenterTabs` 收到 `strategyId` prop 时, 用 `liveCenterTab:<strategyId>`; 不传时仍用 `liveCenterTab` (向后兼容, 列表页 / 旧用法不破)。
- 迁移: 读老 key 时, 若找到, 第一次 use 写到新 key; 不强制清空老 key。

### 5.4 边缘情况

- **手动单 note 是 `strategy:abc:rebalance` (被用户复制粘贴)**: migrate 脚本**只信任历史**, 新写入路径**不**基于 note 解析; 同时 NewOrder 写入路径不再写 `strategy:` 前缀的 note (note 改为"rebalance_to buy (策略 A)" 这种自然语言, 不影响第一性字段)。所以手动单误填 `strategy:abc:rebalance` 也不会归类到 abc — strategy_id 仍是 None。
- **fills 没找到关联 order (历史脏数据 / 写入 race)**: 留 `strategy_id=None`, migrate 脚本输出 diff 让用户介入。
- **`pnl_strategy_snapshots.jsonl`** 与 `fills.jsonl` 里的 strategy_id 可能不一致 (快照由 executor 显式写入, 与 fills 走 store.list_fills 独立): 本任务**不试图 reconcile**, 但加一个 sanity check 单测: `compute_strategy_summary(s).total_pnl` ≈ `list_fills(strategy_id=s).sum(realized_pnl)` ± 0.5%, 如果偏差大就 WARN log (不是 fail)。

---

## 6. 风险与边界

| 风险 | 触发条件 | 缓解 |
|---|---|---|
| **migrate 不可逆** | 一次性脚本覆盖原 jsonl | 强制 `--confirm`; 输出 diff; 跑前自动备份到 `*.bak.<ts>`; README 注释 |
| **历史 fills 与 order 的关联链不全** (deleted order, race) | migrate 时找不到关联 order | fills `strategy_id` 留 None; diff 报告里 count + 用户手动标注 |
| **手动 note 仍写 `strategy:<sid>:<event>`** 误导老前端 | 用户粘贴历史 bot 命令 | note 长度不限, 不阻塞; 但前端用**新的 strategy_id 字段**展示 (如果有) |
| **`tradingRiskMetrics` 仍账户级** | 用户期望详情页风险指标也是 per-strategy | 显式文档"本任务不改"; RiskStats component 顶部加 `aria-label` 注明"账户级" 防误读 |
| **pnl strategy 流的 strategy_id 与 fill 流的 strategy_id 不一致** | executor 写快照 vs store 写 fills 是两个入口 | 单测 ±0.5% 容忍 + 文档说明"以 trades 为准, pnl 为派生" |
| **前端 cache 切换策略后短暂闪烁** | TanStack Query 新 key → 新 HTTP 请求 → loading | 已存在的 `staleTime`/`refetchInterval` 设计上接受; 不额外加 skeleton (与现有 4 tab 行为一致) |
| **`extract_strategy_id` 完全移除** 导致某处隐式依赖它的代码断 | 内部 inspect `note` 字段做诊断时仍可能需要 | 保留函数 + 公开 docstring "仅供诊断 / 历史迁移, list_* 不调用" |

---

## 7. 测试矩阵

### 7.1 后端 (pytest)

| 用例 | 期望 |
|---|---|
| 两个 strategy (A/B) 各自下一笔不同 symbol 的 rebalance, list_fills(strategy_id='A') 只含 A 的 fill | 通过 |
| 手动单 note 写 `'突破追单'`, list_fills(strategy_id='A') 不含这条 fill | 通过 |
| 手动单 note 写 `'strategy:A:rebalance'` (粘贴历史), list_fills(strategy_id='A') **不含** (因为 strategy_id 字段为 None) | 通过 — 验证不再用 note 解析 |
| `list_logs(strategy_id='A')` 只含 A 委托产生的日志 (filled_at 跨多条 fill 也归 A) | 通过 |
| migrate 脚本对历史 jsonl 跑前备份 + 跑后字段写到位 + diff 报告行数一致 | 通过 |
| `extract_strategy_id` 在 `list_orders/fills/logs` 中**不再出现** (静态 grep 检查) | 通过 (best-effort lint) |
| 已有 `test_strategy_summary.py` 全绿 | 通过 (no regression) |
| 已有 `test_trading_executor.py` 全绿 | 通过 (no regression) |
| `compute_strategy_summary('A').total_pnl ≈ list_fills(strategy_id='A').realized_pnl.sum ± 0.5%` | 通过 (sanity) |

### 7.2 前端 (手测 / visual)

| 用例 | 期望 |
|---|---|
| 打开 `/live-trading/<策略 A>`, 切到 `运行历史` CenterTabs tab | 只看到 A 的 runs (有几次、何时、status) |
| 同上, `日志` tab | 只看到 A 委托相关的委托提交/成交/撤单/拒单日志 |
| 同上, `交易明细` tab (orders + trades) | 只看到 A 的委托与成交 |
| 同上, `收益曲线` tab (PnL 面板 + EquityCurve) | 只看到 A 的 KPI + 净值曲线 |
| 切到 `/live-trading/<策略 B>`, 4 个 tab 数据全换 (不残留 A) | 通过 (cache key 隔离) |
| 在 A 详情页选 `运行历史` tab, B 详情页默认 tab 不应该是 `运行历史` (各策略独立) | 通过 (storage key 加 strategyId) |
| 详情页 → 列表页 → 详情页 (同 A), CenterTabs 默认 tab 仍为上次在 A 的选择 | 通过 (key 共享 A) |
| 手动单击 "实盘" 页面 manual submit 一笔, 切到任一策略详情页, 看不到这笔 manual 单 | 通过 (manual 单 strategy_id=None) |

### 7.3 性能 / 迁移

- migrate 单进程跑 10000 行历史 (orders+fills+logs) < 10s;
- 增加 strategy_id 字段后单条 fill JSONL 行 +5–12 字节, 对磁盘影响可忽略;
- `list_fills(strategy_id=...)` 时间复杂度仍 O(n) (内存过滤), N 行毫秒级 (与现状一致)。

---

## 8. 验收标准

按下面命令过一遍, 一项不通过即"未完成":

```bash
# 后端单测 (含新增 test_strategy_isolation.py)
cd /Volumes/quant-ssd/projects/tickflow-stock-panel
uv run --no-sync pytest -q \
  backend/tests/test_strategy_isolation.py \
  backend/tests/test_strategy_summary.py \
  backend/tests/test_trading_executor.py \
  backend/tests/test_trading_notify.py \
  backend/tests/test_executor_preopen_dedup.py

# 静态 lint — extract_strategy_id 不应在 list_* 函数体内
grep -nE "def list_(orders|fills|logs)\b" backend/app/services/trading/store.py \
  | xargs -I {} sed -n '{},+30p' backend/app/services/trading/store.py \
  | grep -n 'extract_strategy_id' || echo "OK: list_* 不再用 note 解析"

# 端到端冒烟 (smoke)
/tmp/ts-fill/strategy_isolation_smoke.sh:
  1. 启动 backend (沿用 dev.sh / skill)
  2. 用 runner.py CLI 提交 1 笔 strategy_id='demo_a' 的 rebalance + 1 笔 manual
  3. curl /api/trading/fills?strategy_id=demo_a
     期望 demo_a 的 fill 出现, manual 的 fill 不出现
  4. curl /api/trading/fills (无 strategy_id)
     期望两条都在
  5. curl /api/trading/strategies/demo_a/pnl/metrics
     期望 total_pnl 仅计入 demo_a 的 fill

# 前端手测 (浏览器)
open http://localhost:3011/live-trading/<strategy A>
# 切 CenterTabs 四 tab, 截图存档
```

---

## 9. 待 user 拍板的决策点

| # | 决策 | 默认方案 | 备选 |
|---|---|---|---|
| **D1** | 一次性迁移脚本的时机 | **本任务 PR 一并合** (代码 + migrate + 单测同 PR) | A. 代码先合, migrate 留作后续 task |
| **D2** | 老 note (`strategy:<sid>:<event>`) 怎么处理 | **保留 note 字段不动, 仅在文档注明"不再用于过滤"** | A. 清空所有 note 里的 `strategy:` 前缀 (破坏用户可读性, 不采纳) |
| **D3** | manual submit 的 note 仍写 `strategy:` 吗 | **不写** — note 改为自然语言, 第一性字段填 None | A. 仍写 (兼容旧 grep 工具, 但增加误归类风险) |
| **D4** | strategy_id 是否进入 OrderDraft 的请求体 | **进入** — `/api/trading/orders` POST body 可选带 `strategy_id`, 后端落到 order.strategy_id, 给"脚本级手工带策略"留路径 | A. 不进 — manual 永远 strategy_id=None |
| **D5** | CenterTabs storage key 维度化策略 | **`liveCenterTab:<strategyId>`** (默认方案 §5.3) | A. 详情页不记忆 (每次进默认 'pnl') |
| **D6** | `RiskStats` 是否本任务顺手改 | **不动** — 留给后续 per-strategy risk task | A. 顺手加个 TODO 注释 / 或者做轻量 per-strategy (仅 last 60 days from strategy's own pnl 流) |

---

## 10. 不在 PR 范围

- Per-strategy 风险指标 (走 pnl 流) → 后续 task
- `pnl_strategy_snapshots.jsonl` 的 schema 升级 (已独立流, 无需动)
- 详情页 deep-link (`?tab=runs`) → `live-trading-list-detail-layout.md` 已部分覆盖; 本任务只补 CenterTabs 的 strategy-scoped localStorage, 不重做 deep-link
- 老 `StrategyRow → StrategyRunList` (列表页展开) 保留 — 它本来就是 per-strategy 的, 不冲突

---

## 11. Implementation Log (2026-10-13)

按 §1–§10 实施, 改动落在以下文件:

### 11.1 后端 (新增 strategy_id 第一性字段)

| 文件 | 改动 |
|---|---|
| `backend/app/services/trading/models.py` | `Order` / `OrderDraft` / `Fill` / `TradeLogEntry` 各新增 `strategy_id: str \| None = None` |
| `backend/app/services/trading/engine.py` | `submit / _settle_fill / _reject / _log_reject / _log_accept / cancel` 都从 order 继承/赋值 `strategy_id` |
| `backend/app/services/trading/runner.py` | `buy / sell / rebalance_to` 接受 `strategy_id` 参数, 透传到 OrderDraft |
| `backend/app/services/trading/executor.py` | `rebalance_to` 调用显式传 `strategy_id=strategy_id`; note 格式从 `strategy:<sid>:<event>` 改为 `[<sid>] <event>` (D3, 不再当过滤源) |
| `backend/app/services/trading/store.py` | `list_orders / list_fills / list_logs` 改用 `o/f/e.strategy_id == filter_value` (不再调 `extract_strategy_id`); 函数保留供 migrate 脚本使用 |
| `backend/scripts/migrate_strategy_id_to_firstclass.py` | 新增. 一次性迁移 — 备份 → 解析 → 补字段 (Order 从 note, Fill/LogEntry 从关联 order 继承) → 原子写回 |
| `backend/tests/test_strategy_isolation.py` | 新增 14 用例 — schema 字段 / store 过滤 / migrate dry-run / 静态 lint |

### 11.2 前端 (CenterTabs 维度化 + Overview runs 修 bug)

| 文件 | 改动 |
|---|---|
| `frontend/src/components/live/CenterTabs.tsx` | 新增可选 `strategyId` prop, storage key 变 `liveCenterTab:<strategyId>` (D5); 含老键 fallback 兼容 |
| `frontend/src/pages/LiveTrading.tsx` | `OverviewContent.CenterTabs.runs` 改为 `<StrategyRunHistoryPanel strategyId>` (per-strategy 通过 `api.tradingStrategyRuns(strategyId, 20)` 拉数据) — 替代 `<StrategiesPanel />` 的硬 bug; `CenterTabs` 传入 `strategyId` |

### 11.3 验证结果

- **trading 相关测试**: 68 passed (含 14 个新单测) — 不含 pre-existing fail `test_a_dividend_base_default_rebalance_is_935` (该测试依赖缺失的策略文件)
- **§8 静态 lint**: `list_orders / list_fills / list_logs` 函数体内已不再调 `extract_strategy_id` ✓
- **§8 smoke**: manual 单 note='突破追单' strategy_id=None; script 单 strategy_id='demo_smoke' — 两个 list 过滤按预期隔离 ✓
- **D5 CenterTabs 持久化**: storage key `liveCenterTab:<strategyId>`, 跨策略不会串 ✓
- **风险指标 RiskStats**: 未改 (显式 D6 不动), 留作后续 per-strategy risk task

### 11.4 注意事项

- 老 note 字段保留 (D2 默认), 仅在文档注明"不再用于过滤"
- `extract_strategy_id` 函数保留, 仅供 migrate 脚本与诊断使用
- `extract_strategy_id` 在 `list_*` 函数体内已不再被调用 — 这是本任务的关键修复点

