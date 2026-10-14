/**
 * 实盘列表页 (策略表格) 的纯函数, 与 `splitStrategiesByStatus` / `strategyIsExpanded` 同层.
 * 提为模块级便于独立测试.
 *
 * 关联: docs/live-trading-list-detail-layout.md §3.3 / §5.1
 */
import type { LiveStrategyApi, StrategyScheduleApi } from './api'

// ── Tabs 过滤 ──
export type StrategyTab = 'all' | 'active' | 'stopped'

const pad2 = (n: number): string => (n < 10 ? `0${n}` : `${n}`)

/** 把 enabled schedules 聚合为简短频率描述. 多个 enabled 用 ' / ' 连接. */
export function summarizeFrequency(schedules: StrategyScheduleApi[]): string {
  const enabled = schedules.filter(s => s.enabled)
  if (enabled.length === 0) return '—'
  return enabled.map(s => `每日 @ ${pad2(s.hour)}:${pad2(s.minute)}`).join(' / ')
}

/** 派生 tab 状态: 'active' = 至少 1 个 schedule 启用; 'stopped' = 全部 disabled; 0 schedules 视为 active 占位. */
function derivedTab(s: LiveStrategyApi): Exclude<StrategyTab, 'all'> {
  if (s.schedules.length === 0) return 'active'
  const anyEnabled = s.schedules.some(sc => sc.enabled)
  return anyEnabled ? 'active' : 'stopped'
}

export function filterStrategiesByTab(
  list: LiveStrategyApi[],
  tab: StrategyTab,
): LiveStrategyApi[] {
  if (tab === 'all') return list
  const want = tab === 'active' ? 'active' : 'stopped'
  return list.filter(s => derivedTab(s) === want)
}

export interface StrategyTabCounts {
  all: number
  active: number
  stopped: number
}

export function strategyTabCounts(list: LiveStrategyApi[]): StrategyTabCounts {
  let active = 0, stopped = 0
  for (const s of list) {
    if (derivedTab(s) === 'active') active += 1
    else stopped += 1
  }
  return { all: list.length, active, stopped }
}

// ── 默认选中的策略 (D4 redirect 用) ──
/**
 * 返回 "首个启用的策略" (active=有 enabled schedule, 或 0 schedules 占位).
 * 若全部 stop, 则返回 strategies[0] (作为最后兜底).
 * 空数组返回 undefined.
 */
export function selectDefaultStrategyId(list: LiveStrategyApi[]): string | undefined {
  const fallback = list[0]?.id
  return (
    list.find(s => derivedTab(s) === 'active')?.id
    ?? fallback
  )
}

// ── 派生列用: 时限天数 (基于 started_at; PR-1 后端填值) ──
export function deriveRunDays(startedAt: string | null | undefined, now: Date = new Date()): number | null {
  if (!startedAt) return null
  const t = Date.parse(startedAt)
  if (Number.isNaN(t)) return null
  const diff = now.getTime() - t
  if (diff < 0) return 0
  return Math.floor(diff / 86_400_000)
}



/**
 * 追加 (PR2 拆分): 从原 LiveTrading.tsx 55-86 行迁出.
 * re-export 路径不变, ./LiveTrading 仍可导入 (供 expand.test / split.test).
 */
export function strategyIsExpanded(
  s: LiveStrategyApi,
  userExpanded: Record<string, boolean>,
): boolean {
  // 用户对每个 strategy 展开/收起的偏好。Map 内未出现的 strategy,
  // 默认按"是否有任一 schedule 启用"展开 (active=展开, stopped=收敛)。
  // 单独提为模块级纯函数便于测试。
  if (s.id in userExpanded) return userExpanded[s.id]
  return s.schedules.some(sc => sc.enabled)
}

/**
 * 把实盘策略列表拆成 "启用中 / 已停用" 两组:
 *  - 已停用 = 声明了至少 1 个 schedule, 但全部 disabled (与 StrategyRow.fullyStopped 同义);
 *  - 其他 (含 0 schedules 的占位策略) 都归入启用中, 由 UI 自行提示。
 * 提为模块级纯函数便于测试。
 */
export function splitStrategiesByStatus(strategies: LiveStrategyApi[]): {
  active: LiveStrategyApi[]
  stopped: LiveStrategyApi[]
} {
  const active: LiveStrategyApi[] = []
  const stopped: LiveStrategyApi[] = []
  for (const s of strategies) {
    const enabledCount = s.schedules.filter(sc => sc.enabled).length
    if (s.schedules.length > 0 && enabledCount === 0) stopped.push(s)
    else active.push(s)
  }
  return { active, stopped }
}

// ── 小工具 ──────────────────────────────────────────
