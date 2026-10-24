/* History and Ask Celine for every long text. */
import { json, query, request } from './api'

/** what a long text is, as `<kind>:<id>[:<field>]` ("her:persona", "her:talk", "note:4",
    "routine:radar:prompt") */
export type TextOf = string

/** GET /text-versions?of=&q=&before= : newest first, 50 a page */
export type TextVersion = { id: number; at: string; by: string; change: string; size: number }
export type TextHistory = { versions: TextVersion[]; total: number; next: number | null }
/** GET /text-versions/{id} */
export type TextVersionFull = TextVersion & { text: string }

/** POST /text-drafts {of, prompt, text}: Celine writes a draft for that text; it comes
    as events `texts.draft {id, text, done, error}` (the whole draft so far each time) */
type AskRequest = { of: TextOf; prompt: string; text: string }
type AskStarted = { id: string }
export type AskDraft = { id: string; text: string; done: boolean; error: string | null }

/** History and Ask Celine of every long text */
export const textsApi = {
  history: (of: TextOf, q = '', before?: number) =>
    request<TextHistory>(`/text-versions${query({ of, q: q || undefined, before })}`),
  version: (id: number) => request<TextVersionFull>(`/text-versions/${id}`),
  /** POST /text-versions/{id}/revert: that version is its persona, talk or voice again,
      saved at once as a new version (event her.changed) */
  revert: (id: number) => request<{ of: TextOf; text: string }>(`/text-versions/${id}/revert`, json('POST', {})),
  /** its draft comes as events `texts.draft` with this id */
  ask: (body: AskRequest) => request<AskStarted>('/text-drafts', json('POST', body)),
}
