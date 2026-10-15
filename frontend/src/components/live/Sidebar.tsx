import { useEffect, useRef } from 'react'
import { ArrowLeft, BarChart3, ChevronDown, Code2, FileText, ListChecks, Settings } from 'lucide-react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/cn'
import { usePersistedState } from '@/lib/usePersistedState'
import { CodeView } from './CodeView'
import { StrategySettings } from './StrategySettings'

// v2 (live-trading-v2-pr1): 主侧边只剩 3 tab, code/settings 收右上 "策略配置 ▼" 抽屉.
// 旧 5-tab 数据完全保持, 老用户 localStorage 'liveTab5' 仍可读, 找不到的 key
// fallback 到 overview, 不会卡死.
const TABS = [
  { key: 'overview', label: '概览', icon: BarChart3 },
  { key: 'stats', label: '统计', icon: ListChecks },
  { key: 'logs', label: '日志', icon: FileText },
] as const

const STORAGE_KEY = 'liveTab5'
const SETTINGS_KEY = 'liveConfigOpen'

export type LiveTabKey = (typeof TABS)[number]['key']

interface Props {
  active: LiveTabKey
  onChange: (k: LiveTabKey) => void
  strategyId?: string
  onOpenVerify?: () => void  // 打开 ?view=verify 抽屉 (PR1 把回测功能搬出实盘页)
}

export function LiveSidebar({ active, onChange, strategyId, onOpenVerify }: Props) {
  // 配置抽屉: 右上角 "策略配置 ▼" 下拉, 含 code + settings + verify 三块.
  // 点击外部关闭, ESC 关闭, 默认收起. 用 usePersistedState 跨 tab 同步.
  const [configOpen, setConfigOpen] = usePersistedState<boolean>(SETTINGS_KEY, false)
  const configRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!configOpen) return
    const onClick = (e: MouseEvent) => {
      if (configRef.current && !configRef.current.contains(e.target as Node)) {
        setConfigOpen(false)
      }
    }
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setConfigOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onEsc)
    }
  }, [configOpen])
  const toggleConfig = () => setConfigOpen((v) => !v)

  return (
    <aside className="w-[200px] shrink-0 border-r border-border bg-surface/30 py-2 flex flex-col">
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
        {TABS.map((t) => {
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
                  : 'text-secondary hover:bg-elevated/40 hover:text-foreground border-l-2 border-transparent',
              )}
            >
              <Icon className="h-4 w-4 shrink-0" />
              {t.label}
            </button>
          )
        })}
      </nav>

      {/* 策略配置抽屉: 实盘页该有的 code/settings/verify 收这里, 不污染主导航 */}
      {strategyId && (
        <div ref={configRef} className="mt-auto mx-2 mb-2 relative">
          <button
            onClick={toggleConfig}
            className={cn(
              'w-full flex items-center justify-between gap-1 rounded-md border px-3 py-2 text-xs transition-colors cursor-pointer',
              configOpen
                ? 'border-accent/40 bg-accent/10 text-accent'
                : 'border-border bg-surface/40 text-secondary hover:bg-elevated/40 hover:text-foreground',
            )}
            title="策略代码 / schedules / 风控 (回测期功能)"
          >
            <span className="flex items-center gap-1.5">
              <Settings className="h-3.5 w-3.5" />
              策略配置
            </span>
            <ChevronDown className={cn('h-3 w-3 transition-transform', configOpen ? 'rotate-180' : '')} />
          </button>
          {configOpen && (
            <div className="absolute left-full bottom-0 ml-2 w-[min(640px,80vw)] max-h-[70vh] overflow-y-auto rounded-lg border border-border bg-surface shadow-2xl z-50">
              <div className="border-b border-border/60 bg-surface/80 px-3 py-2 text-[11px] uppercase tracking-wide text-muted">
                策略配置 (回测期功能, 不影响实盘盯盘)
              </div>
              <div className="space-y-3 p-3">
                <details open className="rounded-md border border-border bg-surface/40 p-2">
                  <summary className="cursor-pointer text-xs font-medium text-secondary hover:text-foreground">
                    <Code2 className="inline h-3 w-3 mr-1" /> 代码
                  </summary>
                  <div className="mt-2">
                    <CodeView strategyId={strategyId} />
                  </div>
                </details>
                <details open className="rounded-md border border-border bg-surface/40 p-2">
                  <summary className="cursor-pointer text-xs font-medium text-secondary hover:text-foreground">
                    <Settings className="inline h-3 w-3 mr-1" /> 设置
                  </summary>
                  <div className="mt-2">
                    <StrategySettings strategyId={strategyId} />
                  </div>
                </details>
                {onOpenVerify && (
                  <div className="rounded-md border border-border bg-surface/40 p-2">
                    <button
                      onClick={() => {
                        onOpenVerify()
                        setConfigOpen(false)
                      }}
                      className="w-full text-left text-xs text-accent hover:underline"
                    >
                      打开回测验证 (VerifyAndDrift)
                    </button>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </aside>
  )
}

export function getInitialTab(): LiveTabKey {
  if (typeof localStorage === 'undefined') return 'overview'
  const saved = localStorage.getItem(STORAGE_KEY) as LiveTabKey | null
  return saved && TABS.some((t) => t.key === saved) ? saved : 'overview'
}
