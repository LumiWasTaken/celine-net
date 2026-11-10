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
import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import {
  Archive, ArrowLeftRight, ArrowUpRight, Bot, CircleStop, FileText, Flag, History, ListChecks, MessagesSquare, MoreHorizontal, Pencil, Plus, Reply,
  RotateCcw, SearchX, SquareTerminal, Trash2,
} from 'lucide-react'
import { api, conversationApi, request } from '../lib/api'
import { AUTO_CONTEXT, AUTO_CONTEXT_TIP, contextApi } from '../lib/context'
import { agentsApi, useAgents, type Agent, type TreeAgent, type TreeMission } from '../lib/agents'
import { artifactRef } from '../lib/artifacts'
import { count, errorText, listTime, stamp } from '../lib/format'
import {
  isMissionEvent, isOpen, missionsApi, type Mission, type MissionStatus, type MissionStep,
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
import { MissionAsk, StopMission } from '../components/MissionAsk'
import { useWaits, type MissionWait } from '../lib/waits'
import { toneOf, type Said } from '../lib/states'
import { ModelPick, ModelRow } from '../components/ConversationPanel'
import { ModelChooser } from '../components/ModelChooser'
import { LongEditor, TextHistory, textHistory } from '../components/editor'
import {
  Button, ConfirmDialog, Dialog, EmptyState, Facts, Field, GroupHead, ICON, IconButton, InlineError, Input, ListRow,
  ListSection, Loaded, LoadingRow, MenuButton, PageIndex, Place, PlaceHead, Section, SelectBar, ShelfRow, Switch, TextArea, Toolbar,
  StateWord, Step, coarse, useContextMenu, usePicker, VirtualList, type MenuItem, type Shelf,
} from '../ui'
import './Missions.css'

const firstLine = (text: string) => text.trim().split('\n')[0] ?? ''

/** every status of a mission as the server says it (GET /missions `states`): the list's
    filter and group heads */
const States = createContext<Record<MissionStatus, Said> | undefined>(undefined)
type TreeNode = (TreeAgent & { kind: 'agent' }) | (TreeMission & { kind: 'mission' })

/** the tree's sub-agents and missions in its order: each before its own, siblings oldest first */
export function treeOrder(top: string, agents: TreeAgent[], missions: TreeMission[]): TreeNode[] {
  const out: TreeNode[] = []
  const walk = (parent: string) => {
    for (const a of agents) if (a.parent === parent) { out.push({ ...a, kind: 'agent' }); walk(a.id) }
    for (const m of missions) if (m.parent === parent) { out.push({ ...m, kind: 'mission' }); walk(m.id) }
  }
  walk(top)
  return out
}
const missionsNoun = (n: number) => count(n, 'mission')
const agentsNoun = (n: number) => count(n, 'sub-agent')

/** a sub-agent as GET /agents sends it: its model and agent CLI too */
type AgentRow = Agent & { model?: string | null; cli?: string | null; device?: string | null; last?: string }

type Show = 'all' | MissionStatus
const STATUSES: MissionStatus[] = ['active', 'needs_you', 'done', 'stopped']
/** the order of a sub-agent's states in the State filter */
const AGENT_STATES: Agent['state'][] = ['queued', 'working', 'done', 'stopped', 'paused', 'failed']
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
    <States.Provider value={missions.data?.states}>
    <Place title="Missions" action={<IconButton icon={Plus} label="New mission" onClick={() => setStarting(true)} />}
           side={<>
             <ListRow density="sm" icon={Flag} title="Missions" meta={counts?.all} selected={!selected && view === 'missions'}
                      onClick={() => toList('missions')} />
             <ListRow density="sm" icon={Bot} title="Sub-agents" meta={loaded ? theirs.length : undefined} selected={!selected && view === 'agents'}
                      onClick={() => toList('agents')} />
             {recent.length > 0 && <ListSection label="Recent" />}
             {recent.map(m => (
               <ListRow key={m.conversation_id} density="sm" icon={Flag} title={m.name} tooltip={m.name}
                        meta={<StateWord tone={toneOf(m.tone)}>{m.words}</StateWord>}
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
    </States.Provider>
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
  const states = useContext(States)
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
    return STATUSES.map(value => ({ value, label: states?.[value].words ?? '' })).flatMap(s => {
      const these = items.filter(m => m.status === s.value)
      return these.length ? [{ key: `g:${s.value}`, head: s.label, n: these.length }, ...these.map(m => ({ key: m.conversation_id, m }))] : []
    })
  }, [items, group, shelf, states])
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
                                options: [{ value: 'all', label: 'All', count: counts?.all },
                                          ...STATUSES.map(value => ({ value, label: states?.[value].words ?? '', count: counts?.[value] }))] }}
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
                             <ShelfRow shelf={shelf} density="lg" title={r.m.name} subtitle={`${r.m.words}, ${listTime(r.m.updated_at)}`}
                                       onClick={() => open(r.m)} pick={picker.row(r.m.conversation_id)} onSelect={() => picker.start(r.m.conversation_id)}
                                       onRestore={() => void act.run(() => shelf === 'archive' ? api.archiveConversation(r.m.conversation_id, false)
                                         : missionsApi.restore(r.m.conversation_id))}
                                       onTrash={() => setTrashing(r.m)}
                                       onDelete={() => act.run(() => missionsApi.remove(r.m.conversation_id))} />
                           ) : (
                             <ListRow density="lg" icon={Flag} title={r.m.name} tooltip={r.m.name}
                                      subtitle={r.m.note || firstLine(r.m.brief)} muted={r.m.status === 'done' || r.m.status === 'stopped'}
                                      column={<span className="missions-column">{kids.get(r.m.conversation_id) ? agentsNoun(kids.get(r.m.conversation_id)!) : ''}
                                        {r.m.artifact && <FileText size={ICON.sm} aria-label="Result" />}</span>}
                                      cell={<StateWord tone={toneOf(r.m.tone)}>{r.m.words}</StateWord>} meta={<time dateTime={r.m.updated_at}>{listTime(r.m.updated_at)}</time>}
                                      href={format({ section: 'missions', conversation: r.m.conversation_id })} onClick={click(() => open(r.m))}
                                      pick={picker.row(r.m.conversation_id)} onContextMenu={e => ctx.open(e, menu(r.m), r.m.name)}
                                      trailing={!picker.on && <MenuButton icon={MoreHorizontal} label="More" items={menu(r.m)} size="sm" />} />
                           )} />
          </div>
        )}
      </Loaded>
      {ctx.menu}
      {renaming && <Rename m={renaming} onClose={() => setRenaming(null)} onDone={onChange} />}
      {stopping && <StopMission cid={stopping.conversation_id} name={stopping.name} onClose={() => setStopping(null)} onDone={onChange} />}
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

function TrashMission({ m, onClose, onDone }: { m: Mission; onClose: () => void; onDone: () => void }) {
  const nav = useNav()
  return (
    <ConfirmDialog title={`Move ${m.name} to Trash?`} danger confirmLabel="Move to Trash" body="You can restore it from the Trash."
                   onClose={onClose}
                   onConfirm={async () => {
                     try { await missionsApi.trash(m.conversation_id) } catch (e) { return errorText(e) }
                     if (nav.route.section === 'missions' && nav.route.conversation === m.conversation_id) nav.go({ section: 'missions' }, { replace: true })
                     onDone(); onClose()
                   }} />
  )
}

/** a check-in cadence in minutes as the form takes it: "30 min", "2 h", "1 h 30 min" */
const every = (min: number) => min < 60 ? `${min} min` : `${Math.floor(min / 60)} h${min % 60 ? ` ${min % 60} min` : ''}`

/* New mission: the instructions, and a name when you want one (else it names it) */
function NewMission({ onClose, onStarted }: { onClose: () => void; onStarted: (cid: string) => void }) {
  const [text, setText] = useState('')
  const [name, setName] = useState('')
  const [model, setModel] = useState<string | null>(null)
  const [minTime, setMinTime] = useState('')
  const [checkIn, setCheckIn] = useState('')
  const start = useAction(async () => {
    const r = await missionsApi.start(text.trim(), name.trim() || undefined, model, minTime.trim() || undefined,
                                      checkIn.trim() || undefined)
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
        <Field label="Check-in"><Input value={checkIn} placeholder="Optional" onChange={e => setCheckIn(e.target.value)} /></Field>
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
  // everything under it, the server's tree: its sub-agents at every depth, the missions they started
  const tree = useResource(() => agentsApi.tree(m.conversation_id), [m.conversation_id],
                           { events: ev => ev.type === 'agent.state' || ev.type === 'mission.state' })
  const nodes = tree.data ? treeOrder(tree.data.top, tree.data.agents, tree.data.missions) : []
  const act = useAction(async (fn: () => Promise<unknown>) => { await fn(); onChange() })
  const gone = !!m.trashed_at
  // what it asks you, the server's item of Waiting for you
  const waiting = useWaits().data?.items.find((w): w is MissionWait => w.kind === 'mission' && w.id === m.conversation_id)
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
    ...(nodes.length ? [{ id: 'm-agents', label: 'Sub-agents', count: nodes.length }] : []),
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
      <PlaceHead level={1} crumbs={work ? [...crumbs, page] : crumbs} title={work ? 'Chat' : m.name} titleAttr={work ? undefined : m.name}
                 detail={<StateWord tone={convo.running ? 'running' : toneOf(m.tone)}>{m.words}</StateWord>}
                 actions={<>
                   {work && !gone && <ModelRow cid={m.conversation_id} live={convo.settings} head />}
                   {work && <PlanButton plan={convo.plan} />}
                   {!work && <Button icon={MessagesSquare} onClick={() => setWork(true)}>Open chat</Button>}
                   {isOpen(m.status) && !gone && <Button icon={CircleStop} onClick={() => setStopping(true)}>Stop</Button>}
                 </>}
                 more={more} />
      {act.error && <div className="list-page__note"><InlineError compact message={act.error} /></div>}
      {work ? <div className="missions-work"><Work cid={m.conversation_id} convo={convo} active={active} closed={gone} /></div> : (
        <div className="page"><div className="page__col page__col--wide">
          <PageIndex sections={sections}>
            {waiting && <Section id="m-wait" title="Waiting for you"><MissionAsk w={waiting} stop={false} /></Section>}
            <Section id="m-ins" title="Instructions"
                     action={!gone && m.status !== 'stopped' && <Button icon={Pencil} onClick={() => setBrief('edit')}>Edit</Button>}>
              <p className="missions-prose">{m.brief}</p>
              {m.status !== 'needs_you' && (m.note || lastLine) && <p className="missions-prose missions-prose--quiet">{m.note || lastLine}</p>}
            </Section>
            {/* its plan: its words and its steps, one section; none yet, none shown */}
            {(m.plan.trim() || m.steps.length > 0) && (
              <Section id="m-plan" title="Plan" count={m.steps.length || undefined}>
                {m.plan.trim() && <div className="prose"><Markdown text={underTitle(m.plan)} /></div>}
                {m.steps.length > 0 && <Steps m={m} />}
              </Section>
            )}
            {nodes.length > 0 && tree.data && (
              <Section id="m-agents" title="Sub-agents" count={nodes.length}>
                <div className="missions-rows">{nodes.map(n => <TreeLine key={n.id} n={n} />)}</div>
                <Facts items={[{ label: 'Tokens', value: `${tree.data.total.prompt_tokens} in, ${tree.data.total.completion_tokens} out` }]} />
              </Section>
            )}
            {m.artifact && <Section id="m-result" title="Result"><Result m={m} /></Section>}
            <Section id="m-set" title="Settings"><Settings m={m} live={convo.settings} agents={agents.length} origin={tree.data?.origin.at(-1)} onChange={onChange} /></Section>
          </PageIndex>
        </div></div>
      )}
      {stopping && <StopMission cid={m.conversation_id} name={m.name} onClose={() => setStopping(false)} onDone={onChange} />}
      {renaming && <Rename m={m} onClose={() => setRenaming(false)} onDone={onChange} />}
      {trashing && <TrashMission m={m} onClose={() => setTrashing(false)} onDone={onChange} />}
    </div>
  )
}

/* its plan's headings sit under the section's title: # and ## become ### and #### (note:
   a heading line inside a code block moves too) */
const underTitle = (plan: string) => plan.replace(/^(#{1,4}) /gm, (_, h: string) => `${'#'.repeat(Math.min(4, h.length + 2))} `)

/* The plan's steps, the one it is at marked, each with its model and Change model */
function Steps({ m }: { m: Mission }) {
  const states = useContext(States)
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
  return (
    <>
      <div className="missions-rows">
        {m.steps.map((s, i) => (
          <ListRow key={i} density="lg" leading={<Step n={i + 1} done={s.state === 'done'} current={s.state === 'doing'} />} title={s.title} tooltip={s.title}
                   muted={s.state === 'done'} subtitle={nameOf(s)} aside={s.state === 'done' ? states?.done.words : s.state === 'doing' ? states?.active.words : undefined}
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

const agentLine = (a: AgentRow) => (a.working ? a.step_words ?? a.words : a.report ? firstLine(a.report.text) : a.last ?? '')

/* one sub-agent under the mission (any depth), or a mission one of them started: its state
   in a word, why it failed or what it does now, its model; it opens where it lives */
export function TreeLine({ n }: { n: TreeNode }) {
  const nav = useNav()
  const stop = useAction(() => agentsApi.stop(n.id))
  const working = n.kind === 'agent' && (n.state === 'working' || n.state === 'queued')
  const items: MenuItem[] = [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => nav.goPath(n.link) },
    ...(working ? ['sep' as const, { label: 'Stop', icon: CircleStop, onSelect: () => void stop.run() }] : []),
  ]
  return (
    <ListRow density="lg" depth={n.depth - 1} icon={n.kind === 'mission' ? Flag : Bot} title={n.name} tooltip={stop.error ?? n.name}
             subtitle={n.reason ?? (n.kind === 'agent' && working ? n.step_words : null) ?? undefined}
             aside={n.model ?? undefined} meta={<StateWord tone={toneOf(n.tone)}>{n.words}</StateWord>} muted={!working && n.kind === 'agent'}
             href={n.link} onClick={click(() => nav.goPath(n.link))}
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

/* `origin`: the chat or run that started it, the server's (the tree's origin) */
function Settings({ m, live, agents, origin, onChange }: {
  m: Mission; live: ReturnType<typeof useConversation>['settings']; agents: number
  origin?: { name: string; link: string | null }; onChange: () => void
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
        ...(m.check_in ? [{ label: 'Check-in', value: `Every ${every(m.check_in)}` }] : []),
        { label: 'Created', value: stamp(m.created_at) },
        ...(origin ? [{ label: 'Started from', value: origin.link ? <a href={origin.link}>{origin.name}</a> : origin.name }] : []),
        ...(agents ? [{ label: 'Sub-agents', value: agentsNoun(agents) }] : []),
      ]} />
    </>
  )
}

/* the pages above a sub-agent's conversation: Missions, then the chain above it as the
   server names it (homes.path: the run or chat that started its mission, the mission, the
   sub-agents between) */
const above = (path: { name: string; link: string | null }[] | undefined) =>
  [{ label: 'Missions', href: '/missions' }, ...(path ?? []).slice(0, -1).map(c => ({ label: c.name, href: c.link ?? undefined }))]

/* one sub-agent of a mission: its conversation; its result and details are a page under it */
/* a sub-agent another sub-agent started, or one in the Archive or the Trash (the
   sub-agents list leaves it out): its conversation; any other id is no mission */
function ShelvedAgent({ cid, active }: { cid: string; active: boolean }) {
  const convo = useConversation(cid)
  const row = useResource(() => request<{ conversation: { title: string; agent_of: string | null } }>(`/conversations/${cid}`), [cid])
  const c = row.data?.conversation
  const crumbs = above(convo.conversation?.path)
  if (!c && !row.missing && !row.error) return <LoadingRow label="Loading the chat" />
  if (!c?.agent_of) return <div className="missions-page"><PlaceHead level={1} title="Missions" crumbs={crumbs} />
    <EmptyState icon={SearchX} title="Not found">Nothing at this link.</EmptyState></div>
  return (
    <div className="missions-page">
      <PlaceHead level={1} title={c.title} titleAttr={c.title} crumbs={crumbs} />
      <div className="missions-work"><Work cid={cid} convo={convo} active={active} closed={false} /></div>
    </div>
  )
}

function AgentPage({ a, mission, active }: { a: AgentRow; mission: Mission; active: boolean }) {
  const [result, setResult] = useState(false)
  const convo = useConversation(a.id)
  const stop = useAction(() => agentsApi.stop(a.id))
  // until its conversation answered: Missions and its mission
  const crumbs = convo.conversation?.path ? above(convo.conversation.path)
    : [{ label: 'Missions', href: '/missions' }, { label: mission.name, href: format({ section: 'missions', conversation: mission.conversation_id }) }]
  const self = format({ section: 'missions', conversation: a.id })
  return (
    <div className="missions-page">
      <PlaceHead level={1} title={result ? 'Result' : a.name} titleAttr={result ? undefined : a.name}
                 crumbs={result ? [...crumbs, { label: a.name, href: self, onClick: (e: React.MouseEvent) => { e.preventDefault(); setResult(false) } }] : crumbs}
                 detail={<StateWord tone={toneOf(a.tone)}>{a.words}</StateWord>}
                 actions={<>
                   {!result && <Button icon={FileText} onClick={() => setResult(true)}>Result</Button>}
                   {a.working && <Button icon={CircleStop} loading={stop.busy} onClick={() => void stop.run()}>Stop</Button>}
                 </>} />
      {stop.error && <div className="list-page__note"><InlineError compact message={stop.error} /></div>}
      {!result ? <div className="missions-work"><Work cid={a.id} convo={convo} active={active} closed={false} /></div> : (
        <div className="page"><div className="page__col page__col--prose">
          <Section>
            {a.report ? <div className="prose"><Markdown text={a.report.text} /></div>
              : <p className="missions-prose missions-prose--quiet">{a.working ? a.step_words ?? a.words : 'No result'}</p>}
            <Facts grid items={[
              { label: 'Model', value: a.model ?? 'Default' },
              ...(a.cli ? [{ label: 'Agent CLI', value: a.cli }] : []),
              ...(a.device ? [{ label: 'Device', value: a.device }] : []),
              { label: 'Access', value: a.access === 'read' ? 'Read-only' : 'Can edit' },
              ...(a.folder ? [{ label: 'Folder', value: a.folder }] : []),
              { label: 'Created', value: stamp(a.created_at) },
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
    const out = agents.filter(a => (show === 'all' || show === a.words) && (!of || a.parent === of)
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
  // one option per state word: Stopped holds a stopped and a paused one
  const words = [...new Set(AGENT_STATES.flatMap(s => agents.filter(a => a.state === s).map(a => a.words)))]
  return (
    <div className="list-page">
      <PlaceHead level={1} title="Sub-agents" count={agents.length || undefined} search={{ value: q, onChange: setQ, placeholder: 'Search sub-agents' }} />
      {agents.length > 0 && (
        <div className="list-page__top">
          <Toolbar state={{ label: 'State', value: show, onChange: setShow, options: [
                     { value: 'all', label: 'All', count: agents.length },
                     ...words.map(w => ({ value: w, label: w, count: agents.filter(a => a.words === w).length }))] }}
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
                                    column={<span>{[a.model, a.cli, a.device].filter(Boolean).join(', ')}</span>} cell={a.words}
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
  const agentName = useName()
  // a sub-agent's conversation writes to the sub-agent
  const name = convo.conversation?.agent_name ?? agentName
  return (
    <Talk cid={cid} convo={convo} active={active} name={name} user=""
          send={(text, attachments) => conversationApi.send(cid, text, attachments)}
          closed={closed ? 'It is in the Trash. Restore it to continue.' : null} />
  )
}
