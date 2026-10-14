/**
 * 实盘页共享展示 helper. 拆分自 LiveTrading.tsx (PR2).
 * 关联 docs/live-trading-v2-pr2-decompose-and-wire.md §范围 C.
 */
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'


export function Card({ label, primary, sub, primaryClass, icon: Icon }: {
  label: string; primary: string; sub?: string; primaryClass?: string; icon?: React.ComponentType<{ className?: string }>
}) {
  return (
    <div className="rounded-xl border border-border bg-surface/40 p-3 shadow-sm">
      <div className="flex items-center justify-between text-[11px] uppercase tracking-wide text-muted">
        <span>{label}</span>
        {Icon && <Icon className="h-3.5 w-3.5" />}
      </div>
      <div className={cn('mt-1 font-mono text-lg font-semibold', primaryClass ?? 'text-foreground')}>
        {primary}
      </div>
      {sub && <div className="mt-0.5 text-[11px] text-secondary">{sub}</div>}
    </div>
  )
}


export function PnLCell({ value, pct, prefix }: { value: number | null; pct: number | null; prefix?: string }) {
  if (value == null) return <span className="text-muted/60">—</span>
  return (
    <span className={cn('font-mono', priceColorClass(value))}>
      {prefix && <span className="text-[9px] text-muted mr-0.5">{prefix}</span>}
      {value >= 0 ? '+' : ''}{fmtPrice(value)}
      {pct != null && (
        <span className="ml-1 text-[10px] opacity-70">{fmtPct(pct)}</span>
      )}
    </span>
  )
}

export function SymbolCell({ symbol, name, onPreview }: { symbol: string; name?: string; onPreview: () => void }) {
  return (
    <button onClick={onPreview}
      className="inline-flex items-center gap-1.5 hover:bg-elevated/50 rounded px-0.5 py-0.5 transition-colors cursor-pointer">
      <span className="font-mono font-medium text-foreground">{symbol}</span>
      {(() => { const b = boardTag(symbol); return b && (
        <span className={cn('inline-flex items-center justify-center rounded px-1 text-[9px] font-bold leading-tight border', b.color)}>
          {b.label}
        </span>
      ) })()}
      {name && <span className="text-secondary truncate max-w-28">{name}</span>}
    </button>
  )
}


export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] uppercase tracking-wide text-muted">{label}</span>
      {children}
    </label>
  )
}

export function Pill({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button onClick={onClick}
      className={cn('flex-1 rounded-md px-2.5 py-1.5 text-xs transition-colors cursor-pointer',
        active ? 'bg-accent/15 text-accent border border-accent/30'
               : 'bg-elevated text-secondary border border-border hover:bg-elevated/60')}>
      {children}
    </button>
  )
}


const kindLabel: Record<string, string> = {
  submit: '提交', accept: '受理', fill: '成交', cancel: '撤单', reject: '拒单', matcher_tick: '撮合',
}
const kindCls: Record<string, string> = {
  submit: 'text-secondary bg-secondary/10 border-secondary/30',
  accept: 'text-accent bg-accent/10 border-accent/30',
  fill: 'text-bull bg-bull/10 border-bull/30',
  cancel: 'text-muted bg-muted/10 border-muted/30',
  reject: 'text-danger bg-danger/10 border-danger/30',
  matcher_tick: 'text-muted bg-muted/5 border-muted/30',
}
export function KindBadge({ kind }: { kind: string }) {
  return (
    <span className={cn('inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold border',
      kindCls[kind] || 'text-muted bg-muted/10 border-muted/30')}>
      {kindLabel[kind] ?? kind}
    </span>
  )
}


export function Section({ title, subtitle, icon: Icon, actions, children }: {
  title: string; subtitle?: string; icon?: React.ComponentType<{ className?: string }>
  actions?: React.ReactNode; children: React.ReactNode
}) {
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {Icon && <Icon className="h-4 w-4 text-muted" />}
          <h2 className="text-sm font-medium text-foreground">{title}</h2>
          {subtitle && <span className="text-[11px] text-secondary">{subtitle}</span>}
        </div>
        {actions && <div className="flex items-center gap-1.5">{actions}</div>}
      </div>
      {children}
    </section>
  )
}

export function EmptyHint({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-border px-6 py-10 text-center">
      <div className="text-xs text-muted">{children}</div>
    </div>
  )
}


export function Stat({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-md border border-border bg-surface/30 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={cn('mt-0.5 font-mono', valueClass)}>{value}</div>
    </div>
  )
}

