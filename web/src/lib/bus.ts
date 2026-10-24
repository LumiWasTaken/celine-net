/* The global feed of WS /ws/events (lib/socket.ts), fanned out to whichever view cares,
   with `open` (load again: the feed is followed from now) and `changes` (GET
   /api/changes after a reconnect). */
import { useEffect, useRef } from 'react'
import type { Task } from './kb'
import type { Changes } from './socket'

export type ServerEvent =
  | { type: 'kb.task'; task: Task }
  | { type: 'kb.changed'; kb: string }
  | { type: 'open' }
  | ({ type: 'changes' } & Changes)
  | { type: string; [k: string]: unknown }

type Listener = (ev: ServerEvent) => void
const listeners = new Set<Listener>()

/* "Ask Celine" (search, a message) sends `celine.prefill`; Celine may not be on screen
   yet, so the draft also waits here until it takes it. */
export type Prefill = { text: string; refs: string[] }
let prefill: Prefill | null = null
export function takePrefill(): Prefill | null {
  const t = prefill
  prefill = null
  return t
}

export function emit(ev: ServerEvent) {
  if (ev.type === 'celine.prefill' && typeof ev.text === 'string') {
    prefill = { text: ev.text, refs: Array.isArray(ev.refs) ? ev.refs.filter((r): r is string => typeof r === 'string') : [] }
  }
  for (const l of listeners) {
    try { l(ev) } catch { /* one bad listener does not stop the rest */ }
  }
}

export function useServerEvents(fn: Listener) {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    const l: Listener = ev => ref.current(ev)
    listeners.add(l)
    return () => { listeners.delete(l) }
  }, [])
}
