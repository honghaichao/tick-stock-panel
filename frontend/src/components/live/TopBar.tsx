import type { ReactNode } from 'react'
import { Briefcase, CircleSlash2, ListChecks, TrendingDown, TrendingUp, Wallet } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPrice, priceColorClass } from '@/lib/format'

interface Kpi {
  label: string
  primary: string
  sub?: string
  primaryClass?: string
  icon: ReactNode
}

function KpiCard({ label, primary, sub, primaryClass, icon }: Kpi) {
  return (
    <div className="rounded-xl border border-border bg-surface/40 p-3 shadow-sm">
      <div className="flex items-center justify-between text-[11px] uppercase tracking-wide text-muted">
        <span>{label}</span>
        <span className="h-3.5 w-3.5">{icon}</span>
      </div>
      <div className={cn('mt-1 font-mono text-lg font-semibold', primaryClass ?? 'text-foreground')}>
        {primary}
      </div>
      {sub && <div className="mt-0.5 text-[11px] text-secondary">{sub}</div>}
    </div>
  )
}

interface Props {
  /** 当前策略 id (URL /live-trading/:strategyId). null 表示全局账户视图 (LiveTradingList). */
  strategyId?: string | null
}

export function LiveTopBar({ strategyId = null }: Props = {}) {
  // per-strategy 视图: PnL 由 strategyPnlMetrics 算 (该 strategy 的 fills)
  // 全局账户视图: account 直接读 (跨所有 strategy)
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
  // 持仓市值 = sum(qty * last_price), 与账户 account.market_value 同口径
  const marketValue = positions.reduce((acc, p) => acc + (p.market_value ?? 0), 0)
  const equity = m ? m.current_equity : null
  // 现金等值 (推算): 总资产 - 持仓市值; 跟 account.cash 一致口径
  // 注: 这是显示给用户的"占用资金"提示, 实际口径由后端 account.cash 决定
  const cashProxy = equity != null ? equity - marketValue : null
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <KpiCard
        label={strategyId ? '策略总资产' : '总资产'}
        icon={<Wallet className="h-3.5 w-3.5" />}
        primary={equity != null ? fmtPrice(equity) : '—'}
        sub={m ? `持仓 ¥${fmtPrice(marketValue)} · 占用 ¥${fmtPrice(cashProxy ?? 0)} · 初始 ¥${fmtPrice(m.initial_cash)}` : ''}
      />
      <KpiCard
        label="累计盈亏"
        icon={m && m.total_pnl >= 0 ? <TrendingUp className="h-3.5 w-3.5" /> : <TrendingDown className="h-3.5 w-3.5" />}
        primary={m ? `${m.total_pnl >= 0 ? '+' : ''}${fmtPrice(m.total_pnl)}` : '—'}
        primaryClass={m ? priceColorClass(m.total_pnl) : ''}
        sub={m ? `未实现 ${fmtPrice(m.unrealized_pnl)} · 已实现 ${fmtPrice(m.realized_pnl)}` : ''}
      />
      <KpiCard
        label="运行状态"
        icon={s?.running ? <Briefcase className="h-3.5 w-3.5" /> : <CircleSlash2 className="h-3.5 w-3.5" />}
        primary={s ? (s.running ? '撮合中' : '已停止') : '—'}
        primaryClass={s?.running ? 'text-bull' : 'text-muted'}
        sub={s ? `${s.broker} · ${s.matcher_interval_seconds}s/轮 · 待撮合 ${s.pending_order_count}` : ''}
      />
      <KpiCard
        label="委托概览"
        icon={<ListChecks className="h-3.5 w-3.5" />}
        primary={s ? `${s.filled_order_count} / ${s.filled_order_count + s.pending_order_count}` : '—'}
        sub={s ? '已成交 / 全部 (含 NEW)' : ''}
      />
    </div>
  )
}
