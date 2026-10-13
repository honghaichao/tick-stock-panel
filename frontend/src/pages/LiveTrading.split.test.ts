import { describe, expect, it } from 'vitest'
import { splitStrategiesByStatus } from './LiveTrading'
import type { LiveStrategyApi } from '@/lib/api'

function makeStrategy(id: string, enabledFlags: boolean[]): LiveStrategyApi {
  return {
    id,
    name: id,
    enabled_default: false,
    schedules: enabledFlags.map(enabled => ({
      name: 's',
      hour: 9,
      minute: 30,
      enabled,
    })),
    next_run: null,
  }
}

describe('splitStrategiesByStatus (实盘策略拆分: 启用中 vs 已停用)', () => {
  it('空输入 → 两组都为空', () => {
    const { active, stopped } = splitStrategiesByStatus([])
    expect(active).toEqual([])
    expect(stopped).toEqual([])
  })

  it('全部启用 → 全部进 active', () => {
    const list = [makeStrategy('a', [true]), makeStrategy('b', [true, false])]
    const { active, stopped } = splitStrategiesByStatus(list)
    expect(active.map(s => s.id)).toEqual(['a', 'b'])
    expect(stopped).toEqual([])
  })

  it('全部停用 → 全部进 stopped', () => {
    const list = [makeStrategy('a', [false]), makeStrategy('b', [false, false])]
    const { active, stopped } = splitStrategiesByStatus(list)
    expect(active).toEqual([])
    expect(stopped.map(s => s.id)).toEqual(['a', 'b'])
  })

  it('混合 → 启用中留在主区, 已停用进"更多"', () => {
    const list = [
      makeStrategy('a', [true]),
      makeStrategy('b', [false, false]),
      makeStrategy('c', [false, true]),
      makeStrategy('d', [false]),
    ]
    const { active, stopped } = splitStrategiesByStatus(list)
    expect(active.map(s => s.id)).toEqual(['a', 'c'])
    expect(stopped.map(s => s.id)).toEqual(['b', 'd'])
  })

  it('0 schedules 的策略视为"未停用", 留在主区 (与 StrategyRow.fullyStopped 一致)', () => {
    const list = [makeStrategy('a', []), makeStrategy('b', [false])]
    const { active, stopped } = splitStrategiesByStatus(list)
    expect(active.map(s => s.id)).toEqual(['a'])
    expect(stopped.map(s => s.id)).toEqual(['b'])
  })

  it('不修改入参数组 (避免影响调用方 refetch 后的引用)', () => {
    const list = [makeStrategy('a', [true]), makeStrategy('b', [false])]
    const beforeIds = list.map(s => s.id)
    splitStrategiesByStatus(list)
    expect(list.map(s => s.id)).toEqual(beforeIds)
  })

  it('顺序保持: active / stopped 各自按入参顺序', () => {
    const list = [
      makeStrategy('b', [false]),
      makeStrategy('a', [true]),
      makeStrategy('d', [false]),
      makeStrategy('c', [true]),
    ]
    const { active, stopped } = splitStrategiesByStatus(list)
    expect(active.map(s => s.id)).toEqual(['a', 'c'])
    expect(stopped.map(s => s.id)).toEqual(['b', 'd'])
  })
})
