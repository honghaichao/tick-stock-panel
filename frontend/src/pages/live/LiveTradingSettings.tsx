/**
 * 实盘页 — Settings tab.
 * PR1 已把 code/settings 收到 Sidebar 右上"策略配置 ▼"抽屉, 主侧边只剩 3 tab
 * (overview/stats/logs). 此处保留 file 以便 PR4 重新路由或扩展.
 */
import { StrategySettings } from '@/components/live/StrategySettings'

export function LiveTradingSettings({ strategyId }: { strategyId: string }) {
  return <StrategySettings strategyId={strategyId} />
}
