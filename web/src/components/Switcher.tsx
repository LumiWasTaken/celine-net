/* Ctrl+K: one sheet with a filter field over every conversation (both places), mission and
   sub-agent, document, intercom thread, schedule and day of Routines; the arrow keys move,
   Enter opens. And "?": the sheet that lists the shortcuts. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Bot, CalendarClock, FileText, Mail } from 'lucide-react'
import { api, type ConversationSummary } from '../lib/api'
import { agentsApi, type Agent } from '../lib/agents'
import { documentsApi } from '../lib/documents'
import { listTime, nothingMatches, shortDate } from '../lib/format'
import { intercomApi } from '../lib/intercom'
import { nightWords, routinesApi } from '../lib/routines'
import { useNav, type Route } from '../lib/nav'
import { kindIcon } from './ConversationList'
import { useResource } from '../lib/useResource'
import { EmptyState, ICON, InlineError, ListRow, LoadingRow, SearchField, Sheet } from '../ui'

/** `route`, or the server's path (a conversation's link) */
type Item = { key: string; id: string; route: Route | string; title: string; detail: string; icon: typeof Bot; at: string | number; meta?: string }

/* the places beyond the conversations: each one on its own, so one that fails leaves the others */
async function others(): Promise<Item[]> {
  const none = () => [] as Item[]
  const docs = documentsApi.items({ sort: 'changed', order: 'desc', limit: 200 }).then(r => r.items.map(d => ({
    key: `d:${d.id}`, id: String(d.id), route: { section: 'documents', item: d.id }, title: d.title,
    detail: d.folder ? `Document in ${d.folder}` : 'Document', icon: FileText, at: d.updated_at * 1000,
  }) as Item), none)
  const threads = intercomApi.threads({ with: 'user', kind: null }).then(p => p.groups.flatMap(g => g.items).map(t => ({
    key: `i:${t.thread}`, id: String(t.thread), route: { section: 'intercom', thread: t.thread },
    title: t.subject || t.name, detail: t.subject ? `Intercom with ${t.name}` : 'Intercom', icon: Mail, at: t.last.created_at,
  }) as Item), none)
  const schedules = routinesApi.schedules().then(r => r.schedules.map(s => ({
    key: `s:${s.name}`, id: s.name, route: { section: 'routines', night: r.tonight, schedule: s.name }, title: s.title,
    detail: `Schedule, ${s.on ? s.every : 'Off'}`, icon: CalendarClock, at: 0,
  }) as Item), none)
  const nights = routinesApi.runs().then(r => r.nights.map(n => ({
    key: `n:${n.night}`, id: n.night, route: { section: 'routines', night: n.night },
    title: nightWords(n.night),
    detail: 'Routines', icon: CalendarClock, at: 0, meta: shortDate(new Date(`${n.night}T12:00:00`).getTime()),
  }) as Item), none)
  return (await Promise.all([docs, threads, schedules, nights])).flat()
}

export function Switcher({ onClose }: { onClose: () => void }) {
  const nav = useNav()
  const data = useResource(() => Promise.all([api.listConversations(), agentsApi.list().catch(() => [] as Agent[]), others()]), [])
  const [q, setQ] = useState('')
  const [at, setAt] = useState(0)
  const items = useMemo<Item[]>(() => {
    if (!data.data) return []
    const [list, agents, rest] = data.data
    const out: Item[] = []
    const byId = new Map<string, ConversationSummary>(list.map(c => [c.id, c]))
    for (const c of list) {
      if (!c.link) continue
      out.push({
        key: c.id, id: c.id, route: c.link, at: c.updated_at,
        title: c.mission_name || c.title || 'Untitled', icon: kindIcon(c),
        detail: c.kind === 'mission' ? 'Mission' : '',
      })
    }
    for (const a of agents) {
      const parent = byId.get(a.parent)
      out.push({ key: `a:${a.id}`, id: a.id, route: a.link, at: a.updated_at, title: a.name, icon: Bot,
                 detail: parent ? `Sub-agent of ${parent.mission_name || parent.title || 'Untitled'}` : 'Sub-agent' })
    }
    out.push(...rest)
    const query = q.trim().toLowerCase()
    const words = query.split(/\s+/).filter(Boolean)
    if (!words.length) return out
    // the name itself first, then a name that starts with it, then names that hold every word
    const rank = (i: Item) => {
      const t = i.title.toLowerCase()
      return t === query ? 0 : t.startsWith(query) ? 1 : words.every(w => t.includes(w)) ? 2 : 3
    }
    return out.filter(i => words.every(w => `${i.title} ${i.detail}`.toLowerCase().includes(w)))
      .map(i => [rank(i), i] as const).sort((a, b) => a[0] - b[0]).map(([, i]) => i)
  }, [data.data, q])
  useEffect(() => setAt(0), [q])
  const list = useRef<HTMLDivElement>(null)
  useEffect(() => { list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }) }, [at])
  const go = (i: Item | undefined) => {
    if (!i) return
    onClose()
    if (typeof i.route === 'string') nav.goPath(i.route)
    else nav.go(i.route)
  }

  return (
    <Sheet title="Go to" onClose={onClose}>
      <SearchField autoFocus className="switcher__field" value={q} onChange={setQ} placeholder="Search"
                   listbox={{ id: 'switcher-list', active: items[at] ? `sw-${items[at]!.key}` : undefined }}
                   onKeyDown={e => {
                     if (e.key === 'ArrowDown') { e.preventDefault(); setAt(n => Math.min(items.length - 1, n + 1)) }
                     if (e.key === 'ArrowUp') { e.preventDefault(); setAt(n => Math.max(0, n - 1)) }
                     if (e.key === 'Enter') { e.preventDefault(); go(items[at]) }
                     // Esc closes, with or without words typed
                     if (e.key === 'Escape') { e.preventDefault(); onClose() }
                   }} />
      {data.error && <InlineError compact message={data.error} onRetry={data.reload} />}
      {!data.data && !data.error && <LoadingRow />}
      {data.data && (
        <div ref={list} className="switcher" id="switcher-list" role="listbox" aria-label="Results">
          {items.length === 0 && <EmptyState compact>{nothingMatches(q)}</EmptyState>}
          {items.slice(0, 80).map((i, n) => (
            <div key={i.key} id={`sw-${i.key}`} role="option" aria-selected={n === at} onMouseEnter={() => setAt(n)}>
              <ListRow className="switcher__row" title={i.title} subtitle={i.detail || undefined} selected={n === at}
                       leading={<i.icon size={ICON.md} className="switcher__icon" aria-hidden />}
                       meta={i.meta ?? listTime(i.at)} onClick={() => go(i)} />
            </div>
          ))}
        </div>
      )}
    </Sheet>
  )
}

const KEYS: [string, string][] = [
  ['Ctrl K', 'Go to a conversation, a document, a thread, a schedule or a day of Routines'],
  ['Ctrl Shift O', 'New conversation here'],
  ['Alt Up, Alt Down', 'Go to the conversation above or below'],
  ['Enter', 'Send'],
  ['Shift Enter', 'New line'],
  ['Esc', 'Stop the reply when the message box is empty, or close an open run or schedule'],
  ['Ctrl Enter, Ctrl S', 'Save a schedule, start a mission'],
  ['Left, Right', 'Routines: go to the day before or after'],
  ['Up, Down', 'Routines: go to the run above or below'],
  ['N', 'Routines: new schedule'],
  ['?', 'Show keyboard shortcuts'],
]

export function Shortcuts({ onClose }: { onClose: () => void }) {
  return (
    <Sheet title="Shortcuts" onClose={onClose}>
      <dl className="shortcuts">
        {KEYS.map(([k, what]) => (
          <div key={k}><dt>{k.split(', ').map((c, i) => <span key={c}>{i > 0 && ' or '}{c.split(' ').map(x => <kbd key={x}>{x}</kbd>)}</span>)}</dt><dd>{what}</dd></div>
        ))}
      </dl>
    </Sheet>
  )
}
