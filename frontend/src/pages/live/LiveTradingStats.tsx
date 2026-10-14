/**
 * 实盘页 — 统计 tab.
 * per-strategy 视图: 详情页只看本策略的 PnL/Risk. 账户级 (globalBadge) 视图已迁到 LiveTradingList header.
 */
import { RiskStats } from '@/components/live/RiskStats'
import { PnLPanel } from './LiveTradingOverview'

export function LiveTradingStats({ strategyId }: { strategyId?: string | null } = {}) {
  return (
    <div className="space-y-4">
      <RiskStats strategyId={strategyId} />
      <PnLPanel strategyId={strategyId} />
    </div>
  )
}
