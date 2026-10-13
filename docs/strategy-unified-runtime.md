# 策略统一 Runtime（一份代码 = 回测 + 实盘）

> 状态：定稿（用户拍板方案 C：统一接口）
> 范围：1 份策略代码同时跑回测 (matrix-native) 和实盘 (LiveStrategy)
> 依赖：现有 builtin/* (回测) + live/* (实盘) — 不动 framework 大结构

---

## 1. 目标

策略代码沉淀在单一文件里（`data/user_data/strategies/live/{id}.py`）：
- 同一份类同时实现实盘 `on_event` 和回测 `compute_signals`
- 引擎自动发现 + 自动分发：哪个 runtime 调用就用哪套接口
- 不需要写两份代码（一份 .py 里两个 class 也不需要）
- 用户在 Web 端：看一份代码 = 看完整策略逻辑

## 2. 范围

### In
- `LiveStrategy` Protocol 增加可选方法 `compute_signals`
- `backtest.engine` 在加载策略时优先查 `live/*` LiveStrategy，实现 `compute_signals` 就当 matrix 策略用
- `dividend_auntie.py` 加 matrix 算子（dividend_ratio 日 K 计算 + 5 个 filter）
- 回测能跑出 dividend_auntie 结果

### Out
- builtin/*.py 重写为 LiveStrategy（迁移工作量大，本 task 不做）
- 改 LiveStrategy 必填项（保持向后兼容）
- 改 matrix 框架本身

## 3. 设计

### 3.1 单一类接口

```python
class DividendAuntieStrategy(LiveStrategy):
    # ── 实盘 (现有) ──
    id = "dividend_auntie"
    schedules = [...]
    def on_event(self, event, ctx) -> dict[str, int]: ...
    
    # ── 回测 (新增) ──
    META = {  # 复用 builtin 的 META 字段
        "id": "dividend_auntie",
        "name": "菜场大妈",
        "asset_types": ["stock"],
        "timeframes": ["1d"],
        "params": [...],
        "scoring": {...},
        "order_by": "score",
        "descending": True,
        "limit": 5,
    }
    EXECUTION_BACKEND = "matrix_native"  # 标记这是 matrix 策略
    
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume", "dividend_ratio"})
    
    def required_warmup_bars(self, params) -> int:
        return 365  # 一年分红窗口
    
    def compute_signals(self, market: MarketDataMatrix, params) -> SignalMatrix:
        # 用日 K + 分红数据算 dividend_ratio, 5 个 filter
        # 返回 entry/exit matrix
        ...
```

### 3.2 引擎发现链

**当前**：
- 回测：engine 扫描 `builtin/*` → 找 `MATRIX_STRATEGY = XxxMatrixStrategy()`
- 实盘：LiveStrategyRegistry 扫描 `live/*` → 找 `class XxxStrategy(LiveStrategy)` 实例化

**改后**：
- 回测：engine 先扫 `live/*` 找 LiveStrategy 实例（跟实盘一样）；如果该实例有 `compute_signals` + `META`，**当 matrix 策略用**；否则 fallback 到 builtin/
- 实盘：不变

### 3.3 数据流

| 阶段 | 回测 | 实盘 |
|---|---|---|
| 数据源 | `MarketDataMatrix` (日 K 矩阵) | `StrategyDataProvider` (分钟 K + 分红表) |
| 调用 | `strategy.compute_signals(matrix, params) -> SignalMatrix` | `strategy.on_event(event, ctx) -> dict[qty]` |
| 撮合 | backtest engine 按 SignalMatrix 撮合 | TradingEngine 按 pending order 撮合 |
| 频率 | 日级扫一次（跨年） | 每日 3 个 cron 触发 |

## 4. 数据契约

### LiveStrategy Protocol 新增

```python
class LiveStrategy(Protocol):
    # 现有必填
    id: str
    name: str
    enabled_default: bool
    schedules: list[StrategySchedule]
    def on_event(self, event, ctx) -> dict[str, int]: ...
    
    # 新增可选 — 回测矩阵算子
    META: dict | None = None  # 复用 builtin META schema
    EXECUTION_BACKEND: str = ""  # "matrix_native" 才算回测
    def required_fields(self) -> frozenset[str] | None: ...
    def required_warmup_bars(self, params: dict) -> int | None: ...
    def compute_signals(self, market, params) -> SignalMatrix | None: ...
```

**默认值**：3 个新方法都返回 `None` / 默认值；表示"这个策略没有回测版"。
回测引擎看到 None 就 fallback 到 builtin/。

### 错误处理

- LiveStrategy 有 `META` 但没 `compute_signals` → 启动时 WARN，回测走 builtin
- LiveStrategy 有 `compute_signals` 但 `MarketDataMatrix` 缺字段 → 启动时 ERROR（明确告诉用户）
- builtin/* 和 live/* 都有同 id → builtin 优先（向后兼容）；WARN 提示 live 也有

## 5. dividend_auntie 矩阵算子设计

### 5.1 数据流

```
日 K (close, volume, limit_up, limit_down, name)  ← MarketDataMatrix
分年度分红 (symbol, ex_date, bonus_per_share)    ← market.fields["dividends_<year>"]
```

### 5.2 compute_signals 实现

```python
def compute_signals(self, market, params):
    # 1. 算 dividend_ratio: 过去 365 天 bonus_per_share 累计 / market_cap
    div_field = self._build_dividend_field(market)  # 每天累计分红
    
    market_cap = market.close * market.fields["total_shares"]
    div_ratio = div_field / market_cap  # unitless
    
    # 2. 取 top 25% dividend_ratio
    top_25_pct = _top_pct_mask(div_ratio, p=0.25)
    
    # 3. 5 个 filter
    entry = top_25_pct
    entry &= ~_st_filter(market.fields["name"])
    entry &= ~_paused_filter(market.close)
    entry &= ~_limitup_filter(market.close, market.limit_up_locked)
    entry &= ~_limitdown_filter(market.close, market.limit_down_locked)
    entry &= market.close < params["high_price_threshold"]
    
    # 4. entry 当日全市场 → 按市值升序取 top stock_num
    # 简化: entry=True 当日给个 exit=False 信号
    
    return make_signal_matrix(...)
```

### 5.3 复杂度

- `_build_dividend_field`: O(N × 365) — 每只票 365 天滑动窗口
- filter 链: O(N) 逐元素
- 整体: O(N × 365) ≈ 100 万次操作，秒级

## 6. 任务拆分（先做最小可工作版本）

### Phase 1（本 task，做）
- [x] md 设计 (本文)
- [ ] `LiveStrategy` Protocol 加 3 个可选方法（向后兼容）
- [ ] `backtest.engine._load_strategy_meta` 改造：先查 live/* 有 LiveStrategy + META 就用
- [ ] `dividend_auntie.py` 实现 `compute_signals` (matrix 版)
- [ ] 测试：dividend_auntie 回测能跑，结果非空
- [ ] 测试：dividend_auntie 实盘还能跑（向后兼容）

### Phase 2（后续 task）
- 迁移 builtin/*.py 到 LiveStrategy（按需）
- matrix cache 优化
- DSL 抽象（如果需要）

## 7. 测试矩阵

| 用例 | 期望 |
|---|---|
| dividend_auntie LiveStrategy 没 compute_signals (默认) | 回测走 builtin (无变化) |
| dividend_auntie 加了 compute_signals | 回测用 live 版的 matrix 算子 |
| dividend_auntie 跑回测 1 年 | 至少有 5 个 entry 信号（top 25% 高股息） |
| dividend_auntie 跑实盘 1 次 | on_event 返回非空 target（rebalance）或 {} (no_target) |
| 同时有 builtin/dividend_auntie 和 live/dividend_auntie | builtin 优先 + WARN 提示 |
| 旧 builtin 策略 (无 LiveStrategy) | 完全不变 (向后兼容) |

## 8. 风险

- **回测结果与实盘行为差异**：matrix 算子用日 K 全市场扫描；实盘是 on_event 增量。两边业务逻辑要严格 1:1 翻译（边界：日 K 9:30 vs 分钟 K 9:30 撮合价不同）
- **分红数据缺失**：matrix 的 fields 需要日级分红聚合，仓库目前只有 `data_provider.dividends(symbols, since, until)`；需要预聚合
- **META 字段重复**：LiveStrategy 跟 builtin META schema 不完全一致，需要 union + 字段映射

## 9. 验收

```bash
# 回测 dividend_auntie
cd /Volumes/quant-ssd/projects/tickflow-stock-panel/backend
uv run --frozen python -c "
from app.backtest.engine import run_backtest
result = run_backtest(strategy_id='dividend_auntie', start='2025-01-01', end='2025-12-31')
print('trades:', len(result['trades']))
print('final_pnl:', result['metrics']['total_return'])
"
# 期望: trades > 0, final_pnl 是数字

# 实盘 dividend_auntie 还能跑
uv run --frozen python -c "
from app.services.trading.live_strategy import LiveStrategyRegistry
from data.user_data.strategies.live.dividend_auntie import STRATEGY
r = LiveStrategyRegistry()
r.register(STRATEGY)
print('id:', STRATEGY.id)
print('schedules:', len(STRATEGY.schedules))
# on_event 验证
from datetime import date
class Ctx:
    positions = {}; account = {'cash': 1000000.0}; state = {}
    data_provider = None; market = {}; trade_date = date(2026, 1, 15)
    event = 'rebalance'; as_of = ''; fills_recent = []
ctx = Ctx()
print('on_event(rebalance):', STRATEGY.on_event('rebalance', ctx))
"
# 期望: id='dividend_auntie', schedules=3, on_event 返回 dict (可能空因为 data_provider 是 None)
```

## 10. 改动文件

### 后端
- `backend/app/services/trading/live_strategy.py` — Protocol 加 3 个可选方法
- `backend/app/strategy/engine.py` — `_load_strategy_meta` 改造：先查 live/*
- `data/user_data/strategies/live/dividend_auntie.py` — 加 `META / EXECUTION_BACKEND / compute_signals` + 辅助函数
- 测试 `tests/test_strategy_unified.py` (新)
