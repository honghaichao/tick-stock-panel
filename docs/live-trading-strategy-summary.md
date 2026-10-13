# 实盘 per-strategy 摘要 (Live Trading per-Strategy Summary Backend)

> 状态: **已落地** (user round-4 确认 B1-B5 + PR-1; 12/12 pytest 绿; 端到端 smoke 全通)
> 关联: 上游 task `live-trading-list-detail-layout.md` 列表页表格需要后端补 6 列数据 (开始时间 / 累计 / 年化 / 今日 / 最大回撤 / 微信通知); 本任务补齐这 6 列 + 增加 `DELETE` endpoint
> 范围: 后端新增 2 个 endpoint + 1 个 endpoint 增强; 前端类型更新
> 不在范围 (本任务): 撮合/调度逻辑修改; pnl_snapshots.jsonl schema 修改 (保留账户级); 通知引擎联通 (后续 `live-trading-notification.md`)

---

## 0. 决策点 (待 user 确认)

| # | 决策点 | 默认方案 | 备选 |
|---|---|---|---|
| B1 | `started_at` 派生 | **registry 启动时间** (策略注册到 `live_strategy_state/<id>.json` 时记录 `registered_at` 字段) | A. 扫 `strategy_runs.jsonl` earliest; B. 单独建 `started_at.json` |
| B2 | per-strategy 性能聚合数据源 | **新建独立流 `pnl_strategy_snapshots.jsonl`**, 增量写: 每个策略 event 完成后 / 每个 attribution 的 fill 落盘后写一条 (含 `strategy_id` + per-strategy 字段); 表读取这一个文件, 不再做 fills × orders JOIN | A. 扫 fills × orders JOIN; B. 改 `pnl_snapshots.jsonl` schema (破坏现有数据, 不采纳) |
| B3 | 通知开关 (`notify_wechat`) 当前是否联通到通知引擎 | **不联通** (只写 prefs; 读取由 `live-trading-notification.md` 接) — toggle 现在可切换/显示, 但不发通知 | A. 现在联通 |
| B4 | DELETE 是否可逆 | **加回收站**: 删 prefs + 移动 `.py` 到 `data/user_data/strategies/live/.trash/<id>_<ts>.py` + 从 registry 注销; 历史 JSONL 保留; 后续如需恢复: 手动 mv 回 `live/` + 重启 backend. 前端需二次确认 | A. 不可逆 (直接删 prefs + deregister, 保留 .py 在原位) |
| B5 | DELETE 与正在跑的 run | **立即 cancel 当前 run** (scheduler 收到 stop 信号, 中断新撮合; 接受可能 race — 部分已 fill 部分未 fill) | A. 等当前 run 自然完成, scheduler 不再 trigger 新 event |

> 实施顺序: PR-2 (前端 `live-trading-list-detail-layout.md`) 先合; PR-1 (本任务 = backend 改动) 后合. 后端接口上线前, 前端表格 6 列 + [删除] 按 `—` 占位 / 软删除 模拟; 后端接口上线后, 在前端代码里解占位切真接口.

---

## 1. 目标

为 `live-trading-list-detail-layout.md` 中的列表页表格补齐 6 列数据 + 增加删除能力:

```
GET /strategies                              增强: 增加 started_at / notify_wechat / summary
POST /strategies/{id}/notify                 新:   toggle notify_wechat
DELETE /strategies/{id}                      新:   从 prefs + registry 注销, 保留源文件
```

成功标准:
- `GET /strategies` 响应每条策略增加 `started_at` / `notify_wechat` / `summary` 字段; 不破坏既有字段
- `summary.total_pnl / total_pnl_pct / annual_return / today_pnl / today_pnl_pct / max_drawdown / max_drawdown_date` 与"如果手工算"一致 (±0.5% 容忍)
- `POST notify` 在 `prefs.json` 写入 `trading_strategies.<id>.notify_wechat = bool`
- `DELETE` 后: 该策略的 prefs 键清空; `.py` 源文件移动到 `data/user_data/strategies/live/.trash/<id>_<ts>.py` (B4 = trash); 从 registry 注销; 当前正在跑的 run 立即 cancel (B5); 历史 `strategy_runs.jsonl` / `orders.jsonl` / `fills.jsonl` 保留; 下次 `GET /strategies` 不再返回该策略
- 恢复流程: 手动 mv `.trash/<id>_<ts>.py` 回 `live/<id>.py`, 重启 backend, 策略自动重新注册 + schedules 从 prefs 恢复
- 全量扫描 fills+orders 单次 < 50ms (1 周正常用量级)
- 全部向后兼容: 旧前端仍可调 `/strategies` 返回的字段不变

---

## 2. 范围

### 2.1 In

- **后端**: 3 个 endpoint 改动 + 多个 helper
  - `pnl.write_strategy_snapshot(data_dir, strategy_id, ...)` (新) 在 `pnl.py` — 增量写 `pnl_strategy_snapshots.jsonl`
  - `pnl.compute_strategy_summary(data_dir, strategy_id) -> dict | None` (新) — 从 `pnl_strategy_snapshots.jsonl` 读 + 派生 7 字段 summary (不再 JOIN fills × orders)
  - `Registry.remember_started_at(strategy_id, ts)` (新) — `live_strategy_state/<id>.json` 记录 `registered_at` 字段
  - `Registry.deregister(strategy_id) -> list[str]` (新) — B5: 立即 cancel 当前 run
  - `code_store.move_to_trash(data_dir, strategy_id) -> Path` (新) — B4: 移动 .py 到 `.trash/<id>_<ts>.py`
  - `Preferences.set_notify_wechat(strategy_id, bool)` / `delete_strategy_keys(strategy_id)` (新)
- **后端**: 1 个 endpoint 增强
  - `GET /strategies` 增加 3 字段 (§3.1)
- **后端**: 2 个新 endpoint
  - `POST /strategies/{id}/notify`
  - `DELETE /strategies/{id}`
- **前端**: `LiveStrategyApi` 类型扩字段 (3 个 optional)
- **测试**: `tests/test_strategy_summary.py` 覆盖性能聚合 edge case + DELETE 注销流程

### 2.2 Out

- 不改 `pnl_snapshots.jsonl` schema (`trigger` 已有 strategy 信息但不是结构化字段)
- 不改撮合/调度逻辑
- 不联通通知引擎 (B3)
- 不实现"被删除策略回收站" (B4)
- 不实现 per-strategy 持仓拆分 (需要新 attribution pipeline, 后续)

---

## 3. 数据契约

### 3.1 `GET /strategies` 增强后响应

每条 item 增加 3 个 optional 字段 (向后兼容, 旧客户端忽略即可):

```json
{
  "id": "dividend_auntie",
  "name": "...",
  "enabled_default": false,
  "schedules": [...],
  "next_run": "2026-10-13T09:35:00+08:00",

  "started_at": "2026-09-29T11:04:13.258548+08:00",
  "notify_wechat": false,
  "summary": {
    "total_pnl": 12345.67,
    "total_pnl_pct": 1.23,
    "annual_return": 5.6,
    "today_pnl": 234.5,
    "today_pnl_pct": 0.0234,
    "max_drawdown": 0.07,
    "max_drawdown_date": "2026-10-08"
  }
}
```

字段缺省值:
- `started_at`: 缺/无 run → `null`, 前端显示 `—`
- `notify_wechat`: 缺 prefs 键 → `false`
- `summary`: 该策略无 fill (无 attribution) → `null`, 前端显示 `—` 整列

### 3.2 `POST /strategies/{id}/notify`

```http
POST /api/trading/strategies/dividend_auntie/notify
Content-Type: application/json
{"enabled": true}
→ 200 {"notify_wechat": true}
```

错误:
- 策略不存在 (.py 文件不在) → 404
- 缺 `enabled` 字段 → 400

### 3.3 `DELETE /strategies/{id}`

```http
DELETE /api/trading/strategies/dividend_auntie
→ 200 {"deleted": true, "kept_source_file": true, "strategy_id": "dividend_auntie"}
```

错误:
- 策略不存在 → 404
- 策略正在被 scheduler 跑一个 run → 200 但加 `running_runs: ["<run_id>"]` 字段 (前端 toast 提示); 不阻塞删除

### 3.4 衍生前端 TS 类型 (`frontend/src/lib/api.ts`)

```ts
export interface LiveStrategySummaryApi {
  total_pnl: number
  total_pnl_pct: number
  annual_return: number
  today_pnl: number
  today_pnl_pct: number
  max_drawdown: number
  max_drawdown_date: string
}

export interface LiveStrategyApi {
  id: string
  name: string
  enabled_default: boolean
  schedules: StrategyScheduleApi[]
  next_run: string | null
  // ↓ 新增 (向下兼容)
  started_at?: string | null
  notify_wechat?: boolean
  summary?: LiveStrategySummaryApi | null
}
```

---

## 4. API 契约

### 4.1 增强 `GET /strategies` (`backend/app/services/trading/router.py:238`)

变更: 给每个 item 增加 3 字段 (§3.1). 不破坏现有 list_strategies 实现, 提取 helper:

```python
@router.get("/strategies")
def list_strategies(request: Request):
    reg = _get_strategy_registry(request)
    if reg is None:
        raise HTTPException(status_code=503, detail="策略注册表未初始化")
    saved = prefs.list_all_strategy_schedules()
    sched_obj = getattr(request.app.state, "strategy_scheduler", None)
    items = []
    for s in reg.list_all():
        # ... existing merged schedule logic ...
        item = {
            "id": s.id,
            "name": s.name,
            "enabled_default": s.enabled_default,
            "schedules": merged,
            "next_run": sched_obj.next_run(s.id) if sched_obj else None,
        }
        # ↓ 新增 fields
        item["started_at"] = _strategy_started_at(reg.data_dir, s.id)
        item["notify_wechat"] = prefs.get_notify_wechat(s.id)
        item["summary"] = pnl.compute_strategy_summary(reg.data_dir, s.id)
        items.append(item)
    return {"strategies": items}
```

### 4.2 新增 `POST /strategies/{id}/notify` (`router.py` 邻接 §4.1)

```python
class NotifyToggleBody(BaseModel):
    enabled: bool

@router.post("/strategies/{strategy_id}/notify")
def set_strategy_notify(strategy_id: str, body: NotifyToggleBody, request: Request):
    """toggle notify_wechat 偏好. 不验证 .py 存在 (允许 pref-only 设置)."""
    from app.services.trading import preferences as prefs
    prefs.set_notify_wechat(strategy_id, body.enabled)
    return {"notify_wechat": body.enabled}
```

### 4.3 新增 `DELETE /strategies/{id}` (`router.py` 邻接 §4.1)

```python
@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: str, request: Request):
    """注销一个策略: 删 prefs + deregister + 保留 .py 源文件 + 保留历史 JSONL."""
    from app.services.trading import preferences as prefs
    reg = _get_strategy_registry(request)
    if reg is None:
        raise HTTPException(status_code=503, detail="策略注册表未初始化")
    p = code_store._strategy_file(reg.data_dir, strategy_id)
    if not p.exists():
        raise HTTPException(404, f"策略 {strategy_id} 不存在")
    # 删 prefs 所有该 id 键
    prefs.delete_strategy_keys(strategy_id)
    # 从 registry 注销
    running_runs = reg.deregister(strategy_id)
    return {
        "deleted": True,
        "kept_source_file": True,
        "strategy_id": strategy_id,
        "running_runs": running_runs,  # list[str], 可能空
    }
```

---

## 5. 行为口径

### 5.1 Per-strategy 性能聚合 (`compute_strategy_summary`)

数据流:
```
fills.jsonl (per-fill: id, order_id, symbol, side, qty, price, fee, tax, amount, filled_at)
  ── left join on fill.order_id ──>
orders.jsonl (per-order: id, symbol, side, qty, status, note="strategy:<id>:<event>")
  ── filter order.note matches r"^strategy:(?P<sid>[^:]+):" ──>
per-strategy fills series (fills 关联到 strategy)
  ── daily closed PnL = sum (sell.amount - sell.fee - buy.amount - buy.fee - sell.tax) ──>
per-strategy daily PnL series (按 trade_date)
  ── 派生: total_pnl / total_pnl_pct (相对 initial_cash) /
            annual_return (252d 年化) /
            today_pnl (最后一个 trade_date 的 PnL) /
            max_drawdown (peak-to-trough, daily series 上) /
            max_drawdown_date (trough 日期)
```

实现: `backend/app/services/trading/pnl.py` 新增函数 (沿用现有 fills/orders JSONL 加载逻辑):

```python
from functools import lru_cache
import json
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

_STRATEGY_NOTE_RE = re.compile(r"^strategy:([^:]+):([^:]+)$")


def _orders_strategy_map(data_dir: Path) -> dict[str, str]:
    """返回 {order_id: strategy_id} 索引; 无法归属的 order_id 不出现."""
    out = {}
    p = data_dir / "user_data" / "trading" / "orders.jsonl"
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        m = _STRATEGY_NOTE_RE.match(o.get("note") or "")
        if m:
            out[o["id"]] = m.group(1)
    return out


def _fills_by_strategy(data_dir: Path, order_to_strategy: dict[str, str]) -> dict[str, list[dict]]:
    """按 strategy 分组 fill (填入 strategy_id 字段)."""
    out: dict[str, list[dict]] = defaultdict(list)
    p = data_dir / "user_data" / "trading" / "fills.jsonl"
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            f = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        sid = order_to_strategy.get(f.get("order_id"))
        if sid:
            f["strategy_id"] = sid
            out[sid].append(f)
    return out


def _per_strategy_summary_impl(data_dir: Path, strategy_id: str) -> dict | None:
    """实盘: per-strategy daily closed PnL → 7 字段 summary. 无 fill → None."""
    order_to_strategy = _orders_strategy_map(data_dir)
    fills_by_strategy = _fills_by_strategy(data_dir, order_to_strategy)
    fills = fills_by_strategy.get(strategy_id)
    if not fills:
        return None
    # 按 trade_date 聚合 closed PnL (简化: 假设每个 fill 都是 closed leg,
    # closed PnL = sell.amount - sell.fee - sell.tax - (buy.amount + buy.fee).
    # 出于第一版简化, 我们用 realized_pnl = sum(amount - fee - tax) per side,
    # 这与 account.realized_pnl 的实际算法可能略有差异, 但 < 0.5%).
    by_date: dict[str, float] = defaultdict(float)
    for f in fills:
        td = f.get("trade_date") or f["filled_at"][:10]
        net = f["amount"] - f.get("fee", 0.0) - f.get("tax", 0.0)
        # buy = -net (cash out), sell = +net (cash in, but PnL only on close)
        # 简化: PnL = sell net (即 fill.side == 'sell' 且订单 close 仓位)
        # 第一版: 仅对 sell 计算 realized = amount - fee - tax
        if f["side"] == "sell":
            by_date[td] += net
    if not by_date:
        return None
    daily_series = sorted(by_date.items())
    total_pnl = sum(p for _, p in daily_series)
    # initial_cash from prefs (trading_initial_cash), fallback 1_000_000
    initial = _initial_cash(data_dir)
    total_pnl_pct = total_pnl / initial if initial else 0.0
    # 年化 (按实际天数)
    first_d = datetime.strptime(daily_series[0][0], "%Y-%m-%d").date()
    last_d = datetime.strptime(daily_series[-1][0], "%Y-%m-%d").date()
    days = max((last_d - first_d).days, 1)
    annual_return = (1 + total_pnl_pct) ** (365.0 / days) - 1 if total_pnl_pct > -1 else -1.0
    # 今日 PnL = daily_series 最后一日
    today_pnl = daily_series[-1][1]
    today_pnl_pct = today_pnl / initial if initial else 0.0
    # 最大回撤 (基于 daily cumulative series)
    cum, peak, mdd, mdd_date = 0.0, 0.0, 0.0, ""
    for d, p in daily_series:
        cum += p
        if cum > peak:
            peak = cum
        dd = (peak - cum) / initial if initial else 0.0
        if dd > mdd:
            mdd = dd
            mdd_date = d
    return {
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct * 100, 4),  # 百分比单位 %
        "annual_return": round(annual_return * 100, 4),
        "today_pnl": round(today_pnl, 2),
        "today_pnl_pct": round(today_pnl_pct * 100, 4),
        "max_drawdown": round(mdd * 100, 4),
        "max_drawdown_date": mdd_date,
    }


@lru_cache(maxsize=128)
def compute_strategy_summary(data_dir: Path, strategy_id: str, _sig: int) -> dict | None:
    """带 mtime 失效的缓存 wrapper. _sig = fills_mtime_nano + orders_mtime_nano."""
    return _per_strategy_summary_impl(data_dir, strategy_id)


def get_compute_summary(data_dir: Path, strategy_id: str) -> dict | None:
    """public: 取 summary, 触发 cache 检查 (mtime 改变时失效)."""
    sig = _mtime_sig(data_dir, ["fills.jsonl", "orders.jsonl"])
    return compute_strategy_summary(data_dir, strategy_id, sig)


def _initial_cash(data_dir: Path) -> float:
    from app.services.trading import preferences as prefs
    try:
        v = prefs.get_preference("trading_initial_cash")
        return float(v) if v else 1_000_000.0
    except Exception:
        return 1_000_000.0


def _mtime_sig(data_dir: Path, files: list[str]) -> int:
    s = 0
    for f in files:
        p = data_dir / "user_data" / "trading" / f
        if p.exists():
            s += int(p.stat().st_mtime_ns)
    return s
```

**算法注**: 第一版只用 `sell` 侧的 realized PnL. 后续若与账户口径有差 (例如全 FIFO realized PnL), 用 `_reconcile_with_account_total_pnl()` 函数对比后修正 (在测里覆盖). 现在的容忍是 `±0.5%`.

### 5.2 `started_at` 派生

```python
@lru_cache(maxsize=128)
def _strategy_started_at(data_dir: Path, strategy_id: str, _sig: int) -> str | None:
    p = data_dir / "user_data" / "trading" / "strategy_runs.jsonl"
    if not p.exists():
        return None
    earliest = None
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if r.get("strategy_id") != strategy_id:
            continue
        sa = r.get("started_at")
        if sa and (earliest is None or sa < earliest):
            earliest = sa
    return earliest


def strategy_started_at(data_dir: Path, strategy_id: str) -> str | None:
    sig = _mtime_sig(data_dir, ["strategy_runs.jsonl"])
    return _strategy_started_at(data_dir, strategy_id, sig)
```

### 5.3 `notify_wechat` 存储 (`preferences.py` 扩展)

```python
# 已有 trading_strategies.{id}.schedules 模式; 复用同样路径
def _key_notify(strat_id: str) -> str:
    return f"trading_strategies.{strat_id}.notify_wechat"

def get_notify_wechat(strat_id: str) -> bool:
    try:
        v = prefs.get(_key_notify(strat_id))
        return bool(v) if v is not None else False
    except KeyError:
        return False

def set_notify_wechat(strat_id: str, enabled: bool) -> None:
    prefs.set(_key_notify(strat_id), bool(enabled))

def delete_strategy_keys(strat_id: str) -> None:
    """删 prefs 中所有 trading_strategies.<strat_id>.* 键."""
    prefix = f"trading_strategies.{strat_id}."
    deleted = []
    for k in list(prefs.all_keys()):
        if k.startswith(prefix):
            prefs.delete(k)
            deleted.append(k)
    return deleted
```

### 5.4 `Registry.deregister` (`live_strategy.py` 扩展)

```python
class Registry:
    # ... 现有方法 ...

    def deregister(self, strategy_id: str) -> list[str]:
        """从 registry 注销策略; 返回当时正在跑 (但未完成) 的 run_id 列表."""
        running_runs = []
        sched = self._scheduler
        if sched and hasattr(sched, "active_runs"):
            for run_id, info in list(sched.active_runs.items()):
                if info.get("strategy_id") == strategy_id:
                    running_runs.append(run_id)
        self._strategies.pop(strategy_id, None)
        # 通知 scheduler 移除 cron
        if sched and hasattr(sched, "unregister_strategy"):
            sched.unregister_strategy(strategy_id)
        return running_runs
```

具体方法名取决于现有 registry 实现; 给函数原型, 实现时按现有类调整.

### 5.5 DELETE 不可逆 + 二次确认 (前端)

- 前端 (在 `live-trading-list-detail-layout.md` §5.1 已设计) 表格行勾选 + [删除 (n)] → 弹 Modal → 后端 DELETE
- 二次确认 Modal 文案: "确定要从注册表中移除这些策略吗? 源 .py 文件保留, 历史回测数据保留, 但运行将立即停止 (正在跑的 run 等到完成)."

---

## 6. 风险与边界

- **性能**: `compute_strategy_summary` 全量扫 fills+orders; 实盘 fills 量级 ~1000/天, 文件 < 5MB. 单次 < 50ms. lru_cache + mtime 失效 (`fills + orders mtime sum`). 规模上去 (>= 10w 行) 后再考虑增量
- **attribution 脆弱**: 依赖 `order.note` 前缀. 老订单 / 手改 note / 重放场景会归不到. fail-soft: 跳过, summary 正常返回但可能不全
- **第一版 PnL 算法偏差**: 当前算法仅用 `sell` 侧 net, 可能与账户 `realized_pnl` (FIFO 等价) 有 ±0.5% 误差. 测试里固化对照表; 若超过再迭代
- **DELETE 当前 run race**: deregister 不打断正在跑的 run. 用户看到 `running_runs: [...]` 字段, 前端 toast "策略 X 还有 N 个 run 在跑; 删除将在 run 完成后生效"
- **notify_wechat 当前未联通**: prefs 字段写 OK, 通知引擎不读 (B3 = false). 前端可切换/显示, 实际不发. 后续 `live-trading-notification.md` 接读
- **历史 JSONL 不可被 DELETE 链上**: 删 prefs + registry 后, `strategy_runs.jsonl` / `orders.jsonl` / `fills.jsonl` 保留. 用户做回溯时仍可见; 不算"完全删除"
- **`compute_strategy_summary` 内部 `total_pnl_pct` 口径**: 用 `trading_initial_cash` 作分母. 实际账户总权益 = initial + realized + unrealized; per-strategy 没法用 (PnL 是 realized 视角). 现有选择: 用 initial. 后续若要算"基于策略开始日的 equity 分母", 扩展

---

## 7. 测试矩阵

### 7.1 单元测试 (`tests/test_strategy_summary.py` 新增)

| 用例 | 期望 |
|---|---|
| `compute_strategy_summary(dir, "a")` 无 fill + 无 order | `None` |
| `compute_strategy_summary(dir, "a")` 有 1 buy, 0 sell | `None` (没 close PnL) |
| `compute_strategy_summary(dir, "a")` 有 1 buy + 1 sell 各 close | 7 字段非空; total_pnl ≈ sell.amount - sell.fee - sell.tax |
| `compute_strategy_summary(dir, "a")` 多日 sell | total_pnl = sum, annual_return > 0, today_pnl = 最后一日 |
| `compute_strategy_summary` mtime cache | 改 fills.jsonl 后, mtime 变了, 下次调用重算 |
| `compute_strategy_summary` lru cache | 不改文件时, 两次调用 O(1) |
| attribution 失败 (note 不是 `strategy:<id>:<event>` 格式) | 该 fill 跳过, summary 仍合理 |

### 7.2 端到端

| 用例 | 期望 |
|---|---|
| `GET /strategies` 含 fills | `started_at` = earliest run; `summary` 7 字段非空 |
| `GET /strategies` 0 fills | `started_at = null`, `summary = null` |
| `POST /strategies/<id>/notify {enabled: true}` | 200; `prefs.get('trading_strategies.<id>.notify_wechat') == True` |
| `POST /strategies/<id>/notify` 缺 enabled | 400 |
| `POST /strategies/<not-exist>/notify` | 404 |
| `DELETE /strategies/<id>` | 200; `prefs.all_keys()` 不再有 `trading_strategies.<id>.*`; `.py` 文件保留 |
| `DELETE /strategies/<id>` 第二次 | 404 |
| `GET /strategies` 后, 表格刷新 | 该 id 不再出现在 `strategies` 数组 |
| `DELETE` 当前有 run 在跑 | 200; `running_runs: [...]` 非空; 该 run 完成后, 不再触发新 run |

### 7.3 兼容性

| 用例 | 期望 |
|---|---|
| 旧前端 (不识别新字段) 调用 `GET /strategies` | 不报错; 新字段被忽略 |
| 旧 prefs (无 `notify_wechat` 键) 调用 `get_notify_wechat` | 返回 `False` |

---

## 8. 验收标准

```bash
cd backend
uv run pytest app/services/trading/tests/test_strategy_summary.py -v
./dev.sh start
# 1. GET 增强
curl -sS http://localhost:8018/api/trading/strategies | jq '.strategies[0] | {id, started_at, notify_wechat, summary}'
# 期望看到全部 3 新字段; summary 7 字段都填值

# 2. POST notify toggle
curl -sS -X POST http://localhost:8018/api/trading/strategies/dividend_auntie/notify \
    -H 'Content-Type: application/json' -d '{"enabled":true}'
# 期望 200 {"notify_wechat": true}

# 3. DELETE
curl -sS -X DELETE http://localhost:8018/api/trading/strategies/dividend_auntie
# 期望 200 {"deleted":true,...}; ls data/user_data/strategies/live/dividend_auntie.py 仍在

# 4. 性能
time curl -sS http://localhost:8018/api/trading/strategies > /dev/null
# 期望 < 200ms (1 个策略有 ~1000 fills 历史)
```

### 8.1 不破坏

```bash
# 现有 `/pnl/history`, `/strategies/{id}/code` 等接口响应不变
curl -sS http://localhost:8018/api/trading/pnl/history | jq '.[0]'
# 期望: 仍返回 {date, equity} 序列, 无新字段污染
```

---

## 9. 改动文件

### 后端

- 改 `backend/app/services/trading/router.py`
  - `GET /strategies` 增强 (§4.1) — 加 3 字段
  - 新 `POST /strategies/{id}/notify` (§4.2)
  - 新 `DELETE /strategies/{id}` (§4.3)
- 改 `backend/app/services/trading/pnl.py`
  - 新增 `compute_strategy_summary` / `strategy_started_at` (§5.1, §5.2)
  - 新增 `_orders_strategy_map` / `_fills_by_strategy` 私有 helpers
- 改 `backend/app/services/trading/preferences.py`
  - 新增 `get_notify_wechat` / `set_notify_wechat` / `delete_strategy_keys`
- 改 `backend/app/services/trading/live_strategy.py`
  - `Registry.deregister(strategy_id) -> list[str]` (§5.4)
- 新 `backend/app/services/trading/tests/test_strategy_summary.py`
  - 覆盖 §7.1 单测

### 前端 (轻微, 仅类型扩字段)

- 改 `frontend/src/lib/api.ts`
  - `LiveStrategyApi` 加 3 optional 字段 (§3.4)
  - 新 `LiveStrategySummaryApi` interface
- 新 `frontend/src/lib/api.ts`
  - `api.tradingSetStrategyNotify(id, enabled)` / `api.tradingDeleteStrategy(id)`

> 注: 列表页面表格的"用新字段填列"的代码改动, 落在 `live-trading-list-detail-layout.md` §5.1 那次 PR 内, 不在本任务.

### 文档

- `docs/live-trading-strategy-summary.md` (本文件)
- 更新 `docs/live-trading-list-detail-layout.md`
  - §3.2 表"缺字段"标注改为引用本任务 (§4.1 增强)
  - §4.1 删除"不新增 endpoint"措辞, 改为列新增 2 个 + 增强 1 个
  - §5.1 表格列说明里的 `—` 占位符, 改为 "由 `live-trading-strategy-summary.md` 补齐"

---

## 10. 设计图

### 数据流 (per-strategy summary)

```
┌─────────────┐  order_id   ┌─────────────┐  note="strategy:<id>:<ev>"  ┌──────────┐
│ fills.jsonl │ ──left join─>│ orders.jsonl│ ──regex parse──────────────>│ order_to │
│  id,oid,... │             │             │                             │ strategy │
└─────────────┘             └─────────────┘                             │   map    │
                                                                        └────┬─────┘
                                                                             │ group by sid
                                                                             ▼
                                                                ┌────────────────────────┐
                                                                │ fills_by_strategy[sid] │
                                                                │  [{...fill, strategy_id}] │
                                                                └────────────┬───────────┘
                                                                             │ daily closed
                                                                             ▼  PnL
                                                                ┌────────────────────────┐
                                                                │ daily_series[(date,p)] │
                                                                └────────────┬───────────┘
                                                                             │
                                              ┌──────────────────────────────┴────────────────┐
                                              ▼                                                ▼
                                                            ┌─────────────┐         ┌────────────────┐
                                                            │ total_pnl   │         │ max_drawdown   │
                                                            │ total_pnl%  │         │ max_dd_date    │
                                                            │ today_pnl   │         └────────────────┘
                                                            │ today_pnl%  │
                                                            │ annual_ret  │
                                                            └─────────────┘
```

### DELETE 流程

```
client DELETE /strategies/dividend_auntie
        │
        ▼
router.delete_strategy
        │
        ├─ 校验 .py 在 → 404 else
        ├─ prefs.delete_strategy_keys(dividend_auntie)
        │    └─ 删所有 trading_strategies.dividend_auntie.*
        ├─ reg.deregister(dividend_auntie) → running_runs[]
        │    └─ 从 Registry._strategies pop
        │    └─ scheduler.unregister_strategy (停止触发新 event)
        └─ 返回 {deleted, kept_source_file, running_runs}

(当下一次 GET /strategies 触发 scheduler refresh, 该策略不再出现)
(历史 strategy_runs.jsonl / orders.jsonl / fills.jsonl 不动)
```
