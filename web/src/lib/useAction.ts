/* A button's request: busy while it runs, its error kept next to the button, a
   second click while busy ignored. Never throws; `run` resolves to the result or
   undefined on failure.

     const save = useAction((name: string) => kbApi.patchChat(kb, id, { name }))
     <Button loading={save.busy} onClick={() => void save.run(name)}>Save</Button>
     {save.error && <InlineError compact message={save.error} />} */
import { useCallback, useEffect, useRef, useState } from 'react'
import { errorText } from './format'

type Action<A extends unknown[], R> = {
  run: (...args: A) => Promise<R | undefined>
  busy: boolean
  error: string | null
  clear: () => void
}

export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>): Action<A, R> {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fnRef = useRef(fn)
  fnRef.current = fn
  const inFlight = useRef(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])

  const run = useCallback(async (...args: A) => {
    if (inFlight.current) return undefined
    inFlight.current = true
    setBusy(true); setError(null)
    try {
      return await fnRef.current(...args)
    } catch (e) {
      if (alive.current) setError(errorText(e))
      return undefined
    } finally {
      inFlight.current = false
      if (alive.current) setBusy(false)
    }
  }, [])
  const clear = useCallback(() => setError(null), [])
  return { run, busy, error, clear }
}
