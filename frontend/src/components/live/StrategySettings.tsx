import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

interface Props {
  strategyId: string
}

export function StrategySettings({ strategyId }: Props) {
  const qc = useQueryClient()
  const strategiesQ = useQuery({ queryKey: QK.tradingStrategies, queryFn: api.tradingStrategies })
  const prefsQ = useQuery({ queryKey: QK.tradingPreferences, queryFn: api.tradingPreferences })

  const strategy = strategiesQ.data?.strategies.find(s => s.id === strategyId)
  const prefs = prefsQ.data

  const [editingSchedules, setEditingSchedules] = useState<{ name: string; hour: number; minute: number; enabled: boolean; day_of_week: string; kind: 'daily' | 'weekly' | 'monthly' | 'manual' }[]>([])
  const [editing, setEditing] = useState(false)
  const [confirmRun, setConfirmRun] = useState<string | null>(null)

  const putSchedulesMut = useMutation({
    mutationFn: () => api.tradingPutStrategySchedules(strategyId, editingSchedules),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.tradingStrategies })
      setEditing(false)
    },
  })

  const runNowMut = useMutation({
    mutationFn: (event: string) => api.tradingRunStrategyNow(strategyId, event),
  })

  if (!strategy) {
    return <div className="py-12 text-center text-xs text-muted">加载策略信息中…</div>
  }

  const startEdit = () => {
    setEditingSchedules(strategy.schedules.map(s => ({
      name: s.name,
      hour: s.hour,
      minute: s.minute,
      enabled: s.enabled,
      day_of_week: s.day_of_week || 'mon-fri',
      kind: (s.kind || 'daily') as 'daily' | 'weekly' | 'monthly' | 'manual',
    })))
    setEditing(true)
  }

  return (
    <div className="space-y-4">
      {/* 基本信息 */}
      <div className="rounded-xl border border-border bg-surface/30 p-4">
        <h3 className="mb-3 text-sm font-semibold">策略信息</h3>
        <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
          <Field label="策略 ID"><code className="rounded bg-elevated/40 px-1.5 py-0.5">{strategy.id}</code></Field>
          <Field label="名称">{strategy.name}</Field>
          <Field label="默认启用">{strategy.enabled_default ? '是' : '否'}</Field>
          <Field label="定时任务数">{strategy.schedules.length}</Field>
          <Field label="下次运行">{strategy.next_run ? strategy.next_run.replace('T', ' ').slice(0, 19) : '—'}</Field>
          <Field label="初始资金">¥{(prefs?.trading_initial_cash ?? 0).toLocaleString()}</Field>
        </dl>
      </div>

      {/* 定时任务编辑 */}
      <div className="rounded-xl border border-border bg-surface/30 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">定时任务 (schedules)</h3>
          {!editing ? (
            <button
              onClick={startEdit}
              className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-surface/30 px-2 text-[11px] hover:bg-elevated/40"
            >
              编辑
            </button>
          ) : (
            <div className="flex gap-1">
              <button
                onClick={() => setEditing(false)}
                className="inline-flex h-7 items-center rounded-md border border-border bg-surface/30 px-2 text-[11px] hover:bg-elevated/40"
              >取消</button>
              <button
                onClick={() => putSchedulesMut.mutate()}
                disabled={putSchedulesMut.isPending}
                className="inline-flex h-7 items-center rounded-md bg-accent/20 px-2 text-[11px] text-accent hover:bg-accent/30 disabled:opacity-50"
              >{putSchedulesMut.isPending ? '保存中…' : '保存'}</button>
            </div>
          )}
        </div>

        {editing ? (
          <div className="space-y-2">
            {editingSchedules.map((s, i) => (
              <div key={i} className="grid grid-cols-12 items-center gap-2 rounded border border-border/40 bg-surface/30 p-2 text-xs">
                <input
                  type="text" value={s.name} readOnly
                  className="col-span-2 rounded border border-border bg-surface/50 px-2 py-1 font-mono"
                />
                <select
                  value={s.kind}
                  onChange={e => {
                    const next = [...editingSchedules]
                    next[i] = { ...s, kind: e.target.value as 'daily' | 'weekly' | 'monthly' }
                    setEditingSchedules(next)
                  }}
                  className="col-span-2 rounded border border-border bg-surface/50 px-1 py-1"
                >
                  <option value="daily">daily</option>
                  <option value="weekly">weekly</option>
                  <option value="monthly">monthly</option>
                </select>
                <input
                  type="number" min={0} max={23} value={s.hour}
                  onChange={e => {
                    const next = [...editingSchedules]
                    next[i] = { ...s, hour: Number(e.target.value) }
                    setEditingSchedules(next)
                  }}
                  className="col-span-1 rounded border border-border bg-surface/50 px-1 py-1 text-center"
                />
                <span className="text-muted">:</span>
                <input
                  type="number" min={0} max={59} value={s.minute}
                  onChange={e => {
                    const next = [...editingSchedules]
                    next[i] = { ...s, minute: Number(e.target.value) }
                    setEditingSchedules(next)
                  }}
                  className="col-span-1 rounded border border-border bg-surface/50 px-1 py-1 text-center"
                />
                <select
                  value={s.day_of_week}
                  onChange={e => {
                    const next = [...editingSchedules]
                    next[i] = { ...s, day_of_week: e.target.value }
                    setEditingSchedules(next)
                  }}
                  className="col-span-3 rounded border border-border bg-surface/50 px-1 py-1"
                >
                  <option value="mon-fri">mon-fri</option>
                  <option value="*">每天</option>
                </select>
                <label className="col-span-2 flex items-center gap-1 text-[11px]">
                  <input
                    type="checkbox" checked={s.enabled}
                    onChange={e => {
                      const next = [...editingSchedules]
                      next[i] = { ...s, enabled: e.target.checked }
                      setEditingSchedules(next)
                    }}
                  />
                  启用
                </label>
              </div>
            ))}
          </div>
        ) : strategy.schedules.length === 0 ? (
          <div className="py-4 text-center text-[11px] text-muted">无定时任务</div>
        ) : (
          <table className="w-full text-xs">
            <thead className="border-b border-border/60 text-[10px] uppercase text-muted">
              <tr>
                <th className="px-2 py-1 text-left">事件</th>
                <th className="px-2 py-1 text-left">类型</th>
                <th className="px-2 py-1 text-center">时间</th>
                <th className="px-2 py-1 text-left">星期</th>
                <th className="px-2 py-1 text-center">启用</th>
                <th className="px-2 py-1 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {strategy.schedules.map((s, i) => (
                <tr key={i} className="border-b border-border/30 last:border-0">
                  <td className="px-2 py-1.5 font-mono">{s.name}</td>
                  <td className="px-2 py-1.5">{s.kind || 'daily'}</td>
                  <td className="px-2 py-1.5 text-center font-mono">{String(s.hour).padStart(2, '0')}:{String(s.minute).padStart(2, '0')}</td>
                  <td className="px-2 py-1.5 font-mono text-[10px]">{s.day_of_week}</td>
                  <td className="px-2 py-1.5 text-center">{s.enabled ? '✓' : '✗'}</td>
                  <td className="px-2 py-1.5 text-right">
                    <button
                      onClick={() => {
                        if (confirmRun === s.name) {
                          runNowMut.mutate(s.name)
                          setConfirmRun(null)
                        } else {
                          setConfirmRun(s.name)
                          setTimeout(() => setConfirmRun(null), 3000)
                        }
                      }}
                      disabled={!s.enabled || runNowMut.isPending}
                      className={cn(
                        'inline-flex h-6 items-center rounded px-1.5 text-[10px] transition-colors',
                        confirmRun === s.name
                          ? 'bg-warning/20 text-warning'
                          : 'bg-accent/10 text-accent hover:bg-accent/20'
                      )}
                    >
                      {confirmRun === s.name ? '确认?' : '跑'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {putSchedulesMut.error && (
          <div className="mt-2 text-[11px] text-danger">保存失败: {String(putSchedulesMut.error)}</div>
        )}
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-0.5 text-foreground">{children}</div>
    </div>
  )
}
