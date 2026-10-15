import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { usePersistedState } from '@/lib/usePersistedState'

const METRICS = [
  { key: 'alpha', label: 'Alpha', color: '#3b82f6' },
  { key: 'beta', label: 'Beta', color: '#8b5cf6' },
  { key: 'sharpe', label: 'Sharpe', color: '#10b981' },
  { key: 'sortino', label: 'Sortino', color: '#f59e0b' },
  { key: 'info_ratio', label: 'Info Ratio', color: '#ef4444' },
  { key: 'volatility', label: 'Volatility', color: '#6366f1' },
] as const

type MetricKey = (typeof METRICS)[number]['key']

const STORAGE_KEY = 'liveRiskScope'
type Scope = 'today' | 'history'

export function RiskStats({ strategyId: _strategyId }: { strategyId?: string | null } = {}) {
  // v2 (live-trading-v2-pr2): "今日 / 历史" toggle, 默认 "今日" (trader 4 小时盯盘主要看今天).
  // 今日: pnl-metrics 端点的 today_pnl / max_drawdown / current_equity
  // 历史: risk-metrics 端点的 60 天 Alpha / Beta / Sharpe 等
  // v2-pr3: usePersistedState 替代手写 localStorage (跨 tab 同步)
  const [scope, setScope] = usePersistedState<Scope>(STORAGE_KEY, 'today')

  const pnlQ = useQuery({
    queryKey: ['trading', 'pnl-metrics'],
    queryFn: () => api.tradingPnlMetrics(),
    refetchInterval: 8000,
  })
  const riskQ = useQuery({
    queryKey: ['trading', 'risk_metrics'],
    queryFn: () => api.tradingRiskMetrics({ window: 60 }),
    refetchInterval: 60_000,
    enabled: scope === 'history',
  })
  // "今日" 模式: 复用 riskQ.data schema 包装, 让下方 SummaryCard 不报错
  // summary 用 pnlQ 字段 (today_pnl/current_equity/max_drawdown); series 空 (今日 sparkline 单独有)
  const data = scope === 'today'
    ? (pnlQ.data
        ? { summary: pnlQ.data, series: [] as { date: string }[] }
        : undefined)
    : riskQ.data
  const isLoading = scope === 'today' ? pnlQ.isLoading : riskQ.isLoading

  return (
    <div className="rounded-xl border border-border bg-surface/30 p-4 space-y-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">
          {scope === 'today' ? '今日风险指标' : '风险指标 (滚动 60 天, 基准 沪深 300)'}
        </h3>
        <div className="flex rounded border border-border bg-surface/40 text-[10px]">
          <button
            onClick={() => setScope('today')}
            className={cn(
              'px-2 py-0.5 transition-colors cursor-pointer',
              scope === 'today' ? 'bg-accent/15 text-accent font-medium' : 'text-muted hover:text-foreground',
            )}
          >
            今日
          </button>
          <button
            onClick={() => setScope('history')}
            className={cn(
              'px-2 py-0.5 transition-colors cursor-pointer border-l border-border',
              scope === 'history' ? 'bg-accent/15 text-accent font-medium' : 'text-muted hover:text-foreground',
            )}
          >
            历史
          </button>
        </div>
      </div>
      {isLoading || !data ? (
        <div className="py-12 text-center text-xs text-muted">加载中…</div>
      ) : scope === 'today' && pnlQ.data ? (
        // 今日: 简化卡片 (今日盈亏 / 当前权益 / 最大回撤) — trader 4h 盯盘最关心的 3 个数
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <SummaryCard label="今日盈亏" value={pnlQ.data.today_pnl_pct != null ? pnlQ.data.today_pnl_pct * 100 : null} color="#10b981" suffix="%" digits={2} />
          <SummaryCard label="当前权益" value={pnlQ.data.current_equity} color="#3b82f6" prefix="¥" digits={0} />
          <SummaryCard label="最大回撤" value={pnlQ.data.max_drawdown != null ? pnlQ.data.max_drawdown * 100 : null} color="#ef4444" suffix="%" digits={2} />
        </div>
      ) : data.series.length === 0 ? (
        <div className="py-12 text-center text-xs text-muted">
          数据不足 — 需要至少 5 个 pnl_snapshots 交易日 + 沪深 300 收盘价
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            {METRICS.map(m => (
              <SummaryCard key={m.key} label={m.label} value={((data.summary as any) || {})[m.key]} color={m.color} />
            ))}
          </div>
          <div className="space-y-3">
            {METRICS.map(m => (
              <MetricSparkline key={m.key} series={data.series} metricKey={m.key} label={m.label} color={m.color} />
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function SummaryCard({
  label,
  value,
  color,
  prefix = '',
  suffix = '',
  digits = 3,
}: {
  label: string
  value: number | null
  color: string
  prefix?: string
  suffix?: string
  digits?: number
}) {
  return (
    <div className="rounded-lg border border-border bg-surface/40 p-3">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted">
        <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
        {label}
      </div>
      <div className={cn('mt-1 font-mono text-base font-semibold', value == null ? 'text-muted/60' : 'text-foreground')}>
        {value == null ? '—' : `${prefix}${value.toFixed(digits)}${suffix}`}
      </div>
    </div>
  )
}

function MetricSparkline({
  series, metricKey, label, color,
}: { series: { date: string; [k: string]: any }[]; metricKey: MetricKey; label: string; color: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<{ idx: number; x: number; y: number } | null>(null)

  useEffect(() => {
    if (!ref.current || series.length === 0) return
    const values = series.map(s => s[metricKey] as number | null).filter(v => v != null) as number[]
    if (values.length === 0) return
    const lo = Math.min(...values)
    const hi = Math.max(...values)
    const range = hi - lo || 1
    const W = ref.current.clientWidth
    const H = 60
    const stepX = W / Math.max(series.length - 1, 1)
    const points = series.map((s, i) => {
      const v = s[metricKey] as number | null
      const x = i * stepX
      const y = v == null ? null : H - ((v - lo) / range) * (H - 4) - 2
      return { x, y, s }
    })
    const validPath = points.filter(p => p.y != null).map(p => `${p.x.toFixed(1)},${p.y!.toFixed(1)}`).join(' L ')
    const onMove = (e: MouseEvent) => {
      const rect = ref.current!.getBoundingClientRect()
      const x = e.clientX - rect.left
      const idx = Math.round(x / stepX)
      if (idx >= 0 && idx < points.length && points[idx].y != null) {
        setHover({ idx, x: points[idx].x, y: points[idx].y! })
      } else {
        setHover(null)
      }
    }
    const onLeave = () => setHover(null)
    ref.current.innerHTML = `
      <svg width="${W}" height="${H + 4}" viewBox="0 0 ${W} ${H + 4}" class="block">
        <path d="M ${validPath}" fill="none" stroke="${color}" stroke-width="1.5" />
        ${hover ? `<line x1="${hover.x}" x2="${hover.x}" y1="0" y2="${H}" stroke="${color}" stroke-width="0.5" stroke-dasharray="2,2" opacity="0.5" />` : ''}
        ${hover ? `<circle cx="${hover.x}" cy="${hover.y}" r="3" fill="${color}" />` : ''}
      </svg>
    `
    ref.current.addEventListener('mousemove', onMove)
    ref.current.addEventListener('mouseleave', onLeave)
    return () => {
      ref.current?.removeEventListener('mousemove', onMove)
      ref.current?.removeEventListener('mouseleave', onLeave)
    }
  }, [series, metricKey, color, hover])

  const validCount = series.filter(s => s[metricKey] != null).length
  return (
    <div className="rounded-lg border border-border/40 bg-surface/20 p-2">
      <div className="mb-1 flex items-center justify-between text-[10px] text-muted">
        <span className="font-medium uppercase tracking-wide" style={{ color }}>{label}</span>
        <span>{validCount} 个有效点</span>
      </div>
      <div ref={ref} className="h-[60px] w-full" />
    </div>
  )
}
