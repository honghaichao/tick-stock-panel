/**
 * F1-2 (2026-10-13): 回测页 → 实盘页 bridge 按钮.
 *
 * 用法: 嵌入 verify 报告卡顶部, 点击 → 跳 /live-trading/<id>?sync_run_id=<run_id>
 * 仅当 strategy_id 是已注册的 LiveStrategy 时显示 (否则提示 "晋级到实盘").
 */
import { Rocket } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export function SyncToLiveButton({
  strategyId,
  verifyRunId,
  size = 'sm',
}: {
  strategyId: string
  verifyRunId: string
  size?: 'sm' | 'md'
}) {
  const navigate = useNavigate()

  // 检查该 strategy 是否已注册为 LiveStrategy
  const { data: liveStrategies } = useQuery({
    queryKey: ['trading', 'strategies'] as const,
    queryFn: () => api.tradingStrategies(),
    enabled: !!strategyId,
  })

  const isLive = liveStrategies?.strategies?.some((s) => s.id === strategyId) ?? false
  const targetUrl = isLive
    ? `/live-trading/${encodeURIComponent(strategyId)}?sync_run_id=${encodeURIComponent(verifyRunId)}`
    : `/live-trading?promote_run_id=${encodeURIComponent(verifyRunId)}&promote_strategy=${encodeURIComponent(strategyId)}`

  const padding = size === 'md' ? 'px-3 py-1.5' : 'px-2 py-1'
  const textClass = size === 'md' ? 'text-xs' : 'text-[10px]'
  const iconSize = size === 'md' ? 'h-3.5 w-3.5' : 'h-3 w-3'

  return (
    <button
      type="button"
      onClick={() => navigate(targetUrl)}
      title={isLive ? '跳到实盘页 + 预填 sync_run_id' : '跳到实盘页 + 提示晋级到实盘'}
      className={`inline-flex items-center gap-1 rounded-btn border border-accent/30 bg-accent/10 ${padding} ${textClass} font-medium text-accent hover:bg-accent/20`}
    >
      <Rocket className={iconSize} />
      {isLive ? '应用到实盘' : '晋级 + 应用到实盘'}
    </button>
  )
}
