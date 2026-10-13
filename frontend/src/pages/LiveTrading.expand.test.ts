import { describe, expect, it } from 'vitest'
import { strategyIsExpanded } from './LiveTrading'
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

describe('strategyIsExpanded (实盘策略展开/收敛)', () => {
  it('停用策略 (全部 schedule.enabled=false) 未触过用户时默认收敛', () => {
    expect(strategyIsExpanded(makeStrategy('a', [false, false]), {})).toBe(false)
  })
  it('运行中策略 (>=1 schedule.enabled=true) 未触过用户时默认展开', () => {
    expect(strategyIsExpanded(makeStrategy('b', [true, false]), {})).toBe(true)
  })
  it('user 显式展开停用策略 → 展开 (覆盖默认)', () => {
    expect(strategyIsExpanded(makeStrategy('a', [false]), { a: true })).toBe(true)
  })
  it('user 显式收敛运行中策略 → 收敛 (覆盖默认)', () => {
    expect(strategyIsExpanded(makeStrategy('d', [true]), { d: false })).toBe(false)
  })
  it('0 schedules 的策略默认收敛', () => {
    expect(strategyIsExpanded(makeStrategy('e', []), {})).toBe(false)
  })
  it('user 偏好按 strategy id 隔离 (改 a 不影响 b)', () => {
    const userExp = { a: false }
    expect(strategyIsExpanded(makeStrategy('a', [true]), userExp)).toBe(false)
    expect(strategyIsExpanded(makeStrategy('b', [false]), userExp)).toBe(false)
  })
  it('user 偏好持久化: 后续没 userExpanded 命中, 默认恢复', () => {
    // 模拟后端 enabledCount 从 1 变 0 的情况: 之前 user 显式展开过, 不应自动收敛
    const userExp = { a: true }
    const s = makeStrategy('a', [false]) // 现在 enabled 全 false
    expect(strategyIsExpanded(s, userExp)).toBe(true) // 仍是 true (user 偏好保留)
    // 清理 userExp 模拟下次刷新后没记录:
    expect(strategyIsExpanded(s, {})).toBe(false) // 默认收敛
  })
})
