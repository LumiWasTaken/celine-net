/* How a conversation's context is chosen, beyond what GET .../context measures
   (lib/api ContextView): the automatic context switch, a skill or a note loaded by hand,
   and its own block for one message (the message_context report). */
import { json, request } from './api'

/** its mood and the time since the last talk, for the message a reply answers (skills/mind.py) */
export type OnHerMind = {
  mood: string | null
  gap: string | null
}

const e = encodeURIComponent

export const contextApi = {
  /** tools and note sections each message matches, or only what is loaded by hand */
  setAuto: (cid: string, on: boolean) =>
    request<{ on: boolean }>(`/conversations/${e(cid)}/auto-context`, json('PUT', { on })),
  loadSkill: (cid: string, name: string) =>
    request<{ ok: boolean }>(`/conversations/${e(cid)}/context/skills/${e(name)}`, { method: 'PUT' }),
  loadNote: (cid: string, path: string) =>
    request<{ ok: boolean }>(`/conversations/${e(cid)}/context/notes`, json('POST', { path })),
  /** the context block of the message a reply (`mid`, its first step) answers */
  ofReply: (cid: string, mid: string) =>
    request<{ text: string; report: { mind?: OnHerMind } }>(`/conversations/${e(cid)}/messages/${e(mid)}/context`),
}

/** the switch's label, where the server offers it (missions) */
export const AUTO_CONTEXT = 'Load automatically'
/** its tip: what on does (turns/loads.asked, skills/recall.py) */
export const AUTO_CONTEXT_TIP = 'Tools and notes that match each message'
