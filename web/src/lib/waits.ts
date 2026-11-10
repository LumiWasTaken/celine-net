/* Waiting for you (server/celine/waits.py, GET /api/waits): everything that waits on you,
   the server's list and counts, asked again on waits.changed. A request is its card; every
   other item a row with the page that answers it (`link`): a mission that asks you, a lost
   login, an Intercom thread. The shell asks once (useWaitsResource) and hands the answer
   down (WaitsContext, useWaits). */
import { createContext, useContext } from 'react'
import { request } from './api'
import type { Approval } from './settingsApi'
import { useResource } from './useResource'

/** a mission that asks you: `why` its note, `text` what it wrote you that waits (else its
    last answer), `asked` those messages (a reply answers the last), `origin` where it was
    started, `home` its own page */
export type MissionWait = {
  group: string; kind: 'mission'; id: string; title: string; line: string; link: string; home: string | null
  why: string; text: string; asked: number[]; at: string; origin: { name: string; link: string | null } | null
}
export type Wait =
  | { group: string; kind: 'approval'; approval: Approval }
  | MissionWait
  | { group: string; kind: 'login'; id: string; title: string; line: string; link: string }
  | { group: string; kind: 'thread'; id: string; title: string; line: string; link: string; from: string; at: string }

type Waits = { items: Wait[]; count: number; counts: Record<string, number> }

export const waitsApi = {
  list: (signal?: AbortSignal) => request<Waits>('/waits', { signal }),
}

export const isWaitsEvent = (ev: { type: string }) => ev.type === 'waits.changed'

/** the server's list, asked again on waits.changed: the shell's one copy */
export function useWaitsResource() {
  return useResource(signal => waitsApi.list(signal), [], { events: isWaitsEvent })
}

export const WaitsContext = createContext<ReturnType<typeof useWaitsResource> | null>(null)

/** the shell's copy of the list */
export function useWaits() {
  const v = useContext(WaitsContext)
  if (!v) throw new Error('useWaits outside the shell')
  return v
}
