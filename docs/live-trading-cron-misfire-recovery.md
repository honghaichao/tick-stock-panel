# 实盘 cron 启动补跑：盘后重启不再丢盘前时点（Live Trading Cron Misfire Recovery）

> 状态：**待 review**
> 范围：调度层 `StrategyScheduler` 启动补跑逻辑
> 不在范围：撮合层 / 撮合 17:00 清算 / 通知通道

---

## 1. 目标

解决"后端在盘后（17:00 后）重启后，今天 9:05 / 9:35 / 13:30 全部因 `misfire_grace_time=3600` 过期而被 APScheduler 丢弃"导致的盘前无人调度问题。

成功标准：

- 后端在 9:30 重启时，今天 `pre_market` (9:05) 在 grace 窗口内 → 自动补跑一次
- 后端在 14:00 重启时，今天 `pre_market` 仍在 grace 窗口内、`rebalance` (9:35) 仍在 grace 窗口内、`limit_check` (13:30) 仍在 grace 窗口内 → 三个都补跑一次
- 后端在 18:00 重启时，全部已过 grace 窗口 → 不补跑；明天 9:05 正常 cron 触发
- reload 反复触发（5 分钟 5 次）不重复补跑同一时点
- 补跑与正常 cron 触发、日志、撮合、飞书通知路径一致

## 2. 范围

### 2.1 In

- 新增 `StrategyScheduler._recover_missed(strategies)` 方法
  - 在 `apply_all` 末尾调用一次
  - 遍历每个 enabled schedule，计算"今天该时点的 datetime"
  - 在 grace 窗口内 → push 到后台线程执行 `executor.run_now(strategy_id, trigger="recovery", event=event)`
- 新增 `trading_strategies.{id}.last_recovery.{event}.{YYYY-MM-DD}` 持久化标记
  - 同一 (sid, event, date) 仅补跑 1 次；reload / 重启不重复
- 新增 `recovery_grace_minutes` 配置（per-event 可覆盖）
  - 默认值（写死在代码）：
    - `pre_market`：9:05 之后 + 90 分钟（即到 10:35）
    - `rebalance`：9:35 之后 + 180 分钟（即到 12:35）
    - `limit_check`：13:30 之后 + 120 分钟（即到 15:30）
- logger 显式记录每次补跑决定（跳过 / 触发 / 拒绝 [原因]）
- 触发后的 `RunReport.trigger` 字段新增 `"recovery"` 取值（其它取值不变：`"manual"`, `"cron"`, `"api"`）
- 单元测试：mock 时间，覆盖 (现在时点, schedule, 已跑与否, 是否补跑) 真值表

### 2.2 Out

- ❌ 不改 `misfire_grace_time`（保留 3600，作为二级防线）
- ❌ 不改 APScheduler cron 注册流程
- ❌ 不改撮合层、风控、broker
- ❌ 不动 17:00 清算（live-trading-joinquant-parity.md §3.3，独立 cron）
- ❌ 不动 notify/飞书通道（#4 在另一轮修）
- ❌ 不动 UI 层

## 3. 数据契约

### 3.1 preferences.json 新字段

```json
{
  "trading_strategies": {
    "<sid>": {
      "last_recovery": {
        "<event>": {
          "2026-10-14": "2026-10-14T17:30:25+08:00"
        }
      }
    }
  }
}
```

- key=YYYY-MM-DD (Asia/Shanghai)
- value=ISO 本地时间戳
- 写入工具：`preferences.py` 新增 `mark_recovery(sid, event, when_iso)` / `get_last_recovery_date(sid, event)`
- 读不到 / 不存在 → 视为今天未补跑（正常路径）

### 3.2 RunReport.trigger 取值扩展

| 取值 | 含义 | 来源 |
|---|---|---|
| `manual` | 用户在 UI / CLI 主动跑（保留旧语义） | 当前默认值 |
| `cron` | APScheduler 到点自然触发 | `_async_run` |
| `api` | 调 `/api/trading/strategies/{id}/run-now` | router |
| **`recovery`**（新增） | 启动补跑错过时点 | `StrategyScheduler._recover_missed` |
| `sync-from-verify` | 回测→实盘同步入口 | 已存在 |
| `retry` | 重跑 run_now retry | 已存在 |

`preferences.is_strategy_schedule_enabled` 不变。

## 4. API 契约

无新增 / 修改端点。

后端内部：

- `StrategyScheduler.apply_all(strategies)` 末尾追加：`if self._scheduler is not None: self._recover_missed(strategies)`
- `StrategyScheduler._recover_missed(strategies)`：
  - 取 `now = now_cn()`
  - 对每个 (sid, sch) where prefs.is_strategy_schedule_enabled(sid, sch.name):
    - target = today( sch.hour, sch.minute )
    - grace_end = target + timedelta(minutes=recovery_grace[sch.name])
    - in_window = target <= now <= grace_end
    - already = prefs.get_last_recovery_date(sid, sch.name) == today_date
    - if in_window and not already:
      - prefs.mark_recovery(sid, sch.name, now.isoformat())
      - threading.Thread(target=self._executor.run_now, args=(sid,), kwargs={"trigger": "recovery", "event": sch.name}, daemon=True).start()
      - logger.info("[recovery] %s :: %s fired (target=%s, grace_end=%s)", sid, sch.name, target, grace_end)
    - else:
      - logger.info("[recovery] %s :: %s skipped (reason=%s)", sid, sch.name, reason_str(in_window, already))
- `reason_str(in_window, already)`:
  - `already_recovered` if already
  - `out_of_window` else

## 5. 行为口径

| 维度 | 规则 |
|---|---|
| 触发频率 | per (sid, event, date) 1 次 |
| 触发时间 | today(sch.hour, sch.minute) ≤ now ≤ grace_end |
| 并发 | 同 (sid, event) 触发 `recovery` 与 `cron` / `api` 同时在跑：`executor` 已有 per-(sid, event) in_flight 锁，最早的赢，后到的直接返 in_flight report |
| 与 APScheduler 关系 | 不动 cron 注册；recovery 是"补救"，不替代正常 cron |
| reload 重复 | `last_recovery.<date>=<ts>` 持久化 → reload 后读得到 → 跳过 |
| 启动时不补跑上周 | `last_recovery.<date>` key 是 YYYY-MM-DD，按日期匹配；周末启动不会触发已经过了 1+ 天的时点 |
| pre_market grace 边界 | 9:05 ≤ now ≤ 10:35 → 触发；now > 10:35 → 不触发 |
| rebalance grace 边界 | 9:35 ≤ now ≤ 12:35 → 触发；now > 12:35 → 不触发 |
| limit_check grace 边界 | 13:30 ≤ now ≤ 15:30 → 触发；now > 15:30 → 不触发 |
| 交易日判断 | 不做。grace 窗口够长（≥ 90 分钟），非交易日 cron 本来就不会到点 |
| 失败行为 | 补跑 run 失败 → 已有的 `RunReport.status="error"` 记录到 `strategy_runs.jsonl`，与现有一致；不影响其他 schedules |

## 6. 风险与边界

| 风险 | 缓解 |
|---|---|
| 补跑触发后立刻又到了 schedule 正常 cron 时间，重复运行 | pre_market 补跑完时已过 10:35，下次 cron 触发在明天；rebalance / limit_check 同理（grace < 1 天）。in_flight 锁兜底 |
| 补跑和 9:35 rebalance 撞车，rebalance 还没真正调仓就先被 9:05 pre_market 补跑 | 不可能：pre_market grace 9:05-10:35，rebalance grace 9:35-12:35。pre_market 补跑若在 10:00 触发，与真正的 rebalance 间隔正常 |
| 补跑时 market value 还没写（撮合首次 tick 未到） | `run_now` 已有 `wait_for_quote_data(timeout=60s)`，复用了 10-13 的 H2 fix |
| 补跑时策略里 `state` 已经被持久化但今天从未跑过 cron | `executor.run_now` 每次都 load_state，正是预期行为 |
| 补跑 `recovery` 写在 runs.jsonl 后，前端如何显示 | 复用现 trigger 字段，前端按 trigger 列分组 / 染色补 1 行 |
| preferences.json `last_recovery` 字段污染 | 顶层限定为 `trading_strategies.<sid>.last_recovery`，不影响其他字段；写前读合并，不丢旧字段 |
| 跨时区 / DST | Asia/Shanghai 固定 + `datetime.now(CN_TZ)` 已有现成 helper（见 executor.py） |

## 7. 测试矩阵

| # | 命令 | 期望 |
|---|---|---|
| 1 | `pytest tests/test_trading_scheduler_recovery.py -k normal` | 6 用例全过：<br>- 9:30 启动 + pre_market enabled → 补跑<br>- 14:00 启动 → 3 个 schedule 都补跑<br>- 18:00 启动 → 0 个补跑<br>- reload 后再启动 → 不重复补跑<br>- grace 之外的时点跳过<br>- reload 时 lock 持有 → 抛 in_flight 不报错 |
| 2 | 启动后 `cat preferences.json \| jq .trading_strategies.*.last_recovery` | 含 `YYYY-MM-DD` key |
| 3 | `tail -20 strategy_runs.jsonl \| jq '.[] \| select(.trigger=="recovery")'` | 至少 1 条新行 |
| 4 | `tail -50 backend.log \| grep "recovery"` | 含 `[recovery] <sid> :: <event> fired/skipped` |
| 5 | `pytest tests/test_trading_e2e.py` | 全过（不破现有 e2e） |
| 6 | 手动 API `GET /api/trading/strategies` | schedules 字段不变 |

新增 fixture：`tests/test_trading_scheduler_recovery.py` mock `prefs.mark_recovery` / `prefs.get_last_recovery_date`，固定 `now_cn()` 返回值。

## 8. 验收标准

1. 启动补跑逻辑工作如 §5 行为口径表
2. `trigger="recovery"` 在 runs.jsonl 可被识别
3. `preferences.last_recovery` 持久化跨重启有效
4. pytest 全过；新增 ≥ 6 用例，原有 trading 测试零回归
5. 后端日志含明确的 fired/skipped 行
6. 不动 cron 注册、不动 misfire_grace_time、不动 fronted

## 9. 不在本轮

- 通用 cron "补跑历史 N 天"（需要新需求驱动）
- 飞书通知接入触发 (#4 已在外层修)
- 撮合 17:00 清算（独立 doc）
- 国庆等非交易日「grace 自动扩展到第二个交易日」（见 docs/live-trading-joinquant-parity.md 不在范围）
- UI 层 "recovery" 标记颜色与图标（前端同事定）

## 10. PR 计划

按 CONTRIBUTING.md §11：

- 单个 PR
- commit 1（小）：§3.1 preferences 读写 helper
- commit 2（核心）：§4 _recover_missed 实现 + apply_all 末尾调用 + RunReport.trigger 取值扩展
- commit 3（测试）：§7 测试矩阵
- 提交后按现有 PR 复审流程

