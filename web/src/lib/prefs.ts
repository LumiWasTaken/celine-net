/* This device's conveniences in localStorage: drafts, where you left off, what is open or
   closed, the last saved look for the first paint (public/theme.js). Storage may be
   blocked or empty (a private window, cleared site data): then nothing is kept and the app
   works the same. What the server owns is never kept here. */
import { useCallback, useState } from 'react'

export function getPref(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}

/** null or '' removes it */
export function setPref(key: string, value: string | null) {
  try {
    if (value) localStorage.setItem(key, value)
    else localStorage.removeItem(key)
  } catch { /* blocked */ }
}

export function getJson<T>(key: string, fallback: T): T {
  const raw = getPref(key)
  if (raw == null) return fallback
  try { return JSON.parse(raw) as T } catch { return fallback }
}

export function setJson(key: string, value: unknown) {
  setPref(key, value == null ? null : JSON.stringify(value))
}

/** a state kept on this device under `key` (`fallback` until it is set once) */
export function usePref<T>(key: string, fallback: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => getJson(key, fallback))
  const set = useCallback((next: T) => { setV(next); setJson(key, next) }, [key])
  return [v, set]
}
