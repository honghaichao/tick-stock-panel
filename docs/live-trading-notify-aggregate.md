# live-trading-notify-aggregate — 通知中心 + 跨策略聚合 PnL 后端 spec

> 状态: 已落地 (commit 6e0322f5 涵盖), 仅补 spec 文档
> 范围: `backend/app/services/trading/router.py` 新增 4 个端点 + `notify_store` 模块
> 不在范围: 前端接入 (PR2+PR3 集成, 见 `docs/live-trading-v2-pr2-pr3.md`)

---

## 1. 目标

补 commit 6e0322f5 § C 部分(通知中心 + 跨策略聚合 PnL) 的 spec 文档,让接口
契约有据可查,前端 PR2+PR3 集成时有准确的 endpoint / request / response 参考。

成功标准:
- 4 个端点 + 1 个 Pydantic model + 1 个落盘模块,行为有 spec 描述
- 前端接入后(独立 task),`MessagesStream` / `BellNotifications` 等能直接用本 spec 作为 API 参考
- 公网端点实测 200

## 2. 范围

### 2.1 In — 已落地的 4 个端点

#### `GET /api/trading/notify`

- **用途**: 跨策略通知列表(顶部 bell 抽屉 / Monitor 用)
- **Query 参数**:
  - `limit: int = 100` (ge=1, le=1000)
  - `kind: str | None = None` (按 kind 过滤, e.g. `order_rejected`)
- **返回**: `{"items": [...], "total": N}`
- **落盘源**: `data/user_data/trading/notify.jsonl` (按 ts 倒序)
- **空态**: 文件不存在 → `{"items": [], "total": 0}`

#### `POST /api/trading/notify`

- **用途**: 手动追加一条通知(测试 / 调试 / 用户操作入口)
- **Body**: `TradingNotifyIn {kind: str = "info", text: str, source: str = "user", extra: dict | None = None}`
- **kind 白名单**: `notify_store.VALID_KINDS` (见 §3.1)
- **返回**: `{"status": "ok", "entry": {ts, kind, text, source, extra?}}`
- **写盘**: 原子 append, `_write_lock` 守护多线程 (撮合循环 + FastAPI worker 同写)

#### `GET /api/trading/pnl/metrics` (无 strategy_id)

- **用途**: 跨策略聚合 PnL 指标,前端顶栏 'all' / 账户级视图用
- **与现有 `/api/trading/strategies/{sid}/pnl/metrics` 区别**:
  - 现有版本: 单一 strategy_id 过滤
  - 本版本: 无 strategy_id,取**账户级**(account.json + 全部 fills + 全部 snapshots 聚合)
- **返回**: `PnLMetrics.to_dict()` schema 不变(同单策略版本)
- **失败**: 抛 HTTPException 500 + `aggregate pnl metrics 失败: {e}`

#### `GET /api/trading/pnl/history` (无 strategy_id)

- **用途**: 跨策略 PnL 历史快照,前端收益曲线 'all' 视图
- **Query 参数**: `limit: int = 500` (ge=1, le=5000)
- **返回**: `{"snapshots": [...]}` (按 ts 正序)
- **来源**: `pnl.list_history(data_dir, limit=limit)` (跟单策略版本同源)

### 2.2 Out

- ❌ 前端 `BellNotifications.tsx` / `MessagesStream` 接入(PR2+PR3 集成,独立 spec)
- ❌ SSE 长连接推送(`messages.tsx` 现有 polling 5s 已够用,SSE 改造成本大留 PR4)
- ❌ 通知 retention / TTL 自动清理(暂不实现,文件 append-only)
- ❌ 通知全文搜索(grep 类功能,暂不实现)

## 3. 数据契约

### 3.1 `notify.jsonl` schema

每行 JSON object:
```json
{
  "ts": "2026-10-16T14:23:45.123456+08:00",  // CN TZ ISO
  "kind": "order_submitted",                  // 见 VALID_KINDS
  "text": "策略 dividend_auntie 提交买入 600519.SH 100 股 @ 1680.50",
  "source": "system",                          // system / user / strategy_runs 等
  "extra": {"strategy_id": "dividend_auntie", "order_id": "..."}  // 可选
}
```

`VALID_KINDS` (notify_store.py:28):
- `info`, `success`, `warn`, `error`
- `order_submitted`, `order_filled`, `order_rejected`, `order_cleared`
- `clearance`, `retry`, `strategy_run`, `user_message`

未知 kind → 静默回退到 `info`(不报错)。

### 3.2 PnL 聚合指标 schema

跟单策略 `/pnl/metrics` 同源(`PnLMetrics.to_dict()`),关键字段:
- `today_pnl`, `today_pnl_pct` (昨日末笔 → 今日完整 → initial_cash baseline, fix 后)
- `total_pnl`, `realized_pnl`, `unrealized_pnl`
- `initial_cash`, `current_equity`
- `max_drawdown`, `max_drawdown_peak/trough/date`
- `total_trades`, `win_rate`, `avg_pnl_per_trade`

差异: 聚合版本包含**所有 strategy** 的 fills,不是单一 strategy。

### 3.3 不变量

- `notify.jsonl` append-only, 不修历史
- 撮合循环写通知走 `notify_store.append()` (thread-safe)
- 跨策略 PnL 不破坏单策略 `/strategies/{sid}/pnl/metrics` (两个端点独立调 `pnl.compute_metrics`)

## 4. API 契约

| 端点 | 方法 | 入参 | 出参 | 失败码 |
|---|---|---|---|---|
| `/api/trading/notify` | GET | query: limit, kind | `{items, total}` | 503 (data_dir 不可用) |
| `/api/trading/notify` | POST | body: TradingNotifyIn | `{status, entry}` | 422 (kind 校验) |
| `/api/trading/pnl/metrics` | GET | (无) | PnLMetrics dict | 500 (compute 异常) |
| `/api/trading/pnl/history` | GET | query: limit | `{snapshots}` | 503 |

## 5. 行为口径

- **GET notify 排序**: 按 ts 倒序(`list_recent` 内部 reverse + 截 limit)
- **POST notify 幂等**: 不保证,每次 append 一条新 entry(带 ts)
- **GET pnl/metrics 缓存**: 无显式缓存,每次重算 (跟单策略版本同行为)
- **POST notify 自动 source**: 撮合循环 / scheduler / engine 内部调时 source 传 `system`; 用户 UI 调时 source 传 `user`
- **GET notify 文件缺失**: 返回 `{items: [], total: 0}` 不报错(空态)

## 6. 风险与边界

- **notify.jsonl 无限增长**: 没有 TTL / retention,长期累积。建议月度清理(留 30 天),但本 task 不实现。
- **聚合 PnL 性能**: 跨全部 fills + 全部 snapshots 重算,大数据量场景下慢。当前单账户 ~2k snapshots + ~5k fills,实测 < 100ms,可接受。
- **POST notify 鉴权**: 当前无鉴权(沿用其它 trading 端点,登录态校验由 frontend 路由 guard 保证,API 本身信赖)。生产部署需在反代层加认证。
- **kind 白名单外**: 静默 fallback 到 `info`,frontend UI 不会显示特殊 icon。监控 / 告警依赖 source 字段。

## 7. 测试矩阵

| 维度 | 命令 / 操作 | 通过条件 |
|---|---|---|
| 1. 后端测试不破 | `cd backend && uv run --no-project pytest tests/test_trading_*.py` | ≥ 50 passed |
| 2. GET notify | `curl https://<tunnel>/api/trading/notify?limit=10` | 200, 返回 items 数组 |
| 3. POST notify | `curl -X POST -d '{"kind":"info","text":"test"}' -H 'Content-Type: application/json' https://<tunnel>/api/trading/notify` | 200, 返回 entry 含 ts |
| 4. GET pnl/metrics | `curl https://<tunnel>/api/trading/pnl/metrics` | 200, current_equity 跟单账户一致 |
| 5. GET pnl/history | `curl 'https://<tunnel>/api/trading/pnl/history?limit=10'` | 200, snapshots 数组按 ts 正序 |
| 6. notify.jsonl 文件存在 | `ls data/user_data/trading/notify.jsonl` | 文件存在,每行 JSON |
| 7. 跨策略 PnL = 单策略求和 | 手工对照 (当前 1 策略无差异; 多策略后确认求和等于聚合) | 一致 |

## 8. 验收标准

1. 4 个端点公网实测 200
2. notify.jsonl 文件存在且格式正确
3. 跨策略 PnL 跟单账户 account.json 一致
4. spec 跟 commit 6e0322f5 § C 部分代码一致 (字段名 / 路径 / 行为口径对得上)
5. 现有 trading 测试不破

## 9. 不在本轮 (留 follow-up)

- 前端 `BellNotifications` / `MessagesStream` 接入 → `docs/live-trading-v2-pr2-pr3.md` § PR2.1
- SSE 推送改造 → 留 PR4
- notify retention 自动清理 → 留 follow-up
- 鉴权强化 → 留 follow-up
