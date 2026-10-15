/**
 * 实盘列表页表格组件 (JoinQuant 风格, 聚宽模拟策略列表参考).
 * 关联: docs/live-trading-list-detail-layout.md §5.1
 *
 * PR-2 阶段: 列 (开始时间 / 累计 / 年化 / 今日 / 最大回撤 / 微信通知) 暂以 "—" 渲染,
 * 等 PR-1 (live-trading-strategy-summary.md) 上线后解占位.
 */
import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, Trash2 } from 'lucide-react'

import type { LiveStrategyApi } from '@/lib/api'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

import {
  type StrategyTab,
  filterStrategiesByTab,
  strategyTabCounts,
  summarizeFrequency,
  deriveRunDays,
} from '@/lib/liveTradingTable'

interface Props {
  /** 是否启用复选 + 删除按钮 (D3) */
  bulkSelectEnabled: boolean
  selectedIds: ReadonlySet<string>
  onToggleId: (id: string) => void
  onToggleAll: (ids: string[], allSelected: boolean) => void
  activeTab: StrategyTab
  onChangeTab: (tab: StrategyTab) => void
}

export function StrategyTable({
  bulkSelectEnabled,
  selectedIds,
  onToggleId,
  onToggleAll,
  activeTab,
  onChangeTab,
}: Props) {
  const strategiesQ = useQuery({ queryKey: QK.tradingStrategies, queryFn: api.tradingStrategies })
  const strategies: LiveStrategyApi[] = strategiesQ.data?.strategies ?? []
  const navigate = useNavigate()

  const counts = useMemo(() => strategyTabCounts(strategies), [strategies])
  const visible = useMemo(
    () => filterStrategiesByTab(strategies, activeTab),
    [strategies, activeTab],
  )
  const allSelected = visible.length > 0 && visible.every(s => selectedIds.has(s.id))

  if (strategiesQ.isLoading) {
    return <div className="py-12 text-center text-xs text-muted">加载策略列表…</div>
  }
  if (strategiesQ.isError) {
    return (
      <div className="py-12 text-center text-xs text-danger">
        策略列表加载失败: {(strategiesQ.error as Error)?.message ?? '未知错误'}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <Tabs counts={counts} active={activeTab} onChange={onChangeTab} />
      {strategies.length === 0 ? (
        <EmptyHint />
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-surface/30 shadow-sm">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-border/60 bg-surface/60 text-[10px] uppercase tracking-wide text-muted">
                {bulkSelectEnabled && (
                  <th className="w-8 px-2 py-2 font-medium">
                    <input
                      type="checkbox"
                      aria-label="全选当前 tab"
                      checked={allSelected}
                      onChange={() => onToggleAll(visible.map(s => s.id), !allSelected)}
                    />
                  </th>
                )}
                <th className="px-3 py-2 font-medium">名称</th>
                <th className="px-2 py-2 font-medium">频率</th>
                <th className="px-2 py-2 font-medium">状态</th>
                <th className="px-2 py-2 font-medium text-right">今日 PnL</th>
                <th className="px-2 py-2 font-medium">开始时间</th>
                <th className="w-6 px-2 py-2" aria-label="进入详情" />
              </tr>
            </thead>
            <tbody>
              {visible.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-3 py-8 text-center text-xs text-muted">
                    当前 tab 无策略.
                  </td>
                </tr>
              ) : (
                visible.map(s => (
                  <StrategyRow
                    key={s.id}
                    strategy={s}
                    bulkSelectEnabled={bulkSelectEnabled}
                    selected={selectedIds.has(s.id)}
                    onToggleSelect={() => onToggleId(s.id)}
                    onNavigate={() => navigate(`/live-trading/${encodeURIComponent(s.id)}`)}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function Tabs({ counts, active, onChange }: {
  counts: { all: number; active: number; stopped: number }
  active: StrategyTab
  onChange: (t: StrategyTab) => void
}) {
  const items: Array<{ key: StrategyTab; label: string; count: number }> = [
    { key: 'all', label: '全部', count: counts.all },
    { key: 'active', label: '进行中', count: counts.active },
    { key: 'stopped', label: '已关闭', count: counts.stopped },
  ]
  return (
    <div className="flex gap-4 border-b border-border/40 text-xs">
      {items.map(i => (
        <button
          key={i.key}
          type="button"
          onClick={() => onChange(i.key)}
          className={cn(
            'pb-2 pt-1 transition-colors cursor-pointer',
            active === i.key
              ? 'border-b-2 border-accent text-foreground font-medium'
              : 'border-b-2 border-transparent text-secondary hover:text-foreground',
          )}
        >
          {i.label} <span className="text-muted">({i.count})</span>
        </button>
      ))}
    </div>
  )
}

function StrategyRow({
  strategy,
  bulkSelectEnabled,
  selected,
  onToggleSelect,
  onNavigate,
}: {
  strategy: LiveStrategyApi
  bulkSelectEnabled: boolean
  selected: boolean
  onToggleSelect: () => void
  onNavigate: () => void
}) {
  const enabledCount = strategy.schedules.filter(sc => sc.enabled).length
  const statusKind = strategy.schedules.length === 0
    ? 'placeholder'
    : enabledCount > 0 ? 'active' : 'stopped'

  return (
    <tr
      onClick={onNavigate}
      className="cursor-pointer border-b border-border/40 last:border-0 hover:bg-elevated/40 transition-colors"
    >
      {bulkSelectEnabled && (
        <td className="px-2 py-2.5" onClick={e => e.stopPropagation()}>
          <input
            type="checkbox"
            aria-label={`选择 ${strategy.name}`}
            checked={selected}
            onChange={onToggleSelect}
          />
        </td>
      )}
      <td className="px-3 py-2.5">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-foreground truncate">{strategy.name}</span>
          <code className="text-[10px] text-muted">{strategy.id}</code>
        </div>
      </td>
      <td className="px-2 py-2.5 text-secondary">{summarizeFrequency(strategy.schedules)}</td>
      <td className="px-2 py-2.5">
        <StatusBadge kind={statusKind}
          enabledCount={enabledCount}
          total={strategy.schedules.length} />
      </td>
      <td className={cn(
        'px-2 py-2.5 text-right font-mono tabular-nums',
        strategy.summary && (strategy.summary.today_pnl ?? 0) >= 0 ? 'text-bull' : 'text-bear',
        !strategy.summary && 'text-muted',
      )}>
        {strategy.summary
          ? `${(strategy.summary.today_pnl ?? 0) >= 0 ? '+' : ''}${(strategy.summary.today_pnl ?? 0).toFixed(2)}`
          : '—'}
      </td>
      <td className="px-2 py-2.5 text-secondary">
        {strategy.started_at
          ? (() => {
              const days = deriveRunDays(strategy.started_at)
              return days == null
                ? formatDate(strategy.started_at!)
                : `${formatDate(strategy.started_at!)} · ${days}天`
            })()
          : '—'}
      </td>
      <td className="px-2 py-2.5 text-right text-muted">
        <ChevronRight className="inline h-3.5 w-3.5" />
      </td>
    </tr>
  )
}

function StatusBadge({ kind, enabledCount, total }: {
  kind: 'active' | 'stopped' | 'placeholder'
  enabledCount: number
  total: number
}) {
  if (kind === 'active') {
    return (
      <span className="inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold text-bull border-bull/30 bg-bull/10">
        运行中 · {enabledCount}/{total}
      </span>
    )
  }
  if (kind === 'stopped') {
    return (
      <span className="inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold text-muted border-muted/30 bg-muted/10">
        已停用 · 0/{total}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold text-secondary border-secondary/30 bg-secondary/10">
      占位 (无 schedule)
    </span>
  )
}


function EmptyHint() {
  return (
    <div className="rounded-xl border border-dashed border-border bg-surface/20 px-6 py-12 text-center text-xs text-muted">
      <Trash2 className="mx-auto mb-2 h-5 w-5 opacity-40" />
      <div>还没有注册的策略. 在 <code className="rounded bg-elevated/40 px-1 py-0.5">data/user_data/strategies/live/</code> 放入
        <code className="rounded bg-elevated/40 px-1 py-0.5">.py</code> 文件即可自动发现.
      </div>
    </div>
  )
}

function formatDate(iso: string): string {
  // "2026-09-29T11:04:13..." → "2026-09-29"
  return iso.slice(0, 10)
}
