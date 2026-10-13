import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Play, Save, History, AlertCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

interface Props {
  strategyId: string
}

export function CodeView({ strategyId }: Props) {
  const qc = useQueryClient()
  const codeQ = useQuery({
    queryKey: ['trading', 'strategy', strategyId, 'code'],
    queryFn: () => api.tradingStrategyCode(strategyId),
    enabled: !!strategyId,
  })
  const changelogQ = useQuery({
    queryKey: ['trading', 'strategy', strategyId, 'code', 'changelog'],
    queryFn: () => api.tradingStrategyCodeChangelog(strategyId, { limit: 20 }),
    enabled: !!strategyId,
  })

  const [content, setContent] = useState('')
  const [note, setNote] = useState('')
  const [runAfter, setRunAfter] = useState(false)

  useEffect(() => {
    if (codeQ.data) setContent(codeQ.data.content)
  }, [codeQ.data])

  const saveMut = useMutation({
    mutationFn: () => api.tradingStrategyCodePut(strategyId, { content, note, run_after: runAfter }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['trading', 'strategy', strategyId, 'code'] })
      qc.invalidateQueries({ queryKey: ['trading', 'strategy', strategyId, 'code', 'changelog'] })
    },
  })

  if (codeQ.isLoading) {
    return <div className="py-12 text-center text-xs text-muted">加载中…</div>
  }
  if (codeQ.error) {
    return (
      <div className="rounded-xl border border-border bg-surface/30 p-4 text-sm text-danger">
        无法读取策略文件: {String(codeQ.error)}
      </div>
    )
  }

  const lines = content.split('\n')
  const dirty = codeQ.data && codeQ.data.content !== content

  return (
    <div className="space-y-4">
      {/* 编辑区 */}
      <div className="rounded-xl border border-border bg-surface/30">
        <div className="flex items-center justify-between border-b border-border/60 px-3 py-2">
          <div className="flex items-center gap-3">
            <span className="text-xs font-semibold">{strategyId}.py</span>
            {dirty && <span className="rounded bg-warning/10 px-1.5 py-0.5 text-[10px] text-warning">有未保存的修改</span>}
            <span className="text-[10px] text-muted">{lines.length} 行 / {content.length} 字节</span>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={note}
              onChange={e => setNote(e.target.value)}
              placeholder="备注 (可选)"
              className="h-7 w-40 rounded border border-border bg-surface/30 px-2 text-xs"
            />
            <label className="flex items-center gap-1 text-[11px] text-secondary">
              <input
                type="checkbox"
                checked={runAfter}
                onChange={e => setRunAfter(e.target.checked)}
                className="h-3 w-3"
              />
              替换并运行
            </label>
            <button
              onClick={() => saveMut.mutate()}
              disabled={!dirty || saveMut.isPending}
              className={cn(
                'inline-flex h-7 items-center gap-1 rounded-md px-2 text-[11px] font-medium transition-colors',
                !dirty || saveMut.isPending
                  ? 'bg-muted/20 text-muted cursor-not-allowed'
                  : runAfter
                  ? 'bg-bull/20 text-bull hover:bg-bull/30'
                  : 'bg-accent/20 text-accent hover:bg-accent/30'
              )}
            >
              {runAfter ? <Play className="h-3 w-3" /> : <Save className="h-3 w-3" />}
              {saveMut.isPending ? '保存中…' : runAfter ? '替换并运行' : '保存'}
            </button>
          </div>
        </div>
        <div className="flex max-h-[500px] overflow-y-auto">
          {/* 行号 */}
          <div className="select-none border-r border-border/40 bg-surface/50 px-2 py-2 text-right font-mono text-[10px] text-muted">
            {lines.map((_, i) => (
              <div key={i} className="leading-5">{i + 1}</div>
            ))}
          </div>
          {/* 代码编辑 (textarea 覆盖在行号上方, 实现编辑功能) */}
          <textarea
            value={content}
            onChange={e => setContent(e.target.value)}
            spellCheck={false}
            className="flex-1 resize-none bg-transparent px-3 py-2 font-mono text-[12px] leading-5 text-foreground focus:outline-none"
            style={{ minHeight: `${lines.length * 20 + 8}px` }}
          />
        </div>
        {saveMut.error && (
          <div className="border-t border-danger/30 bg-danger/5 px-3 py-2 text-[11px] text-danger">
            <AlertCircle className="mr-1 inline h-3 w-3" />
            {String((saveMut.error as Error).message || saveMut.error)}
          </div>
        )}
        {saveMut.data && (
          <div className="border-t border-bull/30 bg-bull/5 px-3 py-2 text-[11px] text-bull">
            ✓ 已保存 ({saveMut.data.entry?.diff_summary || `${saveMut.data.size} 字节`})
          </div>
        )}
      </div>

      {/* 变更记录 */}
      <div className="rounded-xl border border-border bg-surface/30 p-3">
        <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold">
          <History className="h-3.5 w-3.5" />
          代码变更记录
        </div>
        {changelogQ.isLoading ? (
          <div className="py-4 text-center text-[11px] text-muted">加载中…</div>
        ) : !changelogQ.data || changelogQ.data.entries.length === 0 ? (
          <div className="py-4 text-center text-[11px] text-muted">暂无变更记录</div>
        ) : (
          <ul className="divide-y divide-border/30 text-[11px]">
            {changelogQ.data.entries.map((e, i) => (
              <li key={i} className="py-1.5">
                <div className="flex items-center gap-2">
                  <code className="rounded bg-elevated/40 px-1 py-0.5 font-mono text-[10px] text-secondary">
                    {e.ts.replace('T', ' ').slice(0, 19)}
                  </code>
                  <span className="text-secondary">{e.diff_summary}</span>
                  <span className="text-muted">({e.old_size} → {e.new_size})</span>
                </div>
                {e.note && <div className="mt-0.5 text-muted">备注: {e.note}</div>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
