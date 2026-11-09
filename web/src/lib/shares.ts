/* The agent's shares (server/celine/shares.py): a server it runs in its shellbox, or a file or
   folder of its home, opened in a tab of its own through its open route (url), which lets
   you in to a private one; each runs until its end time (ends_at) unless it ended before. */
import { post, request } from './api'
import type { ServerEvent } from './bus'

export type ShareState = 'live' | 'down' | 'ended'

export type Share = {
  token: string
  title: string
  port: number | null
  path: string | null
  conversation_id: string | null
  /** anyone with the link opens it; else only you, logged in */
  public: boolean
  created_at: string
  /** when it ends, or ended */
  ends_at: string
  /** the server's word: it answers, nothing answers, or it ended */
  state: ShareState
  /** the card's words, from the server: what is shared and from where while it runs
   *  ("Port 8000 in the shellbox"), else "Not running" or "Ended" */
  line: string
  /** where it opens, in a tab of its own */
  url: string
}

/** what shell__share leaves in its call's display: a card's data */
export type ShareCardData = { token: string; title: string }

export const sharesApi = {
  /** the shares that run, newest first: one conversation's, or all */
  list: (conversation: string | undefined, signal?: AbortSignal) =>
    request<{ shares: Share[] }>(`/shares${conversation ? `?conversation=${encodeURIComponent(conversation)}` : ''}`, { signal })
      .then(r => r.shares),
  get: (token: string, signal?: AbortSignal) => request<Share>(`/shares/${token}`, { signal }),
  /** ends it for both of you */
  stop: (token: string) => post<Share>(`/shares/${token}/stop`),
}

/** its share or its end (any, without a token) */
export const shareEvent = (token?: string) => (ev: ServerEvent) =>
  ev.type === 'share.updated' && (token === undefined || ev.token === token)

/** the card's data in a call's display, if it shared something */
export function shareOf(display: Record<string, unknown> | null | undefined): ShareCardData | null {
  const s = display?.share as ShareCardData | undefined
  return s && typeof s.token === 'string' && typeof s.title === 'string' ? s : null
}
