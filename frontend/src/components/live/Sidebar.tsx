import { ArrowLeft, BarChart3, Code2, FileText, ListChecks, Settings } from 'lucide-react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/cn'

const TABS = [
  { key: 'overview', label: '概览', icon: BarChart3 },
  { key: 'stats', label: '统计', icon: ListChecks },
  { key: 'logs', label: '日志', icon: FileText },
  { key: 'code', label: '代码', icon: Code2 },
  { key: 'settings', label: '设置', icon: Settings },
] as const

export type LiveTabKey = (typeof TABS)[number]['key']

const STORAGE_KEY = 'liveTab5'

interface Props {
  active: LiveTabKey
  onChange: (k: LiveTabKey) => void
  /**
   * PR-2 新增. 当前详情页对应的 strategyId; 提供时, 顶部显示
   * "← 返回策略列表" 链接 (跳 /live-trading → D4 redirect 再回到首个策略).
   * 不提供时 (旧调用), 不显示 (向后兼容).
   */
  strategyId?: string
}

export function LiveSidebar({ active, onChange, strategyId }: Props) {
  return (
    <aside className="w-[200px] shrink-0 border-r border-border bg-surface/30 py-2">
      {strategyId && (
        <Link
          to="/live-trading?view=list"
          className="mb-2 mx-3 mt-1 flex items-center gap-1.5 rounded-md border border-border bg-surface/40 px-2 py-1.5 text-[11px] text-secondary hover:bg-elevated/40 transition-colors"
          title="返回策略列表 (绕过 D4 redirect, 显示列表)"
        >
          <ArrowLeft className="h-3 w-3" />
          返回策略列表
        </Link>
      )}
      <nav className="flex flex-col">
        {TABS.map(t => {
          const Icon = t.icon
          const isActive = active === t.key
          return (
            <button
              key={t.key}
              onClick={() => {
                onChange(t.key)
                if (typeof localStorage !== 'undefined') localStorage.setItem(STORAGE_KEY, t.key)
              }}
              className={cn(
                'flex items-center gap-2 px-4 py-2.5 text-sm transition-colors cursor-pointer text-left',
                isActive
                  ? 'bg-accent/10 text-accent border-l-2 border-accent font-medium'
                  : 'text-secondary hover:bg-elevated/40 hover:text-foreground border-l-2 border-transparent'
              )}
            >
              <Icon className="h-4 w-4 shrink-0" />
              {t.label}
            </button>
          )
        })}
      </nav>
    </aside>
  )
}

export function getInitialTab(): LiveTabKey {
  if (typeof localStorage === 'undefined') return 'overview'
  const saved = localStorage.getItem(STORAGE_KEY) as LiveTabKey | null
  return saved && TABS.some(t => t.key === saved) ? saved : 'overview'
}
