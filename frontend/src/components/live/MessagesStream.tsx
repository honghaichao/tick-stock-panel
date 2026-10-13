import { useEffect, useState, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertCircle, CheckCircle2, Info, XCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

export interface LiveMessage {
  id: string
  kind: 'info' | 'success' | 'warn' | 'error' | 'rejected' | 'cleared' | 'retry'
  text: string
  ts: string
}

const MAX_MSGS = 100

/**
 * 聚宽式消息流 — 接收 trading_event SSE, 累积到本地列表, 最多 100 条.
 * 组件外部可以通过 addMessage() 注入, 或者挂 SSE 推消息.
 */
export function MessagesStream({ className }: { className?: string }) {
  const notifyQ = useQuery({
    queryKey: ['trading', 'notify'],
    queryFn: () => api.tradingNotifyList({ limit: 100 }),
    refetchInterval: 10000,
  })
  const [msgs, setMsgs] = useState<LiveMessage[]>([])
  const idsRef = useRef<Set<string>>(new Set())

  // 初始化: useQuery 数据 -> msgs
  useEffect(() => {
    if (notifyQ.data?.items) {
      setMsgs(notifyQ.data.items.map((n: any) => ({
        id: n.ts + '-' + n.kind,
        kind: (n.kind || 'info') as LiveMessage['kind'],
        text: n.text,
        ts: n.ts,
      })))
    }
  }, [notifyQ.data])

  // 暴露 addMessage 给外部 (SSE consumer)
  useEffect(() => {
    ;(window as any).__liveMessagesAdd = (m: Omit<LiveMessage, 'id' | 'ts'>) => {
      const id = `${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
      if (idsRef.current.has(id)) return
      idsRef.current.add(id)
      setMsgs(prev => {
        const next = [{ id, ts: new Date().toISOString(), ...m }, ...prev]
        return next.slice(0, MAX_MSGS)
      })
    }
    return () => {
      delete (window as any).__liveMessagesAdd
    }
  }, [])

  return (
    <div className={cn('rounded-xl border border-border bg-surface/30 p-3', className)}>
      <div className="mb-2 flex items-center justify-between text-[11px] uppercase tracking-wide text-muted">
        <span>消息流</span>
        {msgs.length > 0 && (
          <button
            onClick={() => { setMsgs([]); idsRef.current.clear() }}
            className="text-[10px] text-muted hover:text-secondary"
          >
            清空
          </button>
        )}
      </div>
      {msgs.length === 0 ? (
        <div className="py-6 text-center text-[11px] text-muted">
          暂无消息。委托/成交/拒单/策略事件会推到这里。
        </div>
      ) : (
        <ul className="max-h-[480px] space-y-1 overflow-y-auto text-[11px]">
          {msgs.map(m => <MessageItem key={m.id} m={m} />)}
        </ul>
      )}
    </div>
  )
}

function MessageItem({ m }: { m: LiveMessage }) {
  const Icon = m.kind === 'error' || m.kind === 'rejected'
    ? XCircle
    : m.kind === 'warn'
    ? AlertCircle
    : m.kind === 'success'
    ? CheckCircle2
    : Info
  const cls = m.kind === 'error' || m.kind === 'rejected'
    ? 'text-danger'
    : m.kind === 'warn'
    ? 'text-warning'
    : m.kind === 'success'
    ? 'text-bull'
    : 'text-secondary'
  return (
    <li className="flex items-start gap-2 rounded border border-border/40 bg-surface/30 px-2 py-1.5">
      <Icon className={cn('mt-0.5 h-3 w-3 shrink-0', cls)} />
      <div className="flex-1">
        <div className={cn('text-foreground', cls)}>{m.text}</div>
        <div className="mt-0.5 text-[10px] text-muted">
          {m.ts.replace('T', ' ').slice(0, 19)} · {m.kind}
        </div>
      </div>
    </li>
  )
}
