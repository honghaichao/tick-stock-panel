/**
 * 实盘交易页 — 概览 tab.
 *
 * 包含: PositionsGrid (v2 PR1 卡片网格) / OrdersTable (+OrderDialog) / TradesTable /
 * LogsList / PnLPanel (+EquityCurve) / StrategiesPanel (+子组件) /
 * StrategyRunHistoryPanel / VerifyAndDriftSection (?view=verify 分支渲染).
 * v2 PR1 收口: 持仓卡片化 + 删废弃 4-tab 中栏 + 删右下消息轮询 (合并到顶栏 Bell).
 * 详见 docs/live-trading-v2-pr1-nav-and-wire.md.
 *
 * 拆分自 LiveTrading.tsx (PR2). 详见 docs/live-trading-v2-pr2-decompose-and-wire.md.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, AlertCircle, ChevronDown, ChevronRight, History, LineChart as LineChartIcon, ListChecks, Loader2, Play, Plus, Receipt, Trash2, TrendingDown, TrendingUp, Wallet, Zap } from 'lucide-react'
import type { LiveStrategyApi, PnLSnapshotApi, StrategyRunReportApi } from '@/lib/api'
import { api, type OrderApi, type OrderDraftApi, type PositionApi } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, fmtVolume, priceColorClass } from '@/lib/format'
import { VerifyAndDriftSection } from '@/components/live/VerifyAndDriftSection'
import { PositionCard } from '@/components/live/PositionCard'
import { Card, EmptyHint, Field, KindBadge, Pill, Section, Stat, SymbolCell } from '@/components/live/shared'
import { strategyIsExpanded, splitStrategiesByStatus } from '@/lib/liveTradingTable'
import { toNavItems } from '@/lib/listNav'


const ts = (s: string | null | undefined) => (s ? s.replace('T', ' ').slice(0, 19) : '—')

const statusLabel: Record<OrderApi['status'], string> = {
  new: '已受理',
  partial: '部分成交',
  filled: '已成交',
  canceled: '已撤单',
  rejected: '已拒单',
}

const statusCls: Record<OrderApi['status'], string> = {
  new: 'text-secondary bg-secondary/10 border-secondary/30',
  partial: 'text-secondary bg-secondary/10 border-secondary/30',
  filled: 'text-bull bg-bull/10 border-bull/30',
  canceled: 'text-muted bg-muted/10 border-muted/30',
  rejected: 'text-danger bg-danger/10 border-danger/30',
}



function pad(n: number): string {
  return String(n).padStart(2, '0')
}

// 占位图标别名 (UI 里引用了但未在上面 import)
const LifeLine = LineChartIcon

/**
 * 持仓卡片网格 — v2 PR1.
 *
 * 取代 v1 的 10 列横排方案 (仓内 todo 旧 PR). 1 行 1 票卡片 (1/2/3/4 列自适应),
 * hero 数字是 PnL + 5 日分时 sparkline; 卖 1/3 / 卖 1/2 / 全卖 快捷按钮接
 * PositionCard (本轮不接 OrderDialog, PR2 才接).
 */
function PositionsGrid({
  strategyId,
  onQuickTrade,
}: {
  strategyId?: string | null
  onQuickTrade: (side: 'sell', ratio: 1 | 2 | 'all', symbol: string) => void
}) {
  const positionsQ = useQuery({
    queryKey: QK.tradingPositions(strategyId ?? null),
    queryFn: () => api.tradingPositions(strategyId ? { strategy_id: strategyId } : undefined),
    refetchInterval: 6000,
  })
  const positions = positionsQ.data?.positions ?? []
  const allSymbols = useMemo(() => Array.from(new Set(positions.map(p => p.symbol))), [positions])
  const namesQ = useQuery({
    queryKey: ['instrument-names-grid', allSymbols.join(',')],
    queryFn: () => api.instrumentNames(allSymbols),
    enabled: allSymbols.length > 0,
    staleTime: 300_000,
  })
  const names = namesQ.data?.names ?? {}
  if (positions.length === 0) {
    return <EmptyHint>还没有持仓。提交一笔买入委托或新增批次提醒后, 这里会出现对应记录。</EmptyHint>
  }
  return (
    <Section title="当前持仓" subtitle={`${positions.length} 个标的 · 1 行 1 票卡片`} icon={Wallet}>
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-3">
        {positions.map(p => (
          <PositionCard
            key={p.symbol}
            position={p}
            name={names[p.symbol]}
            onQuickTrade={onQuickTrade}
          />
        ))}
      </div>
    </Section>
  )
}


function OrdersTable({
  strategyId,
  onOpenBuy,
  onOpenSell,
}: {
  strategyId?: string | null
  onOpenBuy: () => void
  onOpenSell: () => void
}) {
  const ordersQ = useQuery({
    queryKey: QK.tradingOrders(strategyId ?? null),
    queryFn: () => api.tradingOrders({ strategy_id: strategyId ?? undefined, limit: 200 }),
    refetchInterval: 6000,
  })
  const orders = ordersQ.data?.orders ?? []
  const [confirmCancelId, setConfirmCancelId] = useState<string | null>(null)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)

  // 4 秒 timeout 兜底 (v2 PR3): 避免网络 hang 时按钮粘住.
  // 用户在 4 秒内点 trash 切换其它单时, onClick 里先 clearTimeout 避免状态被旧 timer 清.
  const cancelTimerRef = useRef<number | undefined>(undefined)
  const cancelMut = useMutation({
    mutationFn: (id: string) => api.tradingOrderCancel(id),
    onSettled: () => {
      if (typeof window === 'undefined') return
      window.clearTimeout(cancelTimerRef.current)
      cancelTimerRef.current = window.setTimeout(() => {
        setConfirmCancelId(null)
        cancelMut.reset()
      }, 4000)
    },
  })
  useEffect(() => () => {
    if (typeof window !== 'undefined') window.clearTimeout(cancelTimerRef.current)
  }, [])

  // 用一个简短 reset 定时器: 3 秒后自动复位确认按钮
  const pending = orders.filter(o => o.status === 'new' || o.status === 'partial')
  const allSymbols = useMemo(() => Array.from(new Set(orders.map(o => o.symbol))), [orders])
  const namesQ = useQuery({
    queryKey: ['instrument-names-orders', allSymbols.join(',')],
    queryFn: () => api.instrumentNames(allSymbols),
    enabled: allSymbols.length > 0,
    staleTime: 300_000,
  })
  const names = namesQ.data?.names ?? {}
  const navItems = useMemo(
    () => toNavItems(allSymbols.map(s => ({ symbol: s, name: names[s] }))),
    [allSymbols, names],
  )

  return (
    <Section title="委托" subtitle={`${pending.length} 个未完成 / 共 ${orders.length} 个`} icon={ListChecks}
      actions={
        <>
          <button onClick={onOpenBuy}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-bull/30 bg-bull/10 px-3 text-xs font-medium text-bull transition-colors hover:bg-bull/15 cursor-pointer">
            <Plus className="h-3.5 w-3.5" />买入
          </button>
          <button onClick={onOpenSell}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-bear/30 bg-bear/10 px-3 text-xs font-medium text-bear transition-colors hover:bg-bear/15 cursor-pointer">
            <Plus className="h-3.5 w-3.5" />卖出
          </button>
        </>
      }>
      {orders.length === 0 ? (
        <EmptyHint>还没有委托。点击上方"买入/卖出"手动下单 (按分钟撮合)。</EmptyHint>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-surface/40 shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border/60 bg-surface/60 text-[10px] uppercase tracking-wide text-muted">
                  <th className="px-3 py-2 font-medium">标的</th>
                  <th className="px-2 py-2 font-medium text-center">方向</th>
                  <th className="px-2 py-2 font-medium text-center">类型</th>
                  <th className="px-2 py-2 font-medium text-right">数量</th>
                  <th className="px-2 py-2 font-medium text-right">价格</th>
                  <th className="px-2 py-2 font-medium text-center">状态</th>
                  <th className="px-3 py-2 font-medium">提交 / 成交</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {orders.map(o => (
                  <tr key={o.id} className="border-b border-border/40 last:border-0 hover:bg-elevated/40">
                    <td className="px-3 py-2.5">
                      <SymbolCell symbol={o.symbol} name={names[o.symbol]} onPreview={() => setPreviewSymbol(o.symbol)} />
                    </td>
                    <td className="px-2 py-2.5 text-center">
                      <span className={cn('inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-semibold border',
                        o.side === 'buy' ? 'text-bull border-bull/30 bg-bull/10' : 'text-bear border-bear/30 bg-bear/10')}>
                        {o.side === 'buy' ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
                        {o.side === 'buy' ? '买' : '卖'}
                      </span>
                    </td>
                    <td className="px-2 py-2.5 text-center text-secondary">{o.order_type === 'limit' ? '限价' : '市价'}</td>
                    <td className="px-2 py-2.5 text-right font-mono text-secondary">
                      {o.filled_qty > 0 && o.filled_qty < o.qty
                        ? `${fmtVolume(o.filled_qty)} / ${fmtVolume(o.qty)}`
                        : fmtVolume(o.qty)}
                    </td>
                    <td className="px-2 py-2.5 text-right font-mono text-foreground">
                      {o.order_type === 'limit' ? (o.limit_price != null ? fmtPrice(o.limit_price) : '—') : '市价'}
                      {o.avg_fill_price != null && (
                        <div className="text-[10px] text-bull">成交 {fmtPrice(o.avg_fill_price)}</div>
                      )}
                    </td>
                    <td className="px-2 py-2.5 text-center">
                      <span className={cn('inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold border',
                        statusCls[o.status])}>
                        {statusLabel[o.status]}
                      </span>
                      {o.reject_reason && (
                        <div className="mt-0.5 text-[10px] text-danger">{o.reject_reason}</div>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-muted text-[11px]">
                      <div>{ts(o.created_at)}</div>
                      {o.filled_at && <div className="text-bull">成交 {ts(o.filled_at)}</div>}
                      {o.canceled_at && <div className="text-muted">撤单 {ts(o.canceled_at)}</div>}
                    </td>
                    <td className="px-3 py-2.5">
                      {(o.status === 'new' || o.status === 'partial') && (
                        confirmCancelId === o.id ? (
                          <button
                            onClick={() => cancelMut.mutate(o.id)}
                            disabled={cancelMut.isPending}
                            className="inline-flex items-center gap-1 rounded-md bg-danger/15 px-1.5 py-0.5 text-[10px] font-medium text-danger border border-danger/30 cursor-pointer">
                            <Trash2 className="h-2.5 w-2.5" />确认撤单
                          </button>
                        ) : (
                          <button onClick={() => {
                            if (typeof window !== 'undefined') window.clearTimeout(cancelTimerRef.current)
                            setConfirmCancelId(o.id)
                          }}
                            title="撤单"
                            className="p-1.5 rounded-md text-secondary transition-all hover:bg-danger/10 hover:text-danger cursor-pointer">
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        )
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol ? names[previewSymbol] : undefined}
        navList={navItems}
        onNavigate={(s) => setPreviewSymbol(s)}
        onClose={() => setPreviewSymbol(null)}
      />
    </Section>
  )
}

function OrderDialog({
  side,
  onClose,
  initial,
  feeRate,
  lastPrice,
}: {
  side: 'buy' | 'sell'
  onClose: () => void
  initial?: { symbol?: string; qty?: number }
  feeRate?: number | null
  lastPrice?: number | null
}) {
  const qc = useQueryClient()
  const [symbolQuery, setSymbolQuery] = useState('')
  const [symbol, setSymbol] = useState(initial?.symbol ?? '')
  const [orderType, setOrderType] = useState<'limit' | 'market'>('limit')
  const [qtyText, setQtyText] = useState(initial?.qty != null ? String(initial.qty) : '100')
  const [priceText, setPriceText] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const stockSearchQ = useQuery({
    queryKey: ['instrument-search', symbolQuery, 'stock,etf'],
    queryFn: () => symbolQuery.length >= 1 ? api.instrumentSearch(symbolQuery, 8, 'stock,etf') : Promise.resolve({ results: [] }),
    enabled: symbolQuery.length >= 1 && !symbol,
    staleTime: 60_000,
  })
  const stockSearch = { results: stockSearchQ.data?.results ?? [] }

  const submitMut = useMutation({
    mutationFn: (draft: OrderDraftApi) => api.tradingOrderSubmit(draft),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['trading'] })
      onClose()
    },
  })

  const qty = Number(qtyText)
  const price = priceText ? Number(priceText) : null
  const canSubmit = !!symbol && qty > 0 && (orderType === 'market' || (price != null && price > 0))

  const onSubmit = () => {
    if (!canSubmit) return
    setSubmitting(true)
    submitMut.mutate(
      {
        symbol,
        side,
        order_type: orderType,
        qty: Math.floor(qty),
        limit_price: orderType === 'limit' ? (price ?? null) : null,
        note: note.trim() || null,
      },
      { onSettled: () => setSubmitting(false) },
    )
  }

  return (
    <Modal onClose={onClose} ariaLabel={side === 'buy' ? '买入下单' : '卖出下单'}>
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-foreground">{side === 'buy' ? '买入下单' : '卖出下单'}</h3>
      </div>
      <div className="space-y-3">
        <div className="flex items-center gap-2 text-xs">
          <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-semibold border"
            style={{ color: side === 'buy' ? 'var(--color-bull, #16a34a)' : 'var(--color-bear, #dc2626)',
                     borderColor: side === 'buy' ? 'var(--color-bull, #16a34a)' : 'var(--color-bear, #dc2626)' }}>
            {side === 'buy' ? '买入' : '卖出'}
          </span>
          <span className="text-muted">{side === 'buy' ? '现金扣减 + 增加持仓' : '可卖数量扣减 + 增加现金 (T+1)'}</span>
        </div>

        <Field label="标的代码">
          <div className="relative">
            <input
              value={symbol || symbolQuery}
              onChange={e => { setSymbolQuery(e.target.value); if (!symbol) setSymbolQuery(e.target.value) }}
              onFocus={() => setSymbolQuery(symbol || symbolQuery)}
              placeholder="例如 600519.SH"
              className="w-full rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
            {stockSearch.results.length > 0 && symbolQuery && !symbol && (
              <ul className="absolute z-10 mt-1 max-h-40 w-full overflow-auto rounded-md border border-border bg-surface shadow-lg">
                {stockSearch.results.slice(0, 8).map(r => (
                  <li key={r.symbol}
                    onClick={() => { setSymbol(r.symbol); setSymbolQuery('') }}
                    className="cursor-pointer px-2.5 py-1 text-xs hover:bg-elevated">
                    <span className="font-mono text-foreground">{r.symbol}</span>
                    <span className="ml-2 text-secondary truncate">{r.name}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="委托类型">
            <div className="flex gap-1.5">
              <Pill active={orderType === 'limit'} onClick={() => setOrderType('limit')}>限价</Pill>
              <Pill active={orderType === 'market'} onClick={() => setOrderType('market')}>市价</Pill>
            </div>
          </Field>
          <Field label="数量 (股)">
            <input
              value={qtyText}
              onChange={e => setQtyText(e.target.value.replace(/[^0-9]/g, ''))}
              className="w-full rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
          </Field>
        </div>

        {orderType === 'limit' && (
          <Field label="限价 (元/股)">
            <input
              value={priceText}
              onChange={e => setPriceText(e.target.value.replace(/[^0-9.]/g, ''))}
              placeholder="例 1500.00"
              className="w-full rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
          </Field>
        )}

        <Field label="备注 (可选)">
          <input value={note} onChange={e => setNote(e.target.value)}
            placeholder="例: 突破后追单"
            className="w-full rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-accent/40" />
        </Field>

        {/* 资金占用 / 手续费 预览 (v2 PR2): 跟随 qty × price 实时重算. 后端风控会精确算, 这里只是估算. */}
        <div className="rounded-md border border-border bg-surface/40 px-2.5 py-1.5 space-y-0.5">
          <div className="flex items-center justify-between text-[10px]">
            <span className="text-muted">资金占用</span>
            <span className="font-mono tabular-nums text-foreground">
              {(() => {
                const q = Math.max(Number(qtyText) || 0, 0)
                const p = orderType === 'limit'
                  ? (priceText ? Number(priceText) : 0)
                  : (lastPrice ?? 0)
                if (q <= 0 || p <= 0) return '—'
                return `¥${(q * p).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
              })()}
            </span>
          </div>
          <div className="flex items-center justify-between text-[10px]">
            <span className="text-muted">手续费 (估算)</span>
            <span className="font-mono tabular-nums text-secondary">
              {(() => {
                if (feeRate == null || feeRate <= 0) return '—'
                const q = Math.max(Number(qtyText) || 0, 0)
                const p = orderType === 'limit'
                  ? (priceText ? Number(priceText) : 0)
                  : (lastPrice ?? 0)
                if (q <= 0 || p <= 0) return '—'
                return `¥${(q * p * feeRate).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
              })()}
            </span>
          </div>
        </div>

        {submitMut.isError && (
          <div className="flex items-start gap-1.5 rounded-md border border-danger/30 bg-danger/5 px-2.5 py-1.5 text-[11px] text-danger">
            <AlertCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
            <span>{(submitMut.error as Error)?.message ?? '下单失败'}</span>
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button onClick={onClose}
            className="rounded-md border border-border px-3 py-1.5 text-xs text-secondary hover:bg-elevated cursor-pointer">取消</button>
          <button onClick={onSubmit} disabled={!canSubmit || submitting || submitMut.isPending}
            className={cn('rounded-md px-3 py-1.5 text-xs font-medium cursor-pointer transition-colors',
              canSubmit
                ? (side === 'buy' ? 'bg-bull text-white hover:bg-bull/90' : 'bg-bear text-white hover:bg-bear/90')
                : 'bg-elevated text-muted cursor-not-allowed')}>
            {(submitting || submitMut.isPending) && <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />}
            确认{side === 'buy' ? '买入' : '卖出'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

function TradesTable({ strategyId }: { strategyId?: string | null } = {}) {
  const tradesQ = useQuery({
    queryKey: QK.tradingTrades(strategyId ?? null),
    queryFn: () => api.tradingTrades({ strategy_id: strategyId ?? undefined, limit: 200 }),
    refetchInterval: 10000,
  })
  const trades = tradesQ.data?.trades ?? []
  const allSymbols = useMemo(() => Array.from(new Set(trades.map(t => t.symbol))), [trades])
  const namesQ = useQuery({
    queryKey: ['instrument-names-trades', allSymbols.join(',')],
    queryFn: () => api.instrumentNames(allSymbols),
    enabled: allSymbols.length > 0,
    staleTime: 300_000,
  })
  const names = namesQ.data?.names ?? {}
  return (
    <Section title="历史成交" subtitle={`${trades.length} 笔 (按时间倒序)`} icon={Receipt}>
      {trades.length === 0 ? (
        <EmptyHint>暂无成交记录。</EmptyHint>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-surface/40 shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border/60 bg-surface/60 text-[10px] uppercase tracking-wide text-muted">
                  <th className="px-3 py-2 font-medium">标的</th>
                  <th className="px-2 py-2 font-medium text-center">方向</th>
                  <th className="px-2 py-2 font-medium text-right">数量</th>
                  <th className="px-2 py-2 font-medium text-right">价格</th>
                  <th className="px-2 py-2 font-medium text-right">成交额</th>
                  <th className="px-2 py-2 font-medium text-right">费用</th>
                  <th className="px-2 py-2 font-medium text-right">印花税</th>
                  <th className="px-3 py-2 font-medium">成交时间</th>
                </tr>
              </thead>
              <tbody>
                {trades.map(t => (
                  <tr key={t.id} className="border-b border-border/40 last:border-0 hover:bg-elevated/40">
                    <td className="px-3 py-2.5">
                      <span className="font-mono text-foreground">{t.symbol}</span>
                      {names[t.symbol] && <span className="ml-1.5 text-secondary truncate max-w-32">{names[t.symbol]}</span>}
                    </td>
                    <td className="px-2 py-2.5 text-center">
                      <span className={cn('inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold border',
                        t.side === 'buy' ? 'text-bull border-bull/30 bg-bull/10' : 'text-bear border-bear/30 bg-bear/10')}>
                        {t.side === 'buy' ? '买' : '卖'}
                      </span>
                    </td>
                    <td className="px-2 py-2.5 text-right font-mono text-secondary">{fmtVolume(t.qty)}</td>
                    <td className="px-2 py-2.5 text-right font-mono text-foreground">{fmtPrice(t.price)}</td>
                    <td className="px-2 py-2.5 text-right font-mono text-secondary">{fmtPrice(t.amount)}</td>
                    <td className="px-2 py-2.5 text-right font-mono text-muted">{fmtPrice(t.fee)}</td>
                    <td className="px-2 py-2.5 text-right font-mono text-muted">{fmtPrice(t.tax)}</td>
                    <td className="px-3 py-2.5 text-muted">{ts(t.filled_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Section>
  )
}


export function LogsList({ strategyId }: { strategyId?: string | null } = {}) {
  const logsQ = useQuery({
    queryKey: QK.tradingLogs(strategyId ?? null),
    queryFn: () => api.tradingLogs({ strategy_id: strategyId ?? undefined, limit: 200 }),
    refetchInterval: 8000,
  })
  const logs = logsQ.data?.logs ?? []
  return (
    <Section title="交易日志" subtitle="委托提交 / 成交 / 撤单 / 拒单" icon={History}>
      {logs.length === 0 ? (
        <EmptyHint>暂无日志。</EmptyHint>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-surface/40 shadow-sm">
          <ul className="divide-y divide-border/40 text-xs">
            {logs.map(l => (
              <li key={l.id} className="px-3 py-2 hover:bg-elevated/40">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <KindBadge kind={l.kind} />
                    <span className="text-foreground">{l.message}</span>
                    {l.symbol && <span className="font-mono text-muted">{l.symbol}</span>}
                  </div>
                  <span className="font-mono text-[10px] text-muted">{ts(l.ts)}</span>
                </div>
                {l.details && (
                  <div className="mt-0.5 text-[10px] text-muted/80">
                    {Object.entries(l.details).map(([k, v]) => `${k}=${v}`).join(' · ')}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Section>
  )
}

export function PnLPanel({ strategyId }: { strategyId?: string | null } = {}) {
  const metricsQ = useQuery({
    queryKey: QK.tradingPnlMetrics(strategyId ?? null),
    queryFn: () => api.tradingPnlMetrics(strategyId ? { strategy_id: strategyId } : undefined),
    refetchInterval: 15000,
  })
  const historyQ = useQuery({
    queryKey: QK.tradingPnlHistory(strategyId ?? null, 'all'),
    queryFn: () => strategyId
      ? api.tradingPnlHistoryStrategy(strategyId, { limit: 500 })
      : api.tradingPnlHistory({ limit: 500 }),
    refetchInterval: 30000,
  })
  const m = metricsQ.data
  const snaps = historyQ.data?.snapshots ?? []

  return (
    <Section title="收益指标 / 净值曲线" subtitle="今日 / 累计 / 最大回撤 / 胜率 (按 fill 历史聚合)" icon={LifeLine}>
      {!m ? (
        <EmptyHint>暂无收益数据 — 提交第一笔委托并成交后这里会出现统计。</EmptyHint>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Card label="今日盈亏" icon={Activity}
              primary={`${m.today_pnl >= 0 ? '+' : ''}${fmtPrice(m.today_pnl)}`}
              primaryClass={priceColorClass(m.today_pnl)}
              sub={`${fmtPct(m.today_pnl_pct)} 较开盘`} />
            <Card label="累计盈亏" icon={LifeLine}
              primary={`${m.total_pnl >= 0 ? '+' : ''}${fmtPrice(m.total_pnl)}`}
              primaryClass={priceColorClass(m.total_pnl)}
              sub={`${fmtPct(m.total_pnl_pct)} / 初始 ¥${fmtPrice(m.initial_cash)}`} />
            <Card label="最大回撤" icon={Activity}
              primary={`${fmtPct(m.max_drawdown)}`}
              primaryClass={m.max_drawdown > 0.10 ? 'text-danger' : (m.max_drawdown > 0.05 ? 'text-warning' : 'text-secondary')}
              sub={m.max_drawdown_date
                ? `${m.max_drawdown_date} ¥${fmtPrice(m.max_drawdown_peak)} → ¥${fmtPrice(m.max_drawdown_trough)}`
                : '未触发'} />
            <Card label="胜率" icon={Activity}
              primary={`${fmtPct(m.win_rate)}`}
              primaryClass={m.win_rate >= 0.5 ? 'text-bull' : (m.total_trades > 0 ? 'text-bear' : 'text-secondary')}
              sub={`${m.winning_trades} 胜 / ${m.losing_trades} 负 / ${m.total_trades} 总`} />
          </div>
          <EquityCurve snapshots={snaps} initialCash={m.initial_cash} />
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 text-[11px]">
            <Stat label="已实现" value={fmtPrice(m.realized_pnl)} valueClass={priceColorClass(m.realized_pnl)} />
            <Stat label="未实现" value={fmtPrice(m.unrealized_pnl)} valueClass={priceColorClass(m.unrealized_pnl)} />
            <Stat label="每笔均盈亏" value={fmtPrice(m.avg_pnl_per_trade)} valueClass={priceColorClass(m.avg_pnl_per_trade)} />
          </div>
        </div>
      )}
    </Section>
  )
}

function EquityCurve({ snapshots, initialCash }: { snapshots: PnLSnapshotApi[]; initialCash: number }) {
  // 用 ECharts 画简单净值曲线 (避免引入额外依赖)
  const chartRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!chartRef.current || snapshots.length === 0) return
    let cancelled = false
    let cleanup: (() => void) | null = null

    ;(async () => {
      const echarts = await import('echarts')
      if (cancelled || !chartRef.current) return
      const dom = chartRef.current
      const inst = echarts.init(dom)
      // 注入基线 (initial_cash)
      const data = [["__init", initialCash] as [string, number],
        ...snapshots.map(s => [s.trade_date + ' ' + s.ts.slice(11, 16), s.total_equity] as [string, number])]
      inst.setOption({
        grid: { left: 50, right: 16, top: 16, bottom: 28 },
        xAxis: { type: 'category', data: data.map(d => d[0]), axisLabel: { fontSize: 10, color: '#888' } },
        yAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#888', formatter: (v: number) => v.toFixed(0) } },
        tooltip: { trigger: 'axis' },
        series: [{
          type: 'line', data: data.map(d => d[1]),
          smooth: true, showSymbol: false,
          lineStyle: { color: '#22c55e', width: 2 },
          areaStyle: { color: 'rgba(34, 197, 94, 0.12)' },
        }],
      })
      const resize = () => inst.resize()
      window.addEventListener('resize', resize)
      cleanup = () => { window.removeEventListener('resize', resize); inst.dispose() }
    })()

    return () => { cancelled = true; cleanup?.() }
  }, [snapshots, initialCash])

  if (snapshots.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border px-4 py-6 text-center text-xs text-muted">
        暂未生成快照。成交 / cron 触发 / 启动后会自动落点。
      </div>
    )
  }
  return <div ref={chartRef} className="h-48 w-full rounded-md border border-border bg-surface/30" />
}

function StrategiesPanel() {
  const strategiesQ = useQuery({ queryKey: QK.tradingStrategies, queryFn: api.tradingStrategies, refetchInterval: 10000 })
  const strategies = strategiesQ.data?.strategies ?? []
  // 每个 strategy 展开/收起的 user 偏好。Map 内未出现的 strategy, 默认
  // 按"是否有任一 schedule 启用"展开 (active=展开, stopped=收起)。
  // 已停用 (全部 schedule.enabled=false) 的策略进入下方"更多"折叠区,
  // 不再展示在外层主列表, 避免一屏塞满停用项; 用户偏好与"更多"开/关各自独立持久。
  const [userExpanded, setUserExpanded] = useState<Record<string, boolean>>({})
  const [moreOpen, setMoreOpen] = useState(false)
  const toggle = (s: LiveStrategyApi) =>
    setUserExpanded(prev => ({ ...prev, [s.id]: !strategyIsExpanded(s, prev) }))

  // 拆分主区 / 更多区; 输入空数组时两者均为空, 由外层 EmptyHint 处理。
  const { active, stopped } = useMemo(
    () => splitStrategiesByStatus(strategies),
    [strategies],
  )

  return (
    <Section title="实盘策略" subtitle="自动调仓 (按分钟 K 撮合, 不接真实券商). 每个 event 单独启停." icon={Zap}
      actions={
        <button onClick={() => strategiesQ.refetch()}
          className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-surface/30 px-2 text-[11px] text-secondary hover:bg-elevated/40 cursor-pointer">
          <Activity className="h-3 w-3" />刷新
        </button>
      }>
      {strategies.length === 0 ? (
        <EmptyHint>还没有注册的策略。在 <code>data/user_data/strategies/live/</code> 放入 <code>.py</code> 文件即可自动发现。</EmptyHint>
      ) : (
        <div className="space-y-2">
          {/* 主区: 仅展示启用中 (含 0 schedules 的占位策略) */}
          {active.length > 0 ? (
            active.map(s => (
              <StrategyRow key={s.id} s={s}
                expanded={strategyIsExpanded(s, userExpanded)}
                onToggle={() => toggle(s)} />
            ))
          ) : (
            <EmptyHint>暂无启用中的策略。已停用策略在下方"更多"区域查看。</EmptyHint>
          )}
          {/* 已停用 -> 更多折叠区, 默认收起; 不展示在外 */}
          {stopped.length > 0 && (
            <StoppedStrategiesMore
              strategies={stopped}
              open={moreOpen}
              onToggleOpen={() => setMoreOpen(o => !o)}
              userExpanded={userExpanded}
              onToggleStrategy={toggle}
            />
          )}
        </div>
      )}
    </Section>
  )
}

function StoppedStrategiesMore({ strategies, open, onToggleOpen, userExpanded, onToggleStrategy }: {
  strategies: LiveStrategyApi[]
  open: boolean
  onToggleOpen: () => void
  userExpanded: Record<string, boolean>
  onToggleStrategy: (s: LiveStrategyApi) => void
}) {
  return (
    <div className="rounded-md border border-border/60 bg-surface/20">
      <button
        type="button"
        onClick={onToggleOpen}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left cursor-pointer hover:bg-elevated/30"
      >
        {open
          ? <ChevronDown className="h-3.5 w-3.5 text-muted shrink-0" />
          : <ChevronRight className="h-3.5 w-3.5 text-muted shrink-0" />}
        <span className="text-xs font-medium text-secondary">更多 ({strategies.length})</span>
        <span className="ml-auto text-[10px] text-muted">已停用策略</span>
      </button>
      {open && (
        <div className="border-t border-border/40 space-y-2 p-2">
          {strategies.map(s => (
            <StrategyRow key={s.id} s={s}
              expanded={strategyIsExpanded(s, userExpanded)}
              onToggle={() => onToggleStrategy(s)} />
          ))}
        </div>
      )}
    </div>
  )
}

function StrategyRow({ s, expanded, onToggle }: { s: LiveStrategyApi; expanded: boolean; onToggle: () => void }) {
  const runsQ = useQuery({
    queryKey: QK.tradingStrategyRuns(s.id),
    queryFn: () => api.tradingStrategyRuns(s.id, 10),
    enabled: expanded,
  })

  const enabledCount = s.schedules.filter(sc => sc.enabled).length
  // 已停用 (全部 schedule.enabled=false) 时, 收起时只露头 + 提示, 不展开 schedule 行
  const fullyStopped = s.schedules.length > 0 && enabledCount === 0

  return (
    <div className="rounded-md border border-border bg-surface/30">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left cursor-pointer hover:bg-elevated/20"
      >
        {expanded
          ? <ChevronDown className="h-3.5 w-3.5 text-muted shrink-0" />
          : <ChevronRight className="h-3.5 w-3.5 text-muted shrink-0" />}
        <Zap className="h-3.5 w-3.5 text-accent shrink-0" />
        <span className="text-sm font-medium text-foreground truncate">{s.name}</span>
        <code className="text-[10px] text-muted shrink-0">{s.id}</code>
        <span className={cn('inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold border shrink-0',
          enabledCount > 0 ? 'text-bull border-bull/30 bg-bull/10' : 'text-muted border-muted/30 bg-muted/10')}>
          {enabledCount} / {s.schedules.length} 已启用
        </span>
        <span className="ml-auto flex items-center gap-2 text-[10px] text-muted shrink-0">
          {!expanded && fullyStopped && <span>点击展开</span>}
          <span>{s.schedules.length} 个 schedule</span>
        </span>
      </button>

      {expanded && (
        <>
          {/* 每个 schedule 单独一行: event 名 + cron + 启停 + 立即跑 */}
          <div className="border-t border-border/40 px-3 py-1.5 space-y-1">
            {s.schedules.map(sc => (
              <ScheduleRow key={sc.name} strategyId={s.id} schedule={sc} />
            ))}
            {s.schedules.length === 0 && (
              <div className="text-[11px] text-muted py-1">该策略未声明 schedule.</div>
            )}
          </div>

          <div className="border-t border-border/40 px-3 py-2">
            <StrategyRunList runs={runsQ.data?.runs ?? []} isLoading={runsQ.isLoading} />
          </div>
        </>
      )}
    </div>
  )
}

function ScheduleRow({ strategyId, schedule }: { strategyId: string; schedule: import('@/lib/api').StrategyScheduleApi }) {
  const qc = useQueryClient()
  const patchMut = useMutation({
    mutationFn: (patch: { enabled?: boolean; hour?: number; minute?: number }) =>
      api.tradingUpdateStrategySchedule(strategyId, schedule.name, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['trading', 'strategies'] }),
  })
  const runMut = useMutation({
    mutationFn: () => api.tradingRunStrategyNow(strategyId, schedule.name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['trading'] })
      qc.invalidateQueries({ queryKey: QK.tradingStrategyRuns(strategyId) })
    },
  })

  const enabledCls = schedule.enabled ? 'text-bull border-bull/30 bg-bull/10' : 'text-muted border-muted/30 bg-muted/10'

  return (
    <div className="flex items-center gap-2 rounded px-2 py-1 hover:bg-elevated/30">
      <code className="rounded bg-elevated/40 px-1.5 py-0.5 font-mono text-[11px] text-secondary">{schedule.name}</code>
      <span className="text-[11px] text-muted">@ {pad(schedule.hour)}:{pad(schedule.minute)}</span>
      <span className={cn('inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold', enabledCls)}>
        {schedule.enabled ? '启用' : '停用'}
      </span>
      <div className="ml-auto flex items-center gap-1">
        <button
          onClick={() => patchMut.mutate({ enabled: !schedule.enabled })}
          disabled={patchMut.isPending}
          title={schedule.enabled ? '停用此 schedule' : '启用此 schedule'}
          className={cn('rounded-md border px-2 py-0.5 text-[10px] font-medium cursor-pointer',
            schedule.enabled ? 'border-bear/30 text-bear hover:bg-bear/10' : 'border-bull/30 text-bull hover:bg-bull/10')}>
          {schedule.enabled ? '停' : '启'}
        </button>
        <button
          onClick={() => runMut.mutate()}
          disabled={runMut.isPending}
          title="立即触发此 schedule"
          className="inline-flex items-center gap-0.5 rounded-md border border-accent/30 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent hover:bg-accent/15 cursor-pointer">
          {runMut.isPending ? <Loader2 className="h-2.5 w-2.5 animate-spin" /> : <Play className="h-2.5 w-2.5" />}
          跑
        </button>
      </div>
    </div>
  )
}


function StrategyRunHistoryPanel({ strategyId }: { strategyId: string }) {
  const runsQ = useQuery({
    queryKey: QK.tradingStrategyRuns(strategyId),
    queryFn: () => api.tradingStrategyRuns(strategyId, 20),
  })
  return (
    <StrategyRunList
      runs={(runsQ.data?.runs ?? []) as StrategyRunReportApi[]}
      isLoading={runsQ.isLoading}
    />
  )
}

function StrategyRunList({ runs, isLoading }: { runs: StrategyRunReportApi[]; isLoading: boolean }) {
  if (isLoading) return <div className="text-[11px] text-muted">加载中…</div>
  if (runs.length === 0) return <div className="text-[11px] text-muted">暂无执行记录。</div>
  return (
    <ul className="divide-y divide-border/30 text-[11px]">
      {runs.slice().reverse().map(r => (
        <li key={r.started_at + r.event} className="py-1.5">
          <div className="flex items-center gap-2">
            <span className={cn('inline-flex items-center rounded px-1.5 py-0.5 font-semibold border',
              r.status === 'success' ? 'text-bull border-bull/30 bg-bull/10'
                : r.status === 'error' ? 'text-danger border-danger/30 bg-danger/10'
                : r.status === 'no_target' ? 'text-secondary border-secondary/30 bg-secondary/10'
                : 'text-muted border-muted/30 bg-muted/10')}>
              {r.status}
            </span>
            <code className="rounded bg-elevated/40 px-1 py-0.5 font-mono text-[10px] text-secondary">{r.event}</code>
            <span className="text-secondary">{r.trigger}</span>
            <span className="text-muted">{r.started_at.replace('T', ' ').slice(0, 19)}</span>
            <span className="text-muted">· {r.duration_ms}ms</span>
            <span className="text-muted">· {r.submitted.length} 委托</span>
          </div>
          {r.error && <div className="mt-0.5 text-danger">{r.error}</div>}
          {r.target && Object.keys(r.target).length > 0 && (
            <div className="mt-0.5 truncate text-muted">
              target = {Object.entries(r.target).map(([s, q]) => `${s}:${q}`).join(', ')}
            </div>
          )}
        </li>
      ))}
    </ul>
  )
}

/**
 * v2 PR1 重构: 持仓卡片化 + 去掉 4-tab 中栏 + 去掉右下消息轮询 (合并到顶栏 Bell),
 * 加上 ?view=verify 分支 (见 docs/live-trading-v2-pr1-nav-and-wire.md §B).
 *
 * ?view=verify: 全屏只显示回测验证段, 由 Sidebar "策略配置 ▼" -> "打开回测验证"
 * 入口触发, 其余 (持仓/委托/成交/PnL/历史/日志) 全隐.
 *
 * - 持仓: PositionsGrid (1 行 1 票卡片网格, 自适应 1/2/3/4 列)
 * - 委托 + 成交: xl 2 列 split-view (无 sub-tab)
 * - PnL 曲线: 单段
 * - 策略运行历史: StrategyRunHistoryPanel (per-strategy) / StrategiesPanel (列表)
 * - 日志: LogsList
 *
 * 不再持有 lotsQ 数据 (PR2 才整合 plan/lots 对齐展示)。
 */
export function LiveTradingOverview({
  strategyId,
  view,
}: { strategyId?: string | null; view?: 'verify' } = {}) {
  // ?view=verify 守卫: 只显示回测验证段. 不进主网格.
  if (view === 'verify' && strategyId) {
    return <VerifyAndDriftSection strategyId={strategyId} />
  }

  // 顶层 OrderDialog 状态 (v2 PR2 状态提升):
  //   - OrdersTable 的 买入/卖出 按钮: 只传 side
  //   - PositionCard 的 卖 1/3 / 1/2 / 全卖: 传 side='sell' + symbol + qty
  // 一份 dialog 实例, 多个触发源.
  const [dialog, setDialog] = useState<null | { side: 'buy' | 'sell'; symbol?: string; qty?: number }>(null)

  // 共享查询 (TanStack dedupe by key):
  //   - tradingPositions 喂 PositionsGrid, 同时作 last_price 查表 + quick-trade 算 qty
  //   - tradingPreferences 喂 fee_rate
  const positionsQ = useQuery({
    queryKey: QK.tradingPositions(strategyId ?? null),
    queryFn: () => api.tradingPositions(strategyId ? { strategy_id: strategyId } : undefined),
    refetchInterval: 6000,
  })
  const positionsBySymbol = useMemo(() => {
    const m: Record<string, PositionApi> = {}
    for (const p of positionsQ.data?.positions ?? []) m[p.symbol] = p
    return m
  }, [positionsQ.data])
  const prefsQ = useQuery({
    queryKey: QK.tradingPreferences,
    queryFn: api.tradingPreferences,
    staleTime: 300_000,
  })
  const feeRate = prefsQ.data?.trading_fee_rate ?? null

  const openBuy = () => setDialog({ side: 'buy' })
  const openSell = () => setDialog({ side: 'sell' })
  const onQuickTrade = (
    side: 'sell',
    ratio: 1 | 2 | 'all',
    symbol: string,
  ) => {
    const pos = positionsBySymbol[symbol]
    if (!pos) return
    const qty = ratio === 'all'
      ? pos.qty
      : ratio === 1 ? Math.floor(pos.qty / 3) : Math.floor(pos.qty / 2)
    if (qty <= 0) {
      toast('无可卖数量', 'error')
      return
    }
    setDialog({ side, symbol, qty })
  }
  const closeDialog = () => setDialog(null)

  return (
    <div className="space-y-4">
      <PositionsGrid strategyId={strategyId} onQuickTrade={onQuickTrade} />
      <PnLPanel strategyId={strategyId} />
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <OrdersTable
          strategyId={strategyId}
          onOpenBuy={openBuy}
          onOpenSell={openSell}
        />
        <TradesTable strategyId={strategyId} />
      </div>
      {strategyId
        ? <StrategyRunHistoryPanel strategyId={strategyId} />
        : <StrategiesPanel />}
      <LogsList strategyId={strategyId} />

      {/* OrderDialog 顶层实例 (v2 PR2 状态提升): 多个触发源共用一个 dialog */}
      {dialog && (
        <OrderDialog
          side={dialog.side}
          onClose={closeDialog}
          initial={{ symbol: dialog.symbol, qty: dialog.qty }}
          feeRate={feeRate}
          lastPrice={dialog.symbol ? positionsBySymbol[dialog.symbol]?.last_price ?? null : null}
        />
      )}
    </div>
  )
}