/**
 * v2 (live-trading-v2-pr3): 统一 localStorage 状态 hook
 *
 * 之前 Sidebar / CenterTabs 各写各的 localStorage, key 各异.
 * 用此 hook 统一:
 *   const [tab, setTab] = usePersistedState<LiveTabKey>('live:sidebar:tab', 'overview')
 *
 * 特性:
 * - SSR 友好: typeof window === 'undefined' 时返 defaultValue
 * - JSON 序列化: T 必须是 JSON-safe
 * - 跨 tab 同步: 监听 'storage' 事件
 */
import { useEffect, useState } from 'react'

export function usePersistedState<T>(
  key: string,
  defaultValue: T,
): [T, (v: T | ((prev: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    if (typeof window === 'undefined') return defaultValue
    try {
      const raw = window.localStorage.getItem(key)
      if (raw === null) return defaultValue
      return JSON.parse(raw) as T
    } catch {
      return defaultValue
    }
  })

  useEffect(() => {
    if (typeof window === 'undefined') return
    try {
      window.localStorage.setItem(key, JSON.stringify(value))
    } catch {
      // quota exceeded / private mode — silently ignore
    }
  }, [key, value])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const onStorage = (e: StorageEvent) => {
      if (e.key !== key || e.newValue === null) return
      try {
        setValue(JSON.parse(e.newValue) as T)
      } catch {
        // ignore corrupt value
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  return [value, setValue]
}
