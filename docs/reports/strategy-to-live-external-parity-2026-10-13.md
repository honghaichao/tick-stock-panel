# 策略→实盘通道外部平台对标评估 (read-only)

> 评估日期: 2026-10-13
> 范围: 策略代码编辑 → 回测验证 → 实盘撮合 → 风控/告警 全链路
> 对标平台: 聚宽 (JoinQuant) / 米筐 (RiceQuant) / 掘金 (MyQuant) / vnpy
> 性质: 仅评估, 不动代码; 报告产出物 `docs/reports/strategy-to-live-external-parity-2026-10-13.md`

---

## 0. 摸底结论 (TL;DR)

| 维度 | 当前水平 | 与聚宽对比 | 阻塞度 |
|---|---|---|---|
| 1. 策略代码提交流程 | **★★★☆☆** | 缺 lint/语法预检 + 缺 verify 自动联跑 | 中 |
| 2. 回测 → 实盘同步 | **★★★★☆** | sync-from-verify 已落地 dry_run + confirm_token; 但 risk params + 撮合语义有 drift | 中 |
| 3. 调度 / 触发 / broker 模式 | **★★★☆☆** | BrokerKind.PAPER 单实现, 无 live broker; scheduler 与聚宽 run_daily 语义对齐 | 高 (阻塞真金白银) |
| 4. 风险 / 异常流 / 告警 | **★★☆☆☆** | 拒单不重试不告警, wecom/feishu 未与 trading 事件联通 | 高 |

整体评分 ≈ **3.0/5** — 已经过一半; 阻塞真交易的是 3 + 4。

---

## Section A: 4 个面成熟度评分

### A.1 策略代码提交流程  ★★★☆☆ (聚宽 5 / 米筐 5 / 掘金 4 / vnpy 3)

**我们当前**:
- 编辑入口: `PUT /api/trading/strategies/{id}/code` (`backend/app/services/trading/router.py:547-580`)
- 文件落盘: `data/user_data/strategies/live/{id}.py` (`backend/app/services/trading/code_store.py:39-43`)
- 校验只有两条:
  - 内容非空 (`code_store.py:54`)
  - 必须含 `'STRATEGY'` 子串 (`code_store.py:55-56`)
- 保存后调用 `scheduler.check_code_changed()` 触发 `on_after_code_changed` 钩子 (`router.py:560-570`)
- 提供 `run_after` 可选旗标 — 直接调 `executor.run_now(...)` (`router.py:572-577`), **没有 verify 中间环节**
- `code_changelog.jsonl` 落 changelog (`code_store.py:79-87`) — 纯文本 diff 摘要, 无 diff 内容

**vs 平台**:
- 聚宽/米筐/掘金 IDE 保存即触发 lint + ast.parse + 沙箱回测 (默认 1 周期); 通过才允许部署
- vnpy 是 desktop IDE, 跟我们的"edit save reload verify"最像, 但 vnpy 提供 pyfolio 离线回测

**缺什么** (具体差异点):
1. `code_store.write_code` 不调 `ast.parse` — 拼错只在运行时 `importlib.reload` 失败 (`live_strategy.py:149-170`); 用户经常要刷新页才能看到 SyntaxError
2. 保存后不自动跑 verify (哪怕 1 个交易日); `run_after` 是直接实盘, 跳过了回测屏障
3. 没有 lint 工具 (ruff / flake8) 钩子, 行业违规 (用未来函数 / 全局状态泄漏) 不会被自动挡
4. 没有"diff review"环节, 直接覆盖; 只在 changelog 留 diff_summary 字符串

### A.2 回测 → 实盘同步  ★★★★☆ (聚宽 5 / 米筐 5 / 掘金 4 / vnpy 3)

**我们当前**:
- `GET /api/trading/strategies/{id}/verify` (`router.py:405-438`) 跑 verifier (`backend/app/services/trading/verifier.py:296-455`)
- `verify` 用历史 close 当成交价 (`verifier.py:388-421`); `simulate_fills=True` 时累计 PnL + 模拟账户
- `POST /api/trading/strategies/{id}/sync-from-verify` (`router.py:329-367`) 走:
  1. `prepare_sync()` (`backend/app/services/trading/sync.py:230-310`) — 拿 verify 报告 target, 用实时价 (`sync.py:202-220`) 算 limit_price, 构造 `would_submit_orders`
  2. dry_run → 返 confirm_token (HMAC, 5 min TTL, `sync.py:25-27`)
  3. dry_run=false + confirm_token → `executor.run_with_target()` (`executor.py:480+`) 走完整 rebalance 流程
- code_version 一致性校验 (`sync.py:266-273`) 阻止 code 改了但没重 verify 的旧报告
- 命名锁防并发同步 (`sync.py:201-210`)

**vs 平台**:
- 聚宽: 回测风控参数 (set_order_cost / set_slippage) 在回测和实盘间 1:1 沿用, 一键部署 = 同一份策略 + 同一份风控
- 米筐: 回测通过后页面"发布"按钮一键上模拟盘, 同步过程中保留 research notebook 上下文

**缺什么**:
1. **risk params 不沿用** — `max_order_amount` / `slippage_kind` / `slippage_value` / `order_volume_ratio` 在 verify 报告里**不记录也不对比**; verify 用了 `initial_cash=1_000_000` 默认, real 走 `prefs.initial_cash()` (`engine.py:169`) — 两边可能不同, 但用户看不到差异
2. **撮合语义 drift** — verify 用 close-as-fill, real 用 limit_price (实时) 撮合 (`sync.py:285-287`); 同股同价同日的 fill rate/滑点不可比。已观察到的 drift: `docs/live-vs-backtest-report.md` 报"1 周对比标的完全不重叠"
3. **数据源 drift** — verify 用 TushareDividendSource + instruments 全市场; 模拟盘早期 `my_Trader` 数据源不全 (`live-vs-backtest-report.md §改进建议`)
4. **`max_position_pct` / `max_position_count` 在实盘根本不用** — `preferences.py:99-103` 定义, `verifier.py:472` 引用用于模拟账户; `engine.py:115-200` 和 `executor.py` 全程不消费。回测如果设了 20% 单票占比, 实盘不会卡

### A.3 调度 / 触发 / Broker 模式  ★★★☆☆ (聚宽 5 / 米筐 4 / 掘金 4 / vnpy 3)

**我们当前**:
- `StrategyScheduler` (`backend/app/services/trading/scheduler.py:34-218`) 包装 APScheduler:
  - 每个 strategy × 每个 schedule 1 个 job (`scheduler.py:75-90`)
  - 同 `(hour, minute)` 多 schedule 按 priority 排序 (`live_strategy.py:30-35` + `scheduler.py:63-72`):
    `monthly(0) < weekly(1) < daily(2) < manual(3)`
  - `misfire_grace_time=3600` 容错过期 (`scheduler.py:85`)
- `BrokerKind` 枚举 (`backend/app/services/trading/models.py:53-56`):
  - `PAPER = "paper"` ✅ 有 `PaperBroker` 实现 (`backend/app/services/trading/paper_broker.py`)
  - `LIVE = "live"` ❌ 注释 "二开接入真实券商时使用, 主线不内置任何 live broker"
- `engine.py:75` 硬编码 `self._broker = paper_broker.PaperBroker()`; 无条件切换
- `main.py:223,318` 注释 "默认 paper, 二开 broker 替换" — 即用用户在 `custom/` 目录写 broker 实例然后替换 `engine._broker`
- 撮合轮询: `match_interval_seconds` 默认 5s (`preferences.py:14`), 用上一根已收盘 K 撮合 (聚宽语义, `live-trading-joinquant-parity.md §5.1`)
- 17:00 自动清算 (`backend/app/services/trading/clearance.py`)
- `run-now` API (`router.py:312-326`): 手动触发一次 `executor.run_now(strategy_id, trigger='api', event=None)`

**vs 平台**:
- 聚宽 UI 显式区分 3 种模式: 模拟 / 模拟撮合 / 实盘, 每个策略 instance 单独选; 同 run_daily 一次, broker 按模式路由
- 掘金 `subscribe(quote, on_bar)` 事件驱动; 我们是 cron + 撮合线程, 不是 subscribe
- vnpy CTP / OES 等真实网关适配; 我们只有 paper

**缺什么**:
1. **无 live broker 实现** — `BrokerKind.LIVE` 是注释占位符; `docs/secondary-development.md:33-44` 给了 Protocol 契约但未提供 xq / ptrade / qmt / 华泰 / 国君 任一券商适配
2. **无 broker 选择 UI** — `models.py:218` 有 `broker: str = "paper"` 字段但没 settings 页能改; `preferences.py` 也没有 `KEY_BROKER_KIND`
3. **scheduler 没有"按 mode 路由"** — 同 schedule 总是走 paper; 即使用户在 custom/ 注入 live broker, 仍需手动重启 + 改环境变量, 无 run-time toggle

### A.4 风险 / 异常流 / 告警  ★★☆☆☆ (聚宽 5 / 米筐 4 / 掘金 4 / vnpy 4)

**我们当前**:
- 引擎内风险控制 (`engine.py:115-200`):
  - 单笔金额上限 (manual: 5 万; strategy: 1000 万) (`preferences.py:83-95` + `engine.py:160`)
  - 可用现金校验 (买) (`engine.py:172-178`)
  - T+1 可卖数量 (卖) (`engine.py:181-191`)
  - 限价单 ref_price 校验 (`engine.py:144-154`)
- 拒单流 (`engine.py:663-682`):
  - `_reject()` 落 order (REJECTED) + 写 TradeLog (REJECT kind) + 推送 SSE `order_rejected` + 调 `_notify()`
- `_notify()` (`engine.py:719-725`) 写 `notify.jsonl` + SSE 推; **只落 JSONL, 不调用 wecom / feishu / email**
- `notify_store.py:5-9` VALID_KINDS 包含 `order_rejected` / `order_cleared` / `retry`, 但实际**没人读 notify.jsonl 再发外网**
- `notify_wechat` per-strategy toggle (`preferences.py:283-298` + `router.py:455-466`):
  - 写入 prefs.json, 返前端; UI 显示 ✓
  - **未被 `_notify()` 读取**; 前端注释明确: `frontend/src/components/live/StrategyTable.tsx:217` `title="等待 PR-1 — prefs.trading_strategies.<id>.notify_wechat"`
- 现有 wecom 通道 (`backend/app/services/wecom_bot_service.py`):
  - 智能机器人长连接 (BotID + Secret), 已启动 (`main.py:215-223`)
  - 但**不订阅 trading 事件** — 是独立 ws 通道, 跟 `notify_store` 完全无连接
- `retry.py` (`backend/app/services/trading/retry.py:200-312`):
  - 是 run-level 重试 (POST `/runs/{run_id}/retry`), 按原 `as_of` 撮合原 buys
  - **不是 order-level 重试** — 券商瞬时网络断导致的 `reject_reason='网络断开'` 不会自动重提

**vs 平台**:
- 聚宽/米筐/掘金: 拒单 → 自动按规则 retry (瞬时网络断重提 N 次, 间隔指数退避) + 全链路 wechat 告警 (成交 / 拒单 / 风控阻断 / 重启)
- vnpy: 主推 on_bar 异常捕获 + push 邮件 / 钉钉; CTP 网关内置心跳重连

**缺什么**:
1. **wecom / feishu 通道与 trading 事件完全未联** — `wecom_bot_service` 存在但只跑保活; `notify_store.append()` 写完即忘
2. **不重试** — broker.submit 永久失败 (非风控, 纯网络层) 直接落 REJECTED, 无 retry 队列
3. **`max_position_pct` / `max_position_count` 未生效** — preferences 有, engine 不消费 (与 A.2 重叠)
4. **price-limit 校验缺失** — `docs/live-vs-backtest-report.md §4` 报"filter_kcbj_stock 只过滤 8, 漏了 .BJ"; paper_broker 隐含 high/low 范围但 engine.submit 阶段没单独做 pre-check
5. **异常回报 fail-mode** — engine crash / FastAPI 重启时未完成 run 没标记失败, SSE 断连时 UI 没重连补单

---

## Section B: 阻塞策略→实盘的关键 Gap

按"影响真交易"程度排 P0/P1/P2:

#### P0 — 阻塞真金白银 (必须先做)

| # | Gap | 文件位置 | 影响 |
|---|---|---|---|
| P0-1 | 无真实 broker 适配, BrokerKind.LIVE 空 | `models.py:53-56`, `engine.py:75`, `secondary-development.md:33-44` | 不接券商 = 不能实盘 |
| P0-2 | wecom/feishu 通道未与 trading notify 联通 | `wecom_bot_service.py` vs `notify_store.py:42-50` | 拒单/异常无人收, 凌晨爆雷 0 感知 |
| P0-3 | 拒单不重试 (瞬时网络层 reject) | `engine.py:663-682`, 无 retry 队列 | 单次网络抖动 = 当日该笔永久丢失 |
| P0-4 | `max_position_pct` / `max_position_count` prefs 定义但未生效 | `preferences.py:99-103` vs `engine.py:115-200` | 用户设 20% 单票占比, 实盘不卡 |

#### P1 — 影响"回测上实盘信心"

| # | Gap | 文件位置 | 影响 |
|---|---|---|---|
| P1-1 | verify 报告未携带风险参数 (`max_order_amount` / `slippage` / `initial_cash`) | `verifier.py:296-455`, `sync.py:230-310` | 用户改 prefs 后旧 verify 仍可同步, drift |
| P1-2 | verify 用 close-as-fill, real 用 limit_price (实时), 同窗口 drift | `verifier.py:388-421` vs `sync.py:285-287` | `live-vs-backtest-report.md` 已观察到 "标的完全不重叠" |
| P1-3 | 保存策略代码不调 `ast.parse` | `code_store.py:54-56` | SyntaxError 只能运行时报, 策略静默失败 |
| P1-4 | `notify_wechat` per-strategy toggle 持久化但未触发 | `preferences.py:283-298`, `StrategyTable.tsx:217` | UI 误导, 用户以为有告警实际没有 |

#### P2 — 影响"开发者效率"

| # | Gap | 文件位置 | 影响 |
|---|---|---|---|
| P2-1 | 保存后无 lint/verify 自动联跑 | `router.py:547-580` | 每次手动点 verify 才能确认策略健康 |
| P2-2 | 没有 diff review 步骤, 直接覆盖 | `code_store.write_code` 整体重写 | 误删 100 行无人把关 |
| P2-3 | broker 模式选择无 UI | `models.py:218` + 无 settings 路由 | 切 paper↔live 必须改代码 |
| P2-4 | 异常回报 fail-mode (engine crash / FastAPI 重启) 无补偿 | `engine.py` 启动路径 (`main.py:223-320`) | 重启 = 未完成 run 状态卡死 |

---

## Section C: "一键从回测到实盘" 在我们系统实现的步骤

参考 `docs/live-trading-p1-ai-loop-backtest-sync.md` 已定的 sync-from-verify 流 (2026-10-13), 当前**已经基本就绪**。"一键" 还需补全的步骤 (按用户视角顺序):

#### C.1 用户操作 (UI)
1. 打开 `/live-trading/:strategyId` → 「5 tab」布局 (已落地, `docs/live-trading-5page-layout.md`)
2. 「代码」tab 改 strategy code (editor + 校验, P1-3 缺)
3. 「概览」tab → 选「Verify」按钮 → 填起止日期 → 跑回测 (`GET /api/trading/strategies/{id}/verify`, `router.py:405-438`)
4. verify 报告落 `verifications/<id>/<run_id>.json`, 显示胜率 / 最大回撤 / 模拟 PnL
5. 「Sync From Verify」按钮 → 弹 dry_run 预览 (`POST .../sync-from-verify` `dry_run=true`, `router.py:329-340`)
6. 用户确认 → 第二次提交 (带 confirm_token) → 真跑 (`dry_run=false`, `router.py:362-367`)

#### C.2 服务端必须就绪的链路
- [x] verify 跑策略 on historical data → 模拟 fill + PnL (`verifier.py:296-455`)
- [x] prepare_sync 把 target → would_submit_orders, 用实时价限价 (`sync.py:230-310`)
- [x] confirm_token HMAC TTL 5min (`sync.py:25-27`)
- [x] code_version 一致性校验 (`sync.py:266-273`)
- [x] 命名锁防并发同步 (`sync.py:201-210`)
- [x] execute_sync 调 `executor.run_with_target()` (`executor.py:480+`)
- [ ] risk params 沿用 (P1-1)
- [ ] 撮合语义统一 (P1-2)
- [ ] 阻断性风控 (`max_position_pct/count`) 联动 (P0-4)

#### C.3 现状距离"一键"的差距
- 步骤 2-3 缺 lint / ast.parse 自动门 (`P1-3`)
- 步骤 5-6 缺"列出最近 verify 报告选择"UX (`verify/list` API 已有, `router.py:441`; UI 接入未验)
- 步骤 6 缺"全量阻断"按钮 (当前只能一笔笔过 run 中继)
- 缺 wecom 通知: 真跑成功后给用户发"已按 verify@xxx 同步 N 单" (P0-2)
- 缺"一键回滚": 同步失败 / 走势异常时, 自动撤所有 NEW+PARTIAL (有 17:00 清算, 但即时撤无)

---

## Section D: 现在做 vs 放后面 (按 ROI)

#### 必须现在做 (P0, ROI 高)

1. **P0-4 风险参数生效** (`max_position_pct` / `max_position_count`)
   - 改动小: `engine.py:115-200` 加 2 个分支 (持仓数 > count 拒 / 单票市值 > pct 拒)
   - ROI: 直接堵实盘风险敞口, 单文件改动 < 100 行
2. **P0-2 wecom 联通 trading notify**
   - 改动: 在 `_notify()` (`engine.py:719-725`) 后增加 push hook, 读 `notify_wechat` flag → 调 `wecom_bot_service.send_text()`
   - ROI: 拒单 / 成交 / 清算都能入用户微信群, 是产品级可用性提升
3. **P1-3 代码保存前 `ast.parse` 预检**
   - 改动: `code_store.write_code` (`code_store.py:51-86`) 加 `ast.parse(content)` 失败抛 `ValueError("语法错误: line X")`
   - ROI: 防止 SyntaxError 把 strategy 静默掉, 1 函数 ~20 行

#### 半年内做 (P1, ROI 中)

4. **P1-1 verify 报告携带 risk params** — verify 时 snapshot `max_order_amount` / `slippage_kind` / `initial_cash` 进报告, sync 时与 prefs 对比, 不一致返 `code_changed_since_verify` 同类型错误
5. **P1-4 `notify_wechat` 真正接通** — 拆 PR-1, 改 `_notify()` 读 prefs flag 后再 push wecom
6. **P0-1 broker selection UI** — `preferences.py` 加 `KEY_BROKER_KIND`, settings 页加下拉 (paper / live-xq / live-ptrade …); `engine._broker` 改成 lazy init 读 prefs, 不再 hardcode (`engine.py:75`)

#### 放后面 (P2, ROI 视产品形态)

7. **P0-3 拒单重试** — broker.submit 返回 reject_reason 含 "网络"/"超时" 关键词时, 进重试队列, 5s/30s/5min 指数退避重试 N 次; broker 已经有 `OrderAck.accepted=False`, 加 retry queue
8. **P1-2 撮合语义统一** — verify 也按 limit_price (实时价估算) + slippage 算 fill, 与 real 一致; 改 `verifier._compute_simulated_pnl` (`verifier.py:388-421`)
9. **P2-1 保存后自动 verify** — `router.py:572-577` `run_after` 后再跑 1 周 verify, 通过才生效
10. **P0-1 live broker 二开** — 真实券商对接风险高 + 法规约束, 推 Phase 2; 当前阶段文档 + Protocol 已够

---

## 附录: 引用文件 / 行号清单

- 撮合核心: `backend/app/services/trading/engine.py:75` (硬编码 paper), `:115-200` (风控), `:663-682` (拒单), `:719-725` (notify stub)
- 撮合 broker 协议: `backend/app/services/trading/broker.py:25-105`
- 纸面 broker: `backend/app/services/trading/paper_broker.py:1-258`
- 调度: `backend/app/services/trading/scheduler.py:34-218`
- 实盘策略协议: `backend/app/services/trading/live_strategy.py:1-287`
- 重试: `backend/app/services/trading/retry.py:1-312` (run-level only)
- 回测验证: `backend/app/services/trading/verifier.py:1-454` (simulate_fills 用 close)
- 回测→实盘同步: `backend/app/services/trading/sync.py:1-428` (dry_run + confirm_token)
- notify 存储: `backend/app/services/trading/notify_store.py:1-82` (只 JSONL + SSE)
- notify_wechat prefs: `backend/app/services/trading/preferences.py:283-298`
- 策略代码读写: `backend/app/services/trading/code_store.py:39-87`
- 端点总览: `backend/app/services/trading/router.py:1-642`
  - 改代码: `:547-580`
  - run-now: `:312-326`
  - sync-from-verify: `:329-367`
  - verify: `:405-438`
  - verify/list: `:441`
  - notify toggle: `:455-466`
- wecom 服务: `backend/app/services/wecom_bot_service.py` (独立 ws, 不联 trading)
- 启动装配: `backend/app/main.py:215-320` (paper broker 注释, sync_orchestrator 装配)
- 文档:
  - `docs/live-trading-joinquant-parity.md` (撮合层/调度层聚宽语义对齐, 已落地)
  - `docs/live-trading-5page-layout.md` (5 tab 布局, 已落地)
  - `docs/live-trading-strategy-isolation.md` (4 视图 per-strategy 隔离, 已落地)
  - `docs/live-trading-p1-ai-loop-backtest-sync.md` (sync-from-verify P2.1 设计, 部分落地)
  - `docs/live-vs-backtest-report.md` (1 周对比 drift 实证)
  - `docs/secondary-development.md:33-44` (BrokerAdapter 二开契约)

> 注: 评分综合考虑 **能力完整度** + **可观测性** + **错误恢复**。聚宽给 5 是对照其商用级一致性 + UI + 告警, 我们给 3 是已实现"最小可用但缺关键链路"。
