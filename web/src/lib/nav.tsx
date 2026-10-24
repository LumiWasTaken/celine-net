/* Routes. One place that knows what a URL means. Real paths (History API), no '#'.

   /new_chat                          its place (the chats): its list, or a new chat, created
                                      on the first send
   /chat/<conversation id>            one chat; a mission moves to /mission/<id>
   /missions                          Missions: the missions
   /mission/<conversation id>         a mission's page, or one of its sub-agents' transcript
   /routines  /routines/<night>       Routines: the night's timeline of runs (a date, default
                                      the server's tonight)
   /routines/<night>/run/<run id>     the same with a run open beside it
   /routines/<night>/schedule/<name>  the same with a schedule open to edit; `new` for a new one
   /routines/<night>/schedule/<name>/edit/<key>[/history]   one of its prompts in the large
                                      editor, or its History
   /routines/<night>/task/<id>        the same with what an event started open (or its sub-agent)
   /notebook                          the notebook (Notebook), the one notebook;
   …?note=<path>[&edit=1|&history=1]  with that note open (in the large editor, or its History)
   /terminals                         Celine's terminals and yours, the commands log (Terminals)
   /terminals?mission=<conversation id>   the same, filtered to that mission
   /terminal/<terminal id>            one terminal: its screen, controls and commands
   /terminal/<terminal id>?reply=<command id>   the same, with that command quoted into its message box
   /browser                           its browser: the live view, its tabs, take over (Browser)
   /artifact/<id>                     one of its artifacts as a page of its own
   /events                            everything it did and everything that ran (Events)
   /intercom  /intercom/<thread id>   the Intercom: threads between you, its conversations and the app
   /chats                             /chats/<source>  /chats/<source>/<chat>  /chats/<source>/<chat>/<message>
   /m/<source>/<message>              a message link; Chats resolves it to its chat and replaces the URL
   /documents?folder=…  /documents/<item id>?folder=…   your documents, one folder (Documents)
   /settings  /settings/<page>[/<sub>]  a list on the left, one page on the right:
       general, it, sources/<source id>, people, search,
       models[/<provider id>], tools, email,
       security (only with the login on)
   /settings/agent/edit/<key>[/history]   one of its long texts (persona, talk, banned) in the
                                      large editor, or its History
   /settings/guard/edit/<key>[/history]   one of Bastet's (persona, talk, note), the same way
   /search?q=…&source=…&chat=…&person=…   the Search page
   /welcome  /welcome/<step>            the onboarding (it, you, model, features, done), shown
                                      without the rail until the features were saved once

   /                                  where you left off on this device (lib/prefs), else
                                      /new_chat; the PWA starts here

   No old links are kept alive: a path that is
   not one of these goes to /new_chat.

   Every in-app <a href="/…"> is routed here without a reload (a click handler on
   document); middle-click and Ctrl-click still open a real tab. The backend serves
   index.html for any path that is not /api or /assets (__main__.py `_spa`). */
import { findDocument } from './refs'
import { getJson, setJson } from './prefs'
import { isDocumentCard, parseRef, refsApi } from './kb'
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode,
} from 'react'

export type Section = 'celine' | 'missions' | 'routines' | 'notebook' | 'terminals' | 'browser' | 'artifact' | 'events' | 'intercom' | 'chats' | 'search' | 'documents' | 'settings' | 'welcome'

export type Route =
  | { section: 'celine'; conversation?: string }
  | { section: 'missions'; conversation?: string }
  | ({ section: 'routines'; night?: string; run?: string; schedule?: string; task?: string } & Editing)
  | { section: 'notebook'; note?: string; edit?: boolean; history?: boolean }
  | { section: 'terminals'; mission?: string; terminal?: string; reply?: string }
  | { section: 'browser' }
  | { section: 'artifact'; id: string }
  | { section: 'events' }
  | { section: 'intercom'; thread?: number }
  | { section: 'chats'; kb?: string; chat?: number; message?: number; resolve?: boolean }
  | ({ section: 'search' } & SearchFilters)
  | { section: 'documents'; item?: number; folder?: string }
  | ({ section: 'settings'; page?: string; sub?: string } & Editing)
  | { section: 'welcome'; step?: string }

/** a long text open in the large editor (`edit`: its key, the place's own words), or its
    History */
export type Editing = { edit?: string; history?: boolean }

export type SearchFilters = {
  q?: string
  kb?: string
  chat?: { ref: string; name: string }
  person?: { ref: string; name: string }
}

type Parsed = { route: Route; redirect?: boolean }

const num = (s?: string) => (s && /^\d+$/.test(s) ? Number(s) : undefined)

/* Path (+ query) → route. `redirect` says the URL should be replaced by format(route). */
function parse(pathname: string, search = ''): Parsed {
  const parts = pathname.split('/').filter(Boolean).map(p => { try { return decodeURIComponent(p) } catch { return p } })
  const [head, a, b, c, d] = parts
  switch (head) {
    case undefined:
      // the app opened at its root: where you left off on this device
      return { route: leftOff(), redirect: true }
    case 'new_chat':
      return { route: { section: 'celine' } }
    case 'chat':
      return a ? { route: { section: 'celine', conversation: a } } : { route: { section: 'celine' }, redirect: true }
    case 'missions':
      return { route: { section: 'missions' } }
    case 'mission':
      return a ? { route: { section: 'missions', conversation: a } } : { route: { section: 'missions' }, redirect: true }
    case 'routines': {
      // a real date, else tonight (the server's)
      const night = a && /^\d{4}-\d{2}-\d{2}$/.test(a) && new Date(`${a}T12:00:00Z`).toISOString().startsWith(a) ? a : undefined
      if (!night) return { route: { section: 'routines' }, redirect: !!a }
      if (b === 'run' && c) return { route: { section: 'routines', night, run: c } }
      if (b === 'schedule' && c) {
        const e = parts[5], h = parts[6]
        if (d === 'edit' && e) return { route: { section: 'routines', night, schedule: c, ...editing(e, h) }, redirect: !!h && h !== 'history' }
        return { route: { section: 'routines', night, schedule: c }, redirect: !!d }
      }
      if (b === 'task' && c) return { route: { section: 'routines', night, task: c } }
      return { route: { section: 'routines', night }, redirect: !!b }
    }
    case 'notebook': {
      const q = new URLSearchParams(search)
      const note = q.get('note') || undefined
      return { route: { section: 'notebook', ...(note && { note, ...(q.get('edit') && { edit: true }), ...(q.get('history') && { history: true }) }) }, redirect: !!a }
    }
    case 'terminals': {
      const mission = new URLSearchParams(search).get('mission')
      return { route: mission ? { section: 'terminals', mission } : { section: 'terminals' } }
    }
    case 'terminal': {
      if (!a) return { route: { section: 'terminals' }, redirect: true }
      const reply = new URLSearchParams(search).get('reply')
      return { route: reply ? { section: 'terminals', terminal: a, reply } : { section: 'terminals', terminal: a } }
    }
    case 'browser':
      return { route: { section: 'browser' } }
    case 'events':
      return { route: { section: 'events' }, redirect: !!a }
    case 'artifact':
      return a ? { route: { section: 'artifact', id: a } } : { route: { section: 'celine' }, redirect: true }
    case 'intercom':
      return { route: { section: 'intercom', thread: num(a) } }
    case 'chats': {
      const chat = num(b), message = chat ? num(c) : undefined
      return { route: { section: 'chats', kb: a, chat, message } }
    }
    case 'm':
      return { route: { section: 'chats', kb: a, message: num(b), resolve: true } }
    case 'documents': {
      const folder = new URLSearchParams(search).get('folder') || undefined
      return { route: { section: 'documents', item: num(a), folder } }
    }
    case 'settings':
      // her page's and Bastet's long texts: /settings/agent|guard/edit/<key>[/history]
      if ((a === 'agent' || a === 'guard') && b === 'edit' && c) return { route: { section: 'settings', page: a, ...editing(c, d) }, redirect: !!d && d !== 'history' }
      return { route: { section: 'settings', page: a, sub: b } }
    case 'welcome':
      return { route: { section: 'welcome', step: a }, redirect: !!b }
    case 'search': {
      const p = new URLSearchParams(search)
      const r: Route = { section: 'search' }
      if (p.get('q')) r.q = p.get('q')!
      if (p.get('source')) r.kb = p.get('source')!
      if (p.get('chat')) r.chat = { ref: p.get('chat')!, name: p.get('chat_name') ?? p.get('chat')! }
      if (p.get('person')) r.person = { ref: p.get('person')!, name: p.get('person_name') ?? p.get('person')! }
      return { route: r }
    }
    default:
      return { route: { section: 'celine' }, redirect: true }
  }
}

/* where you left off, on this device: the place on screen and each place's last route
   (a rail click goes back there, also after a reload). Setup and an artifact page are
   never a place to come back to. */
const LEFT_OFF = 'nav'
type LeftOff = { at?: string; places?: Partial<Record<Section, string>> }
const stay = (r: Route) => r.section !== 'welcome' && r.section !== 'artifact'

/** a path kept on this device as its route, or null (not a path, the root, a place not to come back to) */
function kept(path: unknown): Route | null {
  if (typeof path !== 'string' || !path.startsWith('/') || path === '/') return null
  const u = new URL(path, location.origin)
  const r = parse(u.pathname, u.search).route
  return stay(r) ? r : null
}

function leftOff(): Route {
  return kept(getJson<LeftOff>(LEFT_OFF, {}).at) ?? { section: 'celine' }
}

function placesLeft(): Partial<Record<Section, Route>> {
  const out: Partial<Record<Section, Route>> = {}
  for (const [s, path] of Object.entries(getJson<LeftOff>(LEFT_OFF, {}).places ?? {})) {
    const r = kept(path)
    if (r?.section === s) out[r.section] = r
  }
  return out
}

export function format(r: Route): string {
  const e = encodeURIComponent
  switch (r.section) {
    case 'celine':
      return r.conversation ? `/chat/${e(r.conversation)}` : '/new_chat'
    case 'missions':
      return r.conversation ? `/mission/${e(r.conversation)}` : '/missions'
    case 'routines':
      if (!r.night) return '/routines'
      return `/routines/${r.night}` + (r.run ? `/run/${e(r.run)}` : r.schedule ? `/schedule/${e(r.schedule)}${r.edit ? editPath(r) : ''}`
        : r.task ? `/task/${e(r.task)}` : '')
    case 'notebook':
      return '/notebook' + (r.note ? `?note=${e(r.note)}${r.history ? '&history=1' : r.edit ? '&edit=1' : ''}` : '')
    case 'terminals':
      if (r.terminal) return `/terminal/${e(r.terminal)}` + (r.reply ? `?reply=${e(r.reply)}` : '')
      return '/terminals' + (r.mission ? `?mission=${e(r.mission)}` : '')
    case 'browser':
      return '/browser'
    case 'events':
      return '/events'
    case 'artifact':
      return `/artifact/${e(r.id)}`
    case 'intercom':
      return '/intercom' + (r.thread ? `/${r.thread}` : '')
    case 'chats':
      if (r.resolve && r.kb && r.message) return `/m/${e(r.kb)}/${r.message}`
      return '/chats' + (r.kb ? `/${e(r.kb)}` : '') + (r.kb && r.chat ? `/${r.chat}` : '') +
        (r.kb && r.chat && r.message ? `/${r.message}` : '')
    case 'search':
      return formatSearch(r)
    case 'documents':
      return '/documents' + (r.item ? `/${r.item}` : '') + (r.folder ? `?folder=${encodeURIComponent(r.folder)}` : '')
    case 'settings':
      return '/settings' + (r.page ? `/${e(r.page)}` : '') + (r.page && r.edit ? editPath(r) : r.page && r.sub ? `/${e(r.sub)}` : '')
    case 'welcome':
      return '/welcome' + (r.step ? `/${e(r.step)}` : '')
  }
}

/** the parts of an /edit/<key>[/history] path */
function editing(key: string, rest?: string): Editing {
  return { edit: key, ...(rest === 'history' && { history: true }) }
}

function editPath(r: Editing): string {
  return `/edit/${encodeURIComponent(r.edit!)}${r.history ? '/history' : ''}`
}

function formatSearch(f: SearchFilters): string {
  const p = new URLSearchParams()
  if (f.q) p.set('q', f.q)
  if (f.kb) p.set('source', f.kb)
  if (f.chat) { p.set('chat', f.chat.ref); p.set('chat_name', f.chat.name) }
  if (f.person) { p.set('person', f.person.ref); p.set('person_name', f.person.name) }
  const s = p.toString()
  return '/search' + (s ? `?${s}` : '')
}

/* A message link "whatsapp:123" → its route. */
function refRoute(ref: string): Route | null {
  if (/^documents:/.test(ref)) return null
  const m = parseRef(ref)
  return m ? { section: 'chats', kb: m.kb, message: m.id, resolve: true } : null
}

export function refHref(ref: string): string | null {
  const r = refRoute(ref)
  return r ? format(r) : null
}

export type Nav = {
  route: Route
  go: (r: Route, opts?: { replace?: boolean }) => void
  /** a rail click: back to where you last were in that section */
  goSection: (s: Section) => void
  /** navigate to an in-app path string ('/chats/…') */
  goPath: (path: string, opts?: { replace?: boolean }) => void
  openRef: (ref: string) => void
  /** a document by id, or by its name ([[Some file]] in a note) */
  openDocument: (idOrName: number | string) => void
}

const Ctx = createContext<Nav | null>(null)

export function NavProvider({ children }: { children: ReactNode }) {
  const [route, setRoute] = useState<Route>(() => {
    const parsed = parse(location.pathname, location.search)
    if (parsed.redirect) history.replaceState(history.state, '', format(parsed.route))
    return parsed.route
  })
  const [kept0] = useState(placesLeft)
  const last = useRef<Partial<Record<Section, Route>>>(kept0)
  last.current[route.section] = route
  useEffect(() => {
    if (!stay(route)) return
    const places: Partial<Record<Section, string>> = {}
    for (const r of Object.values(last.current)) if (r && stay(r)) places[r.section] = format(r)
    setJson(LEFT_OFF, { at: format(route), places } satisfies LeftOff)
  }, [route])
  const routeRef = useRef(route)
  routeRef.current = route

  // back / forward
  useEffect(() => {
    const sync = () => {
      const parsed = parse(location.pathname, location.search)
      if (parsed.redirect) history.replaceState(history.state, '', format(parsed.route))
      setRoute(parsed.route)
    }
    window.addEventListener('popstate', sync)
    return () => { window.removeEventListener('popstate', sync) }
  }, [])

  const push = useCallback((url: string, replace?: boolean) => {
    const here = location.pathname + location.search
    if (url === here) return
    if (replace) history.replaceState(history.state, '', url)
    else history.pushState(null, '', url)
  }, [])

  const go = useCallback((r: Route, opts?: { replace?: boolean }) => {
    push(format(r), opts?.replace)
    setRoute(r)
  }, [push])

  const goPath = useCallback((path: string, opts?: { replace?: boolean }) => {
    const u = new URL(path, location.origin)
    const parsed = parse(u.pathname, u.search)
    const r = parsed.route
    push(format(r), opts?.replace)
    setRoute(r)
  }, [push])

  const goSection = useCallback((s: Section) => {
    if (routeRef.current.section === s) return
    go(last.current[s] ?? { section: s } as Route)
  }, [go])

  const openRef = useCallback((ref: string) => {
    // a quote from a document opens that file; its id comes with the card
    if (/^documents:/.test(ref)) {
      void refsApi.resolve([ref]).then(res => {
        const c = res.refs[ref]
        go({ section: 'documents', item: c && isDocumentCard(c) ? c.item.id : undefined })
      }).catch(() => go({ section: 'documents' }))
      return
    }
    const r = refRoute(ref)
    if (r) go(r)
  }, [go])

  const openDocument = useCallback((x: number | string) => {
    if (typeof x === 'number') { go({ section: 'documents', item: x }); return }
    void findDocument(x).then(it => go({ section: 'documents', item: it?.id })).catch(() => go({ section: 'documents' }))
  }, [go])

  // in-app links: <a href="/chats/…"> routes here, no reload
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      const a = (e.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null
      if (!a || (a.target && a.target !== '_self') || a.hasAttribute('download')) return
      const raw = a.getAttribute('href') ?? ''
      if (raw.startsWith('#')) return
      let u: URL
      try { u = new URL(a.href) } catch { return }
      if (u.origin !== location.origin) return
      if (/^\/(api|assets)(\/|$)/.test(u.pathname)) return
      e.preventDefault()
      goPath(u.pathname + u.search)
    }
    document.addEventListener('click', onClick)
    return () => document.removeEventListener('click', onClick)
  }, [goPath])

  const value = useMemo<Nav>(() => ({
    route, go, goSection, goPath, openRef, openDocument,
  }), [route, go, goSection, goPath, openRef, openDocument])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useNav(): Nav {
  const v = useContext(Ctx)
  if (!v) throw new Error('useNav outside NavProvider')
  return v
}
