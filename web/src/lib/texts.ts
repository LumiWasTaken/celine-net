/* History and Ask Celine for every long text. */
import { json, query, request, requestLines } from './api'

/** what a long text is, as `<kind>:<id>[:<field>]` ("her:persona", "her:talk", "note:4",
    "routine:radar:prompt") */
export type TextOf = string

/** GET /text-versions?of=&q=&before= : newest first, 50 a page */
export type TextVersion = { id: number; at: string; by: string; change: string; size: number }
export type TextHistory = { versions: TextVersion[]; total: number; next: number | null }
/** GET /text-versions/{id} */
export type TextVersionFull = TextVersion & { text: string }

/** POST /text-drafts {of, prompt, text}: Celine writes a draft for that text; it streams
    back on that request as lines: `{text}` what it added, then `{done, text, error}` with
    the whole draft */
type AskRequest = { of: TextOf; prompt: string; text: string }
export type AskDraft = { text: string; done?: true; error?: string | null }

/** History and Ask Celine of every long text */
export const textsApi = {
  history: (of: TextOf, q = '', before?: number) =>
    request<TextHistory>(`/text-versions${query({ of, q: q || undefined, before })}`),
  version: (id: number) => request<TextVersionFull>(`/text-versions/${id}`),
  /** POST /text-versions/{id}/revert: that version is its persona, talk or voice again,
      saved at once as a new version (event her.changed) */
  revert: (id: number) => request<{ of: TextOf; text: string }>(`/text-versions/${id}/revert`, json('POST', {})),
  /** each line of its draft to `onDraft` as it streams; `signal` ends it */
  ask: (body: AskRequest, onDraft: (d: AskDraft) => void, signal: AbortSignal) =>
    requestLines('/text-drafts', { ...json('POST', body), signal }, d => onDraft(d as AskDraft)),
}
