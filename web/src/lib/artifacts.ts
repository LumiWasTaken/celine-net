/* The agent's artifacts (server/celine/artifacts): the API, the types, and the one place that
   knows a page's address and the frame's sandbox. */
import { createContext } from 'react'
import { del, post, request, requestText } from './api'
import type { ServerEvent } from './bus'

type ArtifactKind = 'html' | 'markdown'

type ArtifactVersion = { n: number; title: string; note: string; created_at: string; bytes: number }

/** one artifact in a list: its row and its newest version's number, title and note */
export type ArtifactRow = {
  id: string
  kind: ArtifactKind
  conversation_id: string | null
  created_at: string
  trashed_at: string | null
  n: number
  title: string
  note: string
  /** when the newest version was made */
  updated_at: string
}

export type Artifact = ArtifactRow & {
  versions: ArtifactVersion[]
  /** where its conversation opens, the server's (homes.py); null without one */
  link: string | null
}

/** what its write leaves in the tool row's display: a card's data */
export type ArtifactCardData = { id: string; n: number; title: string; kind: ArtifactKind }

export const artifactsApi = {
  /** newest change first: one conversation's, or all; with `trashed` the trashed ones instead */
  list: (q: { conversation?: string; trashed: boolean }, signal?: AbortSignal) => {
    const p = new URLSearchParams()
    if (q.conversation) p.set('conversation', q.conversation)
    if (q.trashed) p.set('trashed', '1')
    return request<{ artifacts: ArtifactRow[] }>(`/artifacts?${p}`, { signal }).then(r => r.artifacts)
  },
  get: (id: string, signal?: AbortSignal) => request<Artifact>(`/artifacts/${id}`, { signal }),
  source: (id: string, n: number) => requestText(`/artifacts/${id}/versions/${n}/source`),
  /** an old version copied to the top as the newest */
  restore: (id: string, n: number) =>
    post<Artifact>(`/artifacts/${id}/versions/${n}/restore`),
  setTrashed: (id: string, trashed: boolean) =>
    trashed ? del<Artifact>(`/artifacts/${id}`) : post<Artifact>(`/artifacts/${id}/restore`),
  /** a trashed one for good, every version */
  remove: (id: string) => del<{ ok: boolean }>(`/artifacts/${id}?for_good=1`),
}

/** a version as the page itself, sandboxed by its own headers */
export const pageUrl = (id: string, n: number) => `/api/artifacts/${id}/versions/${n}`
/** what it wrote, as plain text: the Download */
export const sourceUrl = (id: string, n: number) => `/api/artifacts/${id}/versions/${n}/source`

/** the frame's sandbox: the same as the page's header (artifacts/page.py SANDBOX) */
export const SANDBOX = 'allow-scripts allow-forms'

/** its write or your change of one artifact (any, without an id) */
export const artifactEvent = (id?: string) => (ev: ServerEvent) =>
  ev.type === 'artifact.updated' && (id === undefined || ev.id === id)

/** "page" or "document", for the words around one */
export const kindWord = (kind: string) => (kind === 'markdown' ? 'document' : 'page')

/** In the Celine view: shows an artifact in the side panel. Elsewhere there is none, and
    a chip or card opens the full page in a new tab. */
export const ShowArtifact = createContext<((id: string) => void) | null>(null)

/** `[[artifact:7f3a9c]]`, or the text inside the brackets: the id */
const ARTIFACT_REF = /^(?:\[\[)?artifact:([a-z0-9]{6})(?:\]\])?$/
export const artifactRef = (text: string): string | null => ARTIFACT_REF.exec(text.trim())?.[1] ?? null

/** the card's data in a call's display, if it wrote an artifact */
export function artifactOf(display: Record<string, unknown> | null | undefined): ArtifactCardData | null {
  const a = display?.artifact as ArtifactCardData | undefined
  return a && typeof a.id === 'string' && typeof a.n === 'number' ? a : null
}
