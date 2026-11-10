/* Bastet, the guard: her banners in a conversation (the snapshot's `guard`, the event
   `guard.banner`), your answers in their threads (POST /guard/banners/{id}/replies), and
   the freeze (GET and DELETE /guard/freeze, the event `frozen.changed`), and
   one decision whole for the Events page (GET /guard/decisions/{id}), and on Settings,
   Guard: its own texts (GET /guard/texts, PUT /guard/texts/{field}, the event
   `guard.texts.changed`; server/celine/guard/persona.py), its rules (GET and PUT
   /guard/rules) and the passes that hold (GET /guard/passes, DELETE /guard/passes/{id},
   the event `guard.passes`).
   Every word of a banner is the server's. server/celine/guard/banners.py, api.py;
   docs/ARCHITECTURE.md, "Bastet". */
import { Check, Ticket, type LucideIcon } from 'lucide-react'
import { del, json, request } from './api'
import { useResource } from './useResource'

export type BannerKind = 'stop' | 'freeze' | 'approved' | 'denied' | 'closed' | 'patrol'

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
  /** a stop or a freeze: Allow once, and Allow for 7 days when it has a piece to let through */
  actions: GuardAction[]
  /** what those answers do, behind its "?" */
  help: string | null
  thread: BannerLine[]
  created_at: string
}

/** what you can answer on a stop or a freeze: allow the call once, or allow its rule and piece in its chat for 7 days */
export type GuardAction = 'once' | 'pass'
export const ACTION_WORDS: Record<GuardAction, string> = { once: 'Allow once', pass: 'Allow for 7 days' }
export const ACTION_ICONS: Record<GuardAction, LucideIcon> = { once: Check, pass: Ticket }

/** one decision whole (guard/store.py one) */
type GuardDecision = {
  id: number
  conversation_id: string | null
  tool: string
  rule: string
  /** its rule as Events names it; null for a rule the app no longer has */
  rule_word: string | null
  mode: string
  decision: string
  /** Bastet's line for you, as her banner says it */
  line: string
  /** what the Decision model (the guard role) was asked; null for a rule no question reads */
  question: string | null
  /** the score the Decision model gave (0 to 1); null when none was asked */
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
  /** its title as its Events row says it */
  title: string
  /** the app path of its conversation */
  open: string | null
  created_at: string
}

type ChainLevel = { level: 'L1' | 'L2'; verdict: string; why?: string; quote?: string; model?: string; ms?: number }

type Freeze = { id: number; at: string; by: 'guard' | 'user'; why: string; conversation_id: string | null }

/** a pass that holds now: its rule's label, the piece it lets through (null: the whole rule), where and until when */
export type GuardPass = { id: number; rule: string; label: string; piece: string | null; line: string }

/** what a rule does, as Settings, Guard sets it */
export type RuleAct = 'off' | 'watch' | 'stop' | 'freeze'
/** guard.toml as the Guard page reads it: each rule it sets with its label and what it does, the words of
    what a rule can do, and the page's help texts (names filled in) */
export type GuardRules = {
  rows: { rule: string; label: string; act: RuleAct }[]
  acts: { value: RuleAct; label: string }[]
  help: Record<'mode' | 'decision' | 'second_look' | 'rules' | 'passes', string>
}

const enc = encodeURIComponent

/** Bastet's texts: who she is, how she replies to the agent, how she writes her notes to you */
export type BastetField = 'persona' | 'talk' | 'note'
/** `default`: the shipped text; `own`: yours is in use */
export type BastetText = { text: string; default: string; own: boolean }
type BastetTexts = { texts: Record<BastetField, BastetText> }

export const guardApi = {
  texts: () => request<BastetTexts>('/guard/texts'),
  /** null: the default again */
  putText: (field: BastetField, text: string | null) => request<BastetTexts>(`/guard/texts/${field}`, json('PUT', { text })),
  /** your words in a banner's thread, or one of its buttons */
  answer: (id: number, body: { text: string } | { button: 'unfreeze' | GuardAction }) =>
    request<{ banner: GuardBanner }>(`/guard/banners/${enc(String(id))}/replies`, json('POST', body)),
  passes: () => request<{ passes: GuardPass[] }>('/guard/passes'),
  endPass: (id: number) => del<{ ended: number }>(`/guard/passes/${enc(String(id))}`),
  rules: () => request<GuardRules>('/guard/rules'),
  /** the rule tables to change: { leak: { on, does } } */
  putRules: (tables: Record<string, Record<string, unknown>>) => request<GuardRules>('/guard/rules', json('PUT', tables)),
  decision: (id: number) => request<{ decision: GuardDecision }>(`/guard/decisions/${enc(String(id))}`),
  frozen: () => request<{ frozen: Freeze | null }>('/guard/freeze'),
  unfreeze: () => del<{ unfrozen: Freeze | null }>('/guard/freeze'),
}

/** the open freeze, the server's, asked again on every `frozen.changed` */
export function useFrozen(): Freeze | null {
  const res = useResource(() => guardApi.frozen().then(r => r.frozen), [], { events: ev => ev.type === 'frozen.changed' })
  return res.data ?? null
}
