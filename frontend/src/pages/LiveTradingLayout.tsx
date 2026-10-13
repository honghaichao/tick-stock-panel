/**
 * 实盘首页的路由层 Layout:
 *  - D4: /live-trading (无 strategyId) 自动 redirect 到首个启用的策略.
 *  - /live-trading/:strategyId 渲染详情 (5 tab) 视图.
 *  - 加载失败 / 0 策略 → 仍渲染列表页 (不强制 redirect).
 *
 * 关联: docs/live-trading-list-detail-layout.md §5.3.
 */
import { useEffect } from 'react'
import { Navigate, useLocation, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { selectDefaultStrategyId } from '@/lib/liveTradingTable'

import { LiveTrading } from './LiveTrading'
import { LiveTradingList } from './LiveTradingList'

export function LiveTradingLayout() {
  const params = useParams<{ strategyId?: string }>()
  const strategyId = params.strategyId

  // 详情页优先拿: 只有 strategyId 存在时才走 redirect 守卫.
  if (strategyId) {
    return <LiveTrading strategyId={strategyId} />
  }
  return <RootRedirectGate />
}

/** /live-trading (无 strategyId) 的守卫: 加载后 redirect 到首个启用的策略.
 *
 * Bypass: 加 ?view=list query 时跳过 redirect, 稳定显示列表页.
 * 用于 Sidebar "← 返回策略列表" 链接, 用户期望看到一个真正的列表, 不被弹回 detail.
 */
function RootRedirectGate() {
  const strategiesQ = useQuery({
    queryKey: QK.tradingStrategies,
    queryFn: api.tradingStrategies,
  })
  const location = useLocation()
  // 解析 query (?view=list 形式)
  const viewQuery = new URLSearchParams(location.search).get('view')
  const bypassRedirect = viewQuery === 'list'

  const strategies = strategiesQ.data?.strategies ?? []

  if (bypassRedirect) {
    // 用户显式要列表; 直接渲染, 不论 strategies 多少.
    return <LiveTradingList />
  }

  // 仅在 loaded 且至少 1 个策略时, 触发 redirect (D4).
  // 加载中 / 失败 / 0 策略 → 仍渲染列表页.
  const firstId = strategies.length > 0 ? selectDefaultStrategyId(strategies) : undefined

  if (strategiesQ.isLoading && firstId === undefined) {
    return (
      <div className="min-h-screen grid place-items-center text-xs text-muted">
        加载策略列表…
      </div>
    )
  }
  if (firstId) {
    return <Navigate to={`/live-trading/${encodeURIComponent(firstId)}`} replace />
  }
  // 0 策略 (或 query 失败): 显示列表页
  return <LiveTradingList />
}

// ----- Use navigate side effect (D4 escape hatch from listing page later) -----
// (保持接口稳定, 防止 list 页面未来想用 useEffect 触发相同守卫)
export function useEnsureStrategyRedirect(): void {
  useEffect(() => {
    // 略: 由 RootRedirectGate 承担, 此 hook 仅占位接口稳定
  }, [])
}
