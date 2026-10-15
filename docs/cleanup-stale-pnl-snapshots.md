# cleanup-stale-pnl-snapshots — task spec

## 目标

清理 `data/user_data/trading/pnl_snapshots.jsonl` 里历史脏快照 (engine 启动
瞬间 quote_service 还没 ready 时写入的 `market_value=0` 半态)。这些数据会
污染两个对外指标:
- `pnl-metrics.max_drawdown`: 当前显示 99.75% (peak=1,038,985.57 vs
  trough=2,603.57), 真实最大回撤应该是 ~0% (干净快照 equity 在
  999,976~1,038,985 范围)
- 收益曲线上的"突降到 0"毛刺 (前一轮已修源头, 历史 333 条没清)

## 范围

In:

- 一次性清理脚本: `data/user_data/trading/pnl_snapshots.jsonl`,
  过滤规则 `market_value > 0` (此规则自然满足"有持仓 + 估值完成"和
  "无持仓 + mv 正确"两种合法情况; 排除"有持仓 + 估值未就绪"半态)。
- 备份原文件 → `pnl_snapshots.jsonl.bak.YYYYMMDDHHMMSS` (不放进
  git, 留 7 天后清; 真有问题能回滚)。
- 重写 jsonl 之后, uvicorn 进程不需要重启 (HMR 不监听这个文件,
  下次 PnL 看板 / 收益曲线拉历史曲线就直接看到干净数据)。
- 写个 `scripts/cleanup_stale_pnl_snapshots.py` 复用 (放 scripts/ 而
  不是 tools/, 一次性, 不需要常驻)。

Out:

- 不动 `data/user_data/trading/strategy_runs.jsonl` (那里的"半态"
  是策略 trigger 记录, 不是市场数据)。
- 不动 `data/user_data/trading/pnl_strategy_snapshots.jsonl` (per-
  strategy 视图, 同步清理: 同源数据)。
- 不动 `account.json` (当前 `market_value=1,003,941` 是对的, 已经是
  refresh 之后的值)。
- 不动后端代码 (前一轮 `_refresh_account_market_value` / 
  `_write_pnl_snapshot` / `main.py session_start` 三处守卫已阻止新
  脏数据; 这次只清历史)。
- 不修 PnL metrics 的 `max_drawdown` 算法 (它是对的, 只是被脏数据
  污染输入)。

## 数据契约

### 3.1 输入

`data/user_data/trading/pnl_snapshots.jsonl` 当前 1,599 条:
- 1,154 条 `market_value > 0` (干净, 保留)
- 333 条 `market_value == 0` (半态, 删除; 全部有持仓, 现金
  2,603.57 表明昨日买了 ~1M)
- 112 条 `market_value > 0 但 total_equity = cash` (持仓已估值但
  cash 全仓, 仍然有效, 保留)

按"market_value > 0"过滤后剩 1,154 条干净快照。

### 3.2 输出

`pnl_snapshots.jsonl` (覆盖) — 1,154 条, 按 ts 升序。
`pnl_snapshots.jsonl.bak.YYYYMMDDHHMMSS` — 1,599 条原文件备份。
`pnl_strategy_snapshots.jsonl.bak.YYYYMMDDHHMMSS` — 同源 per-
strategy 备份 (清理后, 顺手).

### 3.3 不变量

- 备份文件 mtime 保留 (原子 rename, 不再被覆盖)
- 不动 `account.json`
- 不动 `fills.jsonl` / `orders.jsonl` / `strategy_runs.jsonl`

## API 契约

无路径变化. `GET /api/trading/pnl-metrics` 和 `GET /api/trading/pnl-history`
输出变化:
- `max_drawdown`: 从 99.75% 降到 ~0% (干净曲线的真实值)
- `max_drawdown_peak/trough/date`: 重算, 取干净快照范围
- 收益曲线无毛刺

## 行为口径

- 过滤规则: 保留 `market_value > 0` 的快照. `market_value == 0` 的
  一律视为半态删除, 不区分"真无持仓"和"估值未就绪"两种情况
  (前一轮已加守卫阻止新半态; 当前账户始终有持仓, 无"真无持仓"快照
  存在)。
- 备份命名: `pnl_snapshots.jsonl.bak.YYYYMMDDHHMMSS` (本地时区);
  备份文件不进 git (在 `.gitignore`).
- 清理后, 立刻可验证:
  - `curl /api/trading/pnl-metrics` → max_drawdown < 1%
  - 收益曲线无 2603.x 平台

## 风险与边界

- 不可逆风险: 一旦覆盖, 旧 jsonl 在备份里, 不在 jsonl. **用
  atomic_write_text** 落新 jsonl, 出错保留旧版.
- 备份不被 git 跟踪, 不会被 merge 误覆盖, 但**也没法靠 git 回滚**。
  7 天后人工确认无问题可 `rm` 掉。
- 不动 `pnl_strategy_snapshots.jsonl` 内的半态 per-strategy view
  (代码同源, 但这个文件不在 metrics 路径上, 用户没感知, **保留
  现状不动**, 免得动了又触发别的 bug).
- 如果用户有跨日复盘需求, 旧脏数据是 10-13 (昨天) 的引擎启动
  序列, 删了影响的是"昨天 9 点 - 9:30 之间的权益曲线", 但 9:30
  之后有完整数据, 10 分钟级别的复盘无影响。
- 清理后第一次访问 PnL 看板, frontend react-query 5min 缓存里
  的 max_drawdown 还是 99.75% (HTTP 304 不发), 用户要刷一次硬刷
  才能看到新值; 这是 frontend 缓存机制, 不在本 task 范围。

## 测试矩阵

| 维度 | 命令 / 操作 | 通过条件 |
|------|-------------|----------|
| 1. dry-run | `python scripts/cleanup_stale_pnl_snapshots.py --dry-run` | 打印要删的快照数 (333), 备份路径, 不动文件 |
| 2. 备份存在 | `ls data/user_data/trading/pnl_snapshots.jsonl.bak.*` | 1 个 bak 文件, mtime=执行时刻, 字节数=原文件 |
| 3. 清理后行数 | `wc -l data/user_data/trading/pnl_snapshots.jsonl` | 1,154 (1,599 - 333 - 112?  实际看脚本输出) |
| 4. mv=0 残留 | `grep -c '"market_value": 0' data/user_data/trading/pnl_snapshots.jsonl` | 0 |
| 5. max_drawdown 修复 | `curl /api/trading/pnl-metrics \| jq .max_drawdown` | < 0.05 (5%) |
| 6. 收益曲线无毛刺 | 浏览器刷实盘页 | 看不到"突降到 0" |
| 7. 已有测试不破 | `pytest tests/test_trading_pnl.py` | 6 passed |

## 验收标准

1. 脚本 `scripts/cleanup_stale_pnl_snapshots.py` 入库, 接受 `--dry-run` /
   `--apply` 两个 flag, 默认 `--apply` (脚本是修复用, 跑一次)。
2. 备份文件存在, 字节数 = 原文件字节数 (md5 一致)。
3. 清理后, `pnl_snapshots.jsonl` 内 0 条 `market_value == 0`。
4. `GET /api/trading/pnl-metrics` 返回的 `max_drawdown` < 0.05。
5. 实盘页收益曲线无毛刺 (用户视觉确认)。
6. `tests/test_trading_pnl.py` 6 passed。
