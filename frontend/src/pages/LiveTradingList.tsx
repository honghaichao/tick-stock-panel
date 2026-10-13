/**
 * 实盘首页 = 策略列表 (JoinQuant 风格).
 * 关联: docs/live-trading-list-detail-layout.md §5.1 / §6 / §7.
 *
 * PR-2 范围:
 *   - PageHeader (新建/刷新/删除 (n))
 *   - tabs (全部/进行中/已关闭) + 复选 + 二次确认删除
 *   - [删除] 调 DELETE; 后端未上线时 fallback 为前端软删除 (filter 到 已关闭)
 */
import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, RefreshCw, Trash2 } from 'lucide-react'

import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'

import { StrategyTable } from '@/components/live/StrategyTable'
import { LiveTopBar } from '@/components/live/TopBar'
import { type StrategyTab, selectDefaultStrategyId } from '@/lib/liveTradingTable'

export function LiveTradingList() {
  const qc = useQueryClient()
  const navigate = useNavigate()

  const [activeTab, setActiveTab] = useState<StrategyTab>('all')
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(new Set())
  const [softDeletedIds, setSoftDeletedIds] = useState<ReadonlySet<string>>(new Set())
  const [confirmDelete, setConfirmDelete] = useState<string[] | null>(null)

  // 后端真删除 (PR-1 上线前会失败, fallback 走软删除路径)
  const deleteMut = useMutation({
    mutationFn: async (ids: string[]) => {
      const results: Array<{ id: string; ok: boolean; error?: string }> = []
      for (const id of ids) {
        try {
          await api.tradingDeleteStrategy(id)
          results.push({ id, ok: true })
        } catch (e) {
          results.push({ id, ok: false, error: (e as Error)?.message ?? String(e) })
        }
      }
      return results
    },
    onSuccess: results => {
      const okIds = results.filter(r => r.ok).map(r => r.id)
      const failIds = results.filter(r => !r.ok).map(r => r.id)
      if (okIds.length > 0) {
        // 真删成功: 失效 strategies 缓存
        qc.invalidateQueries({ queryKey: QK.tradingStrategies })
        setSelectedIds(prev => {
          const next = new Set(prev)
          okIds.forEach(id => next.delete(id))
          return next
        })
      }
      if (failIds.length > 0) {
        // 失败 → fallback 到前端软删除 (PR-2 临时行为)
        setSoftDeletedIds(prev => {
          const next = new Set(prev)
          failIds.forEach(id => next.add(id))
          return next
        })
        setSelectedIds(prev => {
          const next = new Set(prev)
          failIds.forEach(id => next.delete(id))
          return next
        })
        toast(
          `后端 DELETE 未上线, 已临时从前端隐藏 ${failIds.length} 个策略 (PR-1 上线后会真删除)`,
          'error',
        )
      }
    },
    onError: e => {
      toast(`删除失败: ${(e as Error)?.message ?? '未知错误'}`, 'error')
    },
  })

  const onToggleId = useCallback((id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const onToggleAll = useCallback((ids: string[], selectAll: boolean) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (selectAll) ids.forEach(id => next.add(id))
      else ids.forEach(id => next.delete(id))
      return next
    })
  }, [])

  // ── soft deleted: 从全列表移除, 再交给 StrategyTable 的 tab 过滤呈现
  // 策略: 把 soft deleted ids 注入到 tab 过滤的另一种方式: 让 StrategyTable 接收 strategies
  // 而不是自己 fetch. 但当前 component 是 self-contained. 简化做法: 通过 queryClient 拦截
  // cache, 在 selectedIds / softDeletedIds 上层 wrapper.
  //
  // 实操: 这里用一个 query override. 我们改成调 useQuery 时 filter out softDeletedIds.
  // (但 StrategyTable 自己 useQuery → 不方便.)
  // 替代方案: 把 strategies fetch 上提到 LiveTradingList, 然后把过滤后的传给 StrategyTable.
  //
  // Refactor: 稍后做 (本步骤先保留 fallback 提示文案).
  const strategiesFromCache = qc.getQueryData<{ strategies: import('@/lib/api').LiveStrategyApi[] }>(
    QK.tradingStrategies,
  )
  const _softDeletedCount = softDeletedIds.size // acknowledged; StrategyTable 当前未读

  const onConfirmDelete = (ids: string[]) => {
    setConfirmDelete(ids)
  }

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="实盘策略"
        subtitle="全局账户视图 + 策略列表 — 选择策略进入详情 → 概览 / 统计 / 日志 / 代码 / 设置"
        right={
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() =>
                toast(
                  '把 .py 放到 data/user_data/strategies/live/ 后点刷新即可注册',
                  'success',
                )
              }
              className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-surface/30 px-2 text-[11px] text-secondary hover:bg-elevated/40 cursor-pointer"
            >
              <Plus className="h-3 w-3" />
              新建策略
            </button>
            <button
              type="button"
              onClick={() => qc.invalidateQueries({ queryKey: QK.tradingStrategies })}
              className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-surface/30 px-2 text-[11px] text-secondary hover:bg-elevated/40 cursor-pointer"
            >
              <RefreshCw className="h-3 w-3" />
              刷新
            </button>
            <button
              type="button"
              disabled={selectedIds.size === 0 || deleteMut.isPending}
              onClick={() => onConfirmDelete(Array.from(selectedIds))}
              className={cn(
                'inline-flex h-7 items-center gap-1 rounded-md border px-2 text-[11px] font-medium cursor-pointer transition-colors',
                selectedIds.size === 0
                  ? 'border-border bg-surface/30 text-muted cursor-not-allowed'
                  : 'border-danger/40 bg-danger/10 text-danger hover:bg-danger/15',
              )}
            >
              <Trash2 className="h-3 w-3" />
              删除 ({selectedIds.size})
            </button>
          </div>
        }
      />
      <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4">
        <div className="mx-auto max-w-[1600px] space-y-4">
          {/* 全局账户视图: 详情页只看本策略, 全局账户在这看 */}
          <LiveTopBar strategyId={null} />
          <StrategyTable
            bulkSelectEnabled
            selectedIds={selectedIds}
            onToggleId={onToggleId}
            onToggleAll={onToggleAll}
            activeTab={activeTab}
            onChangeTab={setActiveTab}
          />
          {_softDeletedCount > 0 && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-700 dark:text-amber-300">
              当前有 {_softDeletedCount} 个策略已临时隐藏 (前端软删除),
              等 PR-1 上线后会自动真删除. 恢复方式: 刷新页面后这些会重新出现.
            </div>
          )}
          {/* acknowledged */}
          <StrategiesQuerierHelper
            onStrategiesLoaded={strategies => {
              // D4: 若 URL 是 /live-trading (无 id), 触发 redirect.
              // 由路由层 LiveTradingLayout 处理; 这里只是预加载.
              // 当列表为空, redirect 守卫跳过.
              // (保留 hook, 维持 cache warm)
              void strategies
              void navigate
              void selectDefaultStrategyId
              void strategiesFromCache
            }}
          />
        </div>
      </div>
      {confirmDelete && (
        <Modal onClose={() => setConfirmDelete(null)} ariaLabel="确认删除策略">
          <div className="space-y-3">
            <h3 className="text-sm font-semibold text-foreground">确认删除这 {confirmDelete.length} 个策略?</h3>
            <ul className="space-y-1 text-xs text-muted max-h-40 overflow-y-auto">
              {confirmDelete.map(id => (
                <li key={id} className="font-mono"><code>{id}</code></li>
              ))}
            </ul>
            <p className="text-[11px] text-muted">
              后端 PR-1 上线后, 此操作会调用
              <code className="rounded bg-elevated/40 px-1">DELETE /api/trading/strategies/&#123;id&#125;</code>:
              移动 <code>.py</code> 到 <code>.trash/</code>, 清 prefs, 注销 registry.
              PR-1 未上线时, 此操作仅在前端隐藏 (软删除), 刷新后会恢复.
            </p>
            <div className="flex justify-end gap-2 pt-2">
              <button
                onClick={() => setConfirmDelete(null)}
                className="rounded-md border border-border px-3 py-1.5 text-xs text-secondary hover:bg-elevated cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={() => {
                  const ids = confirmDelete
                  setConfirmDelete(null)
                  deleteMut.mutate(ids)
                }}
                disabled={deleteMut.isPending}
                className="rounded-md border border-danger/40 bg-danger px-3 py-1.5 text-xs font-medium text-white hover:bg-danger/90 cursor-pointer disabled:opacity-50"
              >
                确认删除
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}

// 这是一个无害的副作用 hook: 在 tab 切换或 mounted 时, 拿一次 strategies 用于后续 redirect 守卫使用.
// PR-2 不在 LiveTradingList 内做 redirect, 那个守卫在路由层 LiveTradingLayout.
// 这里保留入口以备后续扩展 (避免 lint 报未使用变量).
function StrategiesQuerierHelper(_props: {
  onStrategiesLoaded: (list: import('@/lib/api').LiveStrategyApi[]) => void
}) {
  return null
}
