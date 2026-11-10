/* Waiting for you: everything that waits on you, the server's list (lib/waits) under the
   server's groups with their counts. A request is its card, answered in place; a row opens
   the page that answers it, in this pane: an Intercom thread (the Intercom's thread page),
   what a mission asks (its note, its words, Reply, Mark as done, Stop, in one Section as on
   the mission's own page), a lost login's
   Settings page. A row's "..." and right-click have Open and, for what was written to you,
   Dismiss; the Intercom group's "..." has Dismiss all. An item that is answered
   leaves the list, and its page goes back to the list.
   URL: /waiting, /waiting/thread/<thread id>, /waiting/mission/<conversation id>. */
import { useEffect, useRef } from 'react'
import { ArrowUpRight, CheckCheck, Flag, Hand, Mail, Plug, type LucideIcon } from 'lucide-react'
import { ApprovalCard } from '../components/ApprovalCard'
import { MissionAsk } from '../components/MissionAsk'
import { count, fullDate, listTime } from '../lib/format'
import { intercomApi } from '../lib/intercom'
import { useNav, type Route } from '../lib/nav'
import { useAction } from '../lib/useAction'
import { useWaits, type MissionWait, type Wait } from '../lib/waits'
import {
  Button, Facts, GroupHead, InlineError, ListRow, Loaded, PlaceHead, Section, useContextMenu, VirtualList, type Crumb, type MenuItem,
} from '../ui'
import { ThreadPage } from './Intercom'
import './Waiting.css'

/** each kind's glyph and the noun its group's count is said with */
const KIND: Record<Wait['kind'], { icon: LucideIcon; noun: string }> = {
  approval: { icon: Hand, noun: 'approval request' }, mission: { icon: Flag, noun: 'mission' },
  login: { icon: Plug, noun: 'source' }, thread: { icon: Mail, noun: 'thread' },
}

type Row = { type: 'group'; key: string; w: Wait } | { type: 'item'; key: string; w: Wait }

export function Waiting({ active }: { active: boolean }) {
  const nav = useNav()
  // the item the URL names; while another place is on screen, the one it showed last
  const last = useRef<Extract<Route, { section: 'waiting' }>>({ section: 'waiting' })
  if (nav.route.section === 'waiting') last.current = nav.route
  const { thread, mission } = last.current
  const waits = useWaits()
  const items = waits.data?.items ?? []
  const toList = (e?: React.MouseEvent) => { e?.preventDefault(); nav.go({ section: 'waiting' }) }
  const crumbs: Crumb[] = [{ label: 'Waiting for you', href: '/waiting', onClick: toList }]
  const asks = mission ? items.find((w): w is MissionWait => w.kind === 'mission' && w.id === mission) : undefined
  const open = thread ? items.some(w => w.kind === 'thread' && w.id === String(thread)) : !!asks
  // the open item was answered (or waits no more): back to the list
  const left = active && !!waits.data && (!!thread || !!mission) && !open
  useEffect(() => { if (left) nav.go({ section: 'waiting' }, { replace: true }) }, [left, nav])

  if (thread && open) return <ThreadPage key={thread} id={thread} crumbs={crumbs} />
  if (asks) return <MissionPage w={asks} crumbs={crumbs} />
  return <List />
}

function List() {
  const nav = useNav()
  const waits = useWaits()
  const items = waits.data?.items ?? []
  const ctx = useContextMenu()
  const dismissAll = useAction(() => intercomApi.readAll())
  const dismiss = useAction(async (w: Exclude<Wait, { kind: 'approval' }>) => {
    if (w.kind === 'thread') await intercomApi.dismiss(Number(w.id))
    else if (w.kind === 'mission') await intercomApi.read(w.asked)
  })
  const menu = (w: Exclude<Wait, { kind: 'approval' }>): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => nav.goPath(w.link) },
    ...(w.kind === 'thread' || (w.kind === 'mission' && w.asked.length)
      ? [{ label: 'Dismiss', icon: CheckCheck, onSelect: () => void dismiss.run(w) }] : []),
  ]
  const rows: Row[] = items.flatMap((w, i) => {
    const key = w.kind === 'approval' ? `a:${w.approval.id}` : `${w.kind}:${w.id}`
    const item: Row = { type: 'item', key, w }
    return w.group !== items[i - 1]?.group ? [{ type: 'group', key: `g:${w.group}`, w }, item] : [item]
  })
  return (
    <div className="list-page">
      <PlaceHead level={1} title="Waiting for you" count={waits.data?.count || undefined} />
      {(dismissAll.error ?? dismiss.error) && <div className="list-page__note"><InlineError compact message={(dismissAll.error ?? dismiss.error)!} /></div>}
      <Loaded page res={waits} icon={Hand} label="Loading what waits for you"
              empty={rows.length === 0 && { icon: Hand, title: 'Nothing is waiting for you' }}>
        {data => (
          <div className="list-page__rows">
            <VirtualList items={rows} getKey={r => r.key} label="Waiting for you"
                         estimate={i => (rows[i]!.type === 'group' ? 56 : rows[i]!.w.kind === 'approval' ? 120 : 92)}
                         renderRow={r => r.type === 'group'
                           ? <GroupHead icon={KIND[r.w.kind].icon} title={r.w.group} count={count(data.counts[r.w.group] ?? 0, KIND[r.w.kind].noun)}
                                        menu={r.w.kind === 'thread' ? [{ label: 'Dismiss all', icon: CheckCheck, onSelect: () => void dismissAll.run() }] : undefined} />
                           : r.w.kind === 'approval' ? <div className="waiting__card"><ApprovalCard ask={r.w.approval} onOpen={nav.goPath} /></div>
                           : <ItemRow w={r.w} menu={menu(r.w)} onMenu={ctx.open} />} />
          </div>
        )}
      </Loaded>
      {ctx.menu}
    </div>
  )
}

/** a row of the list: what waits, its whole line, who it is from and when */
function ItemRow({ w, menu, onMenu }: {
  w: Exclude<Wait, { kind: 'approval' }>; menu: MenuItem[]; onMenu: (e: React.MouseEvent, items: MenuItem[], label?: string) => void
}) {
  const from = w.kind === 'thread' ? w.from : w.kind === 'mission' ? w.origin?.name : undefined
  const at = w.kind === 'login' ? undefined : w.at
  return (
    <ListRow density="lg" icon={KIND[w.kind].icon} title={w.title} href={w.link}
             subtitle={from && from !== w.title ? from : undefined}
             body={w.kind === 'mission' && w.why && w.text ? <>{w.why}<br />{w.line}</> : w.line || undefined}
             meta={at && <span title={fullDate(at)}>{listTime(at)}</span>} onContextMenu={e => onMenu(e, menu, w.title)}
             menu={menu} />
  )
}

/** what a mission asks, on a page of its own */
function MissionPage({ w, crumbs }: { w: MissionWait; crumbs: Crumb[] }) {
  const nav = useNav()
  const go = (link: string) => (e: React.MouseEvent) => { e.preventDefault(); nav.goPath(link) }
  return (
    <div className="waiting-page">
      <PlaceHead level={1} title={w.title} titleAttr={w.title} crumbs={crumbs}
                 actions={w.home && <Button icon={Flag} href={w.home} onClick={go(w.home)}>Open mission</Button>} />
      <div className="page"><div className="page__col page__col--wide">
        <Section title="Waiting for you">
          <MissionAsk w={w} />
          {w.origin && <Facts items={[{ label: 'Started from', value: w.origin.link
            ? <a href={w.origin.link}>{w.origin.name}</a> : w.origin.name }]} />}
        </Section>
      </div></div>
    </div>
  )
}
