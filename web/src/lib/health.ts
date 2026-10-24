/* Is the server there at all? Only /api/health decides: a request that fails to
   connect (or a gateway 502/503/504) starts a check, and the server counts as gone
   only when two checks a few seconds apart both fail. An ordinary error (a 404, a 5xx
   from one route, one failed request) never marks it gone; it is shown where it
   happened. While gone, /api/health is asked every few seconds, and the first answer
   brings the app back. Read with useApiDown(); the socket adds its own signal
   (lib/socket.ts, useServerDown).

   The same check notices a session that ended: with the login on, every route answers
   the gate's JSON 404 without a session, /api/health too. Then the page goes to "/",
   which is the login screen (the server's own page, not this app, so nothing loops).
   A 404 that is not the gate's (a proxy with no route while the app restarts) counts
   as no answer. */
import { useSyncExternalStore } from 'react'

const CONFIRM_MS = 3000
const PROBE_MS = 3000

let down = false
let checking = false
let leaving = false
let probe: number | undefined
let inFlight: Promise<boolean> | null = null
const listeners = new Set<() => void>()

function set(next: boolean) {
  if (next === down) return
  down = next
  for (const fn of listeners) fn()
  window.clearInterval(probe)
  probe = next ? window.setInterval(() => void healthy().then(ok => { if (ok) set(false) }), PROBE_MS) : undefined
}

/* One request at a time, however many callers ask. */
function healthy(): Promise<boolean> {
  inFlight ??= ask().finally(() => { inFlight = null })
  return inFlight
}

async function ask(): Promise<boolean> {
  let res: Response
  try { res = await fetch('/api/health', { cache: 'no-store' }) } catch { return false }
  if (res.status === 404 && await isGate404(res)) {
    if (!leaving) { leaving = true; location.replace('/') }
    return false
  }
  return res.ok
}

async function isGate404(res: Response): Promise<boolean> {
  try {
    const body = await res.json() as { error?: { code?: unknown } }
    return body?.error?.code === 'not_found'
  } catch { return false }
}

/* two failed checks, CONFIRM_MS apart: gone */
async function check() {
  if (checking || down) return
  checking = true
  try {
    if (await healthy()) return
    await new Promise(r => setTimeout(r, CONFIRM_MS))
    if (!(await healthy())) set(true)
  } finally { checking = false }
}

/** status 0: no answer at all. Only a failure that looks like a missing server starts a
    check. A 404 may mean the session ended: /api/health tells. */
export function reportApi(status: number) {
  if (status === 0 || status === 502 || status === 503 || status === 504) void check()
  else if (status === 404) void healthy()
}

/** A socket closed: the session may have ended (the gate refuses the handshake). */
export function checkSession() {
  void healthy()
}

export function useApiDown(): boolean {
  return useSyncExternalStore(fn => { listeners.add(fn); return () => { listeners.delete(fn) } }, () => down)
}
