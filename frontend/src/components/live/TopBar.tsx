import { Briefcase, CircleSlash2, TrendingDown, TrendingUp, Wallet } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPrice, priceColorClass } from '@/lib/format'
import { EmergencyControls } from './EmergencyControls'
import { BellNotifications } from './BellNotifications'

interface Kpi {
  label: string
  primary: string
  sub?: string
  primaryClass?: string
  icon: React.ReactNode
  /** hero 数字用 text-3xl/4xl, sub 指标用 text-sm */
  size?: 'hero' | 'sm'
}

function KpiCard({ label, primary, sub, primaryClass, icon, size = 'sm' }: Kpi) {
  return (
    <div className="rounded-xl border border-border bg-surface/40 px-3 py-2 shadow-sm">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-wide text-muted">
        <span>{label}</span>
        <span className="h-3 w-3">{icon}</span>
      </div>
      <div
        className={cn(
          'mt-0.5 font-mono font-semibold tabular-nums',
          size === 'hero' ? 'text-3xl leading-tight' : 'text-sm leading-tight',
          primaryClass ?? 'text-foreground',
        )}
      >
        {primary}
      </div>
      {sub && <div className="mt-0.5 text-[10px] text-secondary">{sub}</div>}
    </div>
  )
}

interface Props {
  strategyId?: string | null
}

export function LiveTopBar({ strategyId = null }: Props = {}) {
  const metricsQ = useQuery({
    queryKey: QK.tradingPnlMetrics(strategyId),
    queryFn: () => api.tradingPnlMetrics(strategyId ? { strategy_id: strategyId } : undefined),
    refetchInterval: 8000,
  })
  const positionsQ = useQuery({
    queryKey: QK.tradingPositions(strategyId),
    queryFn: () => api.tradingPositions(strategyId ? { strategy_id: strategyId } : undefined),
    refetchInterval: 8000,
  })
  const statusQ = useQuery({ queryKey: QK.tradingStatus, queryFn: api.tradingStatus, refetchInterval: 5000 })
  const m = metricsQ.data
  const s = statusQ.data
  const positions = positionsQ.data?.positions ?? []
  const marketValue = positions.reduce((acc, p) => acc + (p.market_value ?? 0), 0)
  const equity = m ? m.current_equity : null
  return (
    <div
      data-testid="topbar"
      className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-[2fr_1fr_1fr_1fr_auto]"
    >
      {/* 顶栏布局: hero (今日盈亏) + 3 个 sub-metric + 救命三件套
       *  - xl+ (≥1280px): 1 行 5 列, hero 2fr 占两列宽
       *  - sm-lg (640-1279px): 4 列, 救命按钮换行到下一行 (auto 让其单独占位)
       *  - <640px: 2 列, 全部换行
       */}
      <KpiCard
        label="今日盈亏"
        size="hero"
        icon={m && m.today_pnl >= 0 ? <TrendingUp className="h-3.5 w-3.5" /> : <TrendingDown className="h-3.5 w-3.5" />}
        primary={m ? `${m.today_pnl >= 0 ? '+' : ''}${fmtPrice(m.today_pnl)}` : '—'}
        primaryClass={m ? priceColorClass(m.today_pnl) : ''}
        sub={m ? `${m.today_pnl_pct >= 0 ? '+' : ''}${(m.today_pnl_pct * 100).toFixed(2)}% · 较开盘` : ''}
      />
      <KpiCard
        label={strategyId ? '策略总资产' : '总资产'}
        icon={<Wallet className="h-3 w-3" />}
        primary={equity != null ? fmtPrice(equity) : '—'}
        sub={m ? `持仓 ¥${fmtPrice(marketValue)}` : ''}
      />
      <KpiCard
        label="累计盈亏"
        icon={m && m.total_pnl >= 0 ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
        primary={m ? `${m.total_pnl >= 0 ? '+' : ''}${fmtPrice(m.total_pnl)}` : '—'}
        primaryClass={m ? priceColorClass(m.total_pnl) : ''}
        sub={m ? `${(m.total_pnl_pct * 100).toFixed(2)}% · 初始 ¥${fmtPrice(m.initial_cash)}` : ''}
      />
      <KpiCard
        label={s?.running ? '撮合中' : '已停止'}
        icon={s?.running ? <Briefcase className="h-3 w-3" /> : <CircleSlash2 className="h-3 w-3" />}
        primary={s ? `${s.filled_order_count} / ${s.filled_order_count + s.pending_order_count}` : '—'}
        primaryClass={s?.running ? 'text-bull' : 'text-muted'}
        sub={s ? `${s.broker} · ${s.matcher_interval_seconds}s/轮` : ''}
      />
      <div className="col-span-2 flex items-center justify-end gap-2 sm:col-span-1 xl:col-span-1">
        <BellNotifications />
        <EmergencyControls strategyId={strategyId} />
      </div>
    </div>
  )
}
