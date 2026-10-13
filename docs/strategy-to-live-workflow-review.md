# 策略→实盘 全链路流程畅通审视 (Strategy-to-Live Workflow Review)

> 状态: **纯评估 (read-only, 待 user review)**
> 评估日期: 2026-10-13
> 关联:
> - `docs/reports/strategy-to-live-external-parity-2026-10-13.md` — 按"维度评分"的既有对标报告 (TL;DR 评分 3.0/5, 列了 P0/P1/P2 缺口)
> - `docs/strategy.md` / `docs/strategy-iteration.md` / `docs/strategy-unified-runtime.md` — 策略生成与回测闭环
> - `docs/live-trading-5page-layout.md` / `live-trading-list-detail-layout.md` / `live-trading-strategy-isolation.md` — 实盘详情/列表/隔离
> - `docs/live-trading-joinquant-parity.md` — 撮合/调度聚宽对齐 (已落地)
> - `docs/live-trading-p1-ai-loop-backtest-sync.md` — sync-from-verify / AI 优化 (代码就绪, 部分 UI 未接入)
> - `docs/architecture-review-h2-supplement.md` — H2 风险已 CRITICAL 评级 + 已部分修
> - `docs/live-vs-backtest-report.md` — 实证 drift (1 周对比标的完全不重叠)

> 与既有对标报告的差异: 既有报告按"维度"评分 (策略提交/回测→实盘/调度+broker/风控+告警 4 个面),本文按"**用户视角的端到端流程**"重新审视,把视角从"模块成熟度"切到"流程卡点/断点/歧义点"。两者互补,本文不重写既有报告的 P0/P1/P2 清单,而是补一份"流程路径"视角的诊断 + 优先级建议。

---

## 0. 一句话结论

按外部主流量化平台 (聚宽 / 米筐 / 掘金 / vnpy) 的"策略→实盘"标准流程审视当前仓库,核心痛点**不是单个模块能力不足,而是端到端链路缺少统一的"状态机 + gate"**,具体表现为:

1. **策略生命周期无显式状态**: 草稿 / 实验 / 回测通过 / 模拟盘 / 实盘 / 暂停 / 归档 之间没有受控转移,任何一步都可"绕过"下一步直接生效。
2. **多目录多入口**: `backend/app/strategy/builtin/`、`data/strategies/ai/`、`data/strategies/custom/`、`data/user_data/strategies/live/` 四个目录,回测和实盘的引擎对它们的扫描规则不同;**用户从"AI 生成策略"到"实盘跑"中间至少有 1 次手动复制 + 1 次重启 + 0 次自动验证**。
3. **回测→实盘同步的 UI 入口隐式**: sync-from-verify 已经在 `live-trading-p1-ai-loop-backtest-sync.md` 设计落地 (`docs/reports/strategy-to-live-external-parity-2026-10-13.md` §C 已记录),但 `LiveTrading.tsx` 没有明显的"按回测结果上模拟盘"按钮,verify 列表的 UI 接入尚未验;用户走"研究→实盘"的最近路径被打断。
4. **失败回退没有"一键"**: `code_changelog.jsonl` 只记摘要, `data/strategies/custom/.iterations/` 是手动维护, `.bak_cleanup_20261012_*/` 是系统级备份; 出问题时用户需要从三个分散位置选,没有"一键回滚"按钮 (聚宽 IDE、米筐历史版本管理都提供)。
5. **多策略并行的资源隔离 + 风险汇总弱**: 单账户视角下,所有策略成交进同一个 orders/fills/account; 单策略视角 (`pnl_strategy_snapshots.jsonl` + per-strategy positions) 已实现,但**账户级风险** (整体回撤、组合相关性) 没有聚合视图。

整体评分沿用既有对标报告: **3.0 / 5**。本文聚焦"流程卡点"侧,**不重复列 P0/P1/P2 缺口**,而是把每个缺口映射到它在"用户路径"中的位置 + 卡点严重度。

---

## 1. 端到端流程现状 (As-Is)

按"用户从零到实盘跑起来"全路径梳理当前仓库已实现的事实。所有步骤引用仓库内真实路径 + 接口。

```
[0] 数据准备 (一次性)
    Settings → 数据源接入 + 同步 (日K / 分钟 / 财务 / 红利)
    ↓ 仓库: backend/app/api/data.py + docs/data-pipeline.md
    状态: 已实现 (★, 但与策略→实盘流程的"门槛检查"未打通)

[1] 策略想法 → 策略代码
    ├─ A 路径 (零代码): 选股页 → 自定义信号 → 编译为 Polars Expr
    │    ↓ 仓库: backend/app/strategy/custom_signals.py + frontend 自定义信号 UI
    │    限制: 只能写"信号", 不能直接变实盘策略 (LiveStrategy.on_event)
    ├─ B 路径 (AI 生成): 选股页 AI 策略生成器 → .py 落到 data/strategies/ai/
    │    ↓ 仓库: backend/app/strategy/ai_generator.py + docs/strategy.md §方式二
    │    限制: ai/ 目录目前仅做"选股扫描", 不能直接挂实盘 (路径不对)
    └─ C 路径 (手写 / 改写 builtin): 在 builtin/ 或 data/strategies/custom/ 或 data/user_data/strategies/live/ 写 .py
         ↓ 仓库: backend/app/strategy/builtin/* + backend/app/strategy/engine.py 自动发现
         限制: 三个目录语义混杂, 用户不知道"哪个对应实盘"

[2] 选股扫描 (回测前)
    Screener 页面 → 跑策略 → 拿到当日候选
    ↓ 仓库: frontend/src/pages/Screener.tsx + backend/app/api/screener.py
    状态: 已实现 (★)

[3] 回测 (历史区间)
    Backtest 页面 → 选策略 + 区间 + 资金 → 跑回测 → 净值/回撤/Sharpe/胜率/盈亏比/归因
    ↓ 仓库: backend/app/api/backtest.py + backend/app/backtest/
    状态: 已实现 (★), 包括分钟回放 + T+1 + 费用 + 滑点 + 因子归因
    派生产物: candidates.csv (导出) → 候选池 (后端存)

[4] 验证 (样本外 / 步进 / 蒙卡)
    Backtest 页面 → 验证 tab → 参数敏感 + 滚动 OOS + 蒙卡
    ↓ 仓库: backend/app/api/backtest.py + backend/app/backtest/walkforward
    状态: 已实现 (★), 独立报告

[5] 显式发布 (仅挖掘路径)
    Mining 页面 → 候选达标 → "显式发布"按钮 → 生成独立策略 (新 ID)
    ↓ 仓库: frontend/src/pages/backtest/MiningWorkbench.tsx
    状态: 已实现 (★), 但发布后**不进实盘**, 仍只在回测/扫描域
    提示文案: "发布会创建独立策略,但不会自动用于实盘或监控"

[6] ★ 晋级到模拟盘 (GAP)
    用户从研究阶段到实盘阶段需要:
      (a) 复制策略到 data/user_data/strategies/live/
      (b) 重启 backend (让 Registry 重新扫描)
      (c) 在 schedules 里把 enabled 打开 (默认 enabled=False)
      (d) 第一次跑要先看 run 报告确认无异常
    ↓ 仓库: backend/app/services/trading/live_strategy.py + docs/secondary-development.md §程序化下单
    状态: 缺一键化 + 缺 lint/ast.parse 预检 + 缺 dry-run "同步试跑"

[7] 模拟盘运行 (按分钟撮合 + 风控 + 滑点)
    StrategyScheduler cron 触发 on_event → target → TradingRunner.rebalance_to → 风控 → 撮合 → fill → PnL → notify
    ↓ 仓库: backend/app/services/trading/scheduler.py + executor.py + engine.py + paper_broker.py
    状态: 已实现 (★), 5 tab 详情 + 列表 + 4 视图 per-strategy 隔离 + 17:00 清算 + 风控 stubs

[8] 监控 + 告警 (并行, 不阻塞)
    监控中心 → 规则 → 触发 → 弹窗 + 语音 + 飞书/微信
    ↓ 仓库: backend/app/api/monitor_rules.py + notify_store + wecom_bot_service + feishu_bot_service
    状态: 规则/通知框架已实现, 但与 trading_event 的 wecom/feishu **未联通** (对标报告 P0-2)

[9] 复盘 + 归因 + 迭代
    Review 页 → 盘后 AI 自动生成 → 复盘 Markdown
    ↓ 仓库: frontend/src/pages/Review.tsx + backend/app/services/...
    状态: 已实现 (★), 但与策略迭代台账 (docs/strategy-iteration.md) 是手动对接, 不进引擎

[10] 退出 (暂停 / 归档 / 删除)
    实盘页 → 停 schedule → (可选) DELETE → .trash/
    ↓ 仓库: backend/app/services/trading/code_store.py + preferences.py
    状态: 已实现 (★), DELETE 已有 trash 回收站
```

**关键观察**:
- ★ = 已实现模块, ☐ = 已有设计但 UI 接入未验, ✗ = 缺
- [1]→[6] 是用户最关心的"晋级路径",中间断点最多:目录错位 + 无 lint 预检 + 无同步试跑 + 需手动重启
- [3]→[7] 的"回测→实盘"已有 sync-from-verify 设计 + 实现 (`live-trading-p1-ai-loop-backtest-sync.md`),但 UI 入口在 LiveTrading.tsx 没暴露
- [8] 是阻塞"真金白银"的 P0 缺口 (对标报告 P0-2), [10] 的回退虽有但分散

---

## 2. 按"流程卡点"重新审视 (Gap-by-Step)

把既有对标报告 (P0/P1/P2) 映射到"用户在哪个步骤踩坑",按踩坑频率 + 严重度排序。

### 2.1 卡点 1 — 策略生成→实盘挂载 (步骤 [1]→[6])

**严重度**: ★★★★★ (用户每次新策略都踩)

**现状**:
- AI 生成 → `data/strategies/ai/ai_*.py` (只能跑选股,不能挂实盘)
- 自定义编写 → `data/strategies/custom/custom_*.py` (同上)
- 实盘必须放 → `data/user_data/strategies/live/{id}.py`

**踩坑路径**:
1. 用户在选股页点 AI 生成,看到 .py 内容
2. 想挂实盘 → 必须复制到 `data/user_data/strategies/live/` (无 UI 按钮)
3. 复制后 → `enabled_default=False`, `schedules` 默认全部 `enabled=False`
4. 需要 backend 重启才能 Registry 重新扫描 (Registry 是启动时一次性加载,非 mtime 热加载)
5. 第一次跑触发 → 报错 (比如 market_cap 全 0、code 语法错、symbol 不存在) → 看 logs 排错
6. 修好后 → 再手动改 schedules 启用 → 再重启

**对标**:
- 聚宽: IDE 保存 → 自动 lint + ast + 沙箱回测 → "部署到模拟盘"按钮一键,后台自动注册 + 自动跑 1 个交易日验证
- 米筐: "发布"按钮 = 自动跑 1 周回测 + 注册到模拟盘 + 启用 schedule

**缺失清单**:
- ✗ 策略目录统一 (ai/custom/live/builtin 4 个目录需要明确"哪一目录 = 实盘")
- ✗ 启用 schedule 的一键引导 (新建后默认禁用 → 用户需要打开 settings 才知道在哪)
- ✗ 保存策略代码前的 `ast.parse` 预检 (对标报告 P1-3,`code_store.py:54-56`)
- ✗ 复制/晋级操作需要重启 backend (对标报告 P0/P1 中未单独列, 但影响所有"热重载"场景)

### 2.2 卡点 2 — 回测→实盘同步 (步骤 [3]→[7])

**严重度**: ★★★★ (策略定型后必经路径)

**现状**:
- 后端 `POST /api/trading/strategies/{id}/sync-from-verify` 已实现 (含 dry_run + confirm_token + 风控过滤 + audit 落盘)
- 后端 `GET /api/trading/strategies/{id}/verify/list` 已有 (verify 报告历史)
- 前端 `LiveTrading.tsx` 没有明显的"按回测结果同步实盘"入口 (回测页和实盘页之间没有 bridge button)

**踩坑路径**:
1. 用户在回测页跑出 verify 报告 (Sharpe 1.2, 回撤 -15%)
2. 想"上模拟盘" → 切到实盘页 → 不知道 verify 报告在哪 → 手动敲代码看 schedule → 启用
3. 等 9:35 rebalance 触发 → 看 fill 才发现"标的跟回测不一样" (`live-vs-backtest-report.md` 已观察)

**对标**:
- 聚宽: 回测报告顶部"投入模拟盘"按钮 → 弹预览 (verify target vs 实时价 + 风控) → 确认
- 米筐: 候选"发布"按钮 = 一键上模拟盘

**缺失清单**:
- ✗ 回测页 → 实盘页的"一键同步"按钮 (verify 报告带 run_id → 实盘策略的 sync-from-verify 入口)
- ☐ LiveTrading.tsx 已加 `tradingStrategyCode` / `tradingStrategyCodePut` / `tradingRunStrategyNow`,但 sync-from-verify 的 API client 方法未在 `frontend/src/lib/api.ts` 找到 (`grep sync-from-verify 0 命中`)
- ☐ verify 报告 UI 列表 (verify/list endpoint 已有,前端未接)
- ✗ drift 检测 UI 化 (回测 vs 实盘标的差异,`live-vs-backtest-report.md` 是手跑脚本)

### 2.3 卡点 3 — 失败回退 / 版本管理 (步骤 [10])

**严重度**: ★★★★ (出问题才踩,但踩一次代价高)

**现状**:
- `code_changelog.jsonl` 记 size + diff_summary 字符串 (无 diff 内容)
- 策略级迭代台账在 `data/strategies/custom/.iterations/` (手维护, docs/strategy-iteration.md 描述)
- 系统级清理在 `.bak_cleanup_20261012_*/` (一次性事件, 非版本管理)
- 实盘策略级没有"历史版本"快照 (dividend_auntie.py 的 `_originals/` 是 dump 不是版本)

**踩坑路径**:
1. 用户改 dividend_auntie.py → 触发 on_event → 报 no_target (或 worse,误下单)
2. 想回退 → 只能去看 changelog 里的 diff_summary 字符串 → 手动 mtime sort → 备份(如果有)→ mv 回去
3. 没有"diff 视图"和"一键回滚"

**对标**:
- 聚宽 IDE: 每次保存自动 v1/v2/v3 快照,UI 上可看 diff 一键回滚
- 米筐: 策略历史版本 + diff 视图 + 一键恢复

**缺失清单**:
- ✗ 实盘策略代码的"自动快照 + diff + 回滚"机制 (现有 changelog 只存摘要,不存内容)
- ✗ 回退路径与 schedule 暂停的联动 (回退后 → 自动暂停 cron,避免自动重跑)
- ☐ docs/strategy-iteration.md §9 提到的"迭代工作台 M1"骨架 (read-only 时间线/diff/回退)

### 2.4 卡点 4 — 策略生命周期状态 (跨多个步骤)

**严重度**: ★★★ (日常不踩,跨多个状态切换时迷)

**现状**:
- `enabled_default: bool` 是策略类的一个字段,语义模糊 ("默认启用?" "现在启用?" 都用它)
- `schedules[].enabled` 是真正"现在跑不跑"的开关
- schedules 全部 enabled=False → 看起来"停用",但策略还在 Registry 里 → 还在 list_strategies 里
- 没有"草稿 / 实验 / 验证通过 / 模拟盘运行中 / 实盘运行中 / 已暂停 / 已归档"等显式状态

**踩坑路径**:
1. 用户写完新策略 → 不知道是"草稿"还是"该挂了" → 手动设 enabled
2. 跑挂后 → 想"暂停观察" → 改 schedules → 但 list_strategies 里还显示 → 不知道它现在是停还是死
3. 想"归档" → DELETE → 移到 .trash/ → 路径变 → 不知道在哪

**对标**:
- 聚宽策略库: 每个策略有"运行中 / 已暂停 / 已归档 / 异常"状态徽标
- 米筐: 策略有"研究中 / 模拟中 / 实盘中"三态

**缺失清单**:
- ✗ 策略生命周期状态机 (草稿/实验/验证/模拟/实盘/暂停/归档) + 受控转移
- ☐ 状态徽标 UI (LiveTrading.tsx 已有 active/stopped split, 但语义简化为"是否有启用 schedule")
- ✗ "归档"和"暂停"是不同的 (归档 = 不再跑 + 不再显示; 暂停 = 不跑但保留在活跃列表)

### 2.5 卡点 5 — 多策略并行 + 组合视图 (步骤 [7]+[8])

**严重度**: ★★★ (单策略用户不踩,多策略用户必踩)

**现状**:
- per-strategy 隔离已落地 (`live-trading-strategy-isolation.md` 4 视图隔离 + pnl_strategy_snapshots.jsonl)
- 账户级视图 vs per-strategy 视图已分桶 (列表页表格 + 详情页)
- 但**没有"组合"概念**: 多策略同时跑时,整体账户的回撤 / 相关性 / 风险敞口没有聚合
- wecom/feishu 通道与 trading_event 联通缺失 (对标报告 P0-2)

**踩坑路径**:
1. 用户同时跑 dividend_auntie + 一只新策略
2. 想看"今天两个策略加起来赚了多少" → 列表页表格 (`StrategyTable.tsx`) 有 per-strategy summary,但没 sum
3. 想看"两个策略持仓有没有重叠" → 没有
4. 想看"其中一个异常退出时有没有通知" → wecom/feishu 没接通 → 沉默

**对标**:
- 聚宽: 策略组 / 组合视图 (组合回测 + 组合监控)
- 米筐: 策略组合 + 风险归因

**缺失清单**:
- ☐ 组合级 PnL / 回撤聚合 (`pnl_snapshots.jsonl` 是账户级,组合级 = 跨策略 sum)
- ☐ 持仓重叠检测 (per-strategy positions 已有, cross-strategy 重叠 / 行业暴露未算)
- ✗ trading_event → wecom/feishu 推送 (对标报告 P0-2,阻塞真交易)

### 2.6 卡点 6 — 监控中心 ↔ 实盘策略的双向引用 (步骤 [7]+[8])

**严重度**: ★★ (规则触发后定位策略需要手动串)

**现状**:
- 监控中心规则支持"策略触发器"类型 (`docs/strategy.md §7` 提及 "上线检验: 监控中心 · 策略触发器")
- 但 monitor_rule 与 live_strategy 是两个独立的存储, 没有"策略触发器" → "实盘策略 ID" 的显式 link
- 实盘策略详情页没有"被监控规则数 / 最近触发"指标

**踩坑路径**:
1. 用户配置监控规则 "策略触发器: dividend_auntie"
2. 想确认这条规则真的接到了 dividend_auntie → 进监控中心列表 → 看规则详情 → 才知道
3. 想看 dividend_auntie 触发过几次 → 实盘页 / 监控中心 / alerts 三个页面翻

**缺失清单**:
- ✗ 双向 link (live_strategy ↔ monitor_rule)
- ☐ 实盘页显示"被监控规则数 + 最近触发"

---

## 3. 改进路线图 (建议, 不立即动代码)

按"用户踩坑频率 × 修复 ROI"排:

### 3.1 必须做 (P0 流程级别, 阻塞真使用)

| # | 缺口 | 落点 | 文件预估 |
|---|---|---|---|
| F0-1 | **策略代码保存前 ast.parse 预检** | code_store.write_code 加 `ast.parse`, SyntaxError 失败抛 400 | `backend/app/services/trading/code_store.py:51-86` (~20 行) |
| F0-2 | **wecom/feishu 联通 trading_event** | `_notify()` 后接 push hook, 读 prefs flag → 调 wecom/feishu | `backend/app/services/trading/engine.py:719-725` + `wecom_bot_service.py` (~50 行) |
| F0-3 | **max_position_pct / max_position_count 实盘生效** | engine.submit 加 2 个分支,持仓数超 / 单票市值超 → 拒 | `backend/app/services/trading/engine.py:115-200` (~50 行) |
| F0-4 | **trading_strategy_code_put 接 run_after + 自动 verify 1 个交易日** | PUT → ast.parse + 1 天 dry verify → 通过才落盘 | `backend/app/services/trading/router.py:547-580` (~80 行) |

### 3.2 应该做 (P1 流程级别, 影响回测→实盘信心)

| # | 缺口 | 落点 | 文件预估 |
|---|---|---|---|
| F1-1 | **策略目录统一**: ai/ + custom/ 落 live/ 自动同步 | engine 扫描规则扩展 + ai_generator 落盘目标改 live/ | `backend/app/strategy/engine.py` + `backend/app/strategy/ai_generator.py` (~60 行) |
| F1-2 | **回测页 → 实盘页 "同步实盘" 一键按钮** | Backtest.tsx verify 报告顶部按钮 → 跳 LiveTrading.tsx ?sync_run_id=xxx | `frontend/src/pages/backtest/` + LiveTrading.tsx (~50 行前端 + 0 后端) |
| F1-3 | **实盘页 verify 报告列表 UI + sync-from-verify 入口** | LiveTrading.tsx 详情页加 verify 子区 + sync 按钮 | `frontend/src/pages/LiveTrading.tsx` + `frontend/src/lib/api.ts` (~80 行前端) |
| F1-4 | **drift 检测 UI**: 实盘 vs 最近 verify 标的对比 | 后端算 drift 报告,前端展示 | `backend/app/services/trading/sync.py` (扩) + LiveTrading.tsx (~120 行) |
| F1-5 | **策略代码自动快照 + diff 视图 + 一键回滚** | code_store 写 .py 时同步备份 `live/.history/{id}.{ts}.py`, GET /code/diff?ts=X | `code_store.py` + `router.py` + LiveTrading.tsx CodeView (~150 行) |

### 3.3 半年内做 (P2, 涉及多模块)

| # | 缺口 | 落点 | 文件预估 |
|---|---|---|---|
| F2-1 | **策略生命周期状态机**: 草稿/实验/验证/模拟/实盘/暂停/归档 + 受控转移 | preferences 加 `trading_strategies.{id}.lifecycle`, engine 启动校验, UI 徽标 | `preferences.py` + `live_strategy.py` + LiveTrading.tsx (~200 行) |
| F2-2 | **组合级视图**: 跨策略 sum PnL / 回撤 / 重叠检测 | pnl_snapshots 加组合维度, store 加 cross-strategy 持仓重叠计算 | `pnl.py` + `store.py` + LiveTrading.tsx 新 tab (~200 行) |
| F2-3 | **策略组 (策略组合)**: 创建组合 → 多策略并行 → 组合回测 | 类似聚宽"组合",新 module `app/services/trading/portfolio.py` | 新文件 + 多个 hook (~500 行) |
| F2-4 | **运行时热重载**: 改 live/*.py 后无须重启 backend | mtime 监听 + importlib.reload + scheduler 重新注册 | `live_strategy.py` + `scheduler.py` (~150 行, 高风险) |
| F2-5 | **回测报告持久化统一入口**: 回测/verify/OOS/挖掘 都进同一 ledger | `data/user_data/backtest_runs/{strategy_id}/{run_id}.json` + ledgers UI | 新文件 (~300 行) |

### 3.4 不做 / 延后 (P3, ROI 视产品形态)

- 真实券商对接 (对标报告 P0-1, BrokerAdapter Protocol 已留口, 二开即可, 不在主线)
- 拒单重试 (对标报告 P0-3, 涉及 broker 多实现, 等真券商接入后再统一)
- 多账户并行 (单账户是设计基线, 多账户是产品级扩展)

---
## 4. 与既有对标报告 (P0/P1/P2) 的映射

| 既有报告 P# | 缺口摘要 | 本文 F# | 落点差异 |
|---|---|---|---|
| P0-1 | 无 live broker | (不在本文) | 二开层, 不是流程 |
| P0-2 | wecom/feishu 联通 | F0-2 | 同 |
| P0-3 | 拒单不重试 | (不在本文) | broker 层 |
| P0-4 | max_position_pct/count 未生效 | F0-3 | 同 |
| P1-1 | verify 不携带 risk params | F1-4 (drift 检测) | 范围更广, 包含 drift |
| P1-2 | verify vs real 撮合语义 drift | F1-4 | 同 |
| P1-3 | code 不调 ast.parse | F0-1 | 同 |
| P1-4 | notify_wechat 持久化但未触发 | F0-2 (合并) | 合并到 F0-2 |
| P2-1 | 保存后无 lint/verify | F0-4 | 同, 扩展 |
| P2-2 | 没 diff review | F1-5 | 同, 扩展为完整版本管理 |
| P2-3 | broker mode 无 UI | (不在本文) | broker 层 |
| P2-4 | 异常 fail-mode | F2-4 (热重载相关) | 部分相关 |

**新增 (本文发现, 既有报告未列)**:
- F1-1 策略目录不统一 (ai/custom/live 三套, 实盘只能放 live/)
- F1-2 回测页→实盘页 bridge 缺 (verify 报告在回测页, 同步实盘按钮在哪?)
- F1-3 实盘页 verify 报告 UI 列表缺
- F1-5 自动快照 + diff + 回滚 (策略级版本管理)
- F2-1 策略生命周期状态机
- F2-2 组合级视图 (多策略并行)
- F2-4 运行时热重载 (改 .py 不用重启)

---

## 5. 落地建议 (下一步)

> 本评估不动代码。下一步请你拍板走哪条:

1. **A 路径 — 按 F0 (P0 流程级) 一次性落地**
   - 涉及 F0-1 / F0-2 / F0-3 / F0-4 共 4 项, 每项 ≤ 80 行
   - 阻塞真使用 + 真交易监控, ROI 最高
   - 估时: 1 个完整周期 (后端改 + 单测 + 联调)
2. **B 路径 — 单点打 F1-2 + F1-3 (回测→实盘 bridge UI)**
   - 后端 0 改动, 仅前端补 verify 报告列表 + sync 按钮 + Backtest.tsx bridge
   - 估时: 半天前端, 立刻可见"流程通了一半"
3. **C 路径 — 完整路线图 (F0 + F1) 多 PR 分批**
   - 按本文 §3.1 + §3.2 拆 PR, 1-2 个月落地
   - 适合"先攒 momentum, 后做深"的节奏
4. **D 路径 — 不动, 仅存档本文作为团队 roadmap 文档**
   - 仅 commit 本 .md, 不改任何代码
   - 后续按需挑选实施

---

## 6. 验证矩阵 (实施任一路径后)

无论选哪条路径,落地后必须验证:

- [ ] 策略代码保存 → 立即 ast.parse 通过 (F0-1)
- [ ] 保存代码 → 后端自动跑 1 天 verify → 通过才落盘 (F0-4)
- [ ] 模拟盘 fill 后 → wecom/feishu 收到推送 (F0-2)
- [ ] 单票市值 > max_position_pct → submit 阶段拒单 (F0-3)
- [ ] 回测页 verify 报告 → 实盘页一键同步 → dry_run 预览 → 确认 → 真实 fill (F1-2 + F1-3)
- [ ] 实盘 vs verify drift 自动报告 (F1-4)
- [ ] 策略代码改坏 → 一键回滚到上版 (F1-5)

---

## 7. 引用 / 行号清单

> 本文档主要事实 + 行号均按仓库当前 HEAD 引用,后续 PR 落地时需 rebase 重核。

- AI 生成路径: `backend/app/strategy/ai_generator.py` (~220 行)
- 自定义编写: `frontend/src/components/screener/StrategyPoolDialog.tsx` (publishingId, drafts 分区)
- 策略目录扫描: `backend/app/strategy/engine.py` 自动发现 + `data/strategies/{builtin,ai,custom}` + `data/user_data/strategies/live/`
- 实盘策略注册: `backend/app/services/trading/live_strategy.py:149-170` (importlib.reload 启动时一次性)
- sync-from-verify endpoint: `backend/app/services/trading/router.py:329-367` + `sync.py:230-310`
- verify list endpoint: `backend/app/services/trading/router.py:441` (UI 未接)
- code save endpoint: `backend/app/services/trading/router.py:547-580` + `code_store.py:39-87`
- 监控规则 ↔ 策略: `backend/app/api/monitor_rules.py` + `docs/strategy.md §7`
- per-strategy pnl 隔离: `docs/live-trading-strategy-isolation.md` (4 视图已落地)
- 策略级历史回退: `data/user_data/strategies/live/.bak_cleanup_20261012_*/` (system-level, 非版本管理)
- 实盘页: `frontend/src/pages/LiveTrading.tsx` (~1700 行, 5 tab + 列表 + 详情)
- 回测页: `frontend/src/pages/Backtest.tsx` + `frontend/src/pages/backtest/` (~1500 行)
- 既有对标报告: `docs/reports/strategy-to-live-external-parity-2026-10-13.md` (255 行, P0/P1/P2 清单)
- 实证 drift: `docs/live-vs-backtest-report.md` (1 周标的完全不重叠)

---

## 8. 自我约束

- 本文档只评估 + 建议, 不写代码;不覆盖工作区已有修改。
- 所有行号引用仓库当前 HEAD, 后续落地前请 `git status` + `git diff` 复核。
- 与既有对标报告 (`docs/reports/strategy-to-live-external-parity-2026-10-13.md`) 互补, 不冲突;既有报告的 P0/P1/P2 清单优先级仍按其原顺序。
- 选 A/B/C/D 任一路径前请先确认;不要基于本文档推测"自动开始干"。
