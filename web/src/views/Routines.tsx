/* Routines: what it does on its own schedule. The place's sidebar holds Runs, Schedules and
   Triggers with their counts, then the day's runs. Runs: one day as a timeline (the
   schedules at their times, a run on each, a run that runs after another drawn as a line to
   it), fourteen days as a row above it (a day whose newest try of a schedule failed in
   the error tone; Earlier and Later step through older ones), the state and the schedule
   to narrow it. Each run says its state as a word and the server's line under it. A run
   opens on its page (its result, the turns with a reviewer, what it started, what it was
   asked; Run now, Retry, Stop). A schedule opens to edit (when, what it runs after, who,
   the prompt, the reviewer, the Limits with When a run fails, its runs; Run now, Delete
   asks first); its prompts open in the large editor with History. Triggers:
   Folder and Event, a switch each, and what they started, which opens with its sub-agents.
   Every state, reason and time is the server's.
   Keys: Esc closes, Left and Right the day, Up and Down the run, N a new schedule, Ctrl+S
   saves the schedule.
   URL: /routines, /routines/<day>, /routines/<day>/run/<id>, /routines/<day>/schedule/<name>,
   /routines/<day>/schedule/<name>/edit/<key>[/history], /routines/<day>/task/<id>. */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowUpRight, Bot, Braces, CalendarClock, ChevronLeft, ChevronRight, CircleStop, History, MoreHorizontal, NotebookPen, Pencil,
  Play, Plus, RotateCcw, SearchX, Trash2, Zap,
} from 'lucide-react'
import type { Message as ChatMessage } from '../lib/api'
import { LOCALE, listTime, shortDate, turnStep } from '../lib/format'
import { agentsApi, useAgents } from '../lib/agents'
import { click } from '../components/ConversationList'
import { TreeLine, Work, treeOrder } from './Missions'
import { isSaveKey, isTyping } from '../lib/keys'
import { format, useNav, type Route } from '../lib/nav'
import {
  ASKED_WORD, FINISHED, RETRIES, RUN_TONE, STATE_WORD, isRunEvent, nightWords, routinesApi, type EventTask, type Night, type OnFail, type Run, type Runs, type RunState,
  type Schedule, type ScheduleBody, type ScheduleWords, type Trigger,
} from '../lib/routines'
import { OFFLINE_TITLE, useOnline } from '../lib/socket'
import { useName } from '../lib/name'
import { useAction } from '../lib/useAction'
import { useConversation } from '../lib/useConversation'
import { useResource } from '../lib/useResource'
import { Markdown } from '../components/Markdown'
import { Portrait } from '../components/Portrait'
import { PlanButton } from '../components/Plan'
import { LongEditor, TextHistory, textHistory } from '../components/editor'
import {
  Avatar, Button, Checkbox, Choice, ConfirmDialog, copied, EmptyState, Facts, Field, GroupHead, ICON, IconButton, InlineError, Input,
  ListRow, ListSection, Loaded, LoadingRow, MenuButton, Message, NumberField, PageIndex, Place, PlaceHead, Section, Segmented,
  StateWord, Switch, Toolbar, useContextMenu, VirtualList, type Crumb, type MenuItem,
} from '../ui'
import './Routines.css'

type State = RunState | 'planned'
type Node = {
  key: string
  label: string
  /** the time of day it shows at, or when it runs next */
  at: string
  /** its place in the day: its slot (ms), then the runs-after order */
  sort: number
  state: State
  reason: string
  run?: Run
  schedule?: Schedule
}
type View = 'runs' | 'schedules' | 'triggers'
type Open = { night?: string; run?: string; schedule?: string; task?: string; edit?: string; history?: boolean }

const weekdayOf = (night: string) => new Date(`${night}T12:00:00`).toLocaleDateString(LOCALE, { weekday: 'short' })
/** in a row: "Sat 10 Oct" */
const dayShort = (night: string) => `${weekdayOf(night)} ${shortDate(Date.parse(`${night}T12:00:00`))}`
/** a day `days` after another (before, when negative) */
const shift = (night: string, days: number) => {
  const d = new Date(`${night}T12:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}
const LIVE: State[] = ['waiting', 'running']

/** what the page edits of a schedule, kept while you go to its prompts and back */
type Draft = {
  key: string; label: string; summary: string; when: 'daily' | 'cron'; time: string; cron: string; after: string[]; role: string
  prompt: string; has_reviewer: boolean; reviewer: string; reviewer_prompt: string; turns: string; note: string; on: boolean
  idle_only: boolean; wall: string; daytime: boolean; on_fail: OnFail
}
const draftOf = (s: Schedule | null): Draft => ({
  key: s?.name ?? '', label: s?.label ?? '', summary: s?.summary ?? '', when: s && !s.time ? 'cron' : 'daily', time: s?.time ?? '02:00', cron: s?.cron ?? '0 2 * * *',
  after: s?.after ?? [], role: s?.role ?? 'chat', prompt: s?.prompt ?? '', has_reviewer: !!s?.reviewer,
  reviewer: s?.reviewer ?? '', reviewer_prompt: s?.reviewer_prompt ?? '', turns: String(s?.turns ?? 8), note: s?.note ?? '',
  on: s?.on ?? true, idle_only: s?.idle_only ?? false, wall: s?.wall_minutes != null ? String(s.wall_minutes) : '', daytime: s?.daytime ?? false,
  on_fail: s?.on_fail ?? 'retry',
})
const bodyOf = (d: Draft): ScheduleBody => ({
  label: d.label.trim(), summary: d.summary, ...(d.when === 'daily' ? { time: d.time } : { cron: d.cron.trim() }), prompt: d.prompt, after: d.after,
  role: d.role, reviewer: d.has_reviewer ? d.reviewer : '', reviewer_prompt: d.has_reviewer ? d.reviewer_prompt : '',
  turns: Number(d.turns) || 0, note: d.note.trim(), on: d.on, idle_only: d.idle_only,
  daytime: d.daytime, wall_minutes: d.wall.trim() ? Number(d.wall) || null : null, on_fail: d.on_fail,
})
/** the long texts of a schedule, by the editor's key */
const TEXTS = { prompt: { field: 'prompt', label: 'Prompt' }, reviewer: { field: 'reviewer_prompt', label: 'Reviewer prompt' } } as const
type TextKey = keyof typeof TEXTS

export function Routines({ active }: { active: boolean }) {
  const nav = useNav()
  // the page the URL names; while another place is on screen, the one it showed last
  const last = useRef<Extract<Route, { section: 'routines' }>>({ section: 'routines' })
  if (nav.route.section === 'routines') last.current = nav.route
  const route = last.current
  const [view, setView] = useState<View>('runs')
  const schedules = useResource(signal => routinesApi.schedules(signal), [], { events: ev => ev.type === 'settings.changed' })
  const night = route.night ?? schedules.data?.tonight
  // the strip's last day: today, or an older one after Earlier (or a link to an old day)
  const [until, setUntil] = useState<string | undefined>(undefined)
  const runs = useResource(signal => routinesApi.runs(night, signal, until), [night, until], { events: isRunEvent })
  const triggers = useResource(signal => routinesApi.triggers(signal), [], { events: ev => ev.type === 'settings.changed' })
  const tasks = useResource(signal => routinesApi.tasks(signal), [], { events: isTaskEvent })
  const today = runs.data?.tonight ?? schedules.data?.tonight
  useEffect(() => {
    if (night && today && night < shift(until ?? today, -13)) setUntil(night)
  }, [night, today]) // eslint-disable-line react-hooks/exhaustive-deps
  const go = (r: Open, replace = false) => nav.go({ section: 'routines', night: r.night ?? night, ...r }, { replace })
  const drafts = useRef(new Map<string, Draft>())

  const byName = useMemo(() => new Map((schedules.data?.schedules ?? []).map(s => [s.name, s])), [schedules.data])
  const nodes = useMemo<Node[]>(() => {
    const out: Node[] = (runs.data?.runs ?? []).map(r => ({
      key: r.id, label: r.label, at: r.at, sort: Date.parse(r.slot), state: r.state, reason: r.reason, run: r, schedule: byName.get(r.schedule),
    }))
    // today also shows what is still to come today (the server's next slot and its day)
    if (night && night === today) {
      for (const s of schedules.data?.schedules ?? []) {
        if (s.on && s.next && s.next_night === night && !out.some(n => n.run?.schedule === s.name)) {
          out.push({ key: `s:${s.name}`, label: s.title, at: s.next_at ?? '', sort: Date.parse(s.next), state: 'planned', reason: '', schedule: s })
        }
      }
    }
    // how many runs-after deep a schedule is: in the same minute, one that runs after another comes after it
    const depth = (name = '', seen: string[] = []): number => {
      const s = byName.get(name)
      return !s || seen.includes(name) ? 0 : Math.max(0, ...s.after.map(a => 1 + depth(a, [...seen, name])))
    }
    return out.sort((a, b) => a.sort - b.sort || depth(a.schedule?.name) - depth(b.schedule?.name))
  }, [runs.data, schedules.data, byName, night, today])

  // the keys of the place, while it is on screen and no dialog is over it
  const save = useRef<(() => void) | null>(null)
  const list = useRef<HTMLDivElement>(null)
  const detail = !!(route.run || route.schedule || route.task)
  useEffect(() => {
    if (!active) return
    const onKey = (e: KeyboardEvent) => {
      if (document.querySelector('.dialog-scrim')) return
      if (isSaveKey(e) && save.current && route.schedule && !route.edit) { e.preventDefault(); save.current(); return }
      if (e.ctrlKey || e.metaKey || e.altKey) return
      if (e.key === 'Escape') {
        if (isTyping(e.target)) (e.target as HTMLElement).blur()
        else if (detail && !route.edit) { e.preventDefault(); go({}) }
        return
      }
      if (isTyping(e.target) || !night || detail) return
      const days = runs.data?.nights.map(n => n.night).reverse() ?? []
      if (view === 'runs' && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
        const to = days[days.indexOf(night) + (e.key === 'ArrowLeft' ? -1 : 1)]
        if (to) { e.preventDefault(); go({ night: to }, true) }
      } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        const rows = [...(list.current?.querySelectorAll<HTMLElement>('.list-row') ?? [])]
        if (!rows.length) return
        e.preventDefault()
        const at = rows.indexOf(document.activeElement as HTMLElement)
        rows[Math.min(rows.length - 1, Math.max(0, at < 0 ? 0 : at + (e.key === 'ArrowUp' ? -1 : 1)))]!.focus()
      } else if (e.key.toLowerCase() === 'n') {
        e.preventDefault(); go({ schedule: 'new' })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const toList = (v: View) => { setView(v); if (detail) go({}) }
  const done = (runs.data?.runs ?? []).length
  const error = schedules.error ?? runs.error
  const s = route.schedule && route.schedule !== 'new' ? byName.get(route.schedule) ?? null : null
  const draft = (name: string) => {
    const k = route.schedule!
    return drafts.current.get(k) ?? draftOf(name === 'new' ? null : byName.get(name) ?? null)
  }
  // a page that failed to load: EmptyState with Retry; a reload that failed over what shows: a line
  const reloadAll = () => { schedules.reload(); runs.reload() }
  const page = !schedules.data && schedules.error ? (
      <div className="list-page"><PlaceHead level={1} title="Routines" /><Loaded page res={schedules} icon={History} /></div>
    )
    : !night || !schedules.data ? <LoadingRow label="Loading" />
    : route.schedule && route.edit && (route.schedule === 'new' || s) && route.edit in TEXTS ? (
      <ScheduleText key={`${route.schedule}:${route.edit}:${route.history ? 1 : 0}`} name={route.schedule} s={s} k={route.edit as TextKey}
                    history={!!route.history} night={night} draft={draft(route.schedule)} variables={schedules.data?.variables ?? []}
                    onDraft={d => drafts.current.set(route.schedule!, d)}
                    onSaved={() => schedules.reload()} go={go} />
    )
    : route.run ? <RunPage key={route.run} id={route.run} night={night} go={go} active={active} schedules={byName} />
    : route.schedule ? (
      route.schedule !== 'new' && !s ? <Missing title="Schedules" onBack={() => go({})} loaded={!!schedules.data} />
        : <SchedulePage key={route.schedule} name={route.schedule} s={s} all={schedules.data?.schedules ?? []} roles={schedules.data?.roles ?? []}
                        words={schedules.data}
                        night={night} draft={draft(route.schedule)} onDraft={d => drafts.current.set(route.schedule!, d)} save={save}
                        onSaved={name => { drafts.current.delete(route.schedule!); schedules.reload(); runs.reload(); go({ schedule: name }, true) }}
                        onDeleted={() => { drafts.current.delete(route.schedule!); schedules.reload(); setView('schedules'); go({}) }} go={go} />
    )
    : route.task ? <TaskPage key={route.task} id={route.task} night={night} tasks={tasks.data ?? []} active={active} go={go} onBack={() => { setView('triggers'); go({}) }} />
    : view === 'schedules' ? <SchedulesList list={list} night={night} all={schedules.data?.schedules ?? []} go={go} onChange={schedules.reload} />
    : view === 'triggers' ? <TriggersList list={list} triggers={triggers} tasks={tasks} night={night} go={go} />
    : (
      <RunsDay list={list} night={night} today={today ?? night} days={runs.data?.nights ?? []} nodes={nodes} runs={runs}
               all={schedules.data?.schedules ?? []} active={active} go={go}
               onEarlier={() => setUntil(shift(runs.data!.nights.at(-1)!.night, -1))}
               onLater={() => { const to = shift(runs.data!.nights[0]!.night, 14); setUntil(today && to >= today ? undefined : to) }} />
    )
  return (
    <Place title="Routines" action={<IconButton icon={Plus} label="New schedule" onClick={() => go({ schedule: 'new' })} />}
           side={<>
             <ListRow density="sm" icon={History} title="Runs" meta={done} selected={!detail && view === 'runs'} onClick={() => toList('runs')} />
             <ListRow density="sm" icon={CalendarClock} title="Schedules" meta={schedules.data?.schedules.length}
                      selected={!detail && view === 'schedules'} onClick={() => toList('schedules')} />
             <ListRow density="sm" icon={Zap} title="Triggers" meta={triggers.data?.length} selected={!detail && view === 'triggers'}
                      onClick={() => toList('triggers')} />
             {nodes.some(n => n.run) && <ListSection label={night ? dayShort(night) : 'Recent'} />}
             {nodes.filter(n => n.run).slice(0, 8).map(n => (
               <ListRow key={n.key} density="sm" title={`${n.at} ${n.label}`} meta={<StateWord tone={RUN_TONE[n.state]}>{STATE_WORD[n.state]}</StateWord>}
                        href={format({ section: 'routines', night, run: n.run!.id })} onClick={click(() => go({ run: n.run!.id }))}
                        selected={route.run === n.run!.id} />
             ))}
           </>}>
      {error && schedules.data && runs.data && <div className="list-page__note"><InlineError compact message={error} onRetry={reloadAll} /></div>}
      {page}
    </Place>
  )
}

function Missing({ title, onBack, loaded }: { title: string; onBack: () => void; loaded: boolean }) {
  return (
    <div className="routines-page">
      <PlaceHead level={1} title={title} crumbs={[{ label: 'Routines', onClick: e => { e.preventDefault(); onBack() } }]} />
      {loaded ? <EmptyState icon={SearchX} title="Not found">Nothing at this link.</EmptyState> : <LoadingRow />}
    </div>
  )
}

/* ── the runs of one day ── */

function RunsDay({ list, night, today, days, nodes, runs, all, active, go, onEarlier, onLater }: {
  list: React.RefObject<HTMLDivElement | null>
  night: string; today: string; days: Night[]; nodes: Node[]; runs: Res<Runs>
  all: Schedule[]; active: boolean
  go: (r: Open, replace?: boolean) => void
  onEarlier: () => void; onLater: () => void
}) {
  const ctx = useContextMenu()
  const [q, setQ] = useState('')
  const [show, setShow] = useState('all')
  const [of, setOf] = useState<string | null>(null)
  const act = useAction(async (fn: () => Promise<unknown>) => { await fn() })
  const OF: Record<string, State[]> = { all: ['planned', 'waiting', 'running', 'done', 'failed', 'skipped', 'stopped'],
                                        running: ['running'], waiting: ['waiting'], failed: ['failed'], done: ['done'] }
  const shown = nodes.filter(n => OF[show]!.includes(n.state) && (!of || n.schedule?.name === of)
    && (!q.trim() || n.label.toLowerCase().includes(q.trim().toLowerCase())))
  const ran = nodes.filter(n => n.run)
  const label = (name: string) => all.find(s => s.name === name)?.title ?? name
  const open = (n: Node) => (n.run ? go({ run: n.run.id }) : n.schedule && go({ schedule: n.schedule.name }))
  const menu = (n: Node): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => open(n) },
    ...(n.run && LIVE.includes(n.state) ? [{ label: 'Stop', icon: CircleStop, onSelect: () => void act.run(() => routinesApi.stop(n.run!.id)) }]
      : n.schedule ? [{ label: 'Run now', icon: Play, onSelect: () => void act.run(async () => { const r = await routinesApi.runNow(n.schedule!.name); go({ night: r.run.night, run: r.run.id }) }) }] : []),
    ...(n.schedule ? ['sep' as const, { label: 'Edit', icon: Pencil, detail: n.schedule.title, onSelect: () => go({ schedule: n.schedule!.name }) }] : []),
  ]
  // the strip: fourteen days, oldest left, a failed one in the error tone
  const newest = days[0]?.night ?? today
  const strip = useRef<HTMLDivElement>(null)
  useEffect(() => { strip.current?.querySelector('[aria-checked="true"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' }) }, [night, days])
  return (
    <div className="list-page" ref={list}>
      <PlaceHead level={1} title={nightWords(night)} count={ran.length || undefined}
                 search={{ value: q, onChange: setQ, placeholder: 'Search runs' }}
                 newLabel="New schedule" onNew={() => go({ schedule: 'new' })} />
      <div className="list-page__top">
        <div className="routines-days" ref={strip}>
          <IconButton icon={ChevronLeft} size="sm" label="Earlier" onClick={onEarlier} />
          <Segmented label="Day" className="routines-days__strip" value={night} onChange={d => go({ night: d }, true)}
                     options={[...days].reverse().map(n => {
                       const day = `${weekdayOf(n.night)} ${Number(n.night.slice(8))}`
                       // the error tone is not the only sign: its name says a run failed
                       return { value: n.night, title: n.failed ? `${nightWords(n.night)}, a run failed` : nightWords(n.night),
                                label: n.failed ? <StateWord tone="error">{day}</StateWord> : day }
                     })} />
          <IconButton icon={ChevronRight} size="sm" label="Later" disabled={newest >= today} onClick={onLater} />
        </div>
        <Toolbar state={{ label: 'State', value: show, onChange: setShow, options: [
                   { value: 'all', label: 'All', count: ran.length },
                   { value: 'running', label: STATE_WORD.running, count: ran.filter(n => n.state === 'running').length },
                   { value: 'waiting', label: STATE_WORD.waiting, count: ran.filter(n => n.state === 'waiting').length },
                   { value: 'failed', label: STATE_WORD.failed, count: ran.filter(n => n.state === 'failed').length },
                   { value: 'done', label: STATE_WORD.done, count: ran.filter(n => n.state === 'done').length }] }}
                 filters={[{ label: 'Schedule', icon: CalendarClock, picked: of ? label(of) : undefined, onClear: () => setOf(null),
                             items: all.map(s => ({ label: s.title, current: s.name === of, onSelect: () => setOf(s.name) })) }]} />
      </div>
      {act.error && <div className="list-page__note"><InlineError compact message={act.error} /></div>}
      <Loaded page res={runs} icon={History} label="Loading runs"
              empty={nodes.length === 0 ? { title: 'No runs', action: night === today ? <Button variant="primary" icon={Plus} onClick={() => go({ schedule: 'new' })}>New schedule</Button> : undefined }
                : shown.length === 0 && { icon: SearchX, title: 'No runs match', action: q && <Button onClick={() => setQ('')}>Clear search</Button> }}>
        {() => (
          <div className="list-page__rows">
            <VirtualList items={shown} estimate={68} getKey={n => n.key} label="Runs" resetOn={`${night}|${show}|${of}`}
                         renderRow={n => {
                           const i = shown.indexOf(n)
                           const prev = shown[i - 1]
                           const after = !!prev?.schedule && !!n.schedule && n.schedule.after.includes(prev.schedule.name)
                           const r = n.run
                           return (
                             <ListRow density="lg" title={n.label} tooltip={n.label} depth={after ? 1 : 0}
                                      muted={n.state === 'planned'}
                                      subtitle={r && LIVE.includes(r.state) && r.conversation_id
                                        ? <LiveStep run={r} turns={n.schedule?.reviewer ? n.schedule.turns : 0} active={active} />
                                        : r?.reason_why || (r?.error ?? (r?.result ? r.result.split('\n')[0] : n.schedule?.after.length
                                          ? `Runs after ${label(n.schedule.after[0]!)}` : n.schedule?.every))}
                                      cell={r?.asked ? <StateWord tone="warn">{ASKED_WORD}</StateWord> : <StateWord tone={RUN_TONE[n.state]}>{STATE_WORD[n.state]}</StateWord>}
                                      meta={n.at}
                                      href={n.run ? format({ section: 'routines', night, run: n.run.id })
                                        : n.schedule ? format({ section: 'routines', night, schedule: n.schedule.name }) : undefined}
                                      onClick={click(() => open(n))} onContextMenu={e => ctx.open(e, menu(n), n.label)}
                                      trailing={<MenuButton icon={MoreHorizontal} label="More" items={menu(n)} size="sm" />} />
                           )
                         }} />
          </div>
        )}
      </Loaded>
      {ctx.menu}
    </div>
  )
}

/** what holds a run's model call, in the server's words, else null */
const waitLine = (w: { noModel?: string | null; limited?: string | null; held?: string | null } | null) =>
  w?.noModel || w?.limited || w?.held || null

/* What it does right now in a live run (from its conversation's feed): what its model
   call waits for, else the server's reason while it waits (its work waits for a model, or
   it waits for its model to come back), else the tool it is in, and the reviewer's turn */
function LiveStep({ run, turns, active }: { run: Run; turns: number; active: boolean }) {
  const convo = useConversation(active ? run.conversation_id : null)
  const calls = Object.values(convo.toolCalls).flat()
  const tool = [...calls].reverse().find(c => c.state === 'running' || c.state === 'pending')
  const words = waitLine(convo.waiting) || run.reason_why || tool?.words.step || turnStep(convo.turn?.state) || STATE_WORD.running
  const said = (convo.messages as ChatMessage[]).filter(m => m.role === 'user' && m.intercom?.from?.startsWith('system:')
    && m.intercom.from !== 'system:schedules').length
  return <>{words}{turns > 0 && said > 0 && `, turn ${Math.min(said, turns)} of ${turns}`}</>
}

/* ── the schedules ── */

function SchedulesList({ list, night, all, go, onChange }: {
  list: React.RefObject<HTMLDivElement | null>; night: string; all: Schedule[]; go: (r: Open) => void; onChange: () => void
}) {
  const ctx = useContextMenu()
  const online = useOnline()
  const [q, setQ] = useState('')
  const [sort, setSort] = useState('Time')
  const [deleting, setDeleting] = useState<Schedule | null>(null)
  const act = useAction(async (fn: () => Promise<unknown>) => { await fn(); onChange() })
  const shown = (sort === 'Name' ? [...all].sort((a, b) => a.title.localeCompare(b.title)) : all)
    .filter(s => !q.trim() || s.title.toLowerCase().includes(q.trim().toLowerCase()))
  const label = (name: string) => all.find(s => s.name === name)?.title ?? name
  const menu = (s: Schedule): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => go({ schedule: s.name }) },
    { label: 'Run now', icon: Play, onSelect: () => void act.run(async () => { const r = await routinesApi.runNow(s.name); go({ night: r.run.night, run: r.run.id }) }) },
    'sep',
    { label: 'Delete', icon: Trash2, danger: true, detail: s.title, onSelect: () => setDeleting(s) },
  ]
  return (
    <div className="list-page" ref={list}>
      <PlaceHead level={1} title="Schedules" count={all.length || undefined} search={{ value: q, onChange: setQ, placeholder: 'Search schedules' }}
                 newLabel="New schedule" onNew={() => go({ schedule: 'new' })} />
      {all.length > 0 && (
        <div className="list-page__top">
          <Toolbar sort={{ label: 'Sort', value: sort, onChange: setSort, options: ['Time', 'Name'].map(v => ({ value: v, label: v })) }} />
        </div>
      )}
      {act.error && <div className="list-page__note"><InlineError compact message={act.error} /></div>}
      {all.length === 0 ? (
        <div className="list-page__empty">
          <EmptyState icon={CalendarClock} title="No schedules yet" action={<Button variant="primary" icon={Plus} onClick={() => go({ schedule: 'new' })}>New schedule</Button>} />
        </div>
      ) : shown.length === 0 ? (
        <div className="list-page__empty"><EmptyState icon={SearchX} title="No schedules match" action={<Button onClick={() => setQ('')}>Clear search</Button>} /></div>
      ) : (
        <div className="list-page__rows">
          <VirtualList items={shown} estimate={68} getKey={s => s.name} label="Schedules"
                       renderRow={s => (
                         <ListRow density="lg" icon={CalendarClock} title={s.title} tooltip={s.title} muted={!s.on}
                                  subtitle={s.after.length ? `${s.every}, runs after ${s.after.map(label).join(', ')}` : s.every}
                                  href={format({ section: 'routines', night, schedule: s.name })}
                                  onClick={click(() => go({ schedule: s.name }))} onContextMenu={e => ctx.open(e, menu(s), s.title)}
                                  press={ctx.press(() => menu(s), s.title)}
                                  trailingAlways
                                  trailing={
                                    <span className="row-actions">
                                      <Switch label={s.title} hideLabel checked={s.on} disabled={!online || act.busy}
                                              onChange={on => void act.run(() => routinesApi.put(s.name, bodyOf({ ...draftOf(s), on })))} />
                                      <MenuButton icon={MoreHorizontal} label={`More for ${s.title}`} items={menu(s)} size="sm" />
                                    </span>
                                  } />
                       )} />
        </div>
      )}
      {ctx.menu}
      {deleting && <DeleteSchedule s={deleting} onClose={() => setDeleting(null)} onDone={onChange} />}
    </div>
  )
}

function DeleteSchedule({ s, onClose, onDone }: { s: Schedule; onClose: () => void; onDone: () => void }) {
  return (
    <ConfirmDialog title={`Delete ${s.title}?`} danger confirmLabel="Delete" body="Its past runs are kept."
                   onClose={onClose}
                   onConfirm={async () => {
                     try { await routinesApi.remove(s.name) } catch (e) { return e instanceof Error ? e.message : String(e) }
                     onClose(); onDone()
                   }} />
  )
}

/* ── the triggers and what they started ── */

type TriggerRow = { t: Trigger } | { b: EventTask }
type Res<T> = ReturnType<typeof useResource<T>>

function TriggersList({ list, triggers, tasks, night, go }: {
  list: React.RefObject<HTMLDivElement | null>
  triggers: Res<Trigger[]>; tasks: Res<EventTask[]>; night: string; go: (r: Open) => void
}) {
  const online = useOnline()
  const ctx = useContextMenu()
  const [q, setQ] = useState('')
  const save = useAction(async (name: string, on: boolean) => {
    const t = await routinesApi.setTrigger(name, on)
    triggers.set(l => l?.map(x => (x.name === t.name ? t : x)))
  })
  const s = q.trim().toLowerCase()
  const ts = (triggers.data ?? []).filter(t => !s || t.label.toLowerCase().includes(s))
  const bs = (tasks.data ?? []).filter(b => !s || [b.title, b.trigger ?? '', b.last_line].some(x => x.toLowerCase().includes(s)))
  type R = { key: string; head: string; n: number; icon: typeof Zap } | { key: string; row: TriggerRow }
  // a head over each group only when both show: alone, the page's title names it
  const both = ts.length > 0 && bs.length > 0
  const rows: R[] = [
    ...(both ? [{ key: 'g:t', head: 'Triggers', n: ts.length, icon: Zap }] : []), ...ts.map(t => ({ key: t.name, row: { t } })),
    ...(both ? [{ key: 'g:r', head: 'Background tasks', n: bs.length, icon: History }] : []), ...bs.map(b => ({ key: b.id, row: { b } })),
  ]
  const nothing = triggers.data !== undefined && !triggers.data.length && !tasks.data?.length
  // a trigger by its label (its name only when it is gone); a run by its title, else its trigger's
  const byName = (name: string | null) => (name ? triggers.data?.find(t => t.name === name)?.label ?? name : '')
  const taskTitle = (b: EventTask) => (b.title && b.title !== b.trigger ? b.title : byName(b.trigger) || 'Run')
  const taskMenu = (id: string): MenuItem[] => [{ label: 'Open', icon: ArrowUpRight, onSelect: () => go({ task: id }) }]
  return (
    <div className="list-page" ref={list}>
      <PlaceHead level={1} title="Triggers" search={{ value: q, onChange: setQ, placeholder: 'Search triggers' }} />
      {(tasks.error ?? save.error) && (
        <div className="list-page__note"><InlineError compact message={(tasks.error ?? save.error)!} onRetry={save.error ? undefined : tasks.reload} /></div>
      )}
      <Loaded page res={triggers} icon={Zap} label="Loading triggers"
              empty={nothing ? { title: 'No triggers yet' }
                : rows.length === 0 && { icon: SearchX, title: 'No triggers match', action: <Button onClick={() => setQ('')}>Clear search</Button> }}>
        {() => (
          <div className="list-page__rows">
            <VirtualList items={rows} estimate={i => ('head' in rows[i]! ? 56 : 68)} getKey={r => r.key} label="Triggers"
                         renderRow={r => 'head' in r ? <GroupHead icon={r.icon} title={r.head} />
                           : 't' in r.row ? (
                             <ListRow density="lg" icon={Zap} title={r.row.t.label} subtitle={r.row.t.about} muted={!r.row.t.on}
                                      column={r.row.t.kind === 'watch' ? 'Folder' : 'Event'}
                                      meta={r.row.t.fired_at ? listTime(r.row.t.fired_at) : ''}
                                      trailingAlways trailing={<Switch label={r.row.t.label} hideLabel checked={r.row.t.on} disabled={!online || save.busy}
                                                                       title={online ? undefined : OFFLINE_TITLE} onChange={on => void save.run((r.row as { t: { name: string } }).t.name, on)} />} />
                           ) : (
                             <ListRow density="lg" title={taskTitle(r.row.b)}
                                      subtitle={r.row.b.last_line || undefined} column={byName(r.row.b.trigger) === taskTitle(r.row.b) ? '' : byName(r.row.b.trigger)}
                                      cell={r.row.b.asked ? <StateWord tone="warn">{ASKED_WORD}</StateWord>
                                        : r.row.b.working ? <StateWord tone="running">{STATE_WORD.running}</StateWord> : <StateWord tone="success">{STATE_WORD.done}</StateWord>}
                                      meta={listTime(r.row.b.updated_at)}
                                      muted={!r.row.b.working}
                                      href={format({ section: 'routines', night, task: r.row.b.id })}
                                      onClick={click(() => go({ task: (r.row as { b: { id: string } }).b.id }))}
                                      onContextMenu={e => ctx.open(e, taskMenu((r.row as { b: { id: string } }).b.id))}
                                      trailing={<MenuButton icon={MoreHorizontal} label="More" items={taskMenu(r.row.b.id)} size="sm" />} />
                           )} />
          </div>
        )}
      </Loaded>
      {ctx.menu}
    </div>
  )
}

/** what changes a row of what the triggers started */
const isTaskEvent = (ev: { type: string }) => ev.type === 'conversation.created' || ev.type === 'conversation.title'
  || ev.type === 'turn.state' || ev.type === 'conversation.deleted' || ev.type === 'conversation.restored'
  || ev.type === 'approvals.changed'

/* What a trigger started, or one of its sub-agents: its sub-agents, the talk and a box to
   write in */
function TaskPage({ id, night, tasks, active, go, onBack }: {
  id: string; night: string; tasks: { id: string; title: string; trigger: string | null }[]; active: boolean
  go: (r: Open) => void; onBack: () => void
}) {
  const agents = useAgents()
  const convo = useConversation(id)
  const t = tasks.find(b => b.id === id)
  const agent = agents.find(a => a.id === id)
  const kids = agents.filter(a => a.parent === id)
  const title = t ? t.title || t.trigger || 'Run' : agent?.name ?? convo.conversation?.title ?? ''
  const parent = agent && tasks.find(b => b.id === agent.parent)
  const crumbs: Crumb[] = [{ label: 'Triggers', onClick: e => { e.preventDefault(); onBack() } },
                           ...(parent ? [{ label: parent.title || parent.trigger || 'Run', href: format({ section: 'routines', night, task: parent.id }) }] : [])]
  return (
    <div className="routines-page">
      <PlaceHead level={1} crumbs={crumbs} title={title} titleAttr={title} actions={<PlanButton plan={convo.plan} />} />
      {kids.length > 0 && (
        <div className="routines-kids">
          {kids.map(a => (
            <ListRow key={a.id} icon={Bot} title={a.name} href={format({ section: 'routines', night, task: a.id })} onClick={click(() => go({ task: a.id }))}
                     subtitle={a.working ? a.step_words ?? STATE_WORD.running : a.report?.text.split('\n')[0] ?? STATE_WORD.done}
                     meta={a.working ? <StateWord tone="running">{STATE_WORD.running}</StateWord> : undefined} />
          ))}
        </div>
      )}
      <div className="routines-work"><Work cid={id} convo={convo} active={active} closed={false} /></div>
    </div>
  )
}

/* ── one run ── */

/** a run linked to this one (Retry of, Retried): its time, opening its page */
function RunLink({ run, night, go }: { run: { id: string; at: string }; night: string; go: (r: Open) => void }) {
  return <a className="routines-link" href={format({ section: 'routines', night, run: run.id })}
            onClick={click(() => go({ night, run: run.id }))}>{run.at}</a>
}

function RunPage({ id, night, go, active, schedules }: {
  id: string; night: string; go: (r: Open, replace?: boolean) => void; active: boolean; schedules: Map<string, Schedule>
}) {
  const online = useOnline()
  const nav = useNav()
  const res = useResource(signal => routinesApi.run(id, signal), [id], { events: ev => isRunEvent(ev) && ((ev as { run?: Run }).run)?.id === id })
  const open = (r: Run) => go({ night: r.night, run: r.id })
  const again = useAction(async (name: string) => { open((await routinesApi.runNow(name)).run) })
  const retry = useAction(async () => { open((await routinesApi.retry(id)).run) })
  const stop = useAction(async () => { await routinesApi.stop(id); res.reload() })
  const d = res.data
  const r = d?.run
  const s = r ? schedules.get(r.schedule) : undefined
  const cid = r?.conversation_id ?? null
  // what the run started: its sub-agents at every depth and the missions they started, the server's tree
  const tree = useResource(() => agentsApi.tree(cid!), [cid],
                           { enabled: !!cid, events: ev => ev.type === 'agent.state' || ev.type === 'mission.state' })
  const nodes = tree.data ? treeOrder(tree.data.top, tree.data.agents, tree.data.missions) : []
  const crumbs: Crumb[] = [{ label: dayShort(r?.night ?? night), href: format({ section: 'routines', night: r?.night ?? night }),
                             onClick: e => { e.preventDefault(); go({ night: r?.night ?? night }) } }]
  const error = again.error ?? retry.error ?? stop.error
  const offline = online ? undefined : OFFLINE_TITLE
  if (res.missing) return <Missing title="Runs" onBack={() => go({})} loaded />
  return (
    <div className="routines-page">
      <PlaceHead level={1} crumbs={crumbs} title={r?.label ?? ''} titleAttr={r?.label}
                 detail={r && <StateWord tone={RUN_TONE[r.state]}>{`${STATE_WORD[r.state]}, ${r.at}`}</StateWord>}
                 actions={r && (!FINISHED.includes(r.state)
                   ? <Button icon={CircleStop} loading={stop.busy} disabled={!online} onClick={() => void stop.run()}>Stop</Button>
                   : s && RETRIES.includes(r.state) && !r.retried
                     ? <Button icon={RotateCcw} loading={retry.busy} disabled={!online} title={offline} onClick={() => void retry.run()}>Retry</Button>
                     : s && <Button icon={Play} loading={again.busy} disabled={!online} title={offline} onClick={() => void again.run(s.name)}>Run now</Button>)}
                 more={r && s ? [{ label: 'Edit', icon: Pencil, detail: s.title, onSelect: () => go({ schedule: s.name }) }] : []} />
      {res.error && <div className="list-page__note"><InlineError message={res.error} onRetry={res.reload} /></div>}
      {error && <div className="list-page__note"><InlineError compact message={error} /></div>}
      {!d && !res.error && <LoadingRow />}
      {r && d && (
        <div className="page"><div className="page__col page__col--wide">
          <PageIndex sections={[{ id: 'r-res', label: 'Result' }, ...(s?.reviewer || cid ? [{ id: 'r-talk', label: 'Turns' }] : []),
                                ...(nodes.length ? [{ id: 'r-agents', label: 'Sub-agents', count: nodes.length }] : []),
                                { id: 'r-ask', label: 'Prompt' }]}>
            <Section id="r-res" title="Result">
              {r.reason && <p className="routines-prose routines-prose--quiet">{r.reason}</p>}
              {r.error && r.error !== r.reason && <InlineError compact message={r.error} />}
              {r.result ? <div className="prose"><Markdown text={r.result} /></div>
                : !r.reason && <p className="routines-prose routines-prose--quiet">{LIVE.includes(r.state) ? STATE_WORD[r.state] : 'No result'}</p>}
              <Facts grid items={[
                ...(d.note && r.state === 'done' ? [{ label: 'Note', value: (
                  <a className="routines-link" href={format({ section: 'notebook', note: d.note })}
                     onClick={e => { e.preventDefault(); nav.go({ section: 'notebook', note: d.note! }) }}>
                    <NotebookPen size={ICON.sm} aria-hidden />{d.note}
                  </a>
                ) }] : []),
                ...(r.again_of ? [{ label: 'Retry of', value: <RunLink run={r.again_of} night={r.night} go={go} /> }] : []),
                ...(r.retried ? [{ label: 'Retried', value: <RunLink run={r.retried} night={r.night} go={go} /> }] : []),
              ]} />
            </Section>
            {(s?.reviewer || cid) && (
              <Section id="r-talk" title="Turns">
                {cid ? <Talk key={cid} cid={cid} active={active} waits={r.reason} /> : <p className="routines-prose routines-prose--quiet">{STATE_WORD.waiting}</p>}
              </Section>
            )}
            {nodes.length > 0 && (
              <Section id="r-agents" title="Sub-agents" count={nodes.length}>
                <div className="routines-rows">{nodes.map(n => <TreeLine key={n.id} n={n} />)}</div>
              </Section>
            )}
            <Section id="r-ask" title="Prompt">
              {d.prompt === null ? <p className="routines-prose routines-prose--quiet">Its schedule was deleted.</p> : <>
                <div className="prose"><Markdown text={d.prompt} /></div>
                {d.reviewer_prompt && <><span className="routines-label">Reviewer prompt</span><div className="prose"><Markdown text={d.reviewer_prompt} /></div></>}
              </>}
            </Section>
          </PageIndex>
        </div></div>
      )}
    </div>
  )
}

/* The run's conversation as turns: the reviewer's lines and its own, what it was asked left
   out (it is the Prompt section); while it works, what its model call waits for, else the
   run's reason (`waits`: the server's, while its work waits for a model) */
function Talk({ cid, active, waits }: { cid: string; active: boolean; waits: string }) {
  const agentName = useName()
  const convo = useConversation(active ? cid : null)
  const items = (convo.messages as ChatMessage[]).filter(m => (m.role === 'user' || m.role === 'assistant') && m.intercom?.from !== 'system:schedules'
    && m.content.trim())
  const working = waitLine(convo.waiting) || waits || STATE_WORD.running
  if (!items.length) return convo.conversation ? <p className="routines-prose routines-prose--quiet">{convo.running ? working : 'No turns'}</p> : <LoadingRow />
  return (
    <div className="routines-talk">
      {items.map(m => {
        const name = m.role === 'user' ? m.intercom?.from_name ?? 'Reviewer' : agentName
        return (
          <Message key={m.id} name={name} face={m.role === 'user' ? <Avatar name={name} size={28} /> : <Portrait size="sm" />}>
            <div className="prose"><Markdown text={m.content} /></div>
          </Message>
        )
      })}
      {convo.running && <p className="routines-prose routines-prose--quiet">{working}</p>}
    </div>
  )
}

/* ── a schedule ── */

/** a schedule's key from its name, for a new one: lowercase words joined by '-' */
const slug = (label: string) =>
  label.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32).replace(/^[^a-z]+/, '')

/** the schedules that run after `name`, directly or through others: it must not run after them */
function followers(all: Schedule[], name: string | null): Map<string, string> {
  const out = new Map<string, string>()
  if (!name) return out
  const by = new Map(all.map(s => [s.name, s]))
  const walk = (n: string, seen: Set<string>): boolean => {
    if (n === name) return true
    if (seen.has(n)) return false
    seen.add(n)
    return (by.get(n)?.after ?? []).some(a => walk(a, seen))
  }
  for (const s of all) if (s.name !== name && walk(s.name, new Set())) out.set(s.name, `${s.title} already runs after this one`)
  return out
}

function SchedulePage({ name, s, all, roles, words, night, draft, onDraft, save: saveKey, onSaved, onDeleted, go }: {
  name: string; s: Schedule | null; all: Schedule[]; roles: { name: string; label: string }[]; words?: ScheduleWords; night: string
  draft: Draft; onDraft: (d: Draft) => void; save: React.MutableRefObject<(() => void) | null>
  onSaved: (name: string) => void; onDeleted: () => void; go: (r: Open) => void
}) {
  const online = useOnline()
  const fresh = name === 'new'
  const [d, setD] = useState(draft)
  const [deleting, setDeleting] = useState(false)
  useEffect(() => { onDraft(d) }, [d]) // eslint-disable-line react-hooks/exhaustive-deps
  const put = (p: Partial<Draft>) => setD(o => ({ ...o, ...p }))
  const id = fresh ? d.key : name
  const body = bodyOf(d)
  const unchanged = !!s && JSON.stringify(body) === JSON.stringify(bodyOf(draftOf(s)))
  const preview = useResource(signal => routinesApi.preview(night, d.prompt, signal), [night, d.prompt], { enabled: !!d.prompt.trim(), debounce: 400 })
  const notePath = useResource(signal => routinesApi.preview(night, d.note, signal), [night, d.note], { enabled: !!d.note.trim(), debounce: 400 })
  const [field, setField] = useState<string | null>(null)
  const save = useAction(async () => {
    setField(null)
    try {
      const out = await routinesApi.put(id, body)
      onSaved(out.name)
    } catch (e) {
      const f = (e as { detail?: Record<string, string> }).detail?.field
      setField(f ? f.split('.').pop()! : null)
      throw e
    }
  })
  const runNow = useAction(async () => { const r = await routinesApi.runNow(name); go({ night: r.run.night, run: r.run.id }) })
  const ready = online && !!d.label.trim() && !!d.prompt.trim() && !!id && !unchanged
  saveKey.current = () => { if (ready) void save.run() }
  useEffect(() => () => { saveKey.current = null }, [saveKey])
  const err = (f: string) => (save.error && field === f ? save.error : undefined)
  const others = all.filter(o => o.name !== name)
  const cycle = followers(all, fresh ? null : name)
  const edit = (k: TextKey) => go({ schedule: name, edit: k })
  const crumbs: Crumb[] = [{ label: 'Schedules', onClick: e => { e.preventDefault(); onDeleted() } }]
  const runs = useResource(signal => routinesApi.runsOf(name, signal), [name], { enabled: !fresh, events: isRunEvent })
  const theirs = [...(runs.data ?? [])].reverse().slice(0, 20)
  return (
    <div className="routines-page">
      <PlaceHead level={1} crumbs={crumbs} title={fresh ? 'New schedule' : s!.title} titleAttr={s?.title}
                 actions={<>
                   {!fresh && <Button icon={Play} loading={runNow.busy} disabled={!online} onClick={() => void runNow.run()}>Run now</Button>}
                   <Button variant="primary" loading={save.busy} disabled={!ready} title={online ? undefined : OFFLINE_TITLE}
                           onClick={() => void save.run()}>Save</Button>
                 </>}
                 more={fresh ? [] : [{ label: 'Delete', icon: Trash2, danger: true, onSelect: () => setDeleting(true) }]} />
      {((save.error && !field) || runNow.error) && <div className="list-page__note"><InlineError compact message={(field ? null : save.error) ?? runNow.error!} /></div>}
      <div className="page"><div className="page__col page__col--wide">
        <PageIndex sections={[{ id: 's-when', label: 'Schedule' }, ...(roles.length > 1 ? [{ id: 's-who', label: 'Model' }] : []), { id: 's-prompt', label: 'Prompt' },
                              { id: 's-rev', label: 'Reviewer' }, { id: 's-limits', label: 'Limits' },
                              ...(fresh ? [] : [{ id: 's-runs', label: 'Runs', count: theirs.length }])]}>
          <Section id="s-when" title="Schedule" action={<Switch label="On" checked={d.on} onChange={on => put({ on })} />}>
            <div className="routines-form">
              {/* a new schedule's key comes from its name */}
              <Field label="Name" error={err('label') ?? err('name')}>
                <Input value={d.label} onChange={e => put({ label: e.target.value, ...(fresh ? { key: slug(e.target.value) } : {}) })} />
              </Field>
              <Field label="Schedule" group error={err('time') ?? err('cron')}
                     hint={d.when === 'cron' && s && d.cron.trim() === s.cron && s.every !== s.cron ? s.every : undefined}>
                <div className="routines-when">
                  <Segmented label="Schedule" value={d.when} onChange={when => put({ when })}
                             options={[{ value: 'daily', label: 'Daily' }, { value: 'cron', label: 'Cron' }]} />
                  {d.when === 'daily'
                    ? <Input mono className="routines-when__value" value={d.time} aria-label="Time" inputMode="numeric" maxLength={5}
                             onChange={e => put({ time: e.target.value })} />
                    : <Input mono className="routines-when__value" value={d.cron} aria-label="Cron" onChange={e => put({ cron: e.target.value })} />}
                </div>
              </Field>
              {others.length > 0 && (
                <Field label="Runs after" group error={err('after')}>
                  <div className="routines-two">
                    {others.map(o => {
                      const on = d.after.includes(o.name)
                      return <Checkbox key={o.name} label={o.title} checked={on} disabled={!!cycle.get(o.name) && !on}
                                       onChange={v => put({ after: v ? [...d.after, o.name] : d.after.filter(n => n !== o.name) })} />
                    })}
                  </div>
                </Field>
              )}
            </div>
          </Section>
          {roles.length > 1 && (
            <Section id="s-who" title="Model">
              <Field label="Model" group error={err('role')}>
                <Choice label="Model" value={d.role} onChange={role => put({ role })} options={roles.map(r => ({ value: r.name, label: r.label }))} />
              </Field>
            </Section>
          )}
          <Section id="s-prompt" title="Prompt" action={<Button icon={Pencil} onClick={() => edit('prompt')}>Edit</Button>}>
            {err('prompt') && <InlineError compact message={err('prompt')!} />}
            {d.prompt.trim() ? <p className="routines-prose routines-mono">{d.prompt}</p>
              : <EmptyState compact title="No prompt yet" action={<Button icon={Pencil} onClick={() => edit('prompt')}>Edit</Button>} />}
            {d.prompt.trim() && (
              <Field label="Preview" group>
                {preview.error ? <InlineError compact message={preview.error} /> : <div className="prose routines-preview"><Markdown text={preview.data ?? ''} /></div>}
              </Field>
            )}
            <Field label="Note" error={err('note')}>
              <Input mono value={d.note} placeholder="diary/{{day_before}}.md" onChange={e => put({ note: e.target.value })} />
            </Field>
            {d.note.trim() && notePath.data && notePath.data !== d.note.trim() && <p className="routines-prose routines-prose--quiet routines-mono">{notePath.data}</p>}
          </Section>
          <Section id="s-rev" title="Reviewer" action={<Switch label="Reviewer" hideLabel checked={d.has_reviewer} onChange={has_reviewer => put({ has_reviewer })} />}>
            {d.has_reviewer ? (
              <div className="routines-form">
                <Field label="Reviewer prompt" group error={err('reviewer_prompt') ?? err('reviewer')}>
                  {d.reviewer_prompt.trim() ? <p className="routines-prose routines-mono">{d.reviewer_prompt}</p> : null}
                  <span><Button icon={Pencil} onClick={() => edit('reviewer')}>Edit</Button></span>
                </Field>
                <NumberField label="Turns" value={d.turns} min={1} max={30} onChange={turns => put({ turns })} invalid={field === 'turns'} />
              </div>
            ) : null}
          </Section>
          <Section id="s-limits" title="Limits">
            <Field label="When a run fails" help={words?.help.on_fail} group error={err('on_fail')}>
              <Segmented label="When a run fails" value={d.on_fail} onChange={on_fail => put({ on_fail })}
                         options={words?.choices.on_fail ?? []} />
            </Field>
            <Switch checked={d.idle_only} onChange={idle_only => put({ idle_only })} label="Only when the app is idle" help={words?.help.idle_only} />
            <NumberField label="Time limit" unit="min" value={d.wall} min={1} max={1440} onChange={wall => put({ wall })} invalid={field === 'wall_minutes'}
                         help={words?.help.wall_minutes} />
          </Section>
          {!fresh && (
            <Section id="s-runs" title="Runs" count={theirs.length}>
              {theirs.length ? (
                <div className="routines-rows">
                  {theirs.map(r => (
                    <ListRow key={r.id} density="lg" title={dayShort(r.night)} subtitle={r.reason_why || r.error || undefined}
                             cell={<StateWord tone={RUN_TONE[r.state]}>{STATE_WORD[r.state]}</StateWord>} meta={r.at}
                             href={format({ section: 'routines', night: r.night, run: r.id })} onClick={click(() => go({ night: r.night, run: r.id }))} />
                  ))}
                </div>
              ) : <p className="routines-prose routines-prose--quiet">No runs</p>}
            </Section>
          )}
        </PageIndex>
      </div></div>
      {deleting && s && <DeleteSchedule s={s} onClose={() => setDeleting(false)} onDone={onDeleted} />}
    </div>
  )
}

/* A schedule's prompt or reviewer prompt in the large editor, or its History. An existing
   schedule saves it at once (its History keeps each version); a new one keeps it until the
   schedule is saved. */
function ScheduleText({ name, s, k, history, night, draft, variables, onDraft, onSaved, go }: {
  name: string; s: Schedule | null; k: TextKey; history: boolean; night: string; draft: Draft; variables: string[]
  onDraft: (d: Draft) => void; onSaved: () => void; go: (r: Open) => void
}) {
  const t = TEXTS[k]
  const value = draft[t.field]
  const saved = s ? (k === 'prompt' ? s.prompt : s.reviewer_prompt) : ''
  const of = s ? `routine:${s.name}:${k}` : undefined
  const crumbs: Crumb[] = [{ label: 'Schedules', href: format({ section: 'routines', night }) },
                           { label: s?.title ?? 'New schedule', href: format({ section: 'routines', night, schedule: name }),
                             onClick: e => { e.preventDefault(); go({ schedule: name }) } }]
  if (history && of) {
    return <TextHistory crumbs={[...crumbs, { label: t.label, href: format({ section: 'routines', night, schedule: name, edit: k }) }]}
                        source={textHistory(of)} current={value}
                        onRestore={text => { onDraft({ ...draft, [t.field]: text }); go({ schedule: name, edit: k }) }} />
  }
  const words: MenuItem[] = variables.filter(v => v !== `result:${name}`).map(v => ({
    label: `{{${v}}}`, onSelect: () => copied(`{{${v}}}`),
  }))
  return (
    <LongEditor crumbs={crumbs} title={t.label} of={of} history={of && format({ section: 'routines', night, schedule: name, edit: k, history: true })}
                texts={[{ key: k, label: t.label, value }]} dirty={value !== saved}
                top={<MenuButton icon={Braces} text="Variables" label="Variables" items={words} size="sm" />}
                hint="Make it shorter, or add a question"
                onCancel={() => go({ schedule: name })}
                onSave={async vals => {
                  const next = { ...draft, [t.field]: vals[k] ?? '', ...(k === 'reviewer' ? { has_reviewer: true } : {}) }
                  if (s) {
                    // only this text changes: the rest of the page's edits wait for its Save
                    const base = draftOf(s)
                    await routinesApi.put(s.name, bodyOf({ ...base, [t.field]: vals[k] ?? '', ...(k === 'reviewer' ? { has_reviewer: true } : {}) }))
                    onDraft({ ...next })
                    onSaved()
                  } else onDraft(next)
                  go({ schedule: name })
                }} />
  )
}
