import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

const METRICS = [
  { key: 'alpha', label: 'Alpha', color: '#3b82f6' },
  { key: 'beta', label: 'Beta', color: '#8b5cf6' },
  { key: 'sharpe', label: 'Sharpe', color: '#10b981' },
  { key: 'sortino', label: 'Sortino', color: '#f59e0b' },
  { key: 'info_ratio', label: 'Info Ratio', color: '#ef4444' },
  { key: 'volatility', label: 'Volatility', color: '#6366f1' },
] as const

type MetricKey = (typeof METRICS)[number]['key']

export function RiskStats({ strategyId: _strategyId }: { strategyId?: string | null } = {}) {
  // 风险指标暂时保留账户级 (per-strategy 风险指标需要单独算收益序列, 待 PR-2 落地).
  const { data, isLoading } = useQuery({
    queryKey: ['trading', 'risk_metrics'],
    queryFn: () => api.tradingRiskMetrics({ window: 60 }),
    refetchInterval: 60000,
  })

  return (
    <div className="rounded-xl border border-border bg-surface/30 p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">风险指标 (滚动 60 天, 基准 沪深 300)</h3>
        {data && <div className="text-[11px] text-muted">{data.series.length} 个数据点</div>}
      </div>
      {isLoading || !data ? (
        <div className="py-12 text-center text-xs text-muted">加载中…</div>
      ) : data.series.length === 0 ? (
        <div className="py-12 text-center text-xs text-muted">
          数据不足 — 需要至少 5 个 pnl_snapshots 交易日 + 沪深 300 收盘价
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            {METRICS.map(m => (
              <SummaryCard key={m.key} label={m.label} value={data.summary[m.key]} color={m.color} />
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

function SummaryCard({ label, value, color }: { label: string; value: number | null; color: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface/40 p-3">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted">
        <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
        {label}
      </div>
      <div className={cn('mt-1 font-mono text-base font-semibold', value == null ? 'text-muted/60' : 'text-foreground')}>
        {value == null ? '—' : value.toFixed(3)}
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
