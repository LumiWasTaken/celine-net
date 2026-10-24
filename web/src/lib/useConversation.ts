import { useEffect, useRef, useState } from 'react'
import {
  RUNNING, type Compaction, type ImageNotice, type Live, type Message, type Queued, type Snapshot, type Switched,
  type ToolCall, type Turn, type TurnState,
} from './api'
import { asBatch } from './media'
import { follow, isDown, snapshotAgain, useConnection, type Frame } from './socket'
import type { ConversationSettings } from './settingsApi'
import type { GuardBanner } from './guard'
import type { Plan } from './api'

/** a hidden tab runs no frames: the gathered text is applied this often there */
const HIDDEN_FLUSH_MS = 250

/** this conversation's model call waiting in the LLM queue (the `llm.queue` event, state waiting) */
export type QueueWait = { turn_id: string; message_id?: string; ahead: number; running: number; model?: string
  /** no chat model can answer now: the server's sentence why */
  noModel?: string | null
  /** an image generation holds the model's GPU: the server's line */
  held?: string | null
  /** the provider is busy and the model is asked again: the server's line */
  limited?: string | null
  /** the request ahead on the model, by name */
  holder?: string | null }

/* waiting and loading are said in words; the others show as dots or the caret */
function queueWait(turn: Turn | null, live: Live | null): QueueWait | null {
  if (!turn || !live || live.phase !== 'waiting') return null
  return { turn_id: turn.id, message_id: live.message_id, ahead: live.ahead ?? 0, running: live.running ?? 0, model: live.model,
           noModel: live.reason === 'no_model' ? live.message ?? '' : null,
           held: live.reason === 'gpu_lease' ? live.message ?? null : null,
           limited: live.reason === 'busy' ? live.message ?? null : null, holder: live.holder ?? null }
}

/** every tool call that `hit` picks, changed by `fn`; the lists without one stay as they are */
function mapCalls(s: State, hit: (c: ToolCall) => boolean, fn: (c: ToolCall) => ToolCall): State {
  return { ...s, toolCalls: Object.fromEntries(Object.entries(s.toolCalls).map(([k, list]) =>
    [k, list.some(hit) ? list.map(c => (hit(c) ? fn(c) : c)) : list])) }
}

/** how a /compact you ran ended (context.compacted / compact_failed / compact_skipped) */
type CompactOutcome = { kind: 'compacted' | 'failed' | 'skipped'; reason?: string; seq: number }

type State = {
  /** the conversation all of this is of */
  cid: string | null
  /** the conversation's row from the snapshot (title, kind, agents, archived_at) */
  conversation: Snapshot['conversation'] | null
  /** the active path */
  messages: Message[]
  toolCalls: Record<string, ToolCall[]>
  /** the one running or next; while turns wait behind it, never the one that just ended */
  turn: Turn | null
  /** the live turns in the order they run (the snapshot's, then turn.state) */
  queue: Queued[]
  error: { message: string; retryable: boolean } | null
  compactions: Compaction[]
  /** the reply being written: which message, and what it waits for */
  live: Live | null
  /** replies another model wrote because the chosen one failed, by message id */
  switched: Record<string, Switched>
  /** the lines after a switch to a model that can't see images (event images.notice) */
  imageNotices: ImageNotice[]
  /** Bastet's banners after the messages that made their calls (event guard.banner) */
  banners: GuardBanner[]
  /** the turn you pressed Stop on, shown stopped until the server says so */
  stopping: string | null
  /** the latest manual compaction's outcome, for the quiet line at the end of the transcript */
  compactOutcome: CompactOutcome | null
  /** the latest `conversation.settings` event (a change from the panel, /think, elsewhere) */
  settings: ConversationSettings | null
  /** tool calls it is still writing (event tool.forming), until they start */
  forming: Forming[]
  /** its plan here (event plan.state) */
  plan: Plan | null
}

export interface Forming { message_id: string; index: number; name: string; chars: number; preview: string; words: string }

const EMPTY: State = { cid: null, conversation: null, messages: [], toolCalls: {}, turn: null, queue: [], error: null, compactions: [], live: null, switched: {}, imageNotices: [], banners: [], stopping: null, compactOutcome: null, settings: null, forming: [], plan: null }

export function useConversation(conversationId: string | null) {
  const [state, setState] = useState<State>(EMPTY)
  const seq = useRef(0)
  const connection = useConnection()

  // one subscription on the app's socket (lib/socket.ts) while the conversation is open;
  // seq only grows and may skip, so a frame at or below it is one already applied.
  // The text of a reply (message.delta, message.reasoning) is gathered and applied once a
  // frame (every 250 ms in a hidden tab, where frames do not run); any other event first
  // applies what was gathered, so the order holds and the end of a turn is never late.
  useEffect(() => {
    seq.current = 0
    setState({ ...EMPTY, cid: conversationId })
    if (!conversationId) return
    const cid = conversationId
    let held: TextFrame[] = []
    // a frame id and a timeout id are separate counters: each is cancelled only as itself
    let frame = 0, timeout = 0
    const cancel = () => {
      if (frame) cancelAnimationFrame(frame)
      if (timeout) clearTimeout(timeout)
      frame = 0; timeout = 0
    }
    const flush = () => {
      cancel()
      if (!held.length) return
      const evs = joinText(held)
      held = []
      setState(s => evs.reduce(apply, s))
    }
    const later = () => {
      if (frame || timeout) return
      if (document.hidden) timeout = window.setTimeout(flush, HIDDEN_FLUSH_MS)
      else frame = requestAnimationFrame(flush)
    }
    const stop = follow(cid, {
      seq: () => seq.current,
      onFrame: ev => {
        if (ev.type === 'resync') { held = []; seq.current = 0; snapshotAgain(cid); return }
        if (ev.type === 'snapshot') { held = []; seq.current = ev.seq; setState(s => apply(s, ev)); return }
        if (typeof ev.seq === 'number') {
          if (ev.seq <= seq.current) return
          seq.current = ev.seq
        }
        if (ev.type === 'message.delta' || ev.type === 'message.reasoning') { held.push(ev); later(); return }
        flush()
        // the version moved or a message went: the server's snapshot, never a guess
        if (ev.type === 'version.changed' || ev.type === 'message.purged') {
          snapshotAgain(cid); return
        }
        setState(s => apply(s, ev))
      },
    })
    return () => { cancel(); stop() }
  }, [conversationId])

  // the render before the reset above still holds the last conversation's: none of it
  // shows (nor asks the server) under this one's id
  const shown = state.cid === conversationId ? state : EMPTY
  // Stop shows at once: the turn reads as stopped until the server confirms it
  const stopped = !!shown.turn && shown.stopping === shown.turn.id && RUNNING.includes(shown.turn.state)
  const turn: Turn | null = stopped
    ? { ...shown.turn!, state: 'aborted', reason: 'stopped', reason_code: 'stopped', continuable: null, live: null }
    : shown.turn
  const running = turn ? RUNNING.includes(turn.state) : false
  const markStopping = (on: boolean) => setState(s => ({ ...s, stopping: on ? s.turn?.id ?? null : null }))
  return {
    ...shown, turn, running, connection, markStopping,
    live: running ? shown.live : null,
    waiting: running ? queueWait(turn, shown.live) : null,
    connected: connection === 'connected',
    /** closed for more than a moment: Send and Regenerate wait */
    reconnecting: isDown(connection),
  }
}

/** text events in a row for the same message and kind joined into one */
type TextFrame = Extract<Frame, { type: 'message.delta' | 'message.reasoning' }>
function joinText(evs: TextFrame[]): TextFrame[] {
  const out: TextFrame[] = []
  for (const ev of evs) {
    const prev = out.at(-1)
    if (prev && prev.type === ev.type && prev.message_id === ev.message_id) out[out.length - 1] = { ...prev, text: prev.text + ev.text }
    else out.push(ev)
  }
  return out
}

function fromSnapshot(s: State, snap: Snapshot): State {
  return { ...s, conversation: snap.conversation, forming: [], messages: snap.messages, toolCalls: snap.tool_calls, compactions: snap.compactions,
           turn: snap.turn, queue: snap.queue, live: snap.turn?.live ?? null, switched: snap.switched,
           imageNotices: snap.image_notices, banners: snap.guard, plan: snap.plan }
}

function apply(s: State, ev: Frame): State {
  switch (ev.type) {
    case 'snapshot':
      return { ...fromSnapshot(s, ev), error: null }
    case 'images.notice':
      return { ...s, imageNotices: [...s.imageNotices,
        { after_message_id: ev.after_message_id, model: ev.model, describer: ev.describer ?? null }] }
    case 'guard.banner': {
      const b = ev.banner as GuardBanner
      const at = s.banners.findIndex(x => x.id === b.id)
      return { ...s, banners: at < 0 ? [...s.banners, b] : s.banners.map((x, i) => (i === at ? b : x)) }
    }
    case 'attachment.described':
      return {
        ...s,
        messages: s.messages.map(m => m.id !== ev.message_id ? m : {
          ...m, attachments: (m.attachments ?? []).map(a => a.id !== ev.attachment_id ? a : {
            ...a, description: ev.description, described_by: ev.described_by, described_for: ev.described_for }) }),
      }
    // a file joined a message already there (one it sent you from its box)
    case 'message.attachments':
      return { ...s, messages: s.messages.map(m => m.id === ev.message_id ? { ...m, attachments: ev.attachments ?? [] } : m) }
    case 'message.created':
      if (s.messages.some(m => m.id === ev.message_id)) return s
      return {
        ...s,
        error: null,
        live: ev.role === 'assistant' ? { message_id: ev.message_id, phase: 'thinking' } : s.live,
        messages: [...s.messages, {
          id: ev.message_id, parent_id: ev.parent_id, role: ev.role,
          content: ev.content ?? '', ordinal: ev.ordinal ?? 0, author: ev.author,
          intercom: ev.intercom ?? null, siblings: 1, created_at: ev.ts,
          attachments: ev.attachments ?? [], meta: ev.meta,
        }],
      }
    case 'message.reopened':
      return { ...s, live: { message_id: ev.message_id, phase: 'thinking' } }
    case 'message.delta':
      return {
        ...s,
        live: s.live?.message_id === ev.message_id ? { message_id: ev.message_id, phase: 'flowing' } : s.live,
        messages: s.messages.map(m =>
          m.id === ev.message_id ? { ...m, content: m.content + ev.text } : m),
      }
    case 'message.reasoning':
      return {
        ...s,
        live: s.live?.message_id === ev.message_id ? { message_id: ev.message_id, phase: 'flowing' } : s.live,
        messages: s.messages.map(m =>
          m.id === ev.message_id ? { ...m, reasoning: (m.reasoning ?? '') + ev.text } : m),
      }
    case 'message.done':
      return {
        ...s,
        live: s.live?.message_id === ev.message_id ? { message_id: ev.message_id, phase: 'done' } : s.live,
        messages: s.messages.map(m =>
          m.id === ev.message_id ? { ...m, content: ev.text } : m),
      }
    // the text changed after the reply ended (citations checked, links made): the phase stays
    case 'message.rewritten':
      return { ...s, messages: s.messages.map(m => m.id === ev.message_id ? { ...m, content: ev.text } : m) }
    case 'conversation.notes_off':
      return s.conversation ? { ...s, conversation: { ...s.conversation, notes_off: ev.notes_off ?? [] } } : s
    case 'tool.forming': {
      const f: Forming = { message_id: ev.message_id, index: ev.index, name: ev.name, chars: ev.chars, preview: ev.preview, words: ev.words }
      return { ...s, forming: [...s.forming.filter(x => !(x.message_id === f.message_id && x.index === f.index)), f] }
    }
    case 'tool.started': {
      s = { ...s, forming: [] }
      const owner: string = ev.message_id
      const list = s.toolCalls[owner] ?? []
      if (list.some(c => c.id === ev.tool_call_id)) {
        // stamped again when the step itself began (a sequential batch waits)
        return { ...s, toolCalls: { ...s.toolCalls, [owner]: list.map(c =>
          c.id === ev.tool_call_id && ev.started_at ? { ...c, started_at: ev.started_at } : c) } }
      }
      return { ...s, toolCalls: { ...s.toolCalls, [owner]: [...list, {
        id: ev.tool_call_id, name: ev.name, arguments: ev.arguments,
        state: 'running', display: null, words: ev.words, started_at: ev.started_at ?? ev.ts ?? null,
      }] } }
    }
    case 'tool.done':
    case 'tool.gated':
      return mapCalls(s, c => c.id === ev.tool_call_id, c => ({
        ...c, state: ev.state ?? (ev.ok ? 'ok' : 'error'),
        display: ev.display ?? null, words: ev.words ?? c.words, result: ev.result ?? null, job_id: ev.job_id ?? c.job_id ?? null,
        finished_at: ev.finished_at ?? null }))
    // promoted to a job: the row shows Stop now, not when the placeholder tool.done lands
    case 'tool.detached':
      return mapCalls(s, c => c.id === ev.tool_call_id, c => ({
        ...c, state: 'detached' as const, job_id: ev.job_id,
        display: asBatch(c.display) ?? { job_id: ev.job_id, tool: ev.name } }))
    // a batch changed: every call that shows it (the one that started it, a read, a stop)
    // shows it as the server has it now
    case 'images.batch':
    case 'music.batch': {
      const b = asBatch(ev.batch)
      if (!b) return s
      return mapCalls(s, c => c.id === b.tool_call_id || asBatch(c.display)?.id === b.id, c => ({ ...c, display: b }))
    }
    case 'tool.progress':
      return mapCalls(s, c => c.id === ev.tool_call_id, c => ({ ...c, progress: { note: ev.note } }))
    case 'llm.queue': {
      // only calls that write the reply (a citation check or a summary has no message)
      if (!ev.message_id || s.live?.message_id !== ev.message_id) return s
      const mid: string = ev.message_id
      const switched = ev.state === 'running' && ev.fallback && ev.instead_of
        ? { ...s.switched, [mid]: { model: ev.model, instead_of: ev.instead_of, why: ev.why ?? null } } : s.switched
      const live: Live | null = ev.state === 'waiting'
        ? { message_id: mid, phase: 'waiting', ahead: ev.ahead ?? 0, running: ev.running ?? 0, model: ev.model,
            reason: ev.reason ?? null, message: ev.message ?? null, holder: ev.holder ?? null }
        : ev.state === 'loading' ? { message_id: mid, phase: 'loading', model: ev.model }
        : ev.state === 'running' ? { message_id: mid, phase: 'thinking' }
        : s.live
      return { ...s, live, switched }
    }
    case 'turn.state': {
      const terminal = !RUNNING.includes(ev.state as TurnState)
      // belt and braces: a finished turn leaves no call spinning
      const toolCalls = terminal ? mapCalls(s, c => c.state === 'running', c => ({ ...c, state: 'interrupted' as const })).toolCalls
        : s.toolCalls
      const queue = terminal ? s.queue.filter(q => q.turn_id !== ev.turn_id)
        : s.queue.some(q => q.turn_id === ev.turn_id) ? s.queue : [...s.queue, { turn_id: ev.turn_id }]
      const told: Turn = { id: ev.turn_id, state: ev.state as TurnState, reason: ev.reason ?? null,
                           reason_code: ev.reason_code ?? null, cut: ev.cut ?? null,
                           continues: ev.continues, continuable: ev.continuable ?? null }
      // the page follows the first live turn: one that ended while another waits hands over
      // to it, one queued behind the running one leaves that one shown
      const first = queue[0]
      const turn: Turn = !first || first.turn_id === ev.turn_id ? told
        : s.turn && s.turn.id === first.turn_id ? s.turn
        : { id: first.turn_id, state: 'queued', reason: null }
      return { ...s, toolCalls, queue, turn, error: terminal ? s.error : null, live: terminal ? null : s.live,
               stopping: terminal ? null : s.stopping, forming: terminal ? [] : s.forming }
    }
    case 'tool.reset':
      return mapCalls(s, c => c.id === ev.tool_call_id, c => ({ ...c, state: 'running' as const }))
    case 'context.compacted': {
      const c = ev as Compaction
      const compactions = s.compactions.some(x => x.id === c.id) ? s.compactions : [...s.compactions, c]
      return { ...s, compactions, compactOutcome: ev.trigger === 'manual' ? { kind: 'compacted', seq: ev.seq ?? 0 } : s.compactOutcome }
    }
    case 'context.compact_failed':
      return ev.trigger === 'manual' ? { ...s, compactOutcome: { kind: 'failed', seq: ev.seq ?? 0 } } : s
    case 'context.compact_skipped':
      return { ...s, compactOutcome: { kind: 'skipped', reason: ev.reason, seq: ev.seq ?? 0 } }
    case 'plan.state':
      return { ...s, plan: ev.plan as Plan }
    case 'conversation.settings': {
      const { type: _t, seq: _s, ...view } = ev
      return { ...s, settings: view as ConversationSettings }
    }
    case 'error':
      if (ev.code === 'not_found' && ev.seq === undefined) return s     // gone: the list says so
      return { ...s, error: { message: ev.message ?? '', retryable: !!ev.retryable } }
    default:
      return s
  }
}
