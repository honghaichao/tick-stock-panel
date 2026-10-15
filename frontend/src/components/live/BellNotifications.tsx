/**
 * v2 (live-trading-v2-pr2): 顶栏右上角 bell + 未读数徽章 + 抽屉
 *
 * 把 MessagesStream (OverviewContent 右下 25% 屏) 折成 1 个图标位.
 * - bell 图标 + 红色未读数 (有未读时)
 * - 5s 轮询 (沿用 tradingNotifyList)
 * - 点击展开 320px 抽屉, 列出最近 20 条
 * - 抽屉打开时 / 5 秒后, 未读数自动清零
 */
import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertCircle, Bell, CheckCircle2, Info, XCircle, X } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

interface LiveMessage {
  id: string
  kind: 'info' | 'success' | 'warn' | 'error' | 'rejected' | 'cleared' | 'retry'
  text: string
  ts: string
}

const KIND_META: Record<LiveMessage['kind'], { icon: typeof Info; cls: string; label: string }> = {
  info: { icon: Info, cls: 'text-secondary', label: '信息' },
  success: { icon: CheckCircle2, cls: 'text-bull', label: '成功' },
  warn: { icon: AlertCircle, cls: 'text-warning', label: '警告' },
  error: { icon: XCircle, cls: 'text-danger', label: '错误' },
  rejected: { icon: XCircle, cls: 'text-danger', label: '拒单' },
  cleared: { icon: CheckCircle2, cls: 'text-secondary', label: '已清仓' },
  retry: { icon: AlertCircle, cls: 'text-warning', label: '重试' },
}

const UNREAD_CLEAR_MS = 5_000
const DISPLAY_LIMIT = 20

export function BellNotifications() {
  const notifyQ = useQuery({
    queryKey: ['trading', 'notify'],
    queryFn: () => api.tradingNotifyList({ limit: 50 }),
    refetchInterval: 5_000,
  })
  const [open, setOpen] = useState(false)
  const [unread, setUnread] = useState(0)
  const [lastSeenTs, setLastSeenTs] = useState<string | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  // 计算未读: 当前最新条 ts > lastSeenTs
  useEffect(() => {
    const items = notifyQ.data?.items ?? []
    if (items.length === 0) {
      setUnread(0)
      return
    }
    if (!lastSeenTs) {
      setUnread(0)
      return
    }
    const newCount = items.filter((it: any) => it.ts > lastSeenTs).length
    setUnread(newCount)
  }, [notifyQ.data, lastSeenTs])

  // 点击外部关闭
  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onEsc)
    }
  }, [open])

  // 打开时 / 5s 后清未读
  useEffect(() => {
    if (!open) return
    const items = notifyQ.data?.items ?? []
    if (items.length > 0) setLastSeenTs(items[0].ts)
    setUnread(0)
    const t = setTimeout(() => {
      // 5s 后也清 (用户可能打开 5s 都不关, 别让数字跳)
      setUnread(0)
    }, UNREAD_CLEAR_MS)
    return () => clearTimeout(t)
  }, [open, notifyQ.data])

  // 初次加载: 把 lastSeenTs 设到最新, 不算"未读"
  useEffect(() => {
    const items = notifyQ.data?.items ?? []
    if (items.length > 0 && lastSeenTs == null) {
      setLastSeenTs(items[0].ts)
    }
  }, [notifyQ.data, lastSeenTs])

  const items = (notifyQ.data?.items ?? []).slice(0, DISPLAY_LIMIT)

  return (
    <div ref={wrapRef} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          'relative inline-flex items-center justify-center h-8 w-8 rounded-md border bg-surface/40 transition-colors cursor-pointer',
          open ? 'border-accent/50 text-foreground' : 'border-border text-secondary hover:bg-elevated/40 hover:text-foreground',
        )}
        title={`消息通知${unread > 0 ? ` (${unread} 条未读)` : ''}`}
      >
        <Bell className="h-4 w-4" />
        {unread > 0 && (
          <span
            className="absolute -top-1 -right-1 min-w-[16px] h-4 px-1 rounded-full bg-danger text-[10px] font-semibold text-white flex items-center justify-center"
            aria-label={`${unread} 条未读`}
          >
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-[320px] max-h-[480px] rounded-lg border border-border bg-surface shadow-2xl z-50 overflow-hidden flex flex-col">
          <div className="flex items-center justify-between border-b border-border/60 bg-surface/80 px-3 py-2">
            <span className="text-[11px] uppercase tracking-wide text-muted">消息通知</span>
            <button
              onClick={() => setOpen(false)}
              className="text-muted hover:text-foreground"
              aria-label="关闭"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          {items.length === 0 ? (
            <div className="py-8 text-center text-[11px] text-muted">暂无消息</div>
          ) : (
            <ul className="flex-1 overflow-y-auto divide-y divide-border/40 text-[11px]">
              {items.map((m: any, i: number) => {
                const km = KIND_META[m.kind as LiveMessage['kind']] ?? KIND_META.info
                const Icon = km.icon
                return (
                  <li key={`${m.ts}-${i}`} className="flex items-start gap-2 px-3 py-2">
                    <Icon className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', km.cls)} />
                    <div className="flex-1 min-w-0">
                      <div className="text-foreground break-words">{m.text}</div>
                      <div className="mt-0.5 text-[10px] text-muted">
                        {m.ts.replace('T', ' ').slice(0, 19)} · {km.label}
                      </div>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
