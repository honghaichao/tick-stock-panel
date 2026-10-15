# fix-remaining-test-fails — 修 2 个剩余测试 fail (matrix + rps_rotation)

> 状态: 待 review
> 范围: 修 2 个 pre-existing 测试 fail
>   A. rps_rotation `_latest_enriched_date` dead code, 矩阵右端没延伸到 today
>   B. matrix `limit_up_locked` 数据不一致 (待 root-cause 分析)
> 不在范围: 修其它测试 fail (4 个 vitest localStorage fail 是环境问题, 留其它 task)

---

## 1. 目标

修上次 Task 3 完成后剩 2 个 fail:
- `test_rps_rotation_intraday.py::test_live_date_newer_than_history_extends_matrix_to_today`
- `backtest/test_matrix_strategy.py::test_direct_parquet_matrix_matches_panel_builder_and_reuses_mmap`

成功标准:
- 2 个 fail → 0 fail
- 现有 trading + backtest + parity 测试不破

## 2. 范围

### 2.1 Fail A — rps_rotation 矩阵右端延伸到 today

- **位置**: `backend/app/services/rps_rotation.py:50-58` `_latest_enriched_date`
- **症状**:
  ```python
  def _latest_enriched_date(repo) -> date | None:
      cache = repo._enriched_history_cache
      if cache is None or cache.is_empty() or "date" not in cache.columns:
          return None
      return cache["date"].max()    # <-- 早 return

      latest = cache["date"].max()    # dead code
      live_date = getattr(repo, "_enriched_cache_date", None)
      if live_date is not None and live_date > latest:
          return live_date
      return latest
  ```
- **根因**: `return cache["date"].max()` 早 return, 下面 `live_date > latest` 检查永远不到. 测试场景:
  - history 缓存截止 `today - 1` (2026-09-23)
  - live 缓存是 `today` (2026-09-24)
  - 期望 `_latest_enriched_date` 返回 2026-09-24, 实际返回 2026-09-23
- **修法**: 删早 return 的 `return cache["date"].max()`, 改成跟下面的 live_date 比较逻辑. 共改 1 行.

### 2.2 Fail B — matrix limit_up_locked 数据不一致

- **位置**: `tests/backtest/test_matrix_strategy.py:455` `test_direct_parquet_matrix_matches_panel_builder_and_reuses_mmap`
- **症状**:
  ```
  limit_up_locked: actual [[0,0,0],[0,0,0],[0,0,0]]
                  desired [[0,1,0],[0,0,0],[0,0,0]]
  Mismatched element [0, 1]: 0 (ACTUAL), 1 (DESIRED)
  ```
- **测试意图**: `build_market_data_matrix` 跟 `load_market_data_matrix_from_parquet` 在 `limit_up_locked` 字段上输出一致.
- **根因**: 待分析. 可能是 compute_limit_signals → load_market_data_matrix_from_parquet 链路 limit_up 字段没正确映射, 或是 mmap 缓存跟 build 的口径不一致.
- **修法**: 1) 先跑测试定位 root cause 2) 修代码或修测试 fixture 3) 不破其它测试

### 2.3 Out

- ❌ vitest 4 个 fail (DimensionMembersDialog / neighborPrefetch / updateCheck / DefaultStrategyParamsDialog) — pre-existing `localStorage.clear()` 环境问题, 跟 live trading 无关, 留其它 task
- ❌ rps_rotation 性能优化 (当前 `_change_pct_range` 每次合并 history + live, 不缓存)
- ❌ matrix data 架构调整 (不动 backend/app/backtest/matrix/* 主体)

## 3. 数据契约

无变化。修 dead code + 修一处数据对齐, 不改 API/JSONL schema.

## 4. API 契约

无变化。

## 5. 行为口径

- **Fail A 修后**:
  - 历史缓存 (历史截止 today-1) + live 缓存 (today) 场景
  - `_latest_enriched_date` 返回 max(history.date.max(), live_cache_date)
  - 矩阵右端延伸到 live_cache_date
- **Fail B 修后**:
  - `build_market_data_matrix` 跟 `load_market_data_matrix_from_parquet` 在 9 个字段上一致
  - 特别 limit_up_locked / limit_down_locked / tradable 这 3 个跟涨跌停/可交易状态相关的字段

## 6. 风险与边界

- **Fail A 风险极低**: 删 1 行 dead code, 启用 live_date 检查. 现有 `test_generation_mismatch_still_returns_matrix_with_live_today` 测试期望 matrix 含 live today 数据, 修后行为跟期望一致.
- **Fail B 风险中**: 修数据对齐可能影响生产实盘矩阵数据. 范围只在 limit_up_locked / limit_down_locked / tradable 字段, 其它字段不破即 OK.
- **不破其它测试**: 跑 `pytest tests/test_rps_rotation_intraday.py tests/backtest/test_matrix_strategy.py tests/test_trading_*.py` 全绿为标准.

## 7. 测试矩阵

| 维度 | 命令 | 通过条件 |
|---|---|---|
| 1. Fail A 修后 | `pytest tests/test_rps_rotation_intraday.py -v` | 5/5 passed (含 test_live_date_newer_than_history_extends_matrix_to_today) |
| 2. Fail B 修后 | `pytest tests/backtest/test_matrix_strategy.py -v` | 26/26 passed (含 test_direct_parquet_matrix_matches_panel_builder_and_reuses_mmap) |
| 3. trading + parity 不破 | `pytest tests/test_trading_*.py tests/test_live_*.py` | ≥ 79 passed |
| 4. 矩阵数据 root cause | pytest -v verbose traceback | 找到 limit_up_locked 错位的具体行号 |

## 8. 验收标准

1. 2 fail → 0 fail
2. 现有 79+ trading + parity 测试不破
3. Fail B 修复不引入新的 fail
4. 0 new ruff error

## 9. 不在本轮 (留 follow-up)

- vitest 4 个 localStorage fail → 独立 task (vitest 环境配置)
- matrix 性能优化 → 留优化 task
