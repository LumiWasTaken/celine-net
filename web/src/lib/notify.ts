/* System notifications while the tab is hidden or not focused: the feed's `notice` events
   (server/celine/events.py `notice`: kind, title, body, link, tag) as a plain
   `new Notification` (the app has no service worker). One per item: the server's tag, so a
   burst of one item replaces its notification. A click focuses the tab and opens the
   server's link. Nothing shows while the tab is visible and focused.

   Which kinds show is saved per device (localStorage), behind one switch that is off until
   you turn it on (Settings, Notifications). The browser's permission is watched
   (navigator.permissions and its change, else on focus), so a revoke or a grant shows at
   once; switched on but no longer allowed: one toast a session, to the page. */
import { useEffect, useSyncExternalStore } from 'react'
import { useServerEvents } from './bus'
import { format } from './nav'
import { toast } from '../ui'
import { getJson, setJson } from './prefs'

/** the kinds of the server's notices, by their switch's word */
export const KINDS = [
  { id: 'reply', label: 'Replies' },
  { id: 'intercom', label: 'Intercom' },
  { id: 'mission', label: 'Missions' },
  { id: 'run', label: 'Routines' },
  { id: 'approval', label: 'Approvals' },
] as const
export type Kind = typeof KINDS[number]['id']
export type Prefs = { on: boolean } & Record<Kind, boolean>

const KEY = 'celine.notifications'
const ASKED = 'celine.notifications.blocked'
const DEFAULTS: Prefs = { on: false, reply: true, intercom: true, mission: true, run: true, approval: true }

/** the browser's word, or why there is none: not https (no secure context), no API */
export type Permission = NotificationPermission | 'insecure' | 'unsupported'

const subs = new Set<() => void>()
const changed = () => { for (const f of subs) f() }
const subscribe = (f: () => void) => { subs.add(f); return () => { subs.delete(f) } }

function readPrefs(): Prefs {
  return { ...DEFAULTS, ...getJson<Partial<Prefs>>(KEY, {}) }
}
let prefs = readPrefs()

export function setPrefs(p: Partial<Prefs>) {
  prefs = { ...prefs, ...p }
  setJson(KEY, prefs)
  changed()
}

const readPermission = (): Permission =>
  !window.isSecureContext ? 'insecure' : !('Notification' in window) ? 'unsupported' : Notification.permission
let permission = readPermission()

function recheck() {
  const p = readPermission()
  if (p !== permission) { permission = p; changed() }
}

let watching = false
function watch() {
  if (watching) return
  watching = true
  // another tab changed the switches
  window.addEventListener('storage', e => { if (e.key === KEY) { prefs = readPrefs(); changed() } })
  // Firefox and Safari have no change event for this permission: the focus asks again
  window.addEventListener('focus', recheck)
  document.addEventListener('visibilitychange', recheck)
  void navigator.permissions?.query({ name: 'notifications' })
    .then(s => s.addEventListener('change', recheck), () => undefined)
}

export function useNotifyPrefs(): Prefs {
  useEffect(watch, [])
  return useSyncExternalStore(subscribe, () => prefs)
}

export function usePermission(): Permission {
  useEffect(watch, [])
  return useSyncExternalStore(subscribe, () => permission)
}

/** the browser's ask, from a click; granted turns the notifications on */
export async function allow() {
  if (permission === 'insecure' || permission === 'unsupported') return
  const p = await Notification.requestPermission()
  recheck()
  if (p === 'granted') setPrefs({ on: true })
}

/** the Check again of a blocked permission */
export const checkAgain = recheck

type Notice = { kind: string; title: string; body: string; link: string | null; tag: string }

function show(n: Notice, open: (link: string) => void, icon: string) {
  if (permission !== 'granted' || !prefs.on || !prefs[n.kind as Kind]) return
  if (document.visibilityState === 'visible' && document.hasFocus()) return
  let shown: Notification
  // note: Chrome on Android throws here (it shows only through a service worker); add one when the phone needs them
  try { shown = new Notification(n.title, { body: n.body, tag: n.tag, icon }) } catch { return }
  shown.onclick = () => {
    window.focus()
    if (n.link) open(n.link)
    shown.close()
  }
}

/** the shell's one call: shows the feed's notices; `icon` its portrait's url */
export function useNotices(open: (link: string) => void, icon: string | undefined) {
  const p = useNotifyPrefs()
  const perm = usePermission()
  useServerEvents(ev => {
    if (ev.type === 'notice') show(ev as unknown as Notice, open, icon ?? '/icon-192.png')
  })
  // switched on here, but the browser no longer allows them: said once a session
  useEffect(() => {
    if (!p.on || perm === 'granted') return
    try {
      if (sessionStorage.getItem(ASKED)) return
      sessionStorage.setItem(ASKED, '1')
    } catch { /* said again on the next load */ }
    toast({ title: 'Notifications', text: 'Blocked in this browser',
            action: { label: 'Open', onClick: () => open(format({ section: 'settings', page: 'notifications' })) } })
  }, [p.on, perm]) // eslint-disable-line react-hooks/exhaustive-deps
}
