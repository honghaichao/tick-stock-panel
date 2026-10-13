# 架构审查补充报告 — H2 (wait_for_quote_data frozen snapshot)

**补充日期**: 2026-10-13
**补充对象**: 早先架构审查报告里的 **H2** (HIGH 风险项)
**审查范围**: `backend/app/services/trading/data_readiness.py`,
                `backend/app/services/trading/executor.py`,
                `data/user_data/strategies/live/_data_readiness.py`,
                `data/user_data/strategies/live/dividend_auntie*.py`,
                `backend/tests/test_data_readiness.py`

---

## 0. 一句话结论

原报告里 H2 的诊断 — "`wait_for_quote_data` 看 `dp.market_cap`,
但 `quote_service._poll_loop` 不会写回 → 真 09:35 场景下 wait 会 timeout
然后 fallback sparse-data, 不解决问题" — **经核实是真实 bug, 不是理论风险**。

但是: 仓库当前已经部署了**针对该 bug 的部分修复** (2026-10-13 提交),
从 `app/services/trading/data_readiness.py` + `executor.py` 这一层已堵住。
**残留风险是策略层 (`dividend_auntie*.py: my_Trader`) 仍在调用旧版 `_data_readiness.py`,
虽未造成线上事故但留了一条未覆盖路径**, 而且修复方案本身缺单测。

**评级**: 从 HIGH 上调到 **CRITICAL**, 理由见 §4.

---

## 1. 复现 / 验证现场

下面三段是仓库里 H2 的"实物证据链", 每条都附文件 + 行号。

### 1.1 `dp.market_cap` 是一次性 snapshot, 不是 live

```python
# backend/app/services/trading/data_provider.py:281-364
def build_data_provider(repo, quote_service=None) -> StrategyDataProvider:
    ...
    last_price: dict[str, float] = {}                                       # ← 仅构造时拉一次
    if quote_service is not None:
        df = quote_service.get_quotes_compat()                                # ← 一次性 read
        ...
    market_cap: dict[str, float] = {}                                       # ← dict, 不可变引用
    for s in symbols:
        ts = shares_map.get(s, 0.0)
        lp = last_price.get(s) or minute_close.get(s, 0.0)
        if ts > 0 and lp > 0:
            market_cap[s] = ts * lp
    return StrategyDataProvider(..., market_cap=market_cap, ...)
```

`StrategyDataProvider` 是个 `@dataclass` 风格实例
(`backend/app/services/trading/data_provider.py:247`),
所有字段 (`market_cap`, `minute_close`, `total_shares`…) 都是普通 `dict`,
executor 在 `_build_context()` 里直接 `data_provider=...` 注入 `LiveStrategyContext`
(`backend/app/services/trading/executor.py:455`),
之后整个 rebalance 周期内都不会刷新。

### 1.2 `_poll_loop` 不写回 `dp.market_cap`

```python
# backend/app/services/quote_service.py:616-690 (摘录)
def _poll_loop(self):
    while self._running and self._enabled:
        try:
            ...
            self._fetch_quotes(...)            # → _flush_live_enriched + parquet
            ...
```

`_poll_loop` 的写入目标是:
- `self._fetched_at`, `self._index_quotes_cache` (in-process dict)
- `self._repo.flush_live_daily(daily_df)` (parquet)
- `self._flush_live_enriched(daily_df, ...)` (parquet + in-process cached df)

**没有任何路径** 把行情写回 `LiveStrategyContext.data_provider.market_cap`。
grep 结果:

```
$ grep -rn "self\._poll_loop\|dp\.market_cap" backend/app/ --include="*.py"
backend/app/services/quote_service.py:295:    self._thread = threading.Thread(target=self._poll_loop, ...)
backend/app/services/depth_service.py:167:    self._thread = threading.Thread(target=self._poll_loop, ...)
# (无任何 dp.market_cap 写入点)
```

### 1.3 `my_Trader` 调的就是旧版的 frozen-poll 版

```python
# data/user_data/strategies/live/dividend_auntie.py:66
from _data_readiness import wait_for_quote_data          # ← 旧版 (基于 dp.market_cap)

# data/user_data/strategies/live/dividend_auntie.py:232 (在 my_Trader 里)
def my_Trader(context):
    dp = context.data_provider
    ...
    wait_for_quote_data(context)                          # ← 上下文已 frozen
```

```python
# data/user_data/strategies/live/dividend_auntie_universe.py:66, 232
# 同上: 同一个旧版 import + 调用, 在 on_event 入口处.
```

```python
# data/user_data/strategies/live/_data_readiness.py:54-83  (旧版核心)
def wait_for_quote_data(context, *, min_coverage, max_wait_s, poll_s):
    dp = getattr(context, "data_provider", None)
    ...
    target = max(1, int(n_inst * min_coverage))
    while True:
        cur = sum(1 for v in dp.market_cap.values() if v > 0)   # ← frozen dict
        if cur >= target: return cur
        ...
```

---

## 2. 当前已部署的部分修复 (2026-10-13)

仓库在 2026-10-13 已经提交了一版"executor 层"修复, **完全吻合原报告 H2 的判断**:

### 2.1 新版 `wait_for_quote_data` 直接 poll 实时源

```python
# backend/app/services/trading/data_readiness.py (新文件, 替代旧 helper)
def wait_for_quote_data(
    quote_service=None, *, context=None,
    min_coverage=0.80, max_wait_s=60.0, poll_s=2.0,
    expected_count=5882, event="?",
) -> int:
    target = max(1, int(expected_count * min_coverage))
    deadline = time.time() + max_wait_s
    while True:
        if quote_service is not None:
            df = qs.get_quotes_compat()                          # ← 实时缓存, 真 live
            col = "close" if "close" in df.columns else "last_price"
            cur = df.filter(pl.col(col) > 0).height
        else:
            cur = _measure_coverage_dp(context)                  # ← 旧路径兜底
        if cur >= target: return cur
        ...
```

docstring 里**直接引用了 "H2"**:

```
关键设计点 — 必须 BEFORE build_data_provider:
  build_data_provider 返回的 market_cap 是 **一次性 snapshot**, 后续 quote_service 数据
  更新不会回流. 所以 wait 必须发生在 _build_context 调 build_data_provider 之前,
  而不是策略的 on_event 入口. (放到策略里就是 Lock-H2 描述的假修复.)
```

### 2.2 executor 在 `_build_context` **之前**先 wait

```python
# backend/app/services/trading/executor.py:230-252 (run_now 入口)
# 2026-10-13 (H2 fix): 阻塞等 quote cache 就绪之后再 _build_context.
# 必须在拿锁前等 — 否则 build_data_provider() 已经把 market_cap frozen 了,
# 此时再 wait 永远无法更新 dp 的 snapshot, 走原 bug 路径.
strat_state = load_state(self._runner.data_dir, strategy_id)
repo = getattr(engine_mod._APP_STATE, "repo", None) if engine_mod._APP_STATE else None
qs_for_wait = getattr(engine_mod._APP_STATE, "quote_service", None) if engine_mod._APP_STATE else None
...
wait_for_quote_data(
    quote_service=qs_for_wait,
    min_coverage=0.80, max_wait_s=60.0, poll_s=2.0,
    expected_count=repo.get_instruments().height,
    event=event,
)
# 加载 state (同步; 已经走过 wait, _build_context 接着会立刻就绪)
```

这样 `build_data_provider()` 是在 quote_service 缓存已经 ≥80% 之后才执行,
产出的 `dp.market_cap` 在策略 on_event 入口处**已带足量 last_price**,
策略层再调一次 `wait_for_quote_data(context)` 几乎是秒返。

---

## 3. 残留风险 — 为什么还是要评 CRITICAL

虽然 executor 已堵, 但下面 3 条**都是真实可触发的问题**, 不是凑数:

### 3.1 策略层还在用旧版 (真实可触发但被掩盖)

`dividend_auntie.py:66` 和 `dividend_auntie_universe.py:66` 仍然:
```
from _data_readiness import wait_for_quote_data
```

**未替换为**:
```
from app.services.trading.data_readiness import wait_for_quote_data
```

行为后果:
- 当 run_now() 走 executor 全路径 → executor 已 wait 完毕 → 策略层的旧版 wait
  看到 `dp.market_cap` 已经 ≥ 80% → 秒返 → 无害。
- **但**:
  1. 如果未来加 cron 入口 / 调试脚本绕过 `run_now()` 直接构造 context,
     旧版 `my_Trader` 第一行就是 fresh-frozen → fallback sparse-data → 复现原 bug。
  2. 如果 executor 的 wait 被某次重构挪到 `_build_context` 内部或删掉,
     同一份代码路径立刻退化, **没有强制约束保证它俩的执行顺序**。
  3. 旧文件 `data/user_data/strategies/live/_data_readiness.py` 是 user_data 下
     的"用户配置"区域, 理论上策略维护方可能去改它 → 引入回归。

### 3.2 修复方案本身没有单测 (可触发的回归风险)

```
$ grep -rn "app.services.trading.data_readiness\|trading.data_readiness" backend/tests/*.py
backend/tests/test_data_readiness.py:1          # ← 只测旧版 _data_readiness.py (importlib 加载)
```

`test_data_readiness.py` 7 个 case 全是 mock `dp.market_cap` dict 被测试 thread 直接 mutate,
**完全没覆盖新版 `app.services.trading.data_readiness.wait_for_quote_data(quote_service=...)`**
那条执行路径, 也没覆盖 executor.run_now() 的"先 wait 再 build"次序。
也就是说:

- 任何人把 executor 里那 8 行 wait 挪到 `_build_context` 之后 → 测试**仍然全绿**,
  触发线上的回归。
- 任何人重命名 `_measure_coverage_qs` / 改 polars 过滤逻辑 → 没有 unit guard。

### 3.3 真 09:35 case 的 30% coverage 仍然不能在策略层自救

即便修复在 executor 上, 用户描述的"30% coverage 30 秒处"那一段是真的可达的,
只是被提前到了 run_now() 阶段 (早 50ms 内强制等 ≤ 60 秒)。
如果某天的 quote provider 整体响应慢 (例如过节期间 TickFlow 限速),
executor 等满 60s 后仍只能拿到 60% coverage → fallback sparse-data, "不解决问题"
这个原 bug 表述照样成立 — 只是**触发点从策略层挪到了 executor 层**,
对用户而言 09:35 调仓仍可能空跑。这条支持评级上提到 CRITICAL。

---

## 4. 评级: HIGH → CRITICAL?

| 维度 | 评 HIGH 时的判断 | 评 CRITICAL 时的判断 |
|---|---|---|
| **是否理论风险** | "可能" | **已确认是真 bug** (1.1-1.3 的 grep 证据) |
| **触发频率** | 罕见开盘瞬间 | **每次开盘 rebalance** 都过这条路径 |
| **业务影响** | "no_target 不调仓" | 同上, 且无 observability 之外的兜底 |
| **是否线上已救** | — | 部分救, 触发点改到 executor, **对用户同样会空跑** |
| **单元覆盖** | — | 修复方案**0 单测**, 回归风险高 |
| **回归窗口期** | — | 1.26×10³ 行的 executor, _build_context 与 run_now 重构可能性真实 |

**结论**: 上调到 **CRITICAL**。

理由不是"线上事故已发生", 而是:
> 真 bug (不再是理论)、高频路径 (每次开盘)、业务后果显著 (开盘不调仓)、
> 已部分修复但留了一条未测且行为不对称的策略层调用 — 满足 CRITICAL 三项之一即足够, 现在凑齐了。

---

## 5. 最便宜的修复路径 (3-5 行 vs 大改)

### 5.1 推荐: **3-8 行**, 把策略层切到新版

**思路**: 让 `LiveStrategyContext` 透传一个 `quote_service` 引用 (1 个新字段),
策略层 import 替换 + 调用切到新版 (1 行) — 跨 3 个文件, 总改动 < 8 行。

#### diff A — LiveStrategyContext 增字段 (1 行)

```diff
# backend/app/services/trading/live_strategy.py:92 (LiveStrategyContext)
+    quote_service: Any = None  # executor 注入的实时行情单例; wait_for_quote_data live poll 用
```

#### diff B — executor 注入 qs (1 行)

```diff
# backend/app/services/trading/executor.py:455 (_build_context)
        return LiveStrategyContext(
            ...,
+           quote_service=qs,
            data_provider=data_provider,
            ...,
        )
```

#### diff C — 策略 import + 调用切到新版 (每文件 2 行 × 2 文件)

```diff
# data/user_data/strategies/live/dividend_auntie.py:66
-from _data_readiness import wait_for_quote_data
+from app.services.trading.data_readiness import wait_for_quote_data

# data/user_data/strategies/live/dividend_auntie.py:232
-wait_for_quote_data(context)
+wait_for_quote_data(quote_service=context.quote_service, expected_count=dp.instruments.height, event=context.event)
```

```diff
# data/user_data/strategies/live/dividend_auntie_universe.py:66, 232 — 同上
```

**总改动**: < 8 行, 跨 3 个文件。
策略代码从此**不再依赖 user_data/_data_readiness.py**, 后者可以删除 (1 个文件清理)。

### 5.2 不推荐: 大改 (5-7 个文件)

如果想让 dp.market_cap 自己变 live, 需要:
- 把 `StrategyDataProvider` 从 `@dataclass` 改成带回调的胖对象
- 让 `_poll_loop` 多写一个 dict 回去 (破坏模块边界)
- 让 `LiveStrategyContext` 持 lock 保护 mutation
- 适配 backtest / 单元测试 fixture 的 snapshot 语义

预计 80+ 行改动 + 单测, 还可能引入新的并发 bug。**性价比远低于 5.1**。

### 5.3 顺手可做的低成本加强 (推荐一并做)

| 改动 | 行数 | 价值 |
|---|---|---|
| 把 `tests/test_data_readiness.py` 加 2 个 case 覆盖新版 `wait_for_quote_data(quote_service=...)`: 一个 mock qs 立刻 ≥80%, 一个 mock qs 慢涨 | +25 行 | 把 §3.2 的回归风险归零 |
| 删除 `data/user_data/strategies/live/_data_readiness.py` (切完策略 import 后已无人用) | -1 文件 | 去掉"两套 wait" 双源真相 |
| executor.run_now 加 1 行 `assert cur >= target` 强约束 (即 §2.2 wait 返回值) | +3 行 | 强约束"等够再 build"的不变量, 防止以后重构挪 wait |

---

## 6. 验证命令 (reviewer / 用户可立即跑)

```bash
# 1. 复现旧版的 frozen-snapshot 假设 (应看到 dp.market_cap 写入集中点都在 build_data_provider)
grep -n "market_cap\[" backend/app/services/trading/data_provider.py
grep -rn "self\._poll_loop\|dp\.market_cap =\|dp\.market_cap\[" backend/app/ --include="*.py"

# 2. 确认策略层还在用旧版 (切完 import 后应为空输出)
grep -n "_data_readiness" data/user_data/strategies/live/*.py

# 3. 跑现有 readiness 测试 (不应回归)
cd backend && uv run python -m pytest tests/test_data_readiness.py -x --tb=short

# 4. 看 09:35 当天 executor 是否真的先 wait 了
grep -A2 "H2 fix" backend/app/services/trading/executor.py | head -20

# 5. (可选) 在 staging 跑 09:35 dry-run, 观察日志里是否出现
#    "[rebalance] wait_for_quote_data: READY count=N (target=M)"
# 这条 log 只在新版 poll 路径会出现 — 旧版 fake 修复的版本打不出这个 prefix。
```

---

## 7. 给合并报告的提示

合并到早先的架构审查报告时, 把这一份作为 **Annex B: H2 实地复盘** 追加,
并在主报告的 H2 风险条目下加一行:

```
CRITICAL (升级): 实地复盘确认, 见 Annex B (docs/architecture-review-h2-supplement.md)。
                当前已有 executor 层部分修复; 策略层 import 替换 = 3 文件 8 行内。
```
