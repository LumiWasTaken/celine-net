/* Loading data the same way everywhere: one hook that fetches, cancels a stale
   request when its inputs change, keeps the last data while reloading, turns every
   failure into a sentence, and reloads on the server events you name.

     const chats = useResource(signal => kbApi.chats(kb, { signal }), [kb],
                               { events: ev => ev.type === 'kb.changed' && ev.kb === kb })
     chats.loading / chats.error / chats.data / chats.reload()
   `missing`: the last failure was a 404, the thing is not there: a page says Not found,
   with nothing to retry.

   `loading` is true only while a request is in flight; `data === undefined` with
   `loading === false` and no error means "not asked yet" (enabled: false). */
import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { ApiError } from './api'
import { useServerEvents, type ServerEvent } from './bus'
import { errorText } from './format'

export type Resource<T> = {
  data: T | undefined
  error: string | null
  /** the error was a 404: what was asked for is not there */
  missing: boolean
  loading: boolean
  reload: () => void
  /** local update after a mutation, without a refetch */
  set: Dispatch<SetStateAction<T | undefined>>
}

export function useResource<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  opts: {
    /** reload when this returns true for a server event */
    events?: (ev: ServerEvent) => boolean
    /** also reload on `open`, when the feed is followed from now (default true when `events` is set) */
    onReconnect?: boolean
    /** false: do not fetch (yet) */
    enabled?: boolean
    /** drop old data when the inputs change (default false: keep it, no flash) */
    resetOnChange?: boolean
    /** ms to wait after the inputs change, for typing */
    debounce?: number
  } = {},
): Resource<T> {
  const { events, enabled = true, resetOnChange = false, debounce = 0 } = opts
  const onReconnect = opts.onReconnect ?? !!events
  const [data, setData] = useState<T | undefined>(undefined)
  const [error, setError] = useState<string | null>(null)
  const [missing, setMissing] = useState(false)
  const [loading, setLoading] = useState(enabled)
  const [tick, setTick] = useState(0)
  const fetchRef = useRef(fetcher)
  fetchRef.current = fetcher
  const first = useRef(true)

  useEffect(() => {
    if (!enabled) { setLoading(false); return }
    if (resetOnChange && !first.current) setData(undefined)
    first.current = false
    const ctl = new AbortController()
    setLoading(true)
    const run = () => {
      fetchRef.current(ctl.signal)
        .then(d => { if (!ctl.signal.aborted) { setData(d); setError(null); setMissing(false) } })
        .catch(e => {
          if (ctl.signal.aborted) return
          setError(errorText(e))
          setMissing(e instanceof ApiError && e.status === 404)
        })
        .finally(() => { if (!ctl.signal.aborted) setLoading(false) })
    }
    const t = debounce ? window.setTimeout(run, debounce) : (run(), undefined)
    return () => { ctl.abort(); if (t) clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, tick])

  const reload = useCallback(() => setTick(t => t + 1), [])
  useServerEvents(ev => {
    if (!enabled) return
    if ((events && events(ev)) || (onReconnect && ev.type === 'open')) reload()
  })

  return { data, error, missing, loading, reload, set: setData }
}
