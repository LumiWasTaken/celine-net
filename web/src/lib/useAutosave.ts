/* Settings that save themselves, with no Save button.

     const auto = useAutosave(async changes => res.set(await api.put({ ...res.data, ...changes })))
     <Switch checked={auto.pending.on ?? saved.on} onChange={v => auto.set('on', v)} />
     {auto.error && <InlineError compact message={auto.error} onRetry={auto.flush} />}

   `set(key, value, when)` shows the value at once (`pending` holds it) and sends it: 'now'
   (a switch, a choice), 'soon' (a slider: `delay` ms after the last change), 'blur' (typing:
   when the field calls `flush` on blur, or the page is left). One save runs at a time; what
   changes meanwhile goes in the next. `save` gets every change waiting and puts the
   server's answer where the page reads it before it resolves, so a value never flips back.
   A failed save keeps the values on screen with its error; `flush` sends them again (Retry).
   Leaving the page (unmount, pagehide) sends what waits. */
import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { errorText } from './format'

export type Autosave<V> = {
  pending: Record<string, V>
  set: (key: string, value: V, when?: 'now' | 'soon' | 'blur') => void
  flush: () => void
  /** drop what waits and the error (a Reset that sends its own save) */
  clear: () => void
  error: string | null
}

export function useAutosave<V = unknown>(save: (changes: Record<string, V>) => Promise<unknown>, delay = 400): Autosave<V> {
  const pending = useRef<Record<string, V>>({})
  const [, render] = useReducer((n: number) => n + 1, 0)
  const [error, setError] = useState<string | null>(null)
  const saveRef = useRef(save)
  saveRef.current = save
  const timer = useRef<number | undefined>(undefined)
  const busy = useRef(false)
  const again = useRef(false)
  const alive = useRef(true)

  const flush = useCallback(async () => {
    window.clearTimeout(timer.current); timer.current = undefined
    if (busy.current) { again.current = true; return }
    const sent = pending.current
    if (!Object.keys(sent).length) return
    busy.current = true
    try {
      await saveRef.current(sent)
      const left = { ...pending.current }
      for (const [k, v] of Object.entries(sent)) if (left[k] === v) delete left[k]
      pending.current = left
      if (alive.current) setError(null)
    } catch (e) {
      if (alive.current) setError(errorText(e))
    } finally {
      busy.current = false
      if (alive.current) render()
      // left meanwhile: no answer reaches the page, so the next save carries this one too
      else if (again.current) { again.current = false; pending.current = { ...sent, ...pending.current }; void flush() }
    }
  }, [])

  // what changed during a save goes once the page has the answer, so `save` builds on it
  useEffect(() => {
    if (again.current && !busy.current) { again.current = false; void flush() }
  })

  const set = useCallback((key: string, value: V, when: 'now' | 'soon' | 'blur' = 'now') => {
    pending.current = { ...pending.current, [key]: value }
    render()
    if (when === 'now') void flush()
    else if (when === 'soon') { window.clearTimeout(timer.current); timer.current = window.setTimeout(() => void flush(), delay) }
  }, [flush, delay])

  const clear = useCallback(() => {
    window.clearTimeout(timer.current); timer.current = undefined
    pending.current = {}
    setError(null)
    render()
  }, [])

  useEffect(() => {
    alive.current = true
    const leave = () => void flush()
    window.addEventListener('pagehide', leave)
    return () => { alive.current = false; window.removeEventListener('pagehide', leave); leave() }
  }, [flush])

  return { pending: pending.current, set, flush: () => void flush(), clear, error }
}
