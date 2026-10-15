/**
 * 救命三件套 (v2 live-trading-v2-pr1):
 *  - 全部撤单 (黄): 撤所有 pending 委托, 不停策略
 *  - 全部停止 (蓝): 停策略 (PUT schedules 全 enabled=false), 留持仓
 *  - 一键平仓 (红): 停策略 + 全部市价卖 (override 5万风控到 1000万)
 *
 * 视觉: 顶栏右上, 颜色编码. 救命按钮的物理位置 (最右) 避免误触.
 * 2 段确认: 第 1 击弹 confirm 弹窗 (写明"动作 + 影响"), 第 2 击真执行.
 */
import { useState } from 'react'
import { AlertTriangle, Ban, Hand, Power } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Modal } from '@/components/Modal'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

type Action = 'cancel-all' | 'stop-all' | 'flatten'

const ACTION_META: Record<Action, { label: string; icon: typeof AlertTriangle; cls: string; confirmMsg: (ctx: string) => string }> = {
  'cancel-all': {
    label: '全部撤单',
    icon: Hand,
    cls: 'border-amber-500/30 bg-amber-500/10 text-amber-700 hover:bg-amber-500/20',
    confirmMsg: (ctx) => `确认要撤销 ${ctx} 全部 pending 委托? 策略仍会运行, 不影响持仓。`,
  },
  'stop-all': {
    label: '全部停止',
    icon: Ban,
    cls: 'border-blue-500/30 bg-blue-500/10 text-blue-700 hover:bg-blue-500/20',
    confirmMsg: (ctx) => `确认要停止 ${ctx} 全部策略 cron? 持仓保留, 但不会再下单。`,
  },
  'flatten': {
    label: '一键平仓',
    icon: AlertTriangle,
    cls: 'border-danger/40 bg-danger/15 text-danger hover:bg-danger/25',
    confirmMsg: (ctx) =>
      `确认要一键平仓 ${ctx} 全部持仓? 这是核弹操作, 不可撤销, 会按市价卖出所有股票, 单笔风控临时提到 1000万。`,
  },
}

interface Props {
  strategyId?: string | null
}

export function EmergencyControls({ strategyId = null }: Props) {
  const [confirm, setConfirm] = useState<Action | null>(null)
  const qc = useQueryClient()
  const ctx = strategyId ? '策略 ' + strategyId + ' 的' : '账户级'

  const cancelAllMut = useMutation({
    mutationFn: () => api.tradingOrdersCancelAll(strategyId ? { strategy_id: strategyId } : undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.tradingOrders(strategyId ?? null) })
      qc.invalidateQueries({ queryKey: QK.tradingStatus })
    },
  })

  const flattenMut = useMutation({
    mutationFn: () => api.tradingPositionsFlatten(strategyId ? { strategy_id: strategyId } : undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.tradingOrders(strategyId ?? null) })
      qc.invalidateQueries({ queryKey: QK.tradingPositions(strategyId ?? null) })
      qc.invalidateQueries({ queryKey: QK.tradingPnlMetrics(strategyId ?? null) })
      qc.invalidateQueries({ queryKey: QK.tradingAccount })
    },
  })

  // 全部停止: 用现有 PUT schedules (全 enabled=false)
  const stopAllMut = useMutation({
    mutationFn: async () => {
      // 拉所有 schedules, 全部 enabled=false 后 PUT 回去
      const r = await fetch(`/api/trading/strategies/${encodeURIComponent(strategyId || '')}/schedules`).then(r => r.json())
      const allDisabled = (r.schedules || []).map((s: any) => ({ ...s, enabled: false }))
      return api.tradingPutStrategySchedules(strategyId || '', allDisabled)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.tradingStrategies })
      qc.invalidateQueries({ queryKey: QK.tradingStatus })
    },
  })

  const isPending = cancelAllMut.isPending || flattenMut.isPending || stopAllMut.isPending

  const execute = () => {
    if (!confirm) return
    if (confirm === 'cancel-all') cancelAllMut.mutate()
    else if (confirm === 'stop-all') stopAllMut.mutate()
    else if (confirm === 'flatten') flattenMut.mutate()
    setConfirm(null)
  }

  return (
    <>
      <div className="flex items-center gap-1.5">
        {(Object.keys(ACTION_META) as Action[]).map((k) => {
          const m = ACTION_META[k]
          const Icon = m.icon
          return (
            <button
              key={k}
              onClick={() => setConfirm(k)}
              disabled={isPending}
              className={cn(
                'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed',
                m.cls,
              )}
              title={m.confirmMsg(ctx)}
            >
              <Icon className="h-3 w-3" />
              {m.label}
            </button>
          )
        })}
      </div>
      {confirm && (
        <Modal onClose={() => setConfirm(null)} panelClassName="max-w-md p-5">
          <div className="mb-3 flex items-center gap-2 text-base font-semibold text-danger">
            <AlertTriangle className="h-4 w-4" />
            确认 {ACTION_META[confirm].label}
          </div>
          <div className="mb-4 text-sm text-secondary leading-relaxed">
            {ACTION_META[confirm].confirmMsg(ctx)}
          </div>
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-[11px] text-danger">
            ⚠ 此操作 {confirm === 'flatten' ? '不可撤销 (按市价成交)' : '可在反向上重启'}, 二次确认后即执行。
          </div>
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => setConfirm(null)}
              disabled={isPending}
              className="rounded-md border border-border bg-surface/40 px-3 py-1.5 text-xs text-secondary hover:bg-elevated/40 cursor-pointer"
            >
              取消
            </button>
            <button
              onClick={execute}
              disabled={isPending}
              className="inline-flex items-center gap-1 rounded-md border border-danger bg-danger px-3 py-1.5 text-xs font-medium text-white hover:bg-danger/90 cursor-pointer disabled:opacity-50"
            >
              <Power className="h-3 w-3" />
              {isPending ? '执行中...' : `确认 ${ACTION_META[confirm].label}`}
            </button>
          </div>
        </Modal>
      )}
    </>
  )
}
