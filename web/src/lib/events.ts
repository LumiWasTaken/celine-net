/* Events: everything it did and everything that ran, the server's list (GET /api/events,
   server/celine/events.py). Its words, its filters and where a row opens are the server's. */
import { query, request } from './api'
import type { ServerEvent } from './bus'
import type { GuardAction } from './guard'
import type { DotTone } from '../ui'

export type EventRow = {
  id: string
  at: string
  kind: string
  title: string
  /** the state in a word: Done, Failed, Running, Stopped, ... */
  outcome: string
  tone: Extract<DotTone, 'ok' | 'error' | 'warn' | 'running' | 'muted'>
  failed: boolean
  /** the day head it sits under, in the app's zone */
  day: string
  /** the conversation it happened in, by its title */
  where: string | null
  detail: string
  /** a decision of Bastet's: its id (guardApi.decision) and what you can answer on it */
  guard: { id: number; actions: GuardAction[] } | null
  links: { conversation: string | null; message: string | null; turn: string | null; open: string | null }
}

type EventsPage = {
  events: EventRow[]
  /** counts of the kind and day: the first page's; null on a page below `before` */
  total: number | null
  failed: number | null
  /** Bastet's decisions she could not make, apart from `failed` */
  undecided: number | null
  next: string | null
  /** the tabs with their counts of the day: the first page's; null below `before` */
  tabs: { id: EventsTab; label: string; total: number; failed: number }[] | null
  /** the tab's Types */
  kinds: { id: string; label: string }[]
  days: { value: string; label: string }[]
  /** Bastet's filters: the decisions, rules and modes her rows hold */
  guard: { decisions: Choice[]; rules: Choice[]; modes: Choice[] }
}

type Choice = { id: string; label: string }
export type EventsState = 'failed' | 'undecided'
export type EventsTab = 'guard' | 'celine' | 'background'

type EventsQuery = {
  tab?: EventsTab; kind?: string | null; day?: string | null; state?: EventsState | null
  /** Bastet's filters; with them (or undecided) the list is her rows only */
  decision?: string | null; rule?: string | null; mode?: string | null
  before?: string; limit?: number
}

export const eventsApi = {
  list: (q: EventsQuery) => request<EventsPage>(`/events${query(q)}`),
}

export const isEventsEvent = (ev: ServerEvent) => ev.type === 'events.added'
