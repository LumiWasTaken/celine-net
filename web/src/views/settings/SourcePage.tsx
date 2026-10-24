/* One page per source, built for that source, always in the same parts
   (12-sources-settings §1.2):

     1. State     a dot and a word in the head; the sentence as one calm line when it
                  is not connected
     2. To fix    only real problems (a lost login, a task or an automatic task that
                  failed), each with the one button that fixes it
     3. Included  what is in the archive from here

   WhatsApp and Discord with a connection (a block in config/mcp.toml) have their own
   pages; any other chat source, or one without a connection, gets the page below. */
import type { ReactNode } from 'react'
import { TriangleAlert } from 'lucide-react'
import { kbApi, type Job, type Kb, type Task } from '../../lib/kb'
import { count, monthYear, sameDay, stamp } from '../../lib/format'
import { failedTasks, type Source } from '../../lib/sources'
import type { Level } from '../../lib/settingsApi'
import { useResource } from '../../lib/useResource'
import { Button, Loaded, Notice, Section, type NoticeAction } from '../../ui'
import { Page, StatusDot, TaskLine } from './parts'
import { WhatsAppSource } from './WhatsAppSource'
import { DiscordSource } from './DiscordSource'
import './settings.css'

export function SourcePage({ source, active }: { source: Source; active: boolean }) {
  const links = useResource(() => kbApi.connectors().then(r => r.connectors), [])
  if (!links.data) {
    return (
      <Page title={source.title}>
        {<Loaded res={links} />}
      </Page>
    )
  }
  const linked = links.data.some(c => c.kb === source.id)
  if (linked && source.platform === 'whatsapp') return <WhatsAppSource source={source} active={active} />
  if (linked && source.platform === 'discord') return <DiscordSource source={source} active={active} />
  return <GenericSource source={source} />
}

/* ── shared by every source page ─────────────────────────────────────────── */

/** the task list, kept fresh by kb.task events; finished tasks leave by themselves */
export function useTasks() {
  return useResource(() => kbApi.tasks().then(r => r.tasks), [],
                     { events: ev => ev.type === 'kb.task' || ev.type === 'kb.task.removed' })
}

/** the automatic tasks' last outcomes, kept fresh by kb.job events */
export function useSchedules() {
  return useResource(() => kbApi.tasks().then(r => r.jobs), [], { events: ev => ev.type === 'kb.job' })
}

/** an automatic task of this source whose last run failed or found its server
    unreachable: a problem for To fix, with no button (the schedule is the retry).
    `skip`: kinds the page already shows. */
export function scheduleProblems(kb: string, schedules: Job[] | undefined, skip: string[] = []): ReactNode[] {
  return (schedules ?? []).filter(j => j.kb === kb && (j.state === 'failed' || j.state === 'unreachable') && !skip.includes(j.kind))
    .map(j => <Problem key={`job:${j.kind}`} sentence={`${j.label} failed at ${stamp(j.since)}`} detail={j.error} />)
}

/** the state sentence, one calm line, when it says more than the word in the head */
export function StateNotice({ level, sentence }: { level: Level; sentence: string }) {
  return level === 'ok' ? null : <Notice>{sentence}</Notice>
}

/** 1. the state sentence with its one action (Sync now, Check now) */
export function StateLine({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="src-state">
      <p className="src-state__text">{children}</p>
      {action && <div className="src-state__action">{action}</div>}
    </div>
  )
}

/** 2. a problem: what is wrong in one sentence, why in one more, and one button.
    `calm`: the same in plain lines, for a first setup that is not a problem (never connected) */
export function Problem({ sentence, detail, action, children, calm = false }: {
  sentence?: ReactNode
  detail?: ReactNode
  action?: NoticeAction
  children?: ReactNode
  calm?: boolean
}) {
  if (calm) {
    return (
      <div className="stack">
        {sentence && <p className="src-line">{sentence}</p>}
        {detail && <p className="src-line">{detail}</p>}
        {children}
        {action && <div><Button icon={action.icon} loading={action.busy} onClick={action.onClick}>{action.label}</Button></div>}
      </div>
    )
  }
  return (
    <Notice tone="warn" title={sentence} action={action} icon={TriangleAlert}>
      {detail && <div>{detail}</div>}
      {children}
    </Notice>
  )
}

export function ToFix({ children }: { children: ReactNode }) {
  const items = (Array.isArray(children) ? children : [children]).flat(3).filter(Boolean)
  if (items.length === 0) return null
  return <Section title="To fix" count={items.length}><div className="stack">{items}</div></Section>
}

/** failed tasks of this source (automatic ones appear only when they fail), each with Retry */
export function failedLines(kb: string, tasks: Task[], onChanged: () => void): ReactNode[] {
  return failedTasks(kb, tasks).map(t => <TaskLine key={t.id} task={t} onChanged={onChanged} />)
}

/** 3. "182 chats, 264,033 messages, 356 people, Mar 2019 to today" */
export function includedSentence(kb: Kb): string {
  if (!(kb.messages ?? 0)) return 'No messages yet.'
  const range = kb.first_ts && kb.last_ts
    ? `, ${monthYear(kb.first_ts)} to ${sameDay(kb.last_ts, Date.now()) ? 'today' : monthYear(kb.last_ts)}` : ''
  return `${count(kb.chats, 'chat')}, ${count(kb.messages, 'message')}, ${count(kb.people, 'person', 'people')}${range}`
}

/* ── any other chat source: its state, tasks and what is in the archive ────── */
function GenericSource({ source }: { source: Source }) {
  const kb = source.kb
  const tasks = useTasks()
  const schedules = useSchedules()
  const list = tasks.data ?? []
  return (
    <Page title={source.title} detail={<StatusDot level={source.level} word />}>
      <StateNotice level={source.level} sentence={source.sentence} />
      <ToFix>{[...scheduleProblems(kb.id, schedules.data), ...failedLines(kb.id, list, tasks.reload)]}</ToFix>
      <Section title="Included">
        <p className="src-line">{includedSentence(kb)}</p>
      </Section>
    </Page>
  )
}
