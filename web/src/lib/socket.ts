/* The one socket of the app, WS /ws/events (ConnectionProvider in App). It follows the
   global feed and every conversation on screen (useConversation calls `follow`):
     {"type": "global", "after": gid}                    the last feed gid seen, kept
                                                         across reconnects; none the first time
     {"type": "subscribe", "conversation": id, "after": seq}   0: a snapshot first
     {"type": "unsubscribe", "conversation": id}
     {"type": "focus", "on": bool}                       on open and on every focus change
   A frame with `gid` (the feed's own number) is the feed's and goes to lib/bus (a feed copy of a
   conversation event carries `conversation` and `seq` too). Any other frame with
   `conversation` goes to that conversation's follower (a snapshot carries the row
   there, with its id). seq and gid only grow and may skip.

   Coming back after a close: GET /api/changes once with the last gid and the
   followed conversations, then the feed again after that gid (or from now when the
   server says resync), each conversation whose seq moved with a fresh snapshot, the
   others after their seq, and a gone one told `not_found`. The result goes out on the
   bus as `changes`. A global resync while open does the same without the
   resubscribing. Following the feed from now (the first time, after a resync) sends
   `open` on the bus: whatever a view loaded may be behind, so it loads again.

   The state is one word:
     connecting    the first attempt; nothing is shown
     connected     nothing is shown (silence means it works)
     reconnecting  closed for more than 1.5 s; send-like controls are disabled
     offline       still closed after 10 s: the server is gone (useServerDown)
   A close that heals within the grace time never shows anything. Every close also asks
   /api/health whether the session ended (lib/health.ts). */
import { checkSession, reloadIfUpdated, useApiDown } from './health'
import {
  api, request, type Attachment, type Compaction, type ImageNotice, type Intercom, type Message, type Plan, type Snapshot, type ToolCall, type ToolWords, type TurnRuns, type TurnState, type Unanswered,
} from './api'
import { emit, type ServerEvent } from './bus'
import type { GuardBanner } from './guard'
import type { ChatApproval, ConversationSettings } from './settingsApi'
import {
  createContext, createElement, useContext, useEffect, useState, type ReactNode,
} from 'react'

type Connection = 'connecting' | 'connected' | 'reconnecting' | 'offline'

const GRACE_MS = 1500
const OFFLINE_MS = 10_000
const FIRST_RETRY_MS = 300
const BACKOFF_BASE_MS = 1000
const MAX_BACKOFF_MS = 4000      // so a server that is back is noticed within a few seconds
const PING_MS = 25_000

/** the wait before reconnect attempt `n` (0: the first after a close); every socket of the app */
export const backoffMs = (n: number) => (n === 0 ? FIRST_RETRY_MS : Math.min(BACKOFF_BASE_MS * 2 ** (n - 1), MAX_BACKOFF_MS))

/** a ping on the socket `get()` returns every PING_MS while it is open, so nothing on the way
    closes it for being idle; returns the stop */
export function keepAlive(get: () => WebSocket | null): () => void {
  const t = window.setInterval(() => {
    const s = get()
    if (s?.readyState === WebSocket.OPEN) s.send(JSON.stringify({ type: 'ping' }))
  }, PING_MS)
  return () => clearInterval(t)
}

/** GET /api/changes: what changed while the client was away */
export type Changes = {
  /** the newest gid */
  global: number
  /** `since` is below what the feed keeps: everything is loaded again */
  resync: boolean
  /** the given conversations whose seq moved */
  conversations: { id: string; seq: number; title: string; archived_at: string | null; active_state: string | null }[]
  /** the given ids that no longer exist */
  gone: string[]
  jobs: { id: string; conversation_id: string; tool: string; state: string; finished_at: string }[]
  /** intercom messages that wait for you */
  unread: number
  agents: Record<string, unknown>[]
}

export const changesApi = {
  get: (since: number | null, conversations: [string, number][] = []) => {
    const q = new URLSearchParams()
    if (since !== null) q.set('since', String(since))
    if (conversations.length) q.set('conversations', conversations.map(([id, seq]) => `${id}:${seq}`).join(','))
    return request<Changes>(`/changes?${q}`)
  },
}

/** a frame of one conversation, as useConversation applies it (server/celine/conversations) */
type Of = { conversation: string; seq?: number; ts?: string }
export type Frame =
  | ({ type: 'snapshot' } & Snapshot)
  | ({ type: 'resync' } & Of)
  | ({ type: 'error'; code?: string; message?: string; retryable?: boolean } & Of)
  | ({ type: 'version.changed' | 'message.purged' } & Of)
  | ({ type: 'images.notice' } & ImageNotice & Of)
  | ({ type: 'guard.banner'; banner: GuardBanner } & Of)
  | ({ type: 'approval.asked'; approval: ChatApproval } & Of)
  | ({ type: 'approval.closed'; id: string } & Of)
  | ({ type: 'attachment.described'; message_id: string; attachment_id: string } & Pick<Attachment, 'description' | 'described_by' | 'described_for'> & Of)
  | ({ type: 'message.attachments'; message_id: string; attachments: Attachment[] } & Of)
  | ({ type: 'message.created'; ts: string; message_id: string; parent_id: string | null; role: Message['role']; content?: string; ordinal?: number
       author?: Message['author']; intercom?: Intercom | null; attachments?: Attachment[]; meta: Message['meta'] } & Of)
  | ({ type: 'message.reopened'; message_id: string } & Of)
  | ({ type: 'message.delta' | 'message.reasoning'; message_id: string; text: string } & Of)
  | ({ type: 'message.done' | 'message.rewritten'; message_id: string; text: string } & Of)
  | ({ type: 'conversation.notes_off'; notes_off?: string[] } & Of)
  | ({ type: 'tool.forming'; message_id: string; index: number; name: string; chars: number; lines: number; preview: string; words: string } & Of)
  | ({ type: 'tool.started'; message_id: string; tool_call_id: string; name: string; arguments: string; words: ToolWords; started_at?: string | null } & Of)
  | ({ type: 'tool.done' | 'tool.gated'; tool_call_id: string; state?: ToolCall['state']; ok?: boolean; display?: ToolCall['display']; words?: ToolWords
       result?: string | null; job_id?: string | null; finished_at?: string | null; runs?: TurnRuns | null } & Of)
  | ({ type: 'tool.detached'; tool_call_id: string; job_id: string; name: string } & Of)
  | ({ type: 'images.batch' | 'music.batch'; batch: unknown } & Of)
  | ({ type: 'tool.progress'; tool_call_id: string; note: string } & Of)
  | ({ type: 'tool.reset'; tool_call_id: string; runs?: TurnRuns | null } & Of)
  | ({ type: 'llm.queue'; message_id?: string | null; state: 'waiting' | 'loading' | 'running' | string; model: string
       fallback?: boolean; instead_of?: string; why?: string | null; ahead?: number; running?: number
       reason?: string | null; message?: string | null; holder?: string | null } & Of)
  | ({ type: 'turn.state'; turn_id: string; state: TurnState; reason?: string | null; reason_code?: string | null; cut?: string | null
       continues?: number; continuable?: { ok: boolean; label: string | null; reason: string | null } | null
       /** what Regenerate answers after this change */
       unanswered?: Unanswered | null } & Of)
  | ({ type: 'context.compacted' } & Compaction & Of)
  | ({ type: 'context.compact_failed'; trigger: string } & Of)
  | ({ type: 'context.compact_skipped'; reason?: string } & Of)
  | ({ type: 'plan.state'; plan: Plan } & Of)
  | ({ type: 'conversation.settings' } & ConversationSettings & Of)

/** what arrives on the socket before it is routed: a feed event (with `gid`) or a conversation's frame */
type Wire = { type: string; gid?: number; feed?: string; code?: string; seq?: number; conversation?: unknown }

/** one conversation on screen: its last seq, and where its frames go */
type Follower = { seq: () => number; onFrame: (ev: Frame) => void }

// by conversation
const followers = new Map<string, Set<Follower>>()
let sock: WebSocket | null = null
let feedAfter: number | null = null

function send(frame: object) {
  if (sock?.readyState === WebSocket.OPEN) sock.send(JSON.stringify(frame))
}

/** whether this page is visible and focused: the server pushes to devices only while no page is
    (server/celine/push.py) */
const sendFocus = () => send({ type: 'focus', on: document.visibilityState === 'visible' && document.hasFocus() })
const FOCUS: [string, EventTarget][] = [['focus', window], ['blur', window], ['visibilitychange', document]]

/** the seq a conversation is followed after: its least advanced follower's */
const seqOf = (set: Set<Follower>) => Math.min(...[...set].map(f => f.seq()))

/** follow a conversation for as long as it is on screen; returns the unfollow. A second
    follower of one conversation asks for a snapshot too: both apply it. */
export function follow(cid: string, f: Follower): () => void {
  const set = followers.get(cid) ?? new Set()
  followers.set(cid, set.add(f))
  send({ type: 'subscribe', conversation: cid, after: seqOf(set) })
  return () => {
    if (!set.delete(f) || set.size || followers.get(cid) !== set) return
    followers.delete(cid)
    send({ type: 'unsubscribe', conversation: cid })
  }
}

/** the conversation from a fresh snapshot (a resync, a version switch) */
export function snapshotAgain(cid: string) {
  if (followers.has(cid)) send({ type: 'subscribe', conversation: cid, after: 0 })
}

function followFeed(after: number | null) {
  feedAfter = after
  send(after === null ? { type: 'global' } : { type: 'global', after })
  if (after === null) emit({ type: 'open' })
}

/** the server ended this conversation's subscription: it is not asked for again */
function gone(cid: string) {
  const set = followers.get(cid)
  followers.delete(cid)
  for (const f of set ?? []) f.onFrame({ type: 'error', conversation: cid, code: 'not_found' })
}

/* back after a close (`again`) or told the feed resynced: one /api/changes, then follow */
async function catchUp(s: WebSocket, again: boolean) {
  const known = [...followers].map(([cid, set]) => [cid, seqOf(set)] as [string, number])
  let ch: Changes
  try { ch = await changesApi.get(feedAfter, known) } catch {
    if (sock === s) s.close()      // the reconnect asks again
    return
  }
  if (sock !== s) return
  followFeed(ch.resync || !again || feedAfter === null ? null : feedAfter)
  if (again) {
    const moved = new Set(ch.conversations.map(c => c.id))
    for (const cid of ch.gone) gone(cid)
    for (const [cid, set] of followers) {
      send({ type: 'subscribe', conversation: cid, after: moved.has(cid) ? 0 : seqOf(set) })
    }
  }
  emit({ type: 'changes', ...ch })
}

function route(ev: Wire, s: WebSocket) {
  if (typeof ev.gid === 'number') {
    feedAfter = ev.gid
    if (ev.type !== 'global') emit(ev as ServerEvent)
    return
  }
  if (ev.type === 'resync' && ev.feed === 'global') { void catchUp(s, false); return }
  const cid = ev.type === 'snapshot' ? (ev.conversation as { id?: unknown } | undefined)?.id : ev.conversation
  if (typeof cid !== 'string') return
  if (ev.type === 'error' && ev.code === 'not_found' && ev.seq === undefined) { gone(cid); return }
  for (const f of followers.get(cid) ?? []) f.onFrame(ev as Frame)
}

function useSocket(): Connection {
  const [state, setState] = useState<Connection>('connecting')

  useEffect(() => {
    let closed = false
    let attempt = 0
    let opened = false
    let retry: number | undefined
    let grace: number | undefined
    let gone: number | undefined
    setState('connecting')

    const connect = () => {
      clearTimeout(retry); retry = undefined
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const s = new WebSocket(`${proto}://${location.host}/ws/events`)
      sock = s
      s.onopen = () => {
        attempt = 0
        clearTimeout(grace); grace = undefined
        clearTimeout(gone); gone = undefined
        setState('connected')
        sendFocus()
        if (opened) {
          // back after a while: an update in between reloads the page instead of a stale app
          void api.configStatus().then(st => reloadIfUpdated(st.build.commit), () => undefined)
          void catchUp(s, true); return
        }
        opened = true
        followFeed(null)
        for (const [cid, set] of followers) send({ type: 'subscribe', conversation: cid, after: seqOf(set) })
      }
      s.onmessage = e => {
        let ev: Wire | null
        try { ev = JSON.parse(String(e.data)) as Wire | null } catch { return }
        if (!ev || ev.type === 'pong') return
        route(ev, s)
      }
      s.onclose = () => {
        if (closed || sock !== s) return
        checkSession()
        const n = attempt++
        if (grace === undefined) {
          grace = window.setTimeout(() => setState(st => (st === 'offline' ? st : 'reconnecting')), GRACE_MS)
        }
        if (gone === undefined) gone = window.setTimeout(() => setState('offline'), OFFLINE_MS)
        retry = window.setTimeout(connect, backoffMs(n))
      }
    }
    connect()

    // the network came back: try now instead of waiting out the backoff
    const onOnline = () => { if (sock && sock.readyState !== WebSocket.OPEN && sock.readyState !== WebSocket.CONNECTING) connect() }
    window.addEventListener('online', onOnline)
    const unping = keepAlive(() => sock)
    for (const [t, on] of FOCUS) on.addEventListener(t, sendFocus)

    return () => {
      closed = true
      clearTimeout(retry); clearTimeout(grace); clearTimeout(gone); unping()
      window.removeEventListener('online', onOnline)
      for (const [t, on] of FOCUS) on.removeEventListener(t, sendFocus)
      const s = sock
      sock = null
      if (s) { s.onclose = null; s.close() }
    }
  }, [])

  return state
}

export const isDown = (c: Connection) => c === 'reconnecting' || c === 'offline'

const Ctx = createContext<Connection>('connecting')

export function ConnectionProvider({ children }: { children: ReactNode }) {
  return createElement(Ctx.Provider, { value: useSocket() }, children)
}

/** the socket's state */
export const useConnection = () => useContext(Ctx)

/** the server is gone: /api/health failed twice, or the socket stayed closed for 10 s */
export function useServerDown(): boolean {
  const apiDown = useApiDown()
  const connection = useContext(Ctx)
  return apiDown || connection === 'offline'
}

/** false while the server can't be reached: send-like buttons disable themselves */
export function useOnline(): boolean {
  const apiDown = useApiDown()
  return !isDown(useContext(Ctx)) && !apiDown
}

export const OFFLINE_TITLE = 'No connection to the server.'

