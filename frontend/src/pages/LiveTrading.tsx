/**
 * 实盘交易页 — 外壳.
 *
 * 拆分自原 LiveTrading.tsx (PR2). 详见 docs/live-trading-v2-pr2-decompose-and-wire.md.
 */
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { PageHeader } from '@/components/PageHeader'
import { LiveTopBar } from '@/components/live/TopBar'
import { LiveSidebar, getInitialTab, type LiveTabKey } from '@/components/live/Sidebar'
import { CurrentStrategyPill } from '@/components/live/CurrentStrategyPill'
import { LiveTradingOverview } from './live/LiveTradingOverview'
import { LiveTradingStats } from './live/LiveTradingStats'
import { LiveTradingLogs } from './live/LiveTradingLogs'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'


/**
 * 实盘交易页外壳.
 *
 * 设计 (CONTRIBUTING §7):
 * - 5 tab 路由 + Sidebar 右上 "策略配置 ▼" 抽屉 (code/settings, PR1 实施)
 * - 各 tab 内容是独立文件, 改一处不动外壳
 * - LiveTabKey 在 Sidebar.tsx 定义, 此处仅作本地 state
 *
 * 拆分说明: docs/live-trading-v2-pr2-decompose-and-wire.md §范围 B
 *  - strategyIsExpanded / splitStrategiesByStatus 是纯函数, 搬到 lib/liveTradingTable.ts
 *  - 9 个共享展示 helper 搬到 components/live/shared.tsx
 *  - 5 个 Content + CurrentStrategyPill 各自独立文件
 */
export function LiveTrading({ strategyId }: { strategyId: string }) {
  const [tab, setTab] = useState<LiveTabKey>(getInitialTab)
  // ?view=verify: 全屏只显示 VerifyAndDriftSection (PR3 把回测功能搬出实盘页).
  // 与 RootRedirectGate 的 ?view=list 是两套独立 query, 不冲突.
  const [searchParams, setSearchParams] = useSearchParams()
  const view = searchParams.get('view') === 'verify' ? 'verify' : undefined

  // 共享 tradingStrategies 查询: PageHeader title 用策略名 (PR3),
  // 与 CurrentStrategyPill 同 query key 复用缓存, 0 额外请求.
  const strategiesQ = useQuery({
    queryKey: QK.tradingStrategies,
    queryFn: api.tradingStrategies,
    staleTime: 60_000,
  })
  const strategyName = strategiesQ.data?.strategies.find(s => s.id === strategyId)?.name

  // onOpenVerify: Sidebar "策略配置 ▼" -> "打开回测验证 (VerifyAndDrift)" 入口触发.
  const openVerify = () => setSearchParams({ view: 'verify' })
  const closeVerify = () => setSearchParams({})

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title={strategyName ? `实盘交易 · ${strategyName}` : `实盘交易 · ${strategyId}`}
        subtitle="聚宽式模拟盘 — 上一根 K 撮合 + 滑点 + 17:00 清算 + 失败重试按 cron 时间撮合"
        right={<CurrentStrategyPill strategyId={strategyId} />}
      />
      <div className="flex flex-1 min-h-0">
        <LiveSidebar active={tab} onChange={setTab} strategyId={strategyId} onOpenVerify={openVerify} />
        <div className="flex-1 min-w-0 overflow-y-auto px-5 py-4">
          <div className="mx-auto max-w-[1600px] space-y-4">
            <LiveTopBar strategyId={strategyId} />
            {view === 'verify' ? (
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <button
                    onClick={closeVerify}
                    className="inline-flex items-center gap-1 rounded-md border border-border bg-surface/40 px-3 py-1.5 text-xs text-secondary hover:bg-elevated/40 cursor-pointer"
                  >
                    ← 返回概览
                  </button>
                  <span className="text-xs text-muted">回测验证历史 + 实盘 vs 验证 drift (仅 per-strategy 显示)</span>
                </div>
                <LiveTradingOverview strategyId={strategyId} view="verify" />
              </div>
            ) : (
              <>
                {tab === 'overview' && <LiveTradingOverview strategyId={strategyId} />}
                {tab === 'stats' && <LiveTradingStats strategyId={strategyId} />}
                {tab === 'logs' && <LiveTradingLogs strategyId={strategyId} />}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/**
 * 保留两个纯函数导出 (测试 + StrategiesPanel.tsx 引用):
 *   - frontend/src/lib/liveTradingTable.ts (主定义)
 *   - 这里 re-export 维持 `./LiveTrading` 导入路径, 测试和外部使用方不动
 *
 * 关联: docs/live-trading-list-detail-layout.md §3.3
 */
export { strategyIsExpanded, splitStrategiesByStatus } from '@/lib/liveTradingTable'
