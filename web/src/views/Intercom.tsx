/* The Intercom: messages between you, its conversations and the app, in threads. The list:
   Waiting for you, All and Guard (Bastet's threads, the server's) with their counts, Type
   and From to narrow it, Sort, Group by whom it is with (Show more for the rest), job results as one line, Search, Select to dismiss many, Dismiss all in More.
   Each row says where it came from (the server's source). A thread is a chat history,
   oldest first, your lines on the right, where each came from under it, the reply pinned
   at its foot; replying marks it read, Dismiss does too. Continue in a chat takes any thread
   into a new chat with it (the server carries its messages); a bug report's thread shows
   Open or Closed and its Close. New message: To (its conversation
   as a second step), Subject, the text and When. Reminders are messages here too.
   URL: /intercom, /intercom/<thread id>. */
import { useContext, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowUpRight, Bot, CheckCheck, CircleCheck, CornerUpLeft, Flag, Info, Link, Mail, MessageCircle, MessageSquarePlus, MoreHorizontal, SearchX, Shapes, Terminal, User,
  type LucideIcon,
} from 'lucide-react'
import { Portrait } from '../components/Portrait'
import { Attached } from '../components/Transcript'
import {
  KINDS, KIND_PLURAL, KIND_WORD, WaitingCount, addressName, intercomApi, isIntercomEvent, isScheduled, senderName,
  type IntercomMessage, type Kind, type Source, type Thread, type ThreadsPage,
} from '../lib/intercom'
import { useName } from '../lib/name'
import { useFeatureOn } from '../lib/features'
import { useNav } from '../lib/nav'
import { count, dayLabel, dueTime, fullDate, listTime, stamp } from '../lib/format'
import { OFFLINE_TITLE, useOnline } from '../lib/socket'
import { useAction } from '../lib/useAction'
import { useResource } from '../lib/useResource'
import {
  Avatar, Button, Choice, ComposerBox, copied, cx, Dialog, Divider, Dot, EventLine, Field, GroupHead, ICON, InlineError, Input,
  ListRow, Loaded, LoadingRow, MenuButton, Message, PlaceHead, Segmented, SelectBar, TextArea, Toolbar, useContextMenu, usePicker,
  coarse, VirtualList, type BulkResult, type MenuItem, type PickProps,
} from '../ui'
import './Intercom.css'

type Show = 'waiting' | 'all' | 'guard'
type Who = { address: string; name: string }

/* pages 1 to `pages`, each after the cursor of the one before */
async function loadPages(q: { with: string; kind: Kind | null; unread?: boolean }, pages: number, signal: AbortSignal): Promise<ThreadsPage> {
  let page = await intercomApi.threads(q, signal)
  const groups = [...page.groups]
  for (let n = 1; n < pages && page.next; n++) {
    page = await intercomApi.threads({ ...q, cursor: page.next }, signal)
    groups.push(...page.groups)
  }
  return { ...page, groups }
}

/* a thread's title: its subject, else who it is with */
const titleOf = (t: Thread) => t.subject?.trim() || t.name
/* where it came from, beside the title on a wide row; empty when the title already names it */
const sourceOf = (t: Thread) => (t.source?.title && !titleOf(t).includes(t.source.title) ? t.source.title : '')
const firstLine = (text: string) => text.split('\n')[0] ?? ''

/** where a message came from: the glyph of its kind */
const SOURCE_ICON: Record<Source['kind'], LucideIcon> = {
  chat: MessageCircle, mission: Flag, run: Flag, task: Terminal, 'sub-agent': Bot,
  token: Info, connector: MessageCircle, app: Info,
}

/** its portrait when the name is its own, else the person's initial */
function Who({ name, size = 'md' }: { name: string; size?: 'sm' | 'md' }) {
  if (name === useName()) return <Portrait size={size} />
  return <Avatar name={name} size={size === 'sm' ? 28 : 40} />
}

/** where a message came from: its glyph and title, a quiet button when it can be opened */
function SourceLink({ s }: { s: Source }) {
  const nav = useNav()
  if (!s.title) return null
  return <Button size="sm" variant="ghost" icon={SOURCE_ICON[s.kind]} title={s.title} href={s.link ?? undefined}
                 disabled={!s.link} onClick={e => { e.preventDefault(); if (s.link) nav.goPath(s.link) }}>{s.title}</Button>
}

/** a thread's face: its own, a mission's flag, the app's mark, else the counterpart's */
function Face({ t }: { t: Thread }) {
  if (t.source?.kind === 'mission') return <span className="intercom-face" aria-hidden><Flag size={ICON.md} /></span>
  if (t.last.from === 'system:app') return <span className="intercom-face" aria-hidden><Info size={ICON.md} /></span>
  if (t.name === 'Everyone') return <span className="intercom-face" aria-hidden><Mail size={ICON.md} /></span>
  return <Who name={t.name} />
}

const copyLink = (thread: number) => {
  copied(`${location.origin}/intercom/${thread}`)
}

/* set by a row's Reply: the thread page puts the cursor in its reply box once */
let replyTo: number | null = null

type Row =
  | { type: 'group'; key: string; name: string; icon: LucideIcon; n: number; who: Who }
  | { type: 'thread'; key: string; t: Thread; waiting: boolean; under?: string }
  | { type: 'more'; key: string; n: number; who: Who }
  | { type: 'jobs'; key: string; n: number }

export function Intercom({ active }: { active: boolean }) {
  const nav = useNav()
  // the thread the URL names; while another place is on screen, the one it showed last
  const last = useRef<number | undefined>(undefined)
  if (nav.route.section === 'intercom') last.current = nav.route.thread
  const threadId = last.current
  const [writing, setWriting] = useState(false)
  return (
    <>
      {threadId ? <ThreadPage key={threadId} id={threadId} /> : <List active={active} onNew={() => setWriting(true)} />}
      {writing && <NewMessage onClose={() => setWriting(false)}
                              onSent={id => { setWriting(false); nav.go({ section: 'intercom', thread: id }) }} />}
    </>
  )
}

function List({ active, onNew }: { active: boolean; onNew: () => void }) {
  const nav = useNav()
  const ctx = useContextMenu()
  const [picked, setShow] = useState<Show | null>(null)
  const [who, setWho0] = useState<Who | null>(null)
  const [kind, setKind] = useState<Kind | null>(null)
  const [q, setQ] = useState('')
  const [sort, setSort] = useState('Recent')
  const [group, setGroup] = useState('From')
  const [pages, setPages] = useState(1)
  const setWho = (w: Who | null) => { setWho0(w); if (w) setShow('all') }
  const all = who ? who.address : 'all'
  useEffect(() => setPages(1), [all, kind])

  const waiting = useResource(signal => loadPages({ with: 'user', kind, unread: true }, 4, signal), [kind],
                              { events: isIntercomEvent, enabled: active })
  const list = useResource(signal => loadPages({ with: all, kind }, pages, signal), [all, kind, pages],
                           { events: isIntercomEvent, enabled: active })
  const guard = useResource(signal => loadPages({ with: 'guard', kind }, pages, signal), [kind, pages],
                            { events: isIntercomEvent, enabled: active })
  const s = q.trim().toLowerCase()
  const matches = (t: Thread) => !s || [t.name, t.subject ?? '', t.last.text, t.last.from_name ?? '', t.source?.title ?? ''].some(x => x.toLowerCase().includes(s))
  // what waits for you: the server's count, the rail's
  const waitN = useContext(WaitingCount)
  // it opens on Waiting for you when something waits, else on All; it stays where it opened
  const show: Show = picked ?? 'waiting'
  const opened = !!waiting.data
  useEffect(() => { if (opened && !waiting.data!.groups.length) setShow(s => s ?? 'all') }, [opened]) // eslint-disable-line react-hooks/exhaustive-deps -- once, on the first answer
  const allN = list.data && !list.data.next && !who ? list.data.groups.reduce((n, g) => n + g.threads, 0) : undefined
  const guardOn = useFeatureOn('guard')
  const guardN = guard.data && !guard.data.next ? guard.data.groups.reduce((n, g) => n + g.threads, 0) : undefined
  const from = who ? list : show === 'waiting' ? waiting : show === 'guard' ? guard : list
  const groups = from.data?.groups ?? []
  const waits = useMemo(() => new Set((waiting.data?.groups ?? []).flatMap(g => g.items).map(t => t.thread)), [waiting.data])

  const rows = useMemo<Row[]>(() => {
    const items = (g: typeof groups[number]) => {
      const these = g.items.filter(matches)
      return sort === 'Name' ? [...these].sort((a, b) => titleOf(a).localeCompare(titleOf(b))) : these
    }
    const out: Row[] = []
    if (group === 'None' || who) {
      const flat = groups.flatMap(items).sort((a, b) => sort === 'Name' ? titleOf(a).localeCompare(titleOf(b)) : b.last.created_at.localeCompare(a.last.created_at))
      const seen = new Set<number>()
      // what waits comes first
      for (const t of [...flat.filter(t => waits.has(t.thread)), ...flat.filter(t => !waits.has(t.thread))]) {
        if (seen.has(t.thread)) continue
        seen.add(t.thread)
        out.push({ type: 'thread', key: `t:${t.thread}`, t, waiting: waits.has(t.thread), under: who?.name })
      }
    } else {
      for (const g of groups) {
        const these = items(g)
        if (!these.length) continue
        const w = { address: g.addresses[0] ?? g.name, name: g.name }
        const icon = these[0]!.source?.kind === 'mission' ? Flag : these[0]!.source?.kind === 'sub-agent' ? Bot
          : these[0]!.last.from === 'system:app' ? Info : MessageCircle
        out.push({ type: 'group', key: `g:${w.address}`, name: g.name, icon, n: g.threads, who: w })
        for (const t of these) out.push({ type: 'thread', key: `t:${t.thread}`, t, waiting: waits.has(t.thread), under: g.name })
        if (g.more > 0) out.push({ type: 'more', key: `m:${w.address}`, n: g.more, who: w })
      }
    }
    if (show === 'all' && !s && !who && list.data?.jobs?.count) out.push({ type: 'jobs', key: 'jobs', n: list.data.jobs.count })
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groups, waits, group, who, sort, s, show, list.data])

  const shown = from
  const picker = usePicker(`${show} ${all} ${kind}`)
  picker.keys.current = rows.flatMap(r => (r.type === 'thread' ? [String(r.t.thread)] : []))
  const dismissAll = useAction(async () => { await intercomApi.readAll(); waiting.reload() })
  const dismiss = useAction(async (thread: number) => { await intercomApi.dismiss(thread); waiting.reload() })
  const dismissMany = async (keys: string[]): Promise<BulkResult> => {
    const out: BulkResult = { done: [], skipped: [] }
    for (const k of keys) {
      try { await intercomApi.dismiss(Number(k)); out.done.push(k) } catch (e) { out.skipped.push({ id: k, reason: e instanceof Error ? e.message : String(e) }) }
    }
    waiting.reload()
    return out
  }
  const toChat = useAction(async (thread: number) => nav.go({ section: 'celine', conversation: await intercomApi.toChat(thread) }))
  const open = (t: Thread) => nav.go({ section: 'intercom', thread: t.thread })
  const menu = (t: Thread, isWaiting: boolean): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => open(t) },
    { label: 'Reply', icon: CornerUpLeft, onSelect: () => { replyTo = t.thread; open(t) } },
    { label: 'Continue in a chat', icon: MessageSquarePlus, onSelect: () => void toChat.run(t.thread) },
    ...(isWaiting ? [{ label: 'Dismiss', icon: CheckCheck, onSelect: () => void dismiss.run(t.thread) }] : []),
    'sep',
    { label: 'Copy link', icon: Link, onSelect: () => copyLink(t.thread) },
  ]
  const word = kind ? KIND_PLURAL[kind].toLowerCase() : null
  const empty = s ? 'No messages match'
    : show === 'waiting' && !who ? (word ? `No ${word} are waiting for you` : 'Nothing is waiting for you')
    : word ? `No ${word} yet` : 'No messages yet'
  const froms = (list.data?.groups ?? []).map(g => ({ address: g.addresses[0] ?? g.name, name: g.name }))
  const failed = !shown.data && shown.error
  const error = dismissAll.error ?? dismiss.error ?? toChat.error

  return (
    <div className="list-page">
      <PlaceHead level={1} title={who ? who.name : 'Intercom'} count={who ? undefined : allN}
                 back={who ? { label: 'All threads', onClick: () => setWho(null) } : undefined}
                 crumbs={who ? [{ label: 'Intercom', onClick: e => { e.preventDefault(); setWho(null) } }] : undefined}
                 search={{ value: q, onChange: setQ, placeholder: 'Search' }} picker={failed ? undefined : picker}
                 actions={!who && <Button icon={CheckCheck} disabled={!waitN} loading={dismissAll.busy} onClick={() => void dismissAll.run()}>Dismiss all</Button>}
                 newLabel="New message" onNew={onNew} />
      {!failed && <div className="list-page__top">
        {picker.on
          ? <SelectBar picker={picker} noun={n => count(n, 'thread')}
                       actions={[{ label: 'Dismiss', icon: CheckCheck, done: 'dismissed', run: dismissMany }]} />
          : <Toolbar state={{ label: 'Show', value: show, onChange: v => { setShow(v as Show); if (v === 'waiting') setWho0(null) },
                              options: [{ value: 'waiting', label: 'Waiting for you', count: waitN ?? undefined },
                                        { value: 'all', label: 'All', count: allN },
                                        ...(guardOn ? [{ value: 'guard', label: 'Guard', count: guardN }] : [])] }}
                     filters={[
                       { label: 'Type', icon: Shapes, picked: kind ? KIND_PLURAL[kind] : undefined, onClear: () => setKind(null),
                         items: KINDS.map(k => ({ label: KIND_PLURAL[k], current: kind === k, onSelect: () => setKind(k) })) },
                       { label: 'From', icon: User, picked: who?.name, onClear: () => setWho(null),
                         items: froms.map(f => ({ label: f.name, face: <Who name={f.name} size="sm" />, current: who?.address === f.address, onSelect: () => setWho(f) })) },
                     ]}
                     sort={{ label: 'Sort', value: sort, onChange: setSort, options: ['Recent', 'Name'].map(v => ({ value: v, label: v })) }}
                     group={who ? undefined : { label: 'Group', value: group, onChange: setGroup, options: ['From', 'None'].map(v => ({ value: v, label: v })) }} />}
      </div>}
      {error && <div className="list-page__note"><InlineError compact message={error} /></div>}
      <Loaded page res={shown} icon={Mail} label="Loading threads"
              empty={rows.length === 0 && { icon: s ? SearchX : Mail, title: empty, action: s ? <Button onClick={() => setQ('')}>Clear search</Button> : undefined }}>
        {() => (
          <div className="list-page__rows">
            <VirtualList items={rows} getKey={r => r.key} estimate={i => (rows[i]!.type === 'thread' ? 76 : rows[i]!.type === 'group' ? 56 : 48)}
                         label="Threads" resetOn={`${show} ${all} ${kind} ${group}`}
                         onEndReached={show !== 'waiting' && from.data?.next && !from.loading ? () => setPages(n => n + 1) : undefined}
                         renderRow={r => r.type === 'group'
                           ? <GroupHead icon={r.icon} title={r.name} count={count(r.n, 'thread')} onClick={() => setWho(r.who)} />
                           : r.type === 'more' ? <ListRow title="Show more" meta={r.n} onClick={() => setWho(r.who)} />
                           : r.type === 'jobs' ? <ListRow icon={Shapes} title={KIND_PLURAL.job} meta={r.n} onClick={() => { setShow('all'); setKind('job') }} />
                           : <ThreadRow t={r.t} waiting={r.waiting} under={r.under} pick={picker.row(String(r.t.thread))} menu={menu(r.t, r.waiting)}
                                        onOpen={() => open(r.t)} onMenu={e => ctx.open(e, menu(r.t, r.waiting), titleOf(r.t))} />} />
          </div>
        )}
      </Loaded>
      {ctx.menu}
    </div>
  )
}

/* `under`: the sender its group head (or the From pick) already names, said once */
function ThreadRow({ t, waiting, under, pick, menu, onOpen, onMenu }: {
  t: Thread; waiting: boolean; under?: string; pick: PickProps; menu: MenuItem[]; onOpen: () => void; onMenu: (e: React.MouseEvent) => void
}) {
  const who = senderName(t.last)
  const source = sourceOf(t)
  const cancelled = t.last.state === 'cancelled'
  const scheduled = isScheduled(t.last)
  // a plain message needs no word; the other kinds say theirs
  const what = cancelled ? 'Cancelled' : scheduled ? <span title={fullDate(t.last.due_at!)}>Scheduled</span>
    : t.last.kind === 'message' ? undefined : KIND_WORD[t.last.kind]
  return (
    <ListRow density="lg" pick={pick} onClick={onOpen} onContextMenu={onMenu} leading={<Face t={t} />} muted={cancelled}
             title={titleOf(t)} tooltip={titleOf(t)}
             subtitle={<>{who !== titleOf(t) && who !== under && <span className="intercom-who">{who}: </span>}{firstLine(t.last.text)}</>}
             column={source !== under ? source : ''} cell={what}
             meta={<span className="intercom-meta"><span title={fullDate(t.last.created_at)}>{listTime(t.last.created_at)}</span>
               {waiting && <Dot tone="accent" label={`${t.unread} waiting for you`} />}</span>}
             trailing={!pick.checked && <MenuButton icon={MoreHorizontal} label="More" items={menu} size="sm" />} />
  )
}

/* ── one thread ── */

function ThreadPage({ id }: { id: number }) {
  const nav = useNav()
  const online = useOnline()
  const thread = useResource(signal => intercomApi.thread(id, signal), [id], { events: isIntercomEvent })
  const msgs = useMemo(() => thread.data?.messages ?? [], [thread.data])
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => { end.current?.scrollIntoView({ block: 'end' }) }, [msgs.length])
  const [text, setText] = useState('')
  const [focus] = useState(() => { const f = replyTo === id; replyTo = null; return f && !coarse() })
  const unread = msgs.filter(m => m.unread).map(m => m.id)
  const dismiss = useAction(async () => { await intercomApi.read(unread); thread.reload() })
  const cancel = useAction(async (mid: number) => { await intercomApi.cancel(mid); thread.reload() })
  const report = useResource(signal => intercomApi.bugOf(id, signal), [id], { events: isIntercomEvent })
  const bug = report.data
  const close = useAction(async () => { if (bug) await intercomApi.closeBug(bug.id); thread.reload(); report.reload() })
  const toChat = useAction(async () => nav.go({ section: 'celine', conversation: await intercomApi.toChat(id) }))
  // a reply goes to whoever wrote the newest message that is not yours; in a thread only
  // you wrote in, to whom you wrote
  const theirs = [...msgs].reverse().find(m => m.from !== 'user' && !m.from.startsWith('system:'))
  const send = useAction(async () => {
    const body = text.trim()
    if (!body) return
    const lastMsg = msgs[msgs.length - 1]!
    await intercomApi.send(theirs ? { to: '', text: body, reply_to: theirs.id }
      : { to: msgs[0]!.to.split(', ').filter(a => a !== 'user').join(', '), text: body, reply_to: lastMsg.id })
    setText('')
    thread.reload()
  })
  // its subject, else who it is with
  const first = msgs[0]
  // (the app's senders, like Bastet, wrote to you alone: their name)
  const to = first ? first.to.split(', ').filter(a => a !== 'user') : []
  const title = first?.subject ?? (theirs ? senderName(theirs)
    : first && to.length ? to.map(a => addressName(a, first.to_names)).join(', ') : first ? senderName(first) : 'Intercom')
  const src = thread.data?.source
  let day = ''
  return (
    <div className="intercom-thread">
      <PlaceHead level={1} title={title} titleAttr={title}
                 crumbs={[{ label: 'Intercom', href: '/intercom', onClick: e => { e.preventDefault(); nav.go({ section: 'intercom' }) } }]}
                 detail={bug ? (bug.state === 'open' ? 'Open' : 'Closed') : undefined}
                 actions={<>
                   {unread.length > 0 && <Button variant="ghost" icon={CheckCheck} loading={dismiss.busy} onClick={() => void dismiss.run()}>Dismiss</Button>}
                   {bug?.state === 'open' && <Button variant="ghost" icon={CircleCheck} loading={close.busy} onClick={() => void close.run()}>Close</Button>}
                   {msgs.length > 0 && <Button variant="ghost" icon={MessageSquarePlus} loading={toChat.busy} onClick={() => void toChat.run()}>Continue in a chat</Button>}
                 </>}
                 more={[
                   ...(src?.link ? [{ label: src.kind === 'mission' ? 'Open mission' : 'Open', detail: src.kind === 'mission' ? undefined : src.title ?? undefined,
                                      icon: SOURCE_ICON[src.kind], onSelect: () => nav.goPath(src.link!) }] : []),
                   { label: 'Copy link', icon: Link, onSelect: () => copyLink(id) },
                 ]} />
      <div className="page intercom-thread__scroll">
        <div className="intercom-thread__col">
          {(thread.error ?? dismiss.error ?? cancel.error ?? close.error ?? toChat.error) && <InlineError message={(thread.error ?? dismiss.error ?? cancel.error ?? close.error ?? toChat.error)!}
                                                                            onRetry={thread.error ? thread.reload : undefined} />}
          {!thread.data && !thread.error && <LoadingRow label="Loading the thread" />}
          {msgs.map(m => {
            const d = dayLabel(Date.parse(m.created_at))
            const sep = d !== day ? (day = d) : null
            return (
              <div key={m.id} className="intercom-thread__item">
                {sep && <Divider>{sep}</Divider>}
                <ThreadMessage m={m} onCancel={() => void cancel.run(m.id)} />
              </div>
            )
          })}
          <div ref={end} />
        </div>
      </div>
      {thread.data && msgs.length > 0 && (
        <div className="intercom-reply">
          {send.error && <InlineError compact message={send.error} />}
          <ComposerBox value={text} onChange={setText} onSend={() => void send.run()} autoFocus={focus}
                       placeholder={theirs ? `Reply to ${senderName(theirs)}` : 'Reply'}
                       ready={!!text.trim() && !send.busy} disabled={online ? undefined : OFFLINE_TITLE} />
        </div>
      )}
    </div>
  )
}

/** one message of a thread: who, to whom, when, and where it came from */
function ThreadMessage({ m, onCancel }: { m: IntercomMessage; onCancel: () => void }) {
  const mine = m.from === 'user'
  if (m.from === 'system:app') {
    return <EventLine quiet icon={Info} meta={<span title={fullDate(m.created_at)}>{stamp(m.created_at)}</span>}
                      className="intercom-event">{m.text}</EventLine>
  }
  const scheduled = isScheduled(m)
  // where it came from, unless that is the sender its name already says
  const from = m.source && m.source.title !== senderName(m) ? m.source : null
  // a mission writing to its own team: "to the team", its name is beside it already
  const own = `team:${m.from.split(':')[1]}`
  const to = m.to.split(', ').filter(a => a !== 'user').map(a => (a === own ? 'the team' : addressName(a, m.to_names))).join(', ')
  const when = m.state === 'cancelled' ? 'Cancelled'
    : scheduled ? `Scheduled ${dueTime(m.due_at)}`
    : m.until_at ? `${stamp(m.created_at)}, until ${stamp(m.until_at)}`
    : stamp(m.created_at)
  return (
    <Message mine={mine} face={mine ? undefined : <Who name={m.from_name ?? ''} size="sm" />} name={senderName(m)}
             className={cx('intercom-msg', m.state === 'cancelled' && 'intercom-msg--cancelled', m.state === 'pending' && 'intercom-msg--pending')}
             head={<>
               {to && <span title={`to ${to}`}>to {to}</span>}
               {m.kind !== 'message' && <span>{KIND_WORD[m.kind]}</span>}
               {m.unread && <Dot tone="accent" label="Waiting for you" />}
             </>}
             time={<span title={fullDate(m.until_at ?? m.due_at ?? m.created_at)}>{when}</span>}
             source={(from || scheduled) && <>
               {from && <SourceLink s={from} />}
               {scheduled && <Button size="sm" variant="ghost" onClick={onCancel}>Cancel</Button>}
             </>}>
      <span className="intercom-text">{m.text}</span>
      <Attached list={m.attachments} />
    </Message>
  )
}

/* ── New message ── */

type When = 'now' | 'in' | 'at' | 'until'
/* the To that stands for all its conversations */
const HER = 'her'

function NewMessage({ onClose, onSent }: { onClose: () => void; onSent: (thread: number) => void }) {
  const online = useOnline()
  const her = useName()
  const recipients = useResource(() => intercomApi.recipients(), [])
  const [to, setTo] = useState('all')
  const [subject, setSubject] = useState('')
  const [text, setText] = useState('')
  const [when, setWhen] = useState<When>('now')
  const [whenText, setWhenText] = useState('')
  const all = recipients.data ?? [{ to: 'all', name: 'Everyone', line: null }]
  // its own: its chats and tasks and their sub-agents, newest first, picked in a second step
  const hers = all.filter(r => r.line)
  const options = [...all.filter(r => !r.line).map(r => ({ value: r.to, label: r.name })), ...(hers.length ? [{ value: HER, label: her || 'Celine' }] : [])]
  const inHers = hers.some(r => r.to === to)
  const whens: { value: When; label: string }[] = [
    { value: 'now', label: 'Now' }, { value: 'in', label: 'In' }, { value: 'at', label: 'At' },
    ...(to === 'all' ? [{ value: 'until' as const, label: 'Until' }] : []),
  ]
  useEffect(() => { if (to !== 'all' && when === 'until') setWhen('now') }, [to, when])
  const ready = online && !!text.trim() && (when === 'now' || !!whenText.trim())
  const send = useAction(async () => {
    if (!ready) return
    const r = await intercomApi.send({ to, text: text.trim(), subject: subject.trim() || undefined, ...(when === 'now' ? {} : { [when]: whenText.trim() }) })
    onSent(r.intercom_id)
  })
  return (
    <Dialog title="New message" size="md" onClose={onClose} dismissable={!send.busy}
            actions={<>
              <Button onClick={onClose}>Cancel</Button>
              <Button variant="primary" loading={send.busy} disabled={!ready}
                      title={!online ? OFFLINE_TITLE : ready ? undefined : 'Write a message first'} onClick={() => void send.run()}>Send</Button>
            </>}>
      <div className="intercom-form">
        <Field label="To" group>
          <Choice list label="To" value={inHers ? HER : to} onChange={v => setTo(v === HER ? hers[0]!.to : v)} options={options} />
        </Field>
        {inHers && (
          <Field label="Chat" group>
            <Choice list label="Chat" value={to} onChange={setTo} options={hers.map(r => ({ value: r.to, label: r.line! }))} />
          </Field>
        )}
        <Field label="Subject"><Input value={subject} onChange={e => setSubject(e.target.value)} /></Field>
        <Field label="Message"><TextArea rows={4} max={12} value={text} autoFocus={!coarse()} onChange={e => setText(e.target.value)} /></Field>
        <Field label="When" group>
          <div className="intercom-when">
            <Segmented label="When" value={when} onChange={v => { setWhen(v); setWhenText('') }} options={whens} />
            {when !== 'now' && (
              <Input className="intercom-when__value" aria-label={when === 'in' ? 'In' : when === 'at' ? 'At' : 'Until'}
                     type={when === 'at' ? 'time' : 'text'} placeholder={when === 'in' ? '30 min' : when === 'until' ? '05:00' : undefined}
                     value={whenText} onChange={e => setWhenText(e.target.value)} />
            )}
          </div>
        </Field>
      </div>
      {send.error && <InlineError compact message={send.error} />}
    </Dialog>
  )
}
