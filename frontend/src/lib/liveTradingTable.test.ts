/**
 * 实盘列表页表格的纯函数测试 — 与 `liveTrading.split.test.ts` / `liveTrading.expand.test.ts` 平级,
 * 关联 docs/live-trading-list-detail-layout.md §5.1 / §7.1.
 */
import { describe, expect, it } from 'vitest'

import type { LiveStrategyApi, StrategyScheduleApi } from './api'

import {
  filterStrategiesByTab,
  strategyTabCounts,
  selectDefaultStrategyId,
  summarizeFrequency,
  deriveRunDays,
} from './liveTradingTable'

function makeSchedules(enabledFlags: Array<[number, number, boolean]>): StrategyScheduleApi[] {
  return enabledFlags.map(([hour, minute, enabled], i) => ({
    name: `s${i}`,
    hour,
    minute,
    enabled,
  }))
}

function makeStrategy(id: string, schedules: StrategyScheduleApi[]): LiveStrategyApi {
  return {
    id,
    name: id,
    enabled_default: false,
    schedules,
    next_run: null,
    // optional fields, PR-2 阶段为 undefined
    started_at: undefined,
    notify_wechat: undefined,
    summary: undefined,
  }
}

describe('filterStrategiesByTab (实盘列表 tabs 过滤)', () => {
  it('空输入 → 空', () => {
    expect(filterStrategiesByTab([], 'all')).toEqual([])
    expect(filterStrategiesByTab([], 'active')).toEqual([])
    expect(filterStrategiesByTab([], 'stopped')).toEqual([])
  })

  it('全部 active → active tab 全留, stopped tab 空, all tab 全留', () => {
    const list = [
      makeStrategy('a', makeSchedules([[9, 30, true]])),
      makeStrategy('b', makeSchedules([[10, 0, true], [14, 0, false]])),
    ]
    expect(filterStrategiesByTab(list, 'all').map(s => s.id)).toEqual(['a', 'b'])
    expect(filterStrategiesByTab(list, 'active').map(s => s.id)).toEqual(['a', 'b'])
    expect(filterStrategiesByTab(list, 'stopped')).toEqual([])
  })

  it('全部停用 → stopped tab 全留, active tab 空', () => {
    const list = [
      makeStrategy('a', makeSchedules([[9, 30, false]])),
      makeStrategy('b', makeSchedules([[10, 0, false], [14, 0, false]])),
    ]
    expect(filterStrategiesByTab(list, 'stopped').map(s => s.id)).toEqual(['a', 'b'])
    expect(filterStrategiesByTab(list, 'active')).toEqual([])
  })

  it('混合 → 分流正确', () => {
    const list = [
      makeStrategy('a', makeSchedules([[9, 30, true]])),    // active
      makeStrategy('b', makeSchedules([[10, 0, false]])),   // stopped
      makeStrategy('c', makeSchedules([])),                  // 占位 = active
    ]
    expect(filterStrategiesByTab(list, 'active').map(s => s.id)).toEqual(['a', 'c'])
    expect(filterStrategiesByTab(list, 'stopped').map(s => s.id)).toEqual(['b'])
  })

  it('0 schedules 占位策略归入 active (与 splitStrategiesByStatus 一致)', () => {
    const placeholder = makeStrategy('p', makeSchedules([]))
    expect(filterStrategiesByTab([placeholder], 'active')).toEqual([placeholder])
    expect(filterStrategiesByTab([placeholder], 'stopped')).toEqual([])
  })
})

describe('strategyTabCounts (tabs 角标)', () => {
  it('空输入 → 全 0', () => {
    expect(strategyTabCounts([])).toEqual({ all: 0, active: 0, stopped: 0 })
  })

  it('混合计数', () => {
    const list = [
      makeStrategy('a', makeSchedules([[9, 30, true]])),     // active
      makeStrategy('b', makeSchedules([[10, 0, false]])),    // stopped
      makeStrategy('c', makeSchedules([])),                  // placeholder = active
      makeStrategy('d', makeSchedules([[9, 30, false], [10, 0, false]])), // stopped
    ]
    expect(strategyTabCounts(list)).toEqual({ all: 4, active: 2, stopped: 2 })
  })
})

describe('selectDefaultStrategyId (D4 默认 redirect 目标)', () => {
  it('空输入 → undefined', () => {
    expect(selectDefaultStrategyId([])).toBeUndefined()
  })

  it('首个 active (有 enabled schedule)', () => {
    const list = [
      makeStrategy('a', makeSchedules([[9, 30, false]])),     // stopped
      makeStrategy('b', makeSchedules([[10, 0, true]])),     // active
      makeStrategy('c', makeSchedules([])),                  // placeholder active
    ]
    expect(selectDefaultStrategyId(list)).toBe('b')
  })

  it('无任何 active 时 fallback 列表第一项', () => {
    const list = [
      makeStrategy('a', makeSchedules([[9, 30, false]])),     // stopped
      makeStrategy('b', makeSchedules([[10, 0, false]])),     // stopped
    ]
    expect(selectDefaultStrategyId(list)).toBe('a')
  })

  it('placeholder (0 schedules) 算 active', () => {
    const list = [
      makeStrategy('a', makeSchedules([[9, 30, false]])),     // stopped
      makeStrategy('p', makeSchedules([])),                  // placeholder
    ]
    expect(selectDefaultStrategyId(list)).toBe('p')
  })

  it('稳定: 第一个 active 是 schedules 启用中, 而不是 any enabled', () => {
    const list = [
      makeStrategy('a', makeSchedules([[9, 30, true], [10, 0, false]])),  // active
      makeStrategy('b', makeSchedules([[11, 0, true]])),                // active
    ]
    expect(selectDefaultStrategyId(list)).toBe('a')
  })
})

describe('summarizeFrequency (频率列派生)', () => {
  it('空 schedules → "—"', () => {
    expect(summarizeFrequency([])).toBe('—')
  })

  it('全部 disabled → "—"', () => {
    expect(summarizeFrequency(makeSchedules([[9, 30, false]]))).toBe('—')
  })

  it('单 enabled → "每日 @ HH:MM"', () => {
    expect(summarizeFrequency(makeSchedules([[9, 5, true]]))).toBe('每日 @ 09:05')
  })

  it('多个 enabled → " / " 连接', () => {
    expect(
      summarizeFrequency(makeSchedules([[9, 30, true], [14, 0, true]])),
    ).toBe('每日 @ 09:30 / 每日 @ 14:00')
  })

  it('enabled 与 disabled 混合 → 只列 enabled', () => {
    expect(
      summarizeFrequency(makeSchedules([[9, 30, true], [14, 0, false]])),
    ).toBe('每日 @ 09:30')
  })
})

describe('deriveRunDays (时限列派生, 基于 started_at)', () => {
  const now = new Date('2026-10-12T12:00:00+08:00')

  it('started_at null/undefined → null', () => {
    expect(deriveRunDays(null, now)).toBeNull()
    expect(deriveRunDays(undefined, now)).toBeNull()
  })

  it('started_at 解析失败 → null', () => {
    expect(deriveRunDays('not-a-date', now)).toBeNull()
  })

  it('started_at 7 天前 → 7 天', () => {
    expect(
      deriveRunDays('2026-10-05T12:00:00+08:00', now),
    ).toBe(7)
  })

  it('started_at 未来时间 → 0', () => {
    expect(deriveRunDays('2026-10-15T12:00:00+08:00', now)).toBe(0)
  })
})
