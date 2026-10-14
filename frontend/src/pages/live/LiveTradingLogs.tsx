/**
 * 实盘页 — 日志 tab.
 */
import { LogsList } from './LiveTradingOverview'

export function LiveTradingLogs({ strategyId }: { strategyId?: string | null } = {}) {
  return (
    <div className="space-y-4">
      <LogsList strategyId={strategyId} />
    </div>
  )
}
