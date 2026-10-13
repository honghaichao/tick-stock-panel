# Live Trading P1 Fix + AI 优化自测 + 回测同步实盘 (2026-10-13)

> 状态: **执行中** (多轮 + 多专家)
> 来源: 09:35 rebalance no_target 修复 + Lock/Curie 架构评审 + 用户新需求
> 关联: docs/live-trading-strategy-isolation.md, docs/secondary-development.md

## 0. 决策点 (默认方案)

| # | 决策 | 默认 |
|---|---|---|
| P1.1 | wait 移出锁的方案 | **方案 A: 把 state 加载 + 阻塞等数据放在锁外 (顺序化 in-flight check 仍在锁内, 但很轻)** |
| P1.2 | wait 失败时 fallback | **超时返回 wait_for_quote_data 当时的 cur, 走稀疏数据 (现状不变)** |
| P1.3 | AI 优化端到端开关 | **"AI 优化" 走两步链: 1) POST /optimize (异步 background task 用 codex CLI) 2) optimize 完成后自动跑 verifier, 通过才 PUT code** |
| P1.4 | AI 模型 | **codex CLI (本机, M3 reasoning) — 不依赖 OpenAI API key** |
| P1.5 | AI prompt 范围 | **"审查这段实盘代码的 5 个常见 bug: 1) state.json 持久化跨重启, 2) 09:35 早盘 market_cap 稀疏, 3) T+1, 4) limit price 边界, 5) 拒单回退. 输出 diff."** |
| P2.1 | 回测→实盘同步 | **`POST /strategies/{id}/sync-from-verify` body=verify_run_id, 把 verify 报告里 target 转成今日 rebalance_to 调用** |
| P2.2 | 同步方式 | **dry_run 模式 + 确认按钮, 避免误同步. 跑 verify → 弹"是否应用" → 真应用** |
| P2.3 | verify 报告里需要的字段 | target (dict[sym, qty]) + as_of + 模拟 PnL 摘要 (已存在) |

## 1. 目标

完成 3 件**独立**但**联动**的工作:

1. **P1 修复**: 解决 Lock/Curie 提的 60s 持锁阻塞 (H1). 改完保证:
   - 09:35 rebalance 阻塞 60s 时, 同 strategy + 同 event 的后续 cron 触发立即得到 "in-flight" 拒绝
   - 跨 strategy 同 event 串行, 不影响 (BUG-C 设计不变)
   - /run-now 同步 handler 不再挂死
   - 实测 60s 阻塞时 5 个并发 run-now 都返回 in_flight 报告, 而非 504

2. **AI 优化自测端到端**:
   - 用户在 frontend 编辑 strategy code → 调 `POST /strategies/{id}/optimize`
   - Backend 用 codex CLI 调 model, 给出 diff
   - diff 应用到本地文件 → 自动跑 verifier (最近 N 天历史) → 验证通过才 PUT 落地
   - 失败回退 (保持旧 code) + 报告哪儿坏

3. **回测结果同步实盘**:
   - `POST /strategies/{id}/verify` 已有 — 跑回测, 报告含 `target` 数组
   - 新增 `POST /strategies/{id}/sync-from-verify` body=`{verify_run_id: "..."}` 
   - 服务端取 verify 报告, **DRY-RUN 默认** 展示"如果现在执行会下什么单", 用户确认才真跑
   - 真跑: executor.run_now(strategy_id, event=rebalance, note="from-verify:{run_id}")
   - 落地: 实际 PnL 随真实行情走 (verifier 用的是历史 close, 实际用 limit_price 撮合)

## 2. 范围

### 2.1 In

**P1 修复 (executor 持锁)**:
- `backend/app/services/trading/executor.py` 改造
- 新建 `backend/app/services/trading/_lock_plan.py` (helpers: state 加载 + 阻塞 + in-flight 检查)
- 测试 `tests/test_executor_p1_refactor.py`

**AI 优化端到端**:
- `backend/app/services/strategy_ai_loop.py` (新): orchestrator
- `backend/app/api/strategy_ai.py` (新 router): POST /optimize, GET /optimize-status
- 改 `backend/app/services/trading/router.py` 加 verify-from-sync 端点
- 测试 `tests/test_strategy_ai_loop.py` (mock codex CLI)

**回测→实盘**:
- 改 `backend/app/services/trading/router.py` 加 `POST /strategies/{id}/sync-from-verify`
- 改 `backend/app/services/trading/verifier.py` 让 `verify()` 报告持久化 + 暴露 run_id
- 改 `backend/app/api/strategy_ai.py` 或 router 暴露 verify-run 历史
- 测试 `tests/test_backtest_to_live_sync.py`

### 2.2 Out

- 不做 codex CLI 之外的 AI provider 切换
- 不动前端 UI (由前端 owner 后续接入)
- 不改 verifier 核心算法 (只改报告持久化 + 字段暴露)
- 不优化撮合引擎 (P1 fix 不重写 executor 主体逻辑)

## 3. 数据契约

### 3.1 P1 修复 (in-flight 报告)

```json
{
  "status": "in_flight",
  "started_at": "2026-10-13T09:35:00.123456+08:00",
  "trigger": "manual" | "cron" | "api",
  "duration_so_far_ms": 1234,
  "waited_for_data_ms": 5612
}
```

`run-now` 接口在同 strategy + event in-flight 时返 200 + 上面 report, 不返 409.

### 3.2 AI 优化端到端

`POST /api/strategies/{id}/optimize` body:
```json
{
  "model": "codex",  // 可选, 默认 codex
  "focus": ["early_open_data_gaps", "T+1"],  // 可选
  "auto_apply": false  // true = AI diff 自动应用 (慎用)
}
```

返回 (202 Accepted):
```json
{
  "optimize_id": "opt_20261013_xxx",
  "status": "running",
  "polling_url": "/api/strategies/{id}/optimize/opt_20261013_xxx"
}
```

poll 时返回 status=running|completed|failed, completed 时:
```json
{
  "status": "completed",
  "diff": "--- a/dividend_auntie.py\n+++ ...",
  "verifier_passed": true,
  "verifier_report": {"total_pnl": ..., "max_drawdown": ...},
  "applied_to_file": true,  // auto_apply=true 时
  "fallback_to_original": false  // 失败时
}
```

### 3.3 回测→实盘

`POST /api/strategies/{id}/sync-from-verify` body:
```json
{"verify_run_id": "vrf_20261013_xxx", "dry_run": true}
```

返回 (dry_run=true 时):
```json
{
  "verify_run_id": "...",
  "as_of": "2026-10-09",
  "target_count": 5,
  "target": {"600027.SH": 1000, ...},
  "dry_run": true,
  "would_submit_orders": [
    {"symbol": "600027.SH", "side": "buy", "qty": 1000, "limit_price": 4.32}
  ],
  "simulated_pnl_summary": {"total_pnl": 0.0, "trades": 5}
}
```

`dry_run=false` 时实际跑 executor.run_now + 返 RunReport.

## 4. 行为口径

### 4.1 P1 修复 (顺序)

1. caller 调 `run_now(strategy_id, event=...)`
2. 检查 `in_flight[(strategy_id, event)]` 标识 (dict + RLock)
3. 如果已有 in_flight, 立即返回 in_flight report (不进入下一段)
4. 否则标记 in_flight, 记录 started_at
5. **锁外** 加载 state, 计算 expected_instrument_count
6. **锁外** 阻塞 wait_for_quote_data (无锁!)
7. **进入锁** (strat_lk + evt_lk), 期间仅做:
   - _build_context() (已经数据齐)
   - 调 strategy.on_event (重计算逻辑)
   - rebalance_to 落单
8. **退出锁** 后: 清 in_flight, 写 run report

注: 关键 in_flight 检查也走 RLock, 但**不持锁等数据** — 30s+ 阻塞不在锁内。

### 4.2 AI 优化端到端

1. 用户调 `POST /optimize`
2. Backend 起 background task, 标记 `optimize_status[running]`
3. 调 codex CLI (用 subprocess 跑 codex 提示词, 模板: "审查这段策略 {id}, 重点关注 [focus list], 输出 diff" + 当前 code 内容)
4. 拿 diff, 应用到 file (如果 auto_apply=true)
5. 自动跑 verifier (历史 30 天) — 验通过则保留, 失败则 fallback 到原 code
6. 标记 status=completed|failed, 存报告

### 4.3 回测→实盘

1. `POST /verify` 跑回测, 服务端持久化报告, 返 verify_run_id
2. `POST /sync-from-verify` 读报告
3. dry_run=true: 仅构造 `would_submit_orders` (limit_price = 当日实时价), 返报告 — 不下任何单
4. dry_run=false: 调 `executor.run_now(strategy_id, event=rebalance, note=f"sync-from-verify:{verify_run_id}")` — 用实时价撮合, 走完整 PnL 路径
5. 实际单提交后等撮合, 真实 PnL 在 live 端

## 5. 风险与边界

| 风险 | 缓解 |
|---|---|
| codex CLI 不可用 → 优化 endpoint 报 503 | endpoint 检测 codex 路径存在, 不存在返 503 + log |
| verifier 失败率高 → AI 优化反复回退 | 优化 endpoint 限制单 strategy 同时运行 1 个, 失败 3 次暂停 1h |
| 同步实盘实际成交与回测大幅偏离 | 文档明示差异, dry_run 强制 review; limit_price 用实时价 |
| in_flight dict 内存泄漏 (策略被删但 in_flight 还存) | 注册 cleanup on registry.unregister |
| codex CLI 调长上下文超时 (codex 跑 30s+) | AI 优化 timeout=60s, 超时返 504, 报告 status=timeout |

## 6. 测试矩阵

| 套件 | 覆盖 |
|---|---|
| test_executor_p1_refactor.py | 5 个并发 run_now 同时 in_flight 行为; wait 60s 时其它 run_now 立即返 in_flight; 阻塞期间 quote_service 持续 fill; 解除锁后 in_flight 清; 异常路径 |
| test_strategy_ai_loop.py | mock codex CLI, 测 diff apply / verifier 失败回退 / 并发限制 / status 流转 |
| test_backtest_to_live_sync.py | dry_run 不下单, dry_run=false 真下单, 跨 strategy 隔离, 限流 |

## 7. 验收

按 docs/live-trading-strategy-isolation.md 同样的清单: 后端单测 100% 绿, live 端 curl smoke 通过, AI 端到端手工跑一次成功 (本机 codex 可用前提), 同步实盘 dry_run + 真跑各 1 次.

