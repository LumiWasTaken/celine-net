/* The agent's browser box: its state and tabs, the user's controls, and its calls run as it runs them. */
import { json, query, request } from './api'

export type Tab = {
  name: string; url: string; title: string; busy: boolean
  /** the conversation that opened it; null: yours, or too new to know */
  conversation: { id: string; title: string; kind: string } | null
}
export type Box = {
  /** the box's own, or unreachable (it does not answer), or off (no box set up) */
  state: 'starting' | 'up' | 'down' | 'unreachable' | 'off'
  /** off: the server's sentence */
  reason?: string
  /** since when you have the browser, epoch seconds */
  taken: number | null
  /** the tab in front, the one on the screen */
  front: string | null
  /** the box's screen in px (browserd's Xvnc): the live view keeps its shape */
  screen?: { w: number; h: number }
  tabs: Tab[]
}

export const browserApi = {
  get: () => request<Box>('/browser'),
  /** take and give answer {taken}; restart answers once the box is up or down again */
  control: (action: 'take' | 'give' | 'restart') =>
    request<{ state?: Box['state'] }>('/browser/control', json('POST', { action })),
  /** a tab to the front or closed; the box says no as a 200 with its sentence in `refused` */
  tab: async (action: 'front' | 'close', tab: string) => {
    const out = await request<{ refused?: string }>('/browser/control', json('POST', { action, tab }))
    if (out.refused) throw new Error(out.refused)
  },
  /** one of its calls, run as it runs it: its text, whether it was refused, its row's display */
  call: (tool: string, args: Record<string, unknown>) =>
    request<{ content: string; is_error: boolean; display: Record<string, unknown> | null }>(
      '/browser/calls', json('POST', { tool, args })),
}

/** one of its browser calls: its line as its tool row says it, how it went, its shots */
export type BrowserCall = {
  id: string
  tool: string
  tab: string | null
  line: string
  state: 'running' | 'done' | 'failed' | 'cancelled'
  at: string
  shots: number
  display: Record<string, unknown>
  conversation: { id: string; title: string }
}

/** GET /browser/calls: its calls, newest first; `failed` how many failed, `next` the `before` of the next page */
export const browserHistory = (p: { q?: string; failed?: boolean; before?: string | null; limit?: number } = {}) =>
  request<{ calls: BrowserCall[]; total: number; failed: number; next: string | null }>(
    `/browser/calls${query({ q: p.q || undefined, failed: p.failed || undefined, before: p.before ?? undefined, limit: p.limit })}`)

export const isBrowserEvent = (ev: { type: string; name?: unknown }) =>
  ev.type === 'browser.state' ||
  ((ev.type === 'tool.started' || ev.type === 'tool.done') && typeof ev.name === 'string' && ev.name.startsWith('browser__'))
