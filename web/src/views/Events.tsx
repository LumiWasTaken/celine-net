/* Events: everything it did and everything that ran, newest first, day by day (GET
   /api/events): its turns, tool calls, notebook writes, background jobs, messages to you,
   Routine runs and Bastet's decisions. One page, no place sidebar. Three tabs the server
   fills (Guard, Celine, Background: events.py TABS), then the Toolbar: All,
   Failed, on Guard also Stopped and Could not decide (Bastet could not decide, apart from
   what failed), Type and Date; on Guard also Decision, Rule and Mode. A row opens where it
   happened; a row of Bastet's opens her decision (EventDecision), and its menu gives Allow
   once and Pass. The words, the filters' options and the links are the server's;
   `events.added` loads the list again. URL: /events[/<tab>], /events/guard/<decision id>
   (nav.tsx); a tab keeps its Type and Bastet's filters to itself. */
import { useRef, useState } from 'react'
import { Activity, CalendarDays, Eye, Info, Scale, SearchX, Shapes, ShieldCheck } from 'lucide-react'
import { clock } from '../lib/format'
import { eventsApi, isEventsEvent, type EventRow, type EventsState, type EventsTab } from '../lib/events'
import { useResource } from '../lib/useResource'
import { useNav } from '../lib/nav'
import {
  Button, GroupHead, ListRow, Loaded, PlaceHead, Segmented, StateWord, Toolbar, useContextMenu, VirtualList, type MenuItem,
} from '../ui'
import { ACTION_ICONS, ACTION_WORDS } from '../lib/guard'
import { EventDecision, answer } from './EventDecision'

const PAGE = 100
const MOST = 500       // the server's most per request
type Bastet = { decision: string | null; rule: string | null; mode: string | null }
const NO_BASTET: Bastet = { decision: null, rule: null, mode: null }

// the State filter's options that only Bastet's rows have
const BASTET_STATES: (EventsState | null)[] = ['undecided', 'stopped']

export function Events({ active }: { active: boolean }) {
  const nav = useNav()
  // the tab and decision the URL names; while another place is on screen, the ones it showed last
  const last = useRef<{ tab: EventsTab; decision?: number }>({ tab: 'agent' })
  if (nav.route.section === 'events') last.current = { tab: nav.route.tab ?? 'agent', decision: nav.route.decision }
  const { tab, decision } = last.current
  const [day, setDay] = useState<string | null>(null)
  const [stateRaw, setState] = useState<EventsState | null>(null)
  // a tab has its own Types, and Bastet's filters and states only on Guard
  const [own, setOwn] = useState<{ tab: EventsTab; kind: string | null; bastet: Bastet }>({ tab, kind: null, bastet: NO_BASTET })
  const kind = own.tab === tab ? own.kind : null
  const bastet = own.tab === tab ? own.bastet : NO_BASTET
  const state = tab === 'guard' || !BASTET_STATES.includes(stateRaw) ? stateRaw : null
  const setBastet = (b: Bastet) => setOwn({ tab, kind, bastet: b })
  const ctx = useContextMenu()
  // Bastet's filters belong to her Type: another Type drops them
  const setKind = (k: string | null) => {
    const hers = k === 'guard' || k === null
    setOwn({ tab, kind: k, bastet: hers ? bastet : NO_BASTET })
    if (!hers && BASTET_STATES.includes(state)) setState(null)
  }
  const setTab = (t: EventsTab) => {
    nav.go(t === 'agent' ? { section: 'events' } : { section: 'events', tab: t })
    if (BASTET_STATES.includes(state)) setState(null)
  }
  const show = (e: EventRow) => { if (e.guard) nav.go({ section: 'events', tab: 'guard', decision: e.guard.id }) }
  // the pages loaded belong to the filters they were loaded for: new filters start at one
  const filter = `${tab}|${kind}|${day}|${state}|${bastet.decision}|${bastet.rule}|${bastet.mode}`
  const [more, setMore] = useState({ filter, pages: 1 })
  const pages = more.filter === filter ? more.pages : 1
  const list = useResource(async () => {
    const q = { tab, kind, day, state, ...bastet }, want = PAGE * pages
    const out = await eventsApi.list({ ...q, limit: Math.min(want, MOST) })
    while (out.next && out.events.length < want) {
      const r = await eventsApi.list({ ...q, before: out.next, limit: Math.min(want - out.events.length, MOST) })
      out.events.push(...r.events)
      out.next = r.next
    }
    return out
  }, [tab, kind, day, state, bastet, pages], { events: isEventsEvent, enabled: active, debounce: 250 })

  const data = list.data
  const rows = data?.events ?? []
  type Row = { key: string; day: string } | { key: string; e: EventRow }
  const items: Row[] = rows.flatMap((e, i) => [
    ...(i === 0 || rows[i - 1]!.day !== e.day ? [{ key: `d:${e.day}`, day: e.day }] : []),
    { key: e.id, e },
  ])
  const menu = (e: EventRow): MenuItem[] => !e.guard ? [] : [
    { label: 'Details', icon: Info, onSelect: () => show(e) },
    ...e.guard.actions.map(a => ({ label: ACTION_WORDS[a], icon: ACTION_ICONS[a], onSelect: () => void answer(e.guard!.id, a) })),
  ]
  const draw = (r: Row) => {
    if ('day' in r) return <GroupHead title={r.day} />
    const e = r.e
    const common = { running: e.tone === 'running', title: e.title, tooltip: e.title, subtitle: e.detail || undefined, column: e.where ? <span>{e.where}</span> : '',
                     cell: e.tone === 'ok' ? undefined : <StateWord tone={e.tone}>{e.outcome}</StateWord>, meta: clock(Date.parse(e.at)) }
    if (!e.guard) return <ListRow {...common} href={e.links.open ?? undefined} />
    return <ListRow {...common} onClick={() => show(e)} onContextMenu={ev => ctx.open(ev, menu(e), e.title)}
                    menu={menu(e)} />
  }
  const words = (list: { id: string; label: string }[] | undefined) => (id: string) => list?.find(x => x.id === id)?.label ?? id
  const kindLabel = data?.kinds.find(k => k.id === kind)?.label
  const dayLabel = data?.days.find(d => d.value === day)?.label
  const pick = (k: keyof Bastet, label: string, icon: typeof Shapes, opts: { id: string; label: string }[]) => ({
    label, icon, picked: bastet[k] ? words(opts)(bastet[k]!) : undefined, onClear: () => setBastet({ ...bastet, [k]: null }),
    items: opts.map(o => ({ label: o.label, current: o.id === bastet[k], onSelect: () => setBastet({ ...bastet, [k]: o.id }) })),
  })
  const clear = () => { setOwn({ tab, kind: null, bastet: NO_BASTET }); setDay(null); setState(null) }
  const none = !!data && !!data.tabs && data.tabs.every(t => t.total === 0) && !day
  // an empty tab with nothing picked is empty, not a search that found nothing
  const filtered = !!(kind || day || state || Object.values(bastet).some(Boolean))
  return (
    <div className="list-page">
      <PlaceHead level={1} title="Events" />
      {data && !none && (
        <div className="list-page__top">
          {data.tabs && (
            <Segmented label="Events" value={tab} onChange={setTab}
                       options={data.tabs.map(t => ({ value: t.id, label: <>{t.label}{!!t.total && <span className="place-toolbar__n">{t.total}</span>}</> }))} />
          )}
          <Toolbar state={{ label: 'State', value: state ?? 'all', onChange: v => {
                       const s = v === 'all' ? null : v as EventsState
                       setState(s)
                       if (BASTET_STATES.includes(s)) setOwn({ tab, kind: null, bastet })
                     }, options: [
                     { value: 'all', label: 'All', count: data.total ?? undefined },
                     ...(tab === 'guard' ? [{ value: 'stopped', label: 'Stopped', count: data.stopped ?? 0 }] : []),
                     { value: 'failed', label: 'Failed', count: data.failed ?? undefined },
                     ...(tab === 'guard' && (data.undecided || state === 'undecided') ? [{ value: 'undecided', label: 'Could not decide', count: data.undecided ?? 0 }] : [])] }}
                   filters={[
                     { label: 'Type', icon: Shapes, picked: kindLabel, onClear: () => setKind(null),
                       items: data.kinds.map(k => ({ label: k.label, current: k.id === kind, onSelect: () => setKind(k.id) })) },
                     { label: 'Date', icon: CalendarDays, picked: dayLabel, onClear: () => setDay(null),
                       items: data.days.map(d => ({ label: d.label, current: d.value === day, onSelect: () => setDay(d.value) })) },
                     ...(tab === 'guard' ? [
                       pick('decision', 'Decision', Scale, data.guard.decisions),
                       pick('rule', 'Rule', ShieldCheck, data.guard.rules),
                       pick('mode', 'Mode', Eye, data.guard.modes),
                     ] : []),
                   ]} />
        </div>
      )}
      <Loaded page res={list} icon={Activity}
              empty={(none || (rows.length === 0 && !filtered)) ? { title: 'No events yet' }
                : rows.length === 0 && { icon: SearchX, title: 'No events match', action: <Button onClick={clear}>Clear search</Button> }}>
        {data => (
          <div className="list-page__rows">
            <VirtualList items={items} estimate={i => ('day' in items[i]! ? 56 : 68)} getKey={r => r.key} renderRow={draw} label="Events"
                         resetOn={filter}
                         onEndReached={() => { if (data.next && !list.loading) setMore({ filter, pages: pages + 1 }) }} />
          </div>
        )}
      </Loaded>
      {active && decision && data && (
        <EventDecision id={decision} mode={words(data.guard.modes)}
                       onClose={() => nav.back({ section: 'events', tab: 'guard' })} />
      )}
      {ctx.menu}
    </div>
  )
}
