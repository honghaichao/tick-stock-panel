/**
 * F1-3 + F1-4 (2026-10-13): 实盘详情页 "回测验证历史 + 实盘 vs 验证 drift" 段.
 *
 * - 顶部 verify 报告列表 (按 ts 倒序, 每条: run_id / 摘要 / "应用到实盘" 按钮)
 * - 底部 drift 卡片 (overlap / only_live / only_verify / overlap_pct)
 *
 * 仅 per-strategy 视图显示 (strategyId 必填).
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { History, Activity, CheckCircle2, XCircle, Loader2, AlertCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

export function VerifyAndDriftSection({ strategyId }: { strategyId: string }) {
  const qc = useQueryClient()
  const [syncErr, setSyncErr] = useState<string | null>(null)
  const [syncing, setSyncing] = useState(false)

  // F1-3: verify 报告列表
  const verifyQ = useQuery({
    queryKey: ['trading', 'verify-list', strategyId],
    queryFn: () => api.tradingStrategyVerifyList(strategyId),
    enabled: !!strategyId,
  })

  // F1-4: drift 检测
  const driftQ = useQuery({
    queryKey: ['trading', 'drift', strategyId],
    queryFn: () => api.tradingStrategyDrift(strategyId, 7),
    enabled: !!strategyId,
  })

  const reports = (verifyQ.data?.reports ?? []) as Array<{
    run_id: string
    start_date?: string
    end_date?: string
    simulate_fills?: boolean
    last_rebalance_target?: Record<string, number>
    last_rebalance_date?: string
  }>

  const handleSync = async (runId: string) => {
    setSyncErr(null)
    setSyncing(true)
    try {
      // 先 dry_run → 拿 confirm_token → 提示用户
      const plan = await api.tradingStrategySyncFromVerify(strategyId, runId, true)
      const ok = window.confirm(
        `同步预览 (${plan.target_count} 笔):\n` +
          (plan.drop_reasons ? `\n被风控拦截: ${Object.keys(plan.drop_reasons).length} 笔` : '') +
          (plan.warnings?.length ? `\n警告: ${plan.warnings.join('; ')}` : '') +
          `\n\n确认执行?`,
      )
      if (!ok) {
        setSyncing(false)
        return
      }
      // 真跑 (需要 confirm_token)
      if (!plan.confirm_token) {
        setSyncErr('dry_run 响应缺少 confirm_token, 请检查后端')
        setSyncing(false)
        return
      }
      await api.tradingStrategySyncFromVerify(strategyId, runId, false, plan.confirm_token)
      qc.invalidateQueries({ queryKey: ['trading', 'positions'] })
      qc.invalidateQueries({ queryKey: ['trading', 'orders'] })
      qc.invalidateQueries({ queryKey: ['trading', 'drift', strategyId] })
    } catch (e: any) {
      setSyncErr(String(e?.message ?? e))
    } finally {
      setSyncing(false)
    }
  }

  if (verifyQ.isLoading || driftQ.isLoading) {
    return (
      <section className="rounded-card border border-border bg-surface/50 p-3">
        <div className="flex items-center gap-2 text-[11px] text-muted">
          <Loader2 className="h-3 w-3 animate-spin" />
          加载回测验证 + drift ...
        </div>
      </section>
    )
  }

  if (verifyQ.error || driftQ.error) {
    return (
      <section className="rounded-card border border-danger/30 bg-danger/5 p-3">
        <div className="flex items-center gap-2 text-[11px] text-danger">
          <AlertCircle className="h-3 w-3" />
          加载失败: {String(verifyQ.error ?? driftQ.error)}
        </div>
      </section>
    )
  }

  const drift = driftQ.data
  const driftPct = drift ? Math.round(drift.overlap_pct * 100) : 0
  const driftCls = driftPct >= 80 ? 'text-bull' : driftPct >= 50 ? 'text-warning' : 'text-danger'

  return (
    <section className="rounded-card border border-border bg-surface/50 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold text-foreground flex items-center gap-1.5">
          <History className="h-3.5 w-3.5" />
          回测验证历史 ({reports.length})
        </h3>
        {drift && (
          <div className="flex items-center gap-1.5 text-[10px] text-muted">
            <Activity className="h-3 w-3" />
            drift:
            <span className={cn('font-mono font-semibold', driftCls)}>{driftPct}%</span>
            <span className="text-muted/70">
              ({drift.overlap.length} 重合 / {drift.only_live.length} 仅实盘 / {drift.only_verify.length} 仅验证)
            </span>
          </div>
        )}
      </div>

      {syncErr && (
        <div className="mb-2 flex items-center gap-1.5 rounded-btn border border-danger/30 bg-danger/5 px-2 py-1 text-[10px] text-danger">
          <XCircle className="h-3 w-3" />
          同步失败: {syncErr}
        </div>
      )}

      {reports.length === 0 ? (
        <div className="text-[11px] text-muted">
          暂无 verify 报告 — 可在 "代码" tab 调一次手动 verify 或 "运行历史" tab 触发。
        </div>
      ) : (
        <div className="space-y-1.5">
          {reports.slice(0, 5).map((r) => {
            const syms = Object.keys(r.last_rebalance_target ?? {})
            return (
              <div
                key={r.run_id}
                className="flex items-center justify-between gap-2 rounded-btn border border-border/60 bg-elevated/40 px-2 py-1.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <code className="font-mono text-[10px] text-secondary">{r.run_id}</code>
                    {r.start_date && r.end_date && (
                      <span className="text-[10px] text-muted">
                        {r.start_date} → {r.end_date}
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 text-[10px] text-muted">
                    target: {syms.length} 只
                    {syms.length > 0 && (
                      <span className="ml-1 font-mono">
                        {syms.slice(0, 3).join(', ')}{syms.length > 3 ? ` +${syms.length - 3}` : ''}
                      </span>
                    )}
                  </div>
                </div>
                <button
                  type="button"
                  disabled={syncing || syms.length === 0}
                  onClick={() => handleSync(r.run_id)}
                  className="inline-flex h-6 items-center gap-1 rounded-btn border border-accent/30 bg-accent/10 px-2 text-[10px] font-medium text-accent hover:bg-accent/20 disabled:opacity-50"
                >
                  {syncing ? <Loader2 className="h-3 w-3 animate-spin" /> : <CheckCircle2 className="h-3 w-3" />}
                  应用到实盘
                </button>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
