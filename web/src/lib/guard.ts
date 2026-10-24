/* Bastet, the guard: her banners in a conversation (the snapshot's `guard`, the event
   `guard.banner`), your answers in their threads (POST /guard/banners/{id}/replies), and
   the freeze (GET and DELETE /guard/freeze, the event `frozen.changed`), and
   one decision whole for the Events page (GET /guard/decisions/{id}), and its own texts on
   Settings, Guard (GET /guard/texts, PUT /guard/texts/{field}, the event
   `guard.texts.changed`; server/celine/guard/persona.py).
   Every word of a banner is the server's. server/celine/guard/banners.py, api.py;
   docs/ARCHITECTURE.md, "Bastet". */
import { del, json, request } from './api'
import { useResource } from './useResource'

export type BannerKind = 'stop' | 'freeze' | 'ask' | 'approved' | 'denied' | 'closed' | 'patrol'

type BannerLine = { by: 'user' | 'guard'; text: string; at: string }

/** one decision of Bastet's after the message that made the call */
export type GuardBanner = {
  id: number
  conversation_id: string
  message_id: string | null
  tool_call_id: string
  tool: string
  rule: string
  kind: BannerKind
  /** what it did and why, in its words for you */
  line: string
  /** its freeze of this turn still holds: Unfreeze */
  frozen: boolean
  thread: BannerLine[]
  created_at: string
}

/** what you can answer on a decision outside its thread: allow the call once, a pass on its rule here */
export type GuardAction = 'once' | 'pass'

/** one decision whole (guard/store.py one) */
type GuardDecision = {
  id: number
  conversation_id: string | null
  tool: string
  rule: string
  mode: string
  decision: string
  /** Bastet's line for you, as her banner says it */
  line: string
  /** what the small role was asked; null for a rule no question reads */
  question: string | null
  /** the score the small role gave (0 to 1); null when none was asked */
  p: number | null
  model: string | null
  ms: number | null
  arguments: Record<string, unknown>
  source: string | null
  quote: string | null
  piece: string | null
  why: string | null
  /** each judge's verdict, first look first (server/celine/guard/chain.py) */
  chain: ChainLevel[]
  actions: GuardAction[]
  created_at: string
}

type ChainLevel = { level: 'L1' | 'L2'; verdict: string; why?: string; quote?: string; model?: string; ms?: number }

type Freeze = { id: number; at: string; by: 'guard' | 'user'; why: string; conversation_id: string | null }

const enc = encodeURIComponent

/** Bastet's texts: who she is, how she replies to Celine, how she writes her notes to you */
export type BastetField = 'persona' | 'talk' | 'note'
/** `default`: the shipped text; `own`: yours is in use */
export type BastetText = { text: string; default: string; own: boolean }
type BastetTexts = { texts: Record<BastetField, BastetText> }

export const guardApi = {
  texts: () => request<BastetTexts>('/guard/texts'),
  /** null: the default again */
  putText: (field: BastetField, text: string | null) => request<BastetTexts>(`/guard/texts/${field}`, json('PUT', { text })),
  /** your words in a banner's thread, or one of its buttons */
  answer: (id: number, body: { text: string } | { button: 'approve' | 'deny' | 'unfreeze' | GuardAction }) =>
    request<{ banner: GuardBanner }>(`/guard/banners/${enc(String(id))}/replies`, json('POST', body)),
  decision: (id: number) => request<{ decision: GuardDecision }>(`/guard/decisions/${enc(String(id))}`),
  frozen: () => request<{ frozen: Freeze | null }>('/guard/freeze'),
  unfreeze: () => del<{ unfrozen: Freeze | null }>('/guard/freeze'),
}

/** the open freeze, the server's, asked again on every `frozen.changed` */
export function useFrozen(): Freeze | null {
  const res = useResource(() => guardApi.frozen().then(r => r.frozen), [], { events: ev => ev.type === 'frozen.changed' })
  return res.data ?? null
}
