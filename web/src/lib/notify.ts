/* System notifications: the feed's `notice` events (server/celine/events.py `notice`: kind,
   title, body, link, tag) while the tab is hidden or not focused, and the server's pushes
   (server/celine/push.py) while no page of the app is open and focused. Both show through
   the service worker (web/public/sw.js, registered here), else as a plain `new Notification`
   (a browser without service workers: Firefox's private windows). One per item: the server's
   tag, so a burst of one item, or its notice and its push, replace each other. A click
   focuses the app and opens the server's link. Nothing shows while the tab is visible and
   focused.

   Which kinds show is saved per device (localStorage), behind one switch that is off until
   you turn it on (Settings, Notifications); a device that takes pushes gives the server the
   same kinds. The browser's permission is watched (navigator.permissions and its change,
   else on focus), so a revoke or a grant shows at once; switched on but no longer allowed:
   one toast a session, to the page. */
import { useEffect, useSyncExternalStore } from 'react'
import { useServerEvents } from './bus'
import { format } from './nav'
import { toast } from '../ui'
import { getJson, setJson } from './prefs'
import { del, request } from './api'

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
  if (endpoint) void subscribePush().catch(() => undefined)     // the server's copy of the kinds
}

const kindsOn = () => KINDS.map(k => k.id).filter(k => prefs[k])

/** the service worker at the root scope; null without one (no secure context, a private window) */
const worker: Promise<ServiceWorkerRegistration | null> =
  window.isSecureContext && 'serviceWorker' in navigator
    ? navigator.serviceWorker.register('/sw.js').catch(() => null)
    : Promise.resolve(null)

/** a device that takes pushes (server/celine/push.py) */
export type PushDevice = { id: number; device: string; endpoint: string; kinds: string[]; created_at: string }

export const pushApi = {
  key: () => request<{ key: string }>('/push/key'),
  list: (signal?: AbortSignal) =>
    request<{ subscriptions: PushDevice[] }>('/push-subscriptions', { signal }).then(r => r.subscriptions),
  add: (sub: PushSubscriptionJSON, kinds: string[]) =>
    request<PushDevice>('/push-subscriptions', { method: 'POST', body: JSON.stringify({ ...sub, kinds }) }),
  remove: (id: number) => del<{ deleted: boolean }>(`/push-subscriptions/${id}`),
}

/** this browser's push endpoint, null while it has none; Settings finds it in the server's list */
let endpoint: string | null = null
let canPush = false
void worker.then(async r => {
  canPush = !!r && 'pushManager' in r
  endpoint = (await r?.pushManager.getSubscription())?.endpoint ?? null
  changed()
}).catch(() => undefined)

export const usePushEndpoint = () => useSyncExternalStore(subscribe, () => endpoint)
/** false: this browser takes no pushes (no service worker; iPhone: only from the Home Screen) */
export const useCanPush = () => useSyncExternalStore(subscribe, () => canPush)

const keyBytes = (b64: string) =>
  Uint8Array.from(atob(b64.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0))

/** Push to this device on: the browser's subscription (asking for the permission first),
    given to the server with this device's kinds */
export async function subscribePush() {
  const reg = await worker
  if (!reg) return
  if (Notification.permission !== 'granted') {
    const p = await Notification.requestPermission()
    recheck()
    if (p !== 'granted') return
  }
  const sub = await reg.pushManager.getSubscription() ?? await reg.pushManager.subscribe({
    userVisibleOnly: true, applicationServerKey: keyBytes((await pushApi.key()).key) })
  await pushApi.add(sub.toJSON(), kindsOn())
  endpoint = sub.endpoint
  changed()
}

/** Push to this device off: out of the server's list, and the browser's subscription ended */
export async function unsubscribePush(devices: PushDevice[]) {
  const sub = await (await worker)?.pushManager.getSubscription()
  const row = devices.find(d => d.endpoint === sub?.endpoint)
  if (row) await pushApi.remove(row.id)
  await sub?.unsubscribe()
  endpoint = null
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

async function show(n: Notice, open: (link: string) => void, icon: string) {
  if (permission !== 'granted' || !prefs.on || !prefs[n.kind as Kind]) return
  if (document.visibilityState === 'visible' && document.hasFocus()) return
  const reg = await worker
  // its click: the service worker's notificationclick, which posts {open} to the page
  if (reg) { await reg.showNotification(n.title, { body: n.body, tag: n.tag, icon, data: { link: n.link } }); return }
  const shown = new Notification(n.title, { body: n.body, tag: n.tag, icon })
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
    if (ev.type === 'notice') void show(ev as unknown as Notice, open, icon ?? '/icon-192.png').catch(() => undefined)
  })
  // a notification's click, from the service worker
  useEffect(() => {
    const sw = navigator.serviceWorker
    if (!sw) return
    const onMessage = (e: MessageEvent) => { if (typeof e.data?.open === 'string') open(e.data.open) }
    sw.addEventListener('message', onMessage)
    return () => sw.removeEventListener('message', onMessage)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
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
