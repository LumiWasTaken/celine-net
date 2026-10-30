/* Missions: the missions, each its instructions: it plans, works with its sub-agents and
   delivers a result. The place's sidebar holds Missions and Sub-agents with their counts,
   then the recent missions. The list: the state (All, Running, Waiting for you, Done,
   Stopped) with the server's counts, Sort and Group, Archive and Trash as shelves of the
   same list, Select, Archive older than; Move to Trash asks first. A mission opens on its
   page (what waits for you, its instructions, plan, sub-tasks, sub-agents, result and
   settings, Stop asks first) or on its Work (the conversation). A sub-agent of a mission
   opens on its conversation, its result and details one switch away. What an event
   started and the runs of the schedules are in Routines.
   URL: /missions, /mission/<id>. */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Archive, ArrowLeftRight, ArrowUpRight, Bot, CircleStop, FileText, Flag, History, ListChecks, MessagesSquare, MoreHorizontal, Pencil, Plus, Reply,
  RotateCcw, SearchX, SquareTerminal, Trash2,
} from 'lucide-react'
import { api, conversationApi, request } from '../lib/api'
import { AUTO_CONTEXT, AUTO_CONTEXT_TIP, contextApi } from '../lib/context'
import { agentsApi, useAgents, type Agent } from '../lib/agents'
import { artifactRef } from '../lib/artifacts'
import { count, errorText, listTime, stamp } from '../lib/format'
import {
  STATE_WORD, isMissionEvent, isOpen, missionsApi, stateTone, type Mission, type MissionStatus, type MissionStep,
} from '../lib/missions'
import { SETTINGS_EVENTS, settingsApi } from '../lib/settingsApi'
import { useName } from '../lib/name'
import { format, useNav, type Route } from '../lib/nav'
import { useOnline } from '../lib/socket'
import { useAction } from '../lib/useAction'
import { useConversation } from '../lib/useConversation'
import { useResource } from '../lib/useResource'
import { bulkActions, useArchiveOlder } from '../components/Bulk'
import { click } from '../components/ConversationList'
import { ArtifactChip, Markdown } from '../components/Markdown'
import { Talk } from '../components/Talk'
import { PlanButton } from '../components/Plan'
import { ModelPick, ModelRow } from '../components/ConversationPanel'
import { ModelChooser } from '../components/ModelChooser'
import { LongEditor, TextHistory, textHistory } from '../components/editor'
import {
  Button, ComposerBox, ConfirmDialog, Dialog, Dot, EmptyState, Facts, Field, GroupHead, ICON, IconButton, InlineError, Input, ListRow,
  ListSection, Loaded, LoadingRow, MenuButton, PageIndex, Place, PlaceHead, Section, SelectBar, ShelfRow, Switch, TextArea, Toolbar,
  coarse, useContextMenu, usePicker, VirtualList, type DotTone, type MenuItem, type Shelf,
} from '../ui'
import './Missions.css'

const firstLine = (text: string) => text.trim().split('\n')[0] ?? ''
const missionsNoun = (n: number) => count(n, 'mission')
const agentsNoun = (n: number) => count(n, 'sub-agent')

/** a sub-agent as GET /agents sends it: its model and agent CLI too */
type AgentRow = Agent & { model?: string | null; cli?: string | null; device?: string | null; last?: string }

type Show = 'all' | MissionStatus
const SHOW: { value: Show; label: string }[] = [
  { value: 'all', label: 'All' }, { value: 'active', label: STATE_WORD.active }, { value: 'needs_you', label: STATE_WORD.needs_you },
  { value: 'done', label: STATE_WORD.done }, { value: 'stopped', label: STATE_WORD.stopped },
]
const SORTS = ['Recent', 'Name', 'Created']
const GROUPS = ['State', 'None']

export function Missions({ active }: { active: boolean }) {
  const nav = useNav()
  // the page the URL names; while another place is on screen, the one it showed last
  const last = useRef<Extract<Route, { section: 'missions' }>>({ section: 'missions' })
  if (nav.route.section === 'missions') last.current = nav.route
  const selected = last.current.conversation ?? null
  const [view, setView] = useState<'missions' | 'agents'>('missions')
  const [shelf, setShelf] = useState<Shelf>('list')
  const [starting, setStarting] = useState(false)
  const missions = useResource(() => missionsApi.list(), [], { events: isMissionEvent })
  const archived = useResource(() => missionsApi.list('archived'), [], { events: isMissionEvent })
  const trashed = useResource(() => missionsApi.list('trashed'), [], { enabled: shelf === 'trash' || !!selected, events: isMissionEvent })
  const reload = () => { missions.reload(); archived.reload(); trashed.reload() }
  const agents = useAgents() as AgentRow[]
  const every = useMemo(() => [...missions.data?.missions ?? [], ...archived.data?.missions ?? [], ...trashed.data?.missions ?? []],
                        [missions.data, archived.data, trashed.data])
  const ids = useMemo(() => new Set(every.map(m => m.conversation_id)), [every])
  // the missions' own sub-agents: the ones whose home is here (the server's word)
  const theirs = useMemo(() => agents.filter(a => a.home === 'missions'), [agents])
  const counts = missions.data?.counts
  const mission = every.find(m => m.conversation_id === selected)
  const agent = agents.find(a => a.id === selected)
  const loaded = missions.data !== undefined && archived.data !== undefined
  // a sub-agent lives in its root's home (the server's): one of a chat or a routine opens there
  useEffect(() => {
    if (active && agent && agent.home !== 'missions') nav.goPath(agent.link, { replace: true })
  }, [active, agent, nav])

  const toList = (v: 'missions' | 'agents') => { setView(v); if (selected) nav.go({ section: 'missions' }) }
  const recent = (missions.data?.missions ?? []).slice(0, 8)
  return (
    <Place title="Missions" action={<IconButton icon={Plus} label="New mission" onClick={() => setStarting(true)} />}
           side={<>
             <ListRow density="sm" icon={Flag} title="Missions" meta={counts?.all} selected={!selected && view === 'missions'}
                      onClick={() => toList('missions')} />
             <ListRow density="sm" icon={Bot} title="Sub-agents" meta={loaded ? theirs.length : undefined} selected={!selected && view === 'agents'}
                      onClick={() => toList('agents')} />
             {recent.length > 0 && <ListSection label="Recent" />}
             {recent.map(m => (
               <ListRow key={m.conversation_id} density="sm" leading={<Dot tone={stateTone(m.status)} />} title={m.name} tooltip={m.name}
                        href={format({ section: 'missions', conversation: m.conversation_id })}
                        onClick={click(() => nav.go({ section: 'missions', conversation: m.conversation_id }))}
                        selected={m.conversation_id === selected} />
             ))}
           </>}>
      {selected ? (
        mission ? <MissionPage key={mission.conversation_id} m={mission} agents={agents.filter(a => a.parent === mission.conversation_id)}
                               active={active} onChange={reload} />
          : agent && ids.has(agent.parent) ? <AgentPage key={agent.id} a={agent} mission={every.find(m => m.conversation_id === agent.parent)!}
                                                       active={active} />
          : loaded ? <ShelvedAgent key={selected} cid={selected} active={active} />
          : <LoadingRow label="Loading the mission" />
      ) : view === 'agents' ? <AgentsList agents={theirs} missions={every} />
        : <MissionsList shelf={shelf} setShelf={setShelf} lists={{ list: missions, archive: archived, trash: trashed }}
                        agents={agents} onNew={() => setStarting(true)} onChange={reload} />}
      {starting && <NewMission onClose={() => setStarting(false)}
                               onStarted={cid => { setStarting(false); missions.reload(); nav.go({ section: 'missions', conversation: cid }) }} />}
    </Place>
  )
}

type MissionsRes = ReturnType<typeof useResource<Awaited<ReturnType<typeof missionsApi.list>>>>

type Row = { key: string; head: string; n: number } | { key: string; m: Mission }

function MissionsList({ shelf, setShelf, lists, agents, onNew, onChange }: {
  shelf: Shelf; setShelf: (s: Shelf) => void
  lists: Record<Shelf, MissionsRes>
  agents: AgentRow[]
  onNew: () => void
  onChange: () => void
}) {
  const nav = useNav()
  const [q, setQ] = useState('')
  const [show, setShow] = useState<Show>('all')
  const [sort, setSort] = useState('Recent')
  const [group, setGroup] = useState('None')
  const [renaming, setRenaming] = useState<Mission | null>(null)
  const [stopping, setStopping] = useState<Mission | null>(null)
  const [trashing, setTrashing] = useState<Mission | null>(null)
  const picker = usePicker(shelf)
  const ctx = useContextMenu()
  const older = useArchiveOlder('mission', missionsNoun)
  const act = useAction(async (fn: () => Promise<unknown>) => { await fn(); onChange() })
  const shown = lists[shelf]
  const counts = lists.list.data?.counts
  const kids = useMemo(() => {
    const out = new Map<string, number>()
    for (const a of agents) out.set(a.parent, (out.get(a.parent) ?? 0) + 1)
    return out
  }, [agents])

  const items = useMemo(() => {
    const s = q.trim().toLowerCase()
    let list = (shown.data?.missions ?? []).filter(m => (shelf !== 'list' || show === 'all' || m.status === show)
      && (!s || [m.name, m.brief, m.note ?? ''].some(x => x.toLowerCase().includes(s))))
    if (sort === 'Name') list = [...list].sort((a, b) => a.name.localeCompare(b.name))
    else if (sort === 'Recent') list = [...list].sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))
    else if (sort === 'Created') list = [...list].sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
    return list
  }, [shown.data, shelf, show, q, sort])
  const rows = useMemo<Row[]>(() => {
    if (group !== 'State' || shelf !== 'list') return items.map(m => ({ key: m.conversation_id, m }))
    return SHOW.slice(1).flatMap(s => {
      const these = items.filter(m => m.status === s.value)
      return these.length ? [{ key: `g:${s.value}`, head: s.label, n: these.length }, ...these.map(m => ({ key: m.conversation_id, m }))] : []
    })
  }, [items, group, shelf])
  picker.keys.current = items.map(m => m.conversation_id)

  const open = (m: Mission) => nav.go({ section: 'missions', conversation: m.conversation_id })
  const menu = (m: Mission): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => open(m) },
    ...(m.status === 'needs_you' ? [{ label: 'Reply', icon: Reply, onSelect: () => open(m) }] : []),
    ...(isOpen(m.status) ? [{ label: 'Stop', icon: CircleStop, onSelect: () => setStopping(m) }] : []),
    { label: 'Rename', icon: Pencil, onSelect: () => setRenaming(m) },
    'sep',
    { label: 'Select', icon: ListChecks, onSelect: () => picker.start(m.conversation_id) },
    { label: 'Archive', icon: Archive, onSelect: () => void act.run(() => api.archiveConversation(m.conversation_id, true)) },
    { label: 'Move to Trash', icon: Trash2, danger: true, onSelect: () => setTrashing(m) },
  ]
  const loaded = shown.data !== undefined
  const nothing = loaded && (shown.data?.missions.length ?? 0) === 0
  const title = shelf === 'archive' ? 'Archive' : shelf === 'trash' ? 'Trash' : 'Missions'
  const n = shelf === 'archive' ? counts?.archived : shelf === 'trash' ? counts?.trashed : counts?.all

  return (
    <div className="list-page">
      <PlaceHead level={1} title={title} count={nothing ? undefined : n}
                 search={{ value: q, onChange: setQ, placeholder: 'Search missions' }} picker={loaded && !nothing ? picker : undefined}
                 shelf={{ archive: true, trash: true, value: shelf, onChange: setShelf }}
                 more={shelf === 'list' ? [older.item] : []} newLabel="New mission" onNew={onNew} />
      {older.dialog}
      {loaded && !nothing && (picker.on || shelf === 'list') && (
        <div className="list-page__top">
          {picker.on
            ? <SelectBar picker={picker} noun={missionsNoun} actions={bulkActions({ what: 'conversations', shelf, noun: missionsNoun, archive: true })} />
            : <Toolbar state={{ label: 'State', value: show, onChange: v => setShow(v as Show),
                                options: SHOW.map(s => ({ ...s, count: s.value === 'all' ? counts?.all : counts?.[s.value] })) }}
                       sort={{ label: 'Sort', value: sort, onChange: setSort, options: SORTS.map(v => ({ value: v, label: v })) }}
                       group={{ label: 'Group', value: group, onChange: setGroup, options: GROUPS.map(v => ({ value: v, label: v })) }} />}
        </div>
      )}
      {act.error && <div className="list-page__note"><InlineError compact message={act.error} /></div>}
      <Loaded page res={shown} icon={Flag} label="Loading missions"
              empty={nothing ? (shelf === 'list' ? { title: 'No missions yet', action: <Button variant="primary" icon={Plus} onClick={onNew}>New mission</Button> }
                  : { icon: shelf === 'trash' ? Trash2 : Archive, title: shelf === 'trash' ? 'Trash is empty' : 'Archive is empty' })
                : items.length === 0 && { icon: SearchX, title: 'No missions match', action: q && <Button onClick={() => setQ('')}>Clear search</Button> }}>
        {() => (
          <div className="list-page__rows">
            <VirtualList items={rows} estimate={i => ('head' in rows[i]! ? 56 : 68)} getKey={r => r.key} label="Missions" resetOn={`${shelf}|${show}|${group}`}
                         renderRow={r => 'head' in r ? <GroupHead title={r.head} count={missionsNoun(r.n)} />
                           : shelf !== 'list' ? (
                             <ShelfRow shelf={shelf} density="lg" title={r.m.name} subtitle={`${STATE_WORD[r.m.status]}, ${listTime(r.m.updated_at)}`}
                                       onClick={() => open(r.m)} pick={picker.row(r.m.conversation_id)} onSelect={() => picker.start(r.m.conversation_id)}
                                       onRestore={() => void act.run(() => shelf === 'archive' ? api.archiveConversation(r.m.conversation_id, false)
                                         : missionsApi.restore(r.m.conversation_id))}
                                       onTrash={() => setTrashing(r.m)}
                                       onDelete={() => act.run(() => missionsApi.remove(r.m.conversation_id))} />
                           ) : (
                             <ListRow density="lg" dot={r.m.asked ? 'warn' : stateTone(r.m.status)} title={r.m.name} tooltip={r.m.name}
                                      subtitle={r.m.note || firstLine(r.m.brief)} muted={r.m.status === 'done' || r.m.status === 'stopped'}
                                      column={<span className="missions-column">{kids.get(r.m.conversation_id) ? agentsNoun(kids.get(r.m.conversation_id)!) : ''}
                                        {r.m.artifact && <FileText size={ICON.sm} aria-label="Result" />}</span>}
                                      cell={r.m.asked ? STATE_WORD.needs_you : STATE_WORD[r.m.status]} meta={<time dateTime={r.m.updated_at}>{listTime(r.m.updated_at)}</time>}
                                      href={format({ section: 'missions', conversation: r.m.conversation_id })} onClick={click(() => open(r.m))}
                                      pick={picker.row(r.m.conversation_id)} onContextMenu={e => ctx.open(e, menu(r.m), r.m.name)}
                                      trailing={!picker.on && <MenuButton icon={MoreHorizontal} label="More" items={menu(r.m)} size="sm" />} />
                           )} />
          </div>
        )}
      </Loaded>
      {ctx.menu}
      {renaming && <Rename m={renaming} onClose={() => setRenaming(null)} onDone={onChange} />}
      {stopping && <StopMission m={stopping} onClose={() => setStopping(null)} onDone={onChange} />}
      {trashing && <TrashMission m={trashing} onClose={() => setTrashing(null)} onDone={onChange} />}
    </div>
  )
}

function Rename({ m, onClose, onDone }: { m: Mission; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(m.name)
  const save = useAction(async () => { await missionsApi.edit(m.conversation_id, { name: name.trim() }); onDone(); onClose() })
  return (
    <Dialog title="Rename" size="sm" onClose={onClose}
            actions={<><Button onClick={onClose}>Cancel</Button>
              <Button variant="primary" loading={save.busy} disabled={!name.trim() || name.trim() === m.name} onClick={() => void save.run()}>Save</Button></>}>
      <form onSubmit={e => { e.preventDefault(); if (name.trim()) void save.run() }}>
        <Input autoFocus={!coarse()} value={name} aria-label="Name" onChange={e => setName(e.target.value)} />
      </form>
      {save.error && <InlineError compact message={save.error} />}
    </Dialog>
  )
}

function StopMission({ m, onClose, onDone }: { m: Mission; onClose: () => void; onDone: () => void }) {
  return (
    <ConfirmDialog title={`Stop ${m.name}?`} danger confirmLabel="Stop" body="Its conversation, plan and terminals stay."
                   onClose={onClose}
                   onConfirm={async () => { try { await missionsApi.stop(m.conversation_id) } catch (e) { return errorText(e) } onDone(); onClose() }} />
  )
}

function TrashMission({ m, onClose, onDone }: { m: Mission; onClose: () => void; onDone: () => void }) {
  const nav = useNav()
  return (
    <ConfirmDialog title={`Move ${m.name} to Trash?`} danger confirmLabel="Move to Trash" body="Restore brings it back."
                   onClose={onClose}
                   onConfirm={async () => {
                     try { await missionsApi.trash(m.conversation_id) } catch (e) { return errorText(e) }
                     if (nav.route.section === 'missions' && nav.route.conversation === m.conversation_id) nav.go({ section: 'missions' }, { replace: true })
                     onDone(); onClose()
                   }} />
  )
}

/* New mission: the instructions, and a name when you want one (else it names it) */
function NewMission({ onClose, onStarted }: { onClose: () => void; onStarted: (cid: string) => void }) {
  const [text, setText] = useState('')
  const [name, setName] = useState('')
  const [model, setModel] = useState<string | null>(null)
  const [minTime, setMinTime] = useState('')
  const start = useAction(async () => {
    const r = await missionsApi.start(text.trim(), name.trim() || undefined, model, minTime.trim() || undefined)
    onStarted(r.conversation_id)
  })
  return (
    <Dialog title="New mission" size="md" onClose={onClose} dismissable={!start.busy}
            actions={<><Button onClick={onClose}>Cancel</Button>
              <Button variant="primary" icon={Flag} loading={start.busy} disabled={!text.trim()} onClick={() => void start.run()}>Start mission</Button></>}>
      <div className="missions-form">
        <Field label="Name"><Input value={name} placeholder="Optional" onChange={e => setName(e.target.value)} /></Field>
        <Field label="Instructions"><TextArea rows={5} max={14} value={text} autoFocus={!coarse()} onChange={e => setText(e.target.value)} /></Field>
        <Field label="Model" group>
          <ModelPick name={model ?? 'Default'} current={model} fallback={null} disabled={start.busy} onModel={setModel} />
        </Field>
        <Field label="Minimum time"><Input value={minTime} placeholder="Optional" onChange={e => setMinTime(e.target.value)} /></Field>
      </div>
      {start.error && <InlineError compact message={start.error} />}
    </Dialog>
  )
}

/* a mission: one scroll with a side index; its Work (the conversation) is a page of its own
   under it, as its instructions' editor is */
function MissionPage({ m, agents, active, onChange }: { m: Mission; agents: AgentRow[]; active: boolean; onChange: () => void }) {
  const nav = useNav()
  const [work, setWork] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [trashing, setTrashing] = useState(false)
  // its instructions in the large editor, or their History; a restored version waits in `draft`
  const [brief, setBrief] = useState<'edit' | 'history' | null>(null)
  const [draft, setDraft] = useState<string | null>(null)
  const convo = useConversation(m.conversation_id)
  const act = useAction(async (fn: () => Promise<unknown>) => { await fn(); onChange() })
  const gone = !!m.trashed_at
  const waiting = m.status === 'needs_you'
  // where it is: its note, else its newest line
  const lastLine = firstLine([...convo.messages].reverse().find(x => x.role === 'assistant' && x.content.trim())?.content ?? '')
  const crumbs = [{ label: 'Missions', href: '/missions', onClick: (e: React.MouseEvent) => { e.preventDefault(); nav.go({ section: 'missions' }) } }]
  const more: MenuItem[] = [
    { label: 'Rename', icon: Pencil, disabled: gone, onSelect: () => setRenaming(true) },
    { label: 'Open terminal', icon: SquareTerminal, onSelect: () => nav.go({ section: 'terminals', mission: m.conversation_id }) },
    'sep',
    m.archived_at ? { label: 'Restore', icon: RotateCcw, onSelect: () => void act.run(() => api.archiveConversation(m.conversation_id, false)) }
      : { label: 'Archive', icon: Archive, disabled: gone, onSelect: () => void act.run(() => api.archiveConversation(m.conversation_id, true)) },
    gone ? { label: 'Restore', icon: RotateCcw, onSelect: () => void act.run(() => missionsApi.restore(m.conversation_id)) }
      : { label: 'Move to Trash', icon: Trash2, danger: true, onSelect: () => setTrashing(true) },
  ]
  const sections = [
    ...(waiting ? [{ id: 'm-wait', label: 'Waiting for you' }] : []),
    { id: 'm-ins', label: 'Instructions' },
    ...(m.plan.trim() || m.steps.length ? [{ id: 'm-plan', label: 'Plan', count: m.steps.length || undefined }] : []),
    ...(agents.length ? [{ id: 'm-agents', label: 'Sub-agents', count: agents.length }] : []),
    ...(m.artifact ? [{ id: 'm-result', label: 'Result' }] : []),
    { id: 'm-set', label: 'Settings' },
  ]
  const of = `mission:${m.conversation_id}:brief`
  const back = (to: 'edit' | null) => (e: React.MouseEvent) => { e.preventDefault(); setBrief(to); setWork(false); if (!to) setDraft(null) }
  const page = { label: m.name, href: format({ section: 'missions', conversation: m.conversation_id }), onClick: back(null) }
  if (brief === 'history') {
    return <TextHistory crumbs={[...crumbs, page, { label: 'Instructions', href: page.href, onClick: back('edit') }]}
                        source={textHistory(of)} current={draft ?? m.brief} onRestore={t => { setDraft(t); setBrief('edit') }} />
  }
  if (brief === 'edit') {
    return <LongEditor crumbs={[...crumbs, page]} title="Instructions" of={of}
                       more={[{ label: 'History', icon: History, onSelect: () => setBrief('history') }]}
                       texts={[{ key: 'brief', label: 'Instructions', value: draft ?? m.brief }]} dirty={draft !== null && draft !== m.brief}
                       hint="Make it shorter, or add a step"
                       onCancel={() => { setDraft(null); setBrief(null) }}
                       onSave={async v => { await missionsApi.edit(m.conversation_id, { brief: v.brief ?? '' }); setDraft(null); setBrief(null); onChange() }} />
  }
  return (
    <div className="missions-page">
      <PlaceHead level={1} crumbs={work ? [...crumbs, page] : crumbs} title={work ? 'Conversation' : m.name} titleAttr={work ? undefined : m.name}
                 detail={<Dot tone={convo.running ? 'running' : stateTone(m.status)} pulse={convo.running || m.status === 'active'} text={STATE_WORD[m.status]} />}
                 actions={<>
                   {work && !gone && <ModelRow cid={m.conversation_id} live={convo.settings} head />}
                   {work && <PlanButton plan={convo.plan} />}
                   {!work && <Button icon={MessagesSquare} onClick={() => setWork(true)}>Open conversation</Button>}
                   {isOpen(m.status) && !gone && <Button icon={CircleStop} onClick={() => setStopping(true)}>Stop</Button>}
                 </>}
                 more={more} />
      {act.error && <div className="list-page__note"><InlineError compact message={act.error} /></div>}
      {work ? <div className="missions-work"><Work cid={m.conversation_id} convo={convo} active={active} closed={gone} /></div> : (
        <div className="page"><div className="page__col page__col--wide">
          <PageIndex sections={sections}>
            {waiting && <Waiting m={m} />}
            <Section id="m-ins" title="Instructions"
                     action={!gone && m.status !== 'stopped' && <Button icon={Pencil} onClick={() => setBrief('edit')}>Edit</Button>}>
              <p className="missions-prose">{m.brief}</p>
              {!waiting && (m.note || lastLine) && <p className="missions-prose missions-prose--quiet">{m.note || lastLine}</p>}
            </Section>
            {/* its plan: its words and its steps, one section; none yet, none shown */}
            {(m.plan.trim() || m.steps.length > 0) && (
              <Section id="m-plan" title="Plan" count={m.steps.length || undefined}>
                {m.plan.trim() && <div className="prose"><Markdown text={underTitle(m.plan)} /></div>}
                {m.steps.length > 0 && <Steps m={m} />}
              </Section>
            )}
            {agents.length > 0 && (
              <Section id="m-agents" title="Sub-agents" count={agents.length}>
                <div className="missions-rows">{agents.map(a => <AgentLine key={a.id} a={a} />)}</div>
              </Section>
            )}
            {m.artifact && <Section id="m-result" title="Result"><Result m={m} /></Section>}
            <Section id="m-set" title="Settings"><Settings m={m} live={convo.settings} agents={agents.length} onChange={onChange} /></Section>
          </PageIndex>
        </div></div>
      )}
      {stopping && <StopMission m={m} onClose={() => setStopping(false)} onDone={onChange} />}
      {renaming && <Rename m={m} onClose={() => setRenaming(false)} onDone={onChange} />}
      {trashing && <TrashMission m={m} onClose={() => setTrashing(false)} onDone={onChange} />}
    </div>
  )
}

/* the mission waits for you: its question, and your reply into its conversation */
function Waiting({ m }: { m: Mission }) {
  const online = useOnline()
  const [text, setText] = useState('')
  const send = useAction(async () => { await conversationApi.send(m.conversation_id, text.trim(), []); setText('') })
  return (
    <Section id="m-wait" title="Waiting for you">
      {m.note && <p className="missions-prose">{m.note}</p>}
      <ComposerBox value={text} onChange={setText} onSend={() => void send.run()} placeholder="Reply"
                   ready={!!text.trim() && !send.busy} disabled={online ? undefined : 'Offline'} />
      {send.error && <InlineError compact message={send.error} />}
    </Section>
  )
}

/* its plan's headings sit under the section's title: # and ## become ### and #### (note:
   a heading line inside a code block moves too) */
const underTitle = (plan: string) => plan.replace(/^(#{1,4}) /gm, (_, h: string) => `${'#'.repeat(Math.min(4, h.length + 2))} `)

/* The plan's steps, the one it is at marked, each with its model and Change model */
function Steps({ m }: { m: Mission }) {
  const online = useOnline()
  const [choosing, setChoosing] = useState<number | null>(null)
  const providers = useResource(() => settingsApi.providers().then(r => r.providers), [], { events: SETTINGS_EVENTS })
  const own = useResource(() => settingsApi.conversationSettings(m.conversation_id), [m.conversation_id], { events: SETTINGS_EVENTS })
  const save = useAction((steps: MissionStep[]) => missionsApi.setSteps(m.conversation_id, steps))
  const mine = own.data?.effective.label ?? own.data?.effective.model ?? null
  const nameOf = (s: MissionStep) => s.model_label ?? (mine ? `Default, ${mine}` : 'Default')
  const pick = (i: number, model: string | null) => {
    setChoosing(null)
    if (m.steps[i]!.model !== model) void save.run(m.steps.map((s, k) => (k === i ? { ...s, model } : s)))
  }
  const tone = (i: number): DotTone => ({ done: 'ok', doing: 'running', todo: 'off' } as const)[m.steps[i]!.state]
  return (
    <>
      <div className="missions-rows">
        {m.steps.map((s, i) => (
          <ListRow key={i} density="lg" dot={tone(i)} title={s.title} tooltip={s.title} muted={tone(i) === 'ok'}
                   subtitle={nameOf(s)} aside={tone(i) === 'ok' ? STATE_WORD.done : tone(i) === 'running' ? STATE_WORD.active : undefined}
                   trailing={<IconButton icon={ArrowLeftRight} size="sm" label="Change model" disabled={!online || save.busy || !!m.trashed_at}
                                         onClick={() => setChoosing(i)} />} />
        ))}
      </div>
      {save.error && <InlineError compact message={save.error} />}
      {choosing !== null && providers.data && (
        <ModelChooser title={`Model for ${m.steps[choosing]!.title}`} providers={providers.data} needs={['chat']}
                      current={m.steps[choosing]!.model}
                      extra={{ label: mine ? `Default, ${mine}` : 'Default', onPick: () => pick(choosing, null) }}
                      onPick={ref => pick(choosing, ref)} onClose={() => setChoosing(null)} />
      )}
    </>
  )
}

const agentWord = (a: AgentRow) => (a.working ? STATE_WORD.active : STATE_WORD.done)
const agentLine = (a: AgentRow) => (a.working ? a.step_words ?? STATE_WORD.active : a.report ? firstLine(a.report.text) : a.last ?? '')

/* one sub-agent of the mission: its state, its model and agent CLI; it opens on its page */
function AgentLine({ a }: { a: AgentRow }) {
  const nav = useNav()
  const stop = useAction(() => agentsApi.stop(a.id))
  const items: MenuItem[] = [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => nav.go({ section: 'missions', conversation: a.id }) },
    ...(a.working ? ['sep' as const, { label: 'Stop', icon: CircleStop, onSelect: () => void stop.run() }] : []),
  ]
  return (
    <ListRow density="lg" dot={a.working ? 'running' : 'ok'} title={a.name} subtitle={agentLine(a)} aside={a.model ?? undefined}
             detail={[a.cli, a.device].filter(Boolean).join(', ') || undefined} meta={agentWord(a)} muted={!a.working}
             href={format({ section: 'missions', conversation: a.id })} onClick={click(() => nav.go({ section: 'missions', conversation: a.id }))}
             tooltip={stop.error ?? undefined}
             trailing={<MenuButton icon={MoreHorizontal} label="More" items={items} size="sm" />} />
  )
}

function Result({ m }: { m: Mission }) {
  const nav = useNav()
  const art = m.artifact ? artifactRef(m.artifact) : null
  const doc = m.artifact?.startsWith('documents:') ? m.artifact.slice('documents:'.length) : null
  if (art) return <span><ArtifactChip id={art} /></span>
  if (doc) {
    return <ListRow icon={FileText} title={doc.split('/').pop() ?? doc} subtitle={doc} href={format({ section: 'documents' })}
                    onClick={e => { e.preventDefault(); nav.openDocument(doc) }} />
  }
  return null
}

function Settings({ m, live, agents, onChange }: {
  m: Mission; live: ReturnType<typeof useConversation>['settings']; agents: number; onChange: () => void
}) {
  const auto = useAction(async (on: boolean) => { await contextApi.setAuto(m.conversation_id, on); onChange() })
  return (
    <>
      <Switch label={AUTO_CONTEXT} title={AUTO_CONTEXT_TIP} checked={m.auto_context} disabled={auto.busy || !!m.trashed_at}
              onChange={on => void auto.run(on)} />
      {auto.error && <InlineError compact message={auto.error} />}
      <div className="missions-model"><ModelRow cid={m.conversation_id} live={live} /></div>
      <Facts grid items={[
        ...(m.until ? [{ label: 'Runs until', value: stamp(m.until) }] : []),
        ...(m.min_until ? [{ label: 'Runs at least until', value: stamp(m.min_until) }] : []),
        { label: 'Started', value: stamp(m.created_at) },
        ...(agents ? [{ label: 'Sub-agents', value: agentsNoun(agents) }] : []),
      ]} />
    </>
  )
}

/* one sub-agent of a mission: its conversation; its result and details are a page under it */
/* a sub-agent another sub-agent started, or one in the Archive or the Trash (the
   sub-agents list leaves it out): its conversation; any other id is no mission */
function ShelvedAgent({ cid, active }: { cid: string; active: boolean }) {
  const convo = useConversation(cid)
  const row = useResource(() => request<{ conversation: { title: string; agent_of: string | null } }>(`/conversations/${cid}`), [cid])
  const c = row.data?.conversation
  const crumbs = [{ label: 'Missions', href: '/missions' }]
  if (!c && !row.missing && !row.error) return <LoadingRow label="Loading the conversation" />
  if (!c?.agent_of) return <div className="missions-page"><PlaceHead level={1} title="Missions" crumbs={crumbs} />
    <EmptyState icon={SearchX} title="No mission at this link" /></div>
  return (
    <div className="missions-page">
      <PlaceHead level={1} title={c.title} titleAttr={c.title} crumbs={crumbs} />
      <div className="missions-work"><Work cid={cid} convo={convo} active={active} closed={false} /></div>
    </div>
  )
}

function AgentPage({ a, mission, active }: { a: AgentRow; mission: Mission; active: boolean }) {
  const nav = useNav()
  const [result, setResult] = useState(false)
  const convo = useConversation(a.id)
  const stop = useAction(() => agentsApi.stop(a.id))
  const go = (r: Route) => (e: React.MouseEvent) => { e.preventDefault(); nav.go(r) }
  const crumbs = [{ label: 'Missions', href: '/missions', onClick: go({ section: 'missions' }) },
                  { label: mission.name, href: format({ section: 'missions', conversation: mission.conversation_id }),
                    onClick: go({ section: 'missions', conversation: mission.conversation_id }) }]
  const self = format({ section: 'missions', conversation: a.id })
  return (
    <div className="missions-page">
      <PlaceHead level={1} title={result ? 'Result' : a.name} titleAttr={result ? undefined : a.name}
                 crumbs={result ? [...crumbs, { label: a.name, href: self, onClick: (e: React.MouseEvent) => { e.preventDefault(); setResult(false) } }] : crumbs}
                 detail={<Dot tone={a.working ? 'running' : 'ok'} pulse={a.working} text={agentWord(a)} />}
                 actions={<>
                   {!result && <Button icon={FileText} onClick={() => setResult(true)}>Result</Button>}
                   {a.working && <Button icon={CircleStop} loading={stop.busy} onClick={() => void stop.run()}>Stop</Button>}
                 </>} />
      {stop.error && <div className="list-page__note"><InlineError compact message={stop.error} /></div>}
      {!result ? <div className="missions-work"><Work cid={a.id} convo={convo} active={active} closed={false} /></div> : (
        <div className="page"><div className="page__col page__col--prose">
          <Section>
            {a.report ? <div className="prose"><Markdown text={a.report.text} /></div>
              : <p className="missions-prose missions-prose--quiet">{a.working ? a.step_words ?? STATE_WORD.active : 'No result'}</p>}
            <Facts grid items={[
              { label: 'Model', value: a.model ?? 'Default' },
              ...(a.cli ? [{ label: 'Agent CLI', value: a.cli }] : []),
              ...(a.device ? [{ label: 'Device', value: a.device }] : []),
              { label: 'Access', value: a.access === 'read' ? 'Read-only' : 'Can edit' },
              ...(a.folder ? [{ label: 'Folder', value: a.folder }] : []),
              { label: 'Started', value: stamp(a.created_at) },
            ]} />
          </Section>
        </div></div>
      )}
    </div>
  )
}

/* every sub-agent of every mission */
function AgentsList({ agents, missions }: { agents: AgentRow[]; missions: Mission[] }) {
  const nav = useNav()
  const ctx = useContextMenu()
  const [q, setQ] = useState('')
  const [show, setShow] = useState('all')
  const [of, setOf] = useState<string | null>(null)
  const [sort, setSort] = useState('Recent')
  const name = useMemo(() => new Map(missions.map(m => [m.conversation_id, m.name])), [missions])
  const list = useMemo(() => {
    const s = q.trim().toLowerCase()
    const out = agents.filter(a => (show === 'all' || (show === 'running') === a.working) && (!of || a.parent === of)
      && (!s || [a.name, agentLine(a), name.get(a.parent) ?? ''].some(x => x.toLowerCase().includes(s))))
    return sort === 'Name' ? [...out].sort((a, b) => a.name.localeCompare(b.name))
      : sort === 'Created' ? [...out].sort((a, b) => b.created_at.localeCompare(a.created_at))
      : [...out].sort((a, b) => b.updated_at.localeCompare(a.updated_at))
  }, [agents, show, of, q, sort, name])
  const menu = (a: AgentRow): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => nav.go({ section: 'missions', conversation: a.id }) },
    ...(name.has(a.parent) ? [{ label: 'Open mission', icon: Flag, onSelect: () => nav.go({ section: 'missions', conversation: a.parent }) }] : []),
    ...(a.working ? ['sep' as const, { label: 'Stop', icon: CircleStop, onSelect: () => void agentsApi.stop(a.id) }] : []),
  ]
  const parents = [...new Set(agents.map(a => a.parent))].filter(p => name.has(p))
  return (
    <div className="list-page">
      <PlaceHead level={1} title="Sub-agents" count={agents.length || undefined} search={{ value: q, onChange: setQ, placeholder: 'Search sub-agents' }} />
      {agents.length > 0 && (
        <div className="list-page__top">
          <Toolbar state={{ label: 'State', value: show, onChange: setShow, options: [
                     { value: 'all', label: 'All', count: agents.length },
                     { value: 'running', label: STATE_WORD.active, count: agents.filter(a => a.working).length },
                     { value: 'done', label: STATE_WORD.done, count: agents.filter(a => !a.working).length }] }}
                   filters={parents.length === 0 ? [] : [{ label: 'Mission', icon: Flag, picked: of ? name.get(of) : undefined, onClear: () => setOf(null),
                               items: parents.map(p => ({ label: name.get(p) ?? p, current: p === of, onSelect: () => setOf(p) })) }]}
                   sort={{ label: 'Sort', value: sort, onChange: setSort, options: SORTS.map(v => ({ value: v, label: v })) }} />
        </div>
      )}
      {agents.length === 0 ? <div className="list-page__empty"><EmptyState icon={Bot} title="No sub-agents yet" /></div>
        : list.length === 0 ? (
          <div className="list-page__empty">
            <EmptyState icon={SearchX} title="No sub-agents match" action={q && <Button onClick={() => setQ('')}>Clear search</Button>} />
          </div>
        ) : (
          <div className="list-page__rows">
            <VirtualList items={list} estimate={84} getKey={a => a.id} label="Sub-agents" resetOn={`${show}|${of}`}
                         renderRow={a => (
                           <ListRow density="lg" icon={Bot} title={a.name} subtitle={agentLine(a)} detail={name.get(a.parent)}
                                    column={<span>{[a.model, a.cli, a.device].filter(Boolean).join(', ')}</span>} cell={agentWord(a)}
                                    meta={listTime(a.updated_at)} muted={!a.working}
                                    href={format({ section: 'missions', conversation: a.id })} onClick={click(() => nav.go({ section: 'missions', conversation: a.id }))}
                                    onContextMenu={e => ctx.open(e, menu(a), a.name)}
                                    trailing={<MenuButton icon={MoreHorizontal} label="More" items={menu(a)} size="sm" />} />
                         )} />
          </div>
        )}
      {ctx.menu}
    </div>
  )
}

/* The conversation and the box to write in: a word to it where it works (Routines shows
   what an event started with it too) */
export function Work({ cid, convo, active, closed }: { cid: string; convo: ReturnType<typeof useConversation>; active: boolean; closed: boolean }) {
  const her = useName()
  // a sub-agent's conversation writes to the sub-agent
  const name = convo.conversation?.agent_name ?? her
  return (
    <Talk cid={cid} convo={convo} active={active} name={name} user=""
          send={(text, attachments) => conversationApi.send(cid, text, attachments)}
          closed={closed ? 'It is in the Trash. Restore it to go on.' : null} />
  )
}
