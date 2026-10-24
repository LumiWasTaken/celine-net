/* Missions: the api clients (POST /missions, GET, PATCH, POST /stop; a mission is a
   conversation, so it is archived, goes to the Trash, comes back and is deleted
   permanently as one), the row types and the words a mission's state is said in
   (docs/ARCHITECTURE.md). A mission is its instructions: it writes the plan, works with its
   sub-agents and delivers an artifact. */
import { del, json, post, request, type PlanStep } from './api'
import { useResource } from './useResource'

export type MissionStatus = 'active' | 'needs_you' | 'done' | 'stopped'

/** GET /missions: each row */
export type Mission = {
  conversation_id: string
  name: string
  /** its default terminal's name in this conversation */
  terminal: string
  brief: string
  /** its plan, markdown; '' until it wrote it */
  plan: string
  /** what it delivered: `artifact:<id>` (a page) or `documents:<path>`; null before */
  artifact: string | null
  report_to: string | null
  /** the time limit as a time, null without one */
  until: string | null
  status: MissionStatus
  note: string | null
  /** its plan's steps, each with the model it runs on (null: the mission's own) */
  steps: MissionStep[]
  /** the number of the step it is at, 1 for the first; null before it starts one */
  step: number | null
  /** notes and tools loaded by need (the automatic context switch) */
  auto_context: boolean
  trashed_at?: string | null
  archived_at?: string | null
  created_at: string
  updated_at: string
}

export type MissionStep = PlanStep

/** GET /missions: the page's numbers, the server's (each status of the list, all of it,
    the Archive and the Trash) */
type MissionCounts = Record<MissionStatus | 'all' | 'archived' | 'trashed', number>

export const missionsApi = {
  /** the brief; with no name it names it; `model`: its chat model, else the default */
  start: (brief: string, name?: string, model?: string | null) =>
    request<{ conversation_id: string }>('/missions', json('POST', { brief, ...(name ? { name } : {}), ...(model ? { model } : {}) })),
  /** the archived or the trashed ones instead */
  list: (which?: 'archived' | 'trashed') =>
    request<{ missions: Mission[]; counts: MissionCounts }>(`/missions${which ? `?${which}=1` : ''}`),
  /** a new name, or the brief changed (both reach it as a message) */
  edit: (cid: string, body: { name?: string; brief?: string }) =>
    request<{ ok: boolean }>(`/missions/${cid}`, json('PATCH', body)),
  stop: (cid: string) => post<{ ok: boolean }>(`/missions/${cid}/stop`),
  setSteps: (cid: string, steps: MissionStep[]) =>
    request<Mission>(`/missions/${cid}`, json('PATCH', { steps })),
  /** into the Trash with its sub-agents, every message kept */
  trash: (cid: string) => del<{ ok: boolean }>(`/conversations/${cid}`),
  restore: (cid: string) => post<{ ok: boolean }>(`/conversations/${cid}/restore`),
  /** a trashed one for good, with its sub-agents */
  remove: (cid: string) => del<{ ok: boolean }>(`/conversations/${cid}?for_good=1`),
}

/** a server event that changes a mission's row */
export const isMissionEvent = (ev: { type: string }) =>
  ev.type === 'mission.state' || ev.type === 'turn.state' || ev.type === 'conversation.created'
  || ev.type === 'conversation.title' || ev.type === 'conversation.deleted'
  || ev.type === 'conversation.restored' || ev.type === 'conversation.purged'
  || ev.type === 'conversation.archived' || ev.type === 'conversation.unarchived'
  || ev.type === 'conversation.auto_context'

/** how many missions wait for you (the server's count), for the rail's dot */
export function useMissionsWaiting(): number {
  const res = useResource(() => missionsApi.list().then(r => r.counts.needs_you), [],
                          { events: ev => ev.type === 'mission.state' || ev.type.startsWith('conversation.') })
  return res.data ?? 0
}

/** a mission still at work: active or waiting for you */
export const isOpen = (status: string | null | undefined) => status === 'active' || status === 'needs_you'

/** the one word for where a mission is */
export const STATE_WORD: Record<MissionStatus, string> = {
  active: 'Running', needs_you: 'Waiting for you', done: 'Done', stopped: 'Stopped',
}

/** the dot's colour for a status */
export function stateTone(status: string): 'running' | 'warn' | 'ok' | 'off' {
  return status === 'needs_you' ? 'warn' : status === 'done' ? 'ok' : status === 'stopped' ? 'off' : 'running'
}
