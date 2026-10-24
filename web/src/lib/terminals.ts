/* Terminals: the api client, the row types, the one set of state words and the chip
   rules the Terminals page, the terminal page and the mission bar share
   (docs/ARCHITECTURE.md). */
import { del, json, post, query, request, requestBytes } from './api'

/** what the daemon says about an open terminal, null once ended or while the shellbox
 *  is unreachable */
export type Live = {
  shell: string | null
  cwd: string | null
  program: string | null
  pid: number | null
  busy: boolean
  paused: boolean
  fullscreen: boolean
  waiting: boolean
  running_for: number | null
  quiet_for: number | null
  last_exit: number | null
}

export type TerminalRow = {
  id: string
  name: string
  /** 'celine', or the user's slug: the page derives yours from it */
  owner: string
  may_type: boolean
  opened_at: string
  ended_at: string | null
  /** 'exit', 'user', 'reset', 'gone', 'done' or 'idle' */
  ended_by: string | null
  exit: number | null
  live: Live | null
  /** the conversation that opened it, and which kind it is; null for the user's own */
  conversation: { id: string; title: string; agent_of: string | null; kind: 'conversation' | 'mission' | 'sub_agent' } | null
}

/** how a command went: its own from its result, a line of yours settled by the server (null on a control) */
type Outcome = {
  state: string | null
  exit: number | null
  seconds: number | null
  running_for: number | null
  quiet_for: number | null
  program: string | null
  paused: boolean | null
}

type FromKind = 'conversation' | 'mission' | 'sub_agent' | 'you'

export type CommandRow = {
  id: string
  /** command and read are its own; line and control are yours */
  kind: 'command' | 'read' | 'line' | 'control'
  at: string
  terminal: { id: string | null; name: string }
  from: { kind: FromKind; conversation_id: string | null; title: string | null }
  text: string
  keys: string
  outcome: Outcome | null
  /** its complete result, or the short word of a control (`cmake paused`) */
  result: string | null
  /** the sentence Celine reads after a control on its terminal */
  note: string | null
  job_id: string | null
}

export type From = 'all' | 'conversations' | 'missions' | 'sub_agents' | 'you'
export type ResultFilter = 'all' | 'failed' | 'in_progress'
export type Action = 'stop' | 'pause' | 'resume' | 'close'

type CommandsQuery = {
  terminal?: string
  from?: From
  result?: ResultFilter
  q?: string
  mission?: string
  limit?: number
  before?: string
}


export const terminalsApi = {
  /** with `trashed` the trashed ones instead */
  list: (mission?: string, trashed = false) =>
    request<{ terminals: TerminalRow[]; down: string | null }>(`/terminals${query({ mission, trashed })}`),
  /** a new terminal of your own */
  create: () => refusable(post<{ id: string; name: string; refused?: string }>('/terminals')),
  get: (id: string) => request<TerminalRow>(`/terminals/${id}`),
  /** the terminal's whole history, its raw bytes as it printed them */
  log: (id: string) => requestBytes(`/terminals/${id}/log`),
  mayType: (id: string, may_type: boolean) =>
    request<{ ok: boolean }>(`/terminals/${id}`, json('PATCH', { may_type })),
  control: (id: string, action: Action) =>
    refusable(request<{ ok: boolean; note: string | null; refused?: string }>(`/terminals/${id}/control`,
      json('POST', { action }))),
  /** every ended terminal into the trash */
  trashEnded: () => del<{ trashed: number }>('/terminals?state=ended'),
  /** an ended one into the trash; its history stays */
  trash: (id: string) => del<{ ok: boolean }>(`/terminals/${id}`),
  restore: (id: string) => post<{ ok: boolean }>(`/terminals/${id}/restore`),
  /** a trashed one for good, its history too */
  remove: (id: string) => del<{ ok: boolean }>(`/terminals/${id}?for_good=1`),
  commands: (q: CommandsQuery) =>
    request<{ commands: CommandRow[]; total: number; next: string | null }>(`/terminal-commands${query(q)}`),
  /** where a message can go, by the names the server gives: the terminal's conversation first */
  targets: (id: string) => request<{ targets: { id: string; name: string }[] }>(`/terminals/${id}/targets`),
  /** your text into a conversation, with a link to the terminal or the quoted command */
  message: (id: string, body: { text: string; conversation_id?: string; command_id?: string }) =>
    request<{ conversation_id: string; title: string }>(`/terminals/${id}/messages`,
      json('POST', body)),
}

/* ── state words: one set everywhere ── */

type StateWord = 'Running' | 'Idle' | 'Waiting for you' | 'Paused' | 'Ended'
export type Tone = 'ok' | 'error' | 'warn' | 'accent' | 'running' | 'muted' | 'you' | 'off'

export const isYours = (t: TerminalRow) => t.owner !== 'celine'

export function stateWord(t: TerminalRow): StateWord {
  const l = t.live
  if (t.ended_at) return 'Ended'
  if (!l) return 'Idle'
  if (l.paused) return 'Paused'
  if (l.program && l.waiting && !l.fullscreen) return 'Waiting for you'
  if (l.busy || l.fullscreen) return 'Running'
  return 'Idle'
}

/** the colour of a state word: the dot in the list and on the page */
export function stateTone(word: StateWord): Tone {
  switch (word) {
    case 'Running': return 'running'
    case 'Idle': return 'ok'
    case 'Waiting for you': return 'warn'
    case 'Paused': return 'you'
    case 'Ended': return 'off'
  }
}

/** words side by side with a wide gap, never a dot: `cmake  exit 1` */
export function spaced(...parts: (string | null | undefined | false)[]): string {
  return parts.filter(Boolean).join('\u00a0\u00a0')
}

/** the words after the state: what runs (`cmake`), or an exit only when it failed */
export function stateDetail(t: TerminalRow): string {
  if (t.ended_at) return t.exit ? `exit ${t.exit}` : ''
  const l = t.live
  if (!l) return ''
  if (stateWord(t) === 'Idle') return l.last_exit ? `exit ${l.last_exit}` : ''
  return l.program ?? ''
}

/* ── command rows ── */

/** the command as a row shows it: the first line typed, the keys, `read`, or `watch` */
export function commandWords(c: CommandRow): string {
  if (c.kind === 'read') return 'read'
  if (c.kind === 'line' || c.kind === 'control') return c.text
  const text = c.text.split('\n').find(l => l.trim())?.trim() ?? ''
  return keysAndText(c.keys, text) || 'watch'
}

/** what a step sent: `keys: K  text: T`, either alone, or '' (it only watched) */
export function keysAndText(keys: string | null | undefined, text: string): string {
  if (keys && text) return `keys: ${keys}  text: ${text}`
  if (keys) return `keys: ${keys}`
  return text
}

/** a log row's result: Running, Done, or Failed with its exit (the server settles a call
 *  that returned while its program ran); a control of yours says its short word, a line
 *  nothing */
export function outcomeWords(c: CommandRow): { words: string; tone: Tone } {
  const o = c.outcome
  if (!o) return { words: c.kind === 'control' ? c.result ?? '' : '', tone: 'muted' }
  const failed = (exit: number | null) => ({ words: exit ? `Failed, exit ${exit}` : 'Failed', tone: 'error' as const })
  switch (o.state) {
    case 'pending': case 'running': case 'detached': return { words: 'Running', tone: 'running' }
    case 'error': case 'gated': return failed(o.exit)
    default: return o.exit && c.kind !== 'read' ? failed(o.exit) : { words: 'Done', tone: 'ok' }
  }
}

/** the text a row opens to: its result, a control's note */
export function rowText(c: CommandRow): string {
  if (c.kind === 'line') return ''
  if (c.kind === 'control') return c.note ?? c.result ?? ''
  return c.result ?? ''
}

/** the server says no as a 200 with its sentence in `refused`; the page shows it as an error */
async function refusable<T extends { refused?: string }>(p: Promise<T>): Promise<T> {
  const out = await p
  if (out.refused) throw new Error(out.refused)
  return out
}

/** a shell tool call started, ended or went on in the background */
export const isShellEvent = (ev: { type: string; name?: unknown }) =>
  (ev.type === 'tool.started' || ev.type === 'tool.done' || ev.type === 'tool.detached') &&
  typeof ev.name === 'string' && ev.name.startsWith('shell__')
