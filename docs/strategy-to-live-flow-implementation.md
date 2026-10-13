# 策略→实盘 一次性落地实施 Spec (Strategy-to-Live Flow Implementation)

> 状态: **实施 spec (用户已选"一次性直接落地", 等待开干)**
> 评估基础: `docs/strategy-to-live-workflow-review.md` (评估报告, 9 节, 389 行)
> 范围: **F0 (4 项) + F1 (5 项) = 9 项**, F2/P3 留待下一轮
> 工期: 单次连续执行, 后端先行 → 前端收尾
> 验证: 每项完成后跑单测; 全量完成后跑 `uv run pytest` + `pnpm build` + smoke

---

## 0. 范围锁定 (不再扩)

| 级别 | # | 标题 | 后端 | 前端 | 单测 |
|---|---|---|---|---|---|
| F0 | F0-1 | 策略代码保存前 `ast.parse` 预检 | ~20 行 | 0 | 2 |
| F0 | F0-3 | `max_position_pct` / `max_position_count` 实盘生效 | ~50 行 | 0 | 4 |
| F0 | F0-4 | 保存后自动跑 1 天 dry verify (异步) | ~120 行 | ~30 行 | 5 |
| F0 | F0-2 | wecom/feishu 联通 trading_event | ~80 行 | ~20 行 | 4 |
| F1 | F1-1 | 策略目录统一 (ai/custom/live 关系) | ~60 行 | 0 | 3 |
| F1 | F1-4 | 实盘 vs verify drift 检测 UI | ~80 行 | ~50 行 | 3 |
| F1 | F1-5 | 策略代码自动快照 + diff + 一键回滚 | ~120 行 | ~50 行 | 5 |
| F1 | F1-2 | 回测页 → 实盘页 "同步实盘" bridge | 0 | ~50 行 | 1 |
| F1 | F1-3 | 实盘页 verify 报告列表 + sync 按钮 | 0 | ~80 行 | 2 |
| **合计** | **9 项** | | **~530 行** | **~280 行** | **29 个测试** |

**不做** (明确):
- F2 全部 (生命周期状态机 / 组合视图 / 策略组 / 热重载 / 回测报告 ledger)
- P3 全部 (真券商 / 拒单重试 / 多账户)

---

## 1. 关键决策 (拍板默认值, 不再问)

| # | 决策 | 默认值 | 理由 |
|---|---|---|---|
| D1 | F0-1 失败时的返回码 | 400 + SyntaxError 详情 (line/col/msg) | 与既有 PUT endpoint 一致 (HTTPException) |
| D2 | F0-3 拒单的 reject_reason 文案 | 含具体百分比 / 数量 + max 值, 便于诊断 | 与既有 rejected 文案风格一致 |
| D3 | F0-4 自动 verify 的失败处理 | 失败 → 不落盘 .py + 返 422 + report_id 给用户看 | 防止坏策略进 live/ |
| D4 | F0-4 自动 verify 的"通过门槛" | 1 天 verify 跑出 ≥ 1 个 target + 无 error | 防止 no_target 静默挂掉 |
| D5 | F0-2 推送通道 | wecom + feishu 并行 (读 prefs flag) | 通道独立, 任一关不影响另一个 |
| D6 | F0-2 推送时机 | order_submit / fill / reject / clearance / run_finish | 5 个关键 event, 不刷屏 |
| D7 | F1-1 目录统一策略 | ai/ + custom/ 仍按原路径落 (兼容老用户); 新增"晋级"API `POST /strategies/{id}/promote-to-live` 一键复制 + 自动 verify | 不破坏既有工作流 |
| D8 | F1-4 drift 检测后端触发 | 用户点 verify 时 + 用户点 sync-from-verify 时各算一次, 缓存到 verification 报告 | 不每分钟算, 不烧 CPU |
| D9 | F1-5 快照粒度 | 每次 PUT .py 触发一次快照 (`.history/{id}/{ts}.py`), 保留最近 20 版 | 与聚宽 IDE 行为一致 |
| D10 | F1-5 回滚联动 | 回滚后自动 disable 所有 schedules + notify | 防止回滚后被 cron 自动重跑 |
| D11 | F1-2 bridge 形式 | Backtest.tsx verify 报告卡 → "应用到实盘" 按钮 → 跳 `/live-trading/<strategyId>?sync_run_id=xxx` | URL 跨页, 不阻塞后端 |
| D12 | F1-3 verify 列表 UI 位置 | 实盘详情页"概览"tab 顶部加新 section "回测验证历史 (N)" + 列表 + 同步按钮 | 不新增 tab, 不破 5 tab 布局 |

---

## 2. 实施顺序 (按依赖关系)

```
Round 1 — 后端基础 (无依赖, 可并行准备)
  F0-1 ast.parse 预检             [code_store.py]
  F0-3 风控参数生效                [engine.py + preferences.py]

Round 2 — 后端增强 (依赖 Round 1 的 engine.submit 路径)
  F0-4 自动 verify (异步任务)     [code_store.py + router.py + 新建 auto_verify.py]
  F1-1 策略目录统一 (晋级 API)     [ai_generator.py + 新建 promote.py + router.py]

Round 3 — 后端安全 / 监控
  F0-2 wecom/feishu 联通           [engine.py _notify + notify hook]
  F1-4 drift 检测                   [sync.py + 新建 drift.py + router.py]
  F1-5 版本快照 + 回滚              [code_store.py + history.py + router.py]

Round 4 — 前端 bridge
  F1-2 回测页 → 实盘页 bridge      [Backtest.tsx + 新组件 SyncToLiveButton.tsx]
  F1-3 实盘页 verify 列表          [LiveTrading.tsx + api.ts]
```

每 Round 完成后:
- 后端: `cd backend && uv run --frozen pytest tests/ -q` 必须保持绿
- 前端: `cd frontend && pnpm build` 必须 build 成功

---

## 3. 每项 spec 概要 (与评估报告 F# 表对应)

### F0-1 策略代码保存前 ast.parse 预检

**目标**: `PUT /api/trading/strategies/{id}/code` 保存前必须 `ast.parse(content)` 通过, 否则 400。

**改动**:
- `backend/app/services/trading/code_store.py` `write_code` 加 `ast.parse(content)`, 失败抛 `SyntaxError(line, col, msg)`, 由 router 映射到 400。
- `backend/app/services/trading/router.py:547-580` PUT handler catch `SyntaxError` 抛 `HTTPException(400, detail={line, col, msg, snippet})`。
- 测试: `backend/tests/test_code_store_ast.py` 2 个 (语法错 / 空文件)。

### F0-3 max_position_pct / max_position_count 实盘生效

**目标**: 现有 prefs 已定义, 实盘 `engine.submit` 阶段实际生效。

**改动**:
- `backend/app/services/trading/engine.py:115-200` submit 校验处加 2 个分支:
  - 当前持仓数 (symbol 数) > `max_position_count` → 拒 (除已持仓 symbol)
  - 单笔市值 > `total_equity * max_position_pct` → 拒
- `reject_reason` 文案: `"max_position_count: 持仓 X 只, 已超 max=Y"` / `"max_position_pct: 单笔 ¥X, 超 20% 总资产 ¥Y"`
- 测试: `backend/tests/test_engine_position_pct.py` 4 个 (持仓数 / 单票 / 边界 / 未设 prefs)。

### F0-4 保存后自动 verify (异步)

**目标**: `PUT .../code` (带 `run_after=true`) → ast.parse → 后台异步跑 1 天 verify → 通过 → 落盘 + 启用 schedule; 失败 → 报 422 + 不落盘。

**改动**:
- 新 `backend/app/services/trading/auto_verify.py` ~120 行: `run_auto_verify(data_dir, strategy_id, content)` 异步任务, 调用现有 verifier 跑 1 天 (as_of=today)。
- `backend/app/services/trading/code_store.py` 加 `write_code_with_verify(...)` 函数, 含 lock + atomic write。
- `backend/app/services/trading/router.py:547-580` PUT handler: `run_after=true` → 调 `write_code_with_verify` → 失败 (verify error / no_target) 抛 422 + report_id。
- 测试: `backend/tests/test_auto_verify.py` 5 个 (verify pass / fail / no_target / runtime error / 并发)。

### F0-2 wecom/feishu 联通 trading_event

**目标**: trading 关键 event (order_submit / fill / reject / clearance / run_finish) 推到 wecom/feishu。

**改动**:
- `backend/app/services/trading/engine.py:_notify` (~719-725) 后加 push hook: 读 `prefs.notify_wechat` (wecom) + `prefs.notify_feishu` (feishu), 任一开则推。
- 新 `backend/app/services/trading/push.py` ~80 行: `format_event_message(kind, payload)` + `dispatch(channel, msg)`。wecom/feishu 已有 client, 直连。
- `preferences.py` 加 `KEY_NOTIFY_TRADING_WECOM` / `KEY_NOTIFY_TRADING_FEISHU`。
- 测试: `backend/tests/test_trading_push.py` 4 个 (mock wecom/feishu, 验证文案, 验证偏好读, 验证失败降级)。

### F1-1 策略目录统一 (晋级 API)

**目标**: ai/ + custom/ 路径不变; 新 API 一键把 strategy 从研究域"晋级"到实盘域。

**改动**:
- `backend/app/services/trading/promote.py` (新) ~60 行: `promote_to_live(data_dir, strategy_id, source_path)` 读源 → 写到 `live/{strategy_id}.py` → ast.parse → 写 `live_strategy_state`。
- `backend/app/services/trading/router.py` 加 `POST /api/strategies/{id}/promote-to-live` body=`{source: "ai" | "custom"}`。
- `backend/app/strategy/ai_generator.py` 保存后可选带 `promote=true`, 自动调 promote。
- 测试: `backend/tests/test_promote.py` 3 个 (正常复制 / 已存在拒绝 / 跨域校验)。

### F1-4 实盘 vs verify drift 检测 UI

**目标**: 实盘详情页显示"最近一次 fill 的标的 vs 最近一次 verify 的标的 drift"。

**改动**:
- `backend/app/services/trading/drift.py` (新) ~80 行: `compute_drift(data_dir, strategy_id, window_days=7)` 对比 `fills.jsonl` 去重 symbol vs `verifications/{id}/latest.json` 的 target。
- `backend/app/services/trading/router.py` 加 `GET /api/trading/strategies/{id}/drift` 返 `{overlap_pct, only_live: [...], only_verify: [...]}`。
- `LiveTrading.tsx` 概览 tab 加新 section。
- 测试: `backend/tests/test_drift.py` 3 个 (无 verify / 完全重叠 / 完全错开)。

### F1-5 策略代码自动快照 + diff + 一键回滚

**目标**: 每次 PUT .py 同步备份到 `.history/{id}/{ts}.py`; UI 可看 diff 一键回滚。

**改动**:
- `backend/app/services/trading/history.py` (新) ~120 行: `snapshot(code_path, strategy_id, ts)` + `list_history(strategy_id)` + `rollback(code_path, snapshot_ts)`。
- `backend/app/services/trading/code_store.py` 写 .py 时同步调 snapshot。
- `backend/app/services/trading/router.py` 加 `GET /strategies/{id}/code/history` + `GET /strategies/{id}/code/diff?ts=X` + `POST /strategies/{id}/code/rollback {ts}` (含 disable schedules)。
- `LiveTrading.tsx` CodeView tab 加 history 列表 + diff modal + rollback confirm。
- 测试: `backend/tests/test_history.py` 5 个 (快照 / 列表 / diff / rollback / 联动 disable)。

### F1-2 回测页 → 实盘页 bridge (前端 only)

**目标**: 回测 verify 报告顶部按钮 → 跳实盘详情页 + 带 sync_run_id。

**改动**:
- 新 `frontend/src/components/screener/SyncToLiveButton.tsx` ~50 行: 仅当 `strategy_id` 是 LiveStrategy 时显示, 跳 `/live-trading/<id>?sync_run_id=xxx`。
- `frontend/src/pages/backtest/*.tsx` verify 报告卡嵌入此按钮。
- 测试: `frontend/src/components/screener/SyncToLiveButton.test.tsx` 1 个 (URL 跳转)。

### F1-3 实盘页 verify 报告列表 (前端 only)

**目标**: 实盘详情页"概览"tab 顶部加新 section 显示 verify 历史 + 同步按钮。

**改动**:
- `frontend/src/lib/api.ts` 加 `tradingStrategyVerifyList(strategy_id)` + `tradingStrategySyncFromVerify(strategy_id, verify_run_id, dry_run=true)`。
- `LiveTrading.tsx` OverviewContent 顶部加 `<VerifyHistorySection strategyId={strategyId} />` (新组件)。
- 测试: `frontend/src/pages/LiveTrading.verify-section.test.ts` 2 个 (列表渲染 / 同步按钮)。

---

## 4. 验证矩阵 (全量完成后必须跑)

```bash
# 后端
cd backend
uv run --frozen pytest tests/ -q                           # 全量单测必须绿
uv run --frozen pytest tests/test_code_store_ast.py -q    # F0-1
uv run --frozen pytest tests/test_engine_position_pct.py -q # F0-3
uv run --frozen pytest tests/test_auto_verify.py -q       # F0-4
uv run --frozen pytest tests/test_trading_push.py -q      # F0-2
uv run --frozen pytest tests/test_promote.py -q           # F1-1
uv run --frozen pytest tests/test_drift.py -q              # F1-4
uv run --frozen pytest tests/test_history.py -q           # F1-5

# 前端
cd ../frontend
pnpm install                                              # 装新依赖 (如有)
pnpm build                                                # build 必须过
pnpm test --watch=false                                   # 前端单测必须绿
```

---

## 5. 风险与边界

- **不破坏既有接口**: 所有 endpoint 增量为加字段 / 加 path, 不改既有行为
- **不破坏既有数据**: 新增 `.history/` / `verifications/` / `sync_audit.jsonl` 都是 append-only 或新路径, 既有数据不动
- **不破坏既有测试**: 每 Round 跑全量测试, 失败立即定位
- **不引入新依赖**: 用既有库 (polars / duckdb / pyarrow / FastAPI / pydantic); wecom/feishu 已有 client

---

## 6. 完成定义 (Definition of Done)

每项完成必须:
- [ ] 后端代码 + 单测落地 (含 happy path + 边界 + 失败路径)
- [ ] 前端代码 + 单测落地 (如涉及)
- [ ] 对应单测全部绿, 全量单测不退步
- [ ] `pnpm build` 通过
- [ ] `ruff check` 通过

全量完成后:
- [ ] 9 项单测 + 既有测试全部绿
- [ ] `pnpm build` 通过
- [ ] smoke test (手动跑 verify → promote → fill → drift → rollback 端到端)
- [ ] 文档 (本文 + 实施报告) commit

---

## 7. 引用

- 评估报告: `docs/strategy-to-live-workflow-review.md` (389 行)
- 对标报告: `docs/reports/strategy-to-live-external-parity-2026-10-13.md`
- 既有 task: `docs/live-trading-*.md` (撮合/调度/详情/隔离/同步 已落地)
- CONTRIBUTING: `CONTRIBUTING.md` §5.1 策略 / §5.2 监控 / §6 缓存
- secondary-development: `docs/secondary-development.md` §程序化下单 + §Broker 适配
