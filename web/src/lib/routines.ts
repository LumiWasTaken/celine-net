/* Routines: the schedules (jobs as data, GET/PUT/DELETE /schedules, POST run, POST
   preview) and their runs (GET /runs, GET /runs/{id}, POST stop, POST retry; `run.state`
   on the feed). Every state, reason and time is the server's; the page says them as they are.
   docs/ARCHITECTURE.md, Schedules. */
import { LOCALE } from './format'
import { del, json, post, request } from './api'
import type { Said } from './states'

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
  reviewer: string
  reviewer_prompt: string
  turns: number
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
  /** When a run fails: retry runs a failed run again up to three times for the same day, skip ends it skipped and quiet */
  on_fail: OnFail
  /** the next slot (UTC), null while off */
  next: string | null
  /** the night of the next slot, and its time of day ("02:00") */
  next_night: string | null
  next_at: string | null
}

export type OnFail = 'retry' | 'skip'

export type ScheduleBody = {
  label: string
  summary: string
  time?: string
  cron?: string
  prompt: string
  after: string[]
  reviewer: string
  reviewer_prompt: string
  turns: number
  note: string
  role: string
  idle_only: boolean
  wall_minutes: number | null
  daytime: boolean
  on: boolean
  /** left out, the stored one stays */
  on_fail?: OnFail
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
  /** that state as the pages say it, the server's */
  words: string
  tone: Said['tone']
  /** why it waits or why it was skipped, in words; '' when nothing holds it */
  reason: string
  /** the reason without the "Waiting" its state word says, for a row under that word */
  reason_why: string
  conversation_id: string | null
  result: string | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  /** the run it runs again (Retry) and its time of day, else null */
  again_of: { id: string; at: string } | null
  /** the run that runs it again and its time of day, else null */
  retried: { id: string; at: string } | null
}

/** one of the fourteen days: failed when a schedule's newest try that day failed or ended
    with an error, waiting while a run of it waits for its model */
export type Night = { night: string; failed: boolean; waiting: boolean }

/** `states`: every state of a run as the server says it; planned: a slot with no run yet */
export type Runs = { runs: Run[]; nights: Night[]; tonight: string; states: Record<RunState | 'planned', Said> }

/** GET /schedules: a field's choices and its "?" text, the server's */
export type ScheduleWords = { choices: { on_fail: { value: OnFail; label: string }[] }; help: Record<'on_fail' | 'idle_only' | 'reviewer' | 'wall_minutes', string> }

/** a run and what it was asked; `facts` its times, who started it and its models, in the server's words */
type RunDetail = { run: Run; prompt: string | null; reviewer_prompt: string | null; note: string | null; facts: { label: string; value: string }[] }

export type Trigger = { name: string; label: string; about: string; skill: string; kind: 'event' | 'watch'; on: boolean; fired_at: string | null }

const enc = encodeURIComponent

export const routinesApi = {
  schedules: (signal?: AbortSignal) =>
    request<{ schedules: Schedule[]; roles: { name: string; label: string }[]; variables: string[]; tonight: string } & ScheduleWords>('/schedules', { signal }),
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
  /** a failed, skipped or stopped run runs again now, for its night */
  retry: (id: string) => post<{ run: Run }>(`/runs/${enc(id)}/retry`),
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

/** a run that is over: Retry or Run now instead of Stop */
export const FINISHED: (RunState | 'planned')[] = ['done', 'failed', 'skipped', 'stopped']

/** a run that ended unfinished: Retry runs it again, once */
export const RETRIES: RunState[] = ['failed', 'skipped', 'stopped']

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
