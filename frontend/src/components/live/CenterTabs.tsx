import { useState } from 'react'
import { LineChart as LineChartIcon, ListChecks, ListOrdered, ScrollText } from 'lucide-react'
import { cn } from '@/lib/cn'

const TABS = [
  { key: 'pnl', label: '收益曲线', icon: LineChartIcon },
  { key: 'orders', label: '交易明细', icon: ListChecks },
  { key: 'runs', label: '运行历史', icon: ListOrdered },
  { key: 'logs', label: '日志', icon: ScrollText },
] as const

type TabKey = (typeof TABS)[number]['key']

interface CenterTabsProps {
  children: Record<TabKey, React.ReactNode>
  /** 当前策略 id; 传值后 tab 选择按策略维度持久化 (D5).
   *  不传则用全局键 'liveCenterTab' (向后兼容 — 列表页/旧用法保持). */
  strategyId?: string | null
}

export function CenterTabs({ children, strategyId }: CenterTabsProps) {
  // D5: 详情页 tab 选择不能跨策略串; 同策略的详情页 ↔ 列表页展开 row 共享 tab 选择.
  const storageKey = strategyId ? `liveCenterTab:${strategyId}` : 'liveCenterTab'
  const [active, setActive] = useState<TabKey>(() => {
    if (typeof localStorage === 'undefined') return 'pnl'
    // 旧键 fallback: 第一次进新维度时, 若新键空但旧键有值, 复制过去 (best-effort).
    const fromNew = localStorage.getItem(storageKey) as TabKey | null
    if (fromNew) return fromNew
    if (strategyId) {
      const legacy = localStorage.getItem('liveCenterTab') as TabKey | null
      if (legacy && TABS.some(t => t.key === legacy)) {
        localStorage.setItem(storageKey, legacy)
        return legacy
      }
    }
    return 'pnl'
  })
  const switchTab = (k: TabKey) => {
    setActive(k)
    if (typeof localStorage !== 'undefined') localStorage.setItem(storageKey, k)
  }
  return (
    <div className="rounded-xl border border-border bg-surface/30">
      <div className="flex border-b border-border/60 px-2">
        {TABS.map(t => (
          <button
            key={t.key}
            onClick={() => switchTab(t.key)}
            className={cn(
              'inline-flex items-center gap-1.5 px-3 py-2 text-xs font-medium transition-colors cursor-pointer',
              active === t.key
                ? 'border-b-2 border-accent text-foreground'
                : 'text-muted hover:text-secondary'
            )}
          >
            <t.icon className="h-3.5 w-3.5" />
            {t.label}
          </button>
        ))}
      </div>
      <div className="p-3">{children[active]}</div>
    </div>
  )
}
