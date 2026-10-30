/* Routines: the schedules (jobs as data, GET/PUT/DELETE /schedules, POST run, POST
   preview) and their runs (GET /runs, GET /runs/{id}, POST stop; `run.state` on the
   feed). Every state, reason and time is the server's; the page says them as they are.
   docs/ARCHITECTURE.md, Schedules. */
import { LOCALE } from './format'
import { del, json, post, request } from './api'
import { useResource } from './useResource'

export type Schedule = {
  name: string
  /** as written, {{username}} and {{name}} kept: what the editor edits and PUTs back */
  label: string
  /** one sentence under its name in the onboarding, '' for none */
  summary: string
  /** the label filled, what the page shows */
  title: string
  cron: string
  /** "02:00" for a daily one, else null */
  time: string | null
  /** when it runs, in words (the server's): "Daily at 02:00", "Sundays at 07:00", "Every 2 hours from 10:00 to 22:00"; else the cron line */
  every: string
  prompt: string
  /** the schedules whose runs of the same day it runs after */
  after: string[]
  /** the reviewer prompt's file, '' for none */
  partner: string
  partner_prompt: string
  rounds: number
  /** a notebook path with variables, '' for none */
  note: string
  role: string
  /** it starts only while nobody is talking with it */
  idle_only: boolean
  /** the most minutes a run may take, null for no limit */
  wall_minutes: number | null
  /** its dates are the calendar date of its slot, not the night's */
  daytime: boolean
  on: boolean
  /** why it is off for good ("Schedule was deleted."), else null */
  gone: string | null
  /** the next slot (UTC), null while off */
  next: string | null
  /** the night of the next slot, and its time of day ("02:00") */
  next_night: string | null
  next_at: string | null
}

export type ScheduleBody = {
  label: string
  summary: string
  time?: string
  cron?: string
  prompt: string
  after: string[]
  partner: string
  partner_prompt: string
  rounds: number
  note: string
  role: string
  idle_only: boolean
  wall_minutes: number | null
  daytime: boolean
  on: boolean
}

export type RunState = 'waiting' | 'running' | 'done' | 'failed' | 'skipped' | 'stopped'

export type Run = {
  id: string
  schedule: string
  label: string
  /** in the list: an approval waits in it or in a sub-agent of it */
  asked?: boolean
  /** the planned time (UTC) */
  slot: string
  /** the slot's time of day in the app's zone */
  at: string
  night: string
  state: RunState
  /** why it waits, in words; '' when nothing holds it */
  reason: string
  conversation_id: string | null
  result: string | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export type Night = { night: string; runs: { schedule: string; state: RunState }[]; late: boolean }

export type Runs = { runs: Run[]; nights: Night[]; tonight: string; attention: 'running' | 'failed' | null }

type RunDetail = { run: Run; prompt: string | null; partner_prompt: string | null; note: string | null }

export type Trigger = { name: string; label: string; about: string; skill: string; kind: 'event' | 'watch'; on: boolean; fired_at: string | null }

const enc = encodeURIComponent

export const routinesApi = {
  schedules: (signal?: AbortSignal) =>
    request<{ schedules: Schedule[]; roles: { name: string; label: string }[]; variables: string[]; tonight: string }>('/schedules', { signal }),
  put: (name: string, body: ScheduleBody) =>
    request<Schedule & { applied: boolean }>(`/schedules/${enc(name)}`, json('PUT', body)),
  remove: (name: string) => del<{ deleted: string }>(`/schedules/${enc(name)}`),
  runNow: (name: string) => post<{ run: Run }>(`/schedules/${enc(name)}/run`),
  preview: (night: string, text: string, signal?: AbortSignal) =>
    request<{ text: string }>('/schedules/preview', { ...json('POST', { night, text }), signal }).then(r => r.text),
  /** the runs of `night` (default tonight), the 14 nights up to `until` (default tonight) */
  runs: (night?: string, signal?: AbortSignal, until?: string) => {
    const q = new URLSearchParams({ ...(night && { night }), ...(until && { until }) }).toString()
    return request<Runs>(`/runs${q ? `?${q}` : ''}`, { signal })
  },
  /** one schedule's newest runs, oldest first */
  runsOf: (schedule: string, signal?: AbortSignal) =>
    request<Runs>(`/runs?schedule=${enc(schedule)}`, { signal }).then(r => r.runs),
  run: (id: string, signal?: AbortSignal) => request<RunDetail>(`/runs/${enc(id)}`, { signal }),
  stop: (id: string) => post<{ run: Run }>(`/runs/${enc(id)}/stop`),
  /** what the events started: their conversations (GET /background), newest first */
  tasks: (signal?: AbortSignal) => request<{ background: EventTask[] }>('/background-conversations', { signal }).then(r => r.background),
  triggers: (signal?: AbortSignal) => request<{ triggers: Trigger[] }>('/triggers', { signal }).then(r => r.triggers),
  setTrigger: (name: string, on: boolean) =>
    request<Trigger & { applied: boolean }>(`/triggers/${enc(name)}`, json('PUT', { on })),
}

/** a server event that changes a run */
export const isRunEvent = (ev: { type: string }) => ev.type === 'run.state' || ev.type === 'approvals.changed'

/** a row an approval waits in */
export const ASKED_WORD = 'Waiting for you'

/** the one word for a run's state */
export const STATE_WORD: Record<RunState | 'planned', string> = {
  waiting: 'Waiting', running: 'Running', done: 'Done', failed: 'Failed', skipped: 'Skipped', stopped: 'Stopped',
  planned: 'Planned',
}

/** the dot of a run's state */
export const RUN_TONE: Record<RunState | 'planned', 'ok' | 'error' | 'running' | 'muted' | 'off'> = {
  waiting: 'muted', running: 'running', done: 'ok', failed: 'error', skipped: 'off', stopped: 'off', planned: 'off',
}

/** a run that is over: Retry or Run now instead of Stop */
export const FINISHED: (RunState | 'planned')[] = ['done', 'failed', 'skipped', 'stopped']

/** the rail's dot for Routines: a run running, or one that failed, else null */
export function useRoutinesAttention(): 'running' | 'failed' | null {
  const res = useResource(signal => routinesApi.runs(undefined, signal), [], { events: isRunEvent })
  return res.data?.attention ?? null
}

/** a day's date as a heading: "Saturday 10 October" */
export const nightWords = (night: string) =>
  new Date(`${night}T12:00:00`).toLocaleDateString(LOCALE, { weekday: 'long', day: 'numeric', month: 'long' })

/** GET /background: the conversation a fired event started (the runs' are under their days) */
export type EventTask = {
  id: string
  title: string
  kind: 'background'
  /** the event's name */
  trigger: string | null
  working: boolean
  last_line: string
  updated_at: string
  /** an approval waits in it or in a sub-agent of it */
  asked: boolean
}
