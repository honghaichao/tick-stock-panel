/**
 * PageHeader 右侧 — 当前选中策略名/id/状态 pill (PR3 策略名展示只动这里).
 */
import { useQuery } from '@tanstack/react-query'
import { QK } from '@/lib/queryKeys'
import { api } from '@/lib/api'

export function CurrentStrategyPill({ strategyId }: { strategyId: string }) {
  const q = useQuery({ queryKey: QK.tradingStrategies, queryFn: api.tradingStrategies })
  const s = q.data?.strategies.find(x => x.id === strategyId)
  if (!s) return null
  const enabledCount = s.schedules.filter(sc => sc.enabled).length
  const status = s.schedules.length === 0
    ? '占位'
    : enabledCount > 0 ? '运行中' : '已停用'
  return (
    <div className="flex items-center gap-2 text-xs text-muted">
      <span className="font-medium text-foreground">{s.name}</span>
      <code className="rounded bg-elevated/40 px-1.5 py-0.5">{s.id}</code>
      <span className="rounded border border-border bg-surface/30 px-1.5 py-0.5 text-[10px]">{status}</span>
    </div>
  )
}
