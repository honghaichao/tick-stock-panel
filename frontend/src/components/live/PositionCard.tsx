/**
 * v2 (live-trading-v2-pr1): 持仓卡片 — 1 行 1 票, hero 数字 PnL, 5 日分时 sparkline.
 *
 * 取代原 PositionsTable 的 10 列横排布局. sparkline 用 inline SVG (无依赖).
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { PositionApi } from '@/lib/api'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'

interface Props {
  position: PositionApi
  name?: string
  onSelect?: (symbol: string) => void
  /** v2-pr2: 持仓卡片快捷按钮 (卖 1/3 / 1/2 / 全) — 通知父组件弹 OrderDialog */
  onQuickTrade?: (side: 'sell', ratio: 1 | 2 | 'all', symbol: string) => void
}

function Sparkline({ data, color }: { data: { close: number }[]; color: string }) {
  if (data.length < 2) return <div className="h-full flex items-center justify-center text-[10px] text-muted">…</div>
  const W = 200
  const H = 36
  const closes = data.map((d) => d.close)
  const min = Math.min(...closes)
  const max = Math.max(...closes)
  const range = max - min || 1
  const stepX = W / (data.length - 1)
  const points = closes
    .map((c, i) => `${(i * stepX).toFixed(1)},${(H - ((c - min) / range) * (H - 4) - 2).toFixed(1)}`)
    .join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-9 w-full">
      <polyline fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" points={points} />
    </svg>
  )
}

export function PositionCard({ position, name, onSelect, onQuickTrade }: Props) {
  const symbol = position.symbol
  const sparkQ = useQuery({
    queryKey: ['spark', symbol],
    queryFn: () => api.klineDailyBatch([symbol], 8),
    staleTime: 60_000,
  })
  const sparkData = useMemo(() => {
    const rows = sparkQ.data?.data?.[symbol] ?? []
    return rows.slice(-5).map((r) => ({ close: Number(r.close) }))
  }, [sparkQ.data, symbol])

  const pnl = position.accumulated_pnl ?? 0
  const pnlPct = position.accumulated_pnl_pct ?? 0
  const todayPnl = position.total_pnl ?? 0
  const todayPnlPct = position.total_pnl_pct ?? 0
  const pnlCls = priceColorClass(pnl)
  const todayCls = priceColorClass(todayPnl)
  const last = position.last_price
  const avg = position.avg_cost
  const changePct = last != null && avg > 0 ? ((last - avg) / avg) * 100 : 0
  const changeCls = changePct > 0 ? 'text-bull' : changePct < 0 ? 'text-bear' : 'text-muted'
  const sparkColor = changePct >= 0 ? '#22c55e' : '#ef4444'

  return (
    <div
      onClick={() => onSelect?.(symbol)}
      className={cn(
        'rounded-xl border border-border bg-surface/40 p-3 shadow-sm transition-colors cursor-pointer',
        'hover:border-accent/40 hover:bg-surface/60',
      )}
    >
      {/* 头部: 标的 + 累计盈亏 hero */}
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-sm font-semibold text-foreground">
            <span className="truncate">{name || symbol}</span>
            <code className="rounded bg-elevated/40 px-1 py-0.5 font-mono text-[10px] text-muted shrink-0">
              {symbol}
            </code>
          </div>
          <div className="mt-0.5 text-[10px] text-muted">
            {position.qty} 股 · 成本 {fmtPrice(avg)}
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className={cn('font-mono font-semibold text-2xl tabular-nums leading-none', pnlCls)}>
            {pnl >= 0 ? '+' : ''}{fmtPrice(pnl)}
          </div>
          <div className={cn('mt-0.5 font-mono text-[10px] tabular-nums', pnlCls)}>
            {fmtPct(pnlPct)} 累计
          </div>
        </div>
      </div>

      {/* 中部: 5 日分时 sparkline (inline SVG, 无依赖) */}
      <div className="h-9 -mx-1">
        <Sparkline data={sparkData} color={sparkColor} />
      </div>

      {/* 底部: 现价 / 当时盈亏 (今日) / 快捷按钮 (PR2 接 OrderDialog) */}
      <div className="mt-2 flex items-end justify-between gap-2 text-[10px]">
        <div className="space-y-0.5">
          <div className="text-muted">
            现价 <span className={cn('font-mono tabular-nums', changeCls)}>{last != null ? fmtPrice(last) : '—'}</span>
            <span className={cn('ml-1 font-mono', changeCls)}>({changePct >= 0 ? '+' : ''}{changePct.toFixed(2)}%)</span>
          </div>
          <div className="text-muted">
            今日 <span className={cn('font-mono tabular-nums', todayCls)}>{todayPnl >= 0 ? '+' : ''}{fmtPrice(todayPnl)}</span>
            <span className={cn('ml-1 font-mono', todayCls)}>({fmtPct(todayPnlPct)})</span>
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
          <button
            type="button"
            title="卖出 1/3 仓位 (按持仓数量 ÷ 3 取整)"
            onClick={() => onQuickTrade?.('sell', 1, position.symbol)}
            className="rounded border border-border bg-surface/40 px-1.5 py-0.5 text-[10px] text-secondary hover:bg-elevated/40"
          >
            卖 1/3
          </button>
          <button
            type="button"
            title="卖出 1/2 仓位 (按持仓数量 ÷ 2 取整)"
            onClick={() => onQuickTrade?.('sell', 2, position.symbol)}
            className="rounded border border-border bg-surface/40 px-1.5 py-0.5 text-[10px] text-secondary hover:bg-elevated/40"
          >
            卖 1/2
          </button>
          <button
            type="button"
            title="全部卖出"
            onClick={() => onQuickTrade?.('sell', 'all', position.symbol)}
            className="rounded border border-bear/30 bg-bear/10 px-1.5 py-0.5 text-[10px] text-bear hover:bg-bear/20"
          >
            全卖
          </button>
        </div>
      </div>
    </div>
  )
}
