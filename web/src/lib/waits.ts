/* Waiting for you (server/celine/waits.py, GET /api/waits): everything that waits on you,
   the server's list and count, asked again on waits.changed. A request is its card; every
   other item a row: a mission in Needs you, a lost login, an Intercom thread. */
import { request } from './api'
import type { Approval } from './settingsApi'
import { useResource } from './useResource'

export type Wait =
  | { kind: 'approval'; approval: Approval }
  | { kind: 'mission' | 'login' | 'thread'; id: string; title: string; line: string; link: string | null }

export const waitsApi = {
  list: (signal?: AbortSignal) => request<{ items: Wait[]; count: number }>('/waits', { signal }),
}

export const isWaitsEvent = (ev: { type: string }) => ev.type === 'waits.changed'

/** the server's list, asked again on waits.changed */
export function useWaits() {
  return useResource(signal => waitsApi.list(signal), [], { events: isWaitsEvent })
}
