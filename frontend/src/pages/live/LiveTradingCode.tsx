/**
 * 实盘页 — Code tab.
 * PR1 已把 code/settings 收到 Sidebar 右上"策略配置 ▼"抽屉, 主侧边只剩 3 tab
 * (overview/stats/logs). 此处保留 file 以便 PR4 重新路由或扩展.
 */
import { CodeView } from '@/components/live/CodeView'

export function LiveTradingCode({ strategyId }: { strategyId: string }) {
  return <CodeView strategyId={strategyId} />
}
