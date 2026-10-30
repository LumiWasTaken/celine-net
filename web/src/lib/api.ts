import type { GuardBanner } from './guard'
import type { Her } from './her'
import { reportApi } from './health'
import type { ChatApproval, ConversationSettings, Feature } from './settingsApi'
import type { Kind } from './intercom'
import type { BulkResult } from '../ui'

type Problem = { file: string; message: string; fatal: boolean }

/** GET /status: it, your name, the config's problems, each model host as the
 *  gateway's ping sees it, and its hold (paused while no chat host answers; event her.hold) */
export type ConfigStatus = {
  owner: Her
  user: string
  problems: Problem[]
  hosts: Record<string, { state: 'up' | 'down' | null; since: number | null }>
  hold: { state: 'paused'; since: number } | null
  /** every feature's on/off (server/celine/settings features); a place or page of a feature
   *  that is off is left out of the rail and Settings */
  features: Record<string, boolean>
  /** the feature catalog: what each feature owns and says about itself */
  catalog: Feature[]
  /** the onboarding finished (the features were saved once) */
  setup: boolean
  /** this build: its commit, when it was built (null: unknown) and its code (the repository at that commit) */
  build: { commit: string; built: string | null; source: string }
  /** the vault is set up and locked */
  vault_locked: boolean
}

/** 'off', or a level the model accepts (from the server: `thinking_levels`), or any value typed by hand */
export type Thinking = string

/** what a new conversation starts with; null / missing = the default */
export type NewConversationSettings = {
  model?: string | null
  thinking?: Thinking | null
  /** tool or skill → on/off, only where it differs from the default */
  tools?: Record<string, boolean>
  /** false: Bastet skips this chat */
  guard?: boolean
}

export type ConversationSummary = {
  id: string
  title: string
  pinned: number
  trigger_key: string | null
  /** the conversation that started this one as its sub-agent */
  agent_of: string | null
  updated_at: string
  seq: number
  active_state: string | null
  /** what it is, fixed at creation: a chat, a mission, a sub-agent (the list has
   *  background tasks and runs nowhere: GET /background) */
  kind: 'chat' | 'mission' | 'agent' | 'background'
  /** the first line of the newest message's text, its own or yours, at most 120 characters
   *  ('' when it has none): the row's preview and its greeting */
  last_line: string
  /** a sub-agent's: read (read-only tools) or work */
  access: 'read' | 'work' | null
  /** out of the list, under Archived */
  archived_at: string | null
  /** on a mission's conversation: the mission's name and its status (active, needs you,
   *  done, stopped) */
  mission_name?: string | null
  mission_status?: string | null
  /** its sub-agents, oldest first (the list has no rows of their own for them) */
  agents: ConversationSummary[]
  /** GET /conversations: the server's group label for the row ("Running", "Pinned", "Today", "9 Oct") */
  group?: string
  /** GET /conversations: its turn in it, running with the server's step words ('' while it
      writes), waiting for you, or how the last turn ended when it failed or paused */
  state?: { tone: 'running' | 'error' | 'warn'; words: string } | null
  /** GET /conversations: where it lives and the path that opens it, the server's (homes.py) */
  home?: Home
  link?: string
}

/** where a conversation lives: with it, under Missions or under Routines (homes.py) */
export type Home = 'her' | 'missions' | 'routines'

/** a page of a list; `next` goes back as `cursor` for the following one (null: the end) */
type Page<K extends string, T> = { [k in K]: T[] } & { next: string | null }

export class ApiError extends Error {
  constructor(readonly code: string, message: string, readonly status: number,
              /** the server's `error.detail` (its values are strings) */
              readonly detail: Record<string, string> = {}) {
    super(message)
  }
}

const OFFLINE = 'No connection to the server.'

/** "?a=1&b=x", leaving out what is unset ('', false, null, undefined); '' when nothing is set */
export function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const s = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '' && v !== false) s.set(k, String(v))
  const str = s.toString()
  return str ? `?${str}` : ''
}

/** a request's init with a JSON body */
export const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

/* Every call goes through here. A reply that is not JSON (a proxy's HTML error page,
   an empty 502) becomes a sentence with the status, never a SyntaxError. A FormData
   body is sent as multipart. A request made while the page goes away (a setting saved on
   leaving, lib/useAutosave.ts) is kept alive past it. */
let leaving = false
addEventListener('pagehide', () => { leaving = true })
addEventListener('pageshow', () => { leaving = false })

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  const form = init?.body instanceof FormData
  try {
    res = await fetch(`/api${path}`, {
      ...init,
      keepalive: leaving && !form,
      headers: form ? init?.headers : { 'Content-Type': 'application/json', ...init?.headers },
    })
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e
    return settle(0, '')
  }
  return settle(res.status, await res.text().catch(() => ''))
}

/* An answer that streams as lines of JSON (POST /text-drafts): each line to `onLine` as it
   lands. Before the first, the same errors as request(); a stream cut midway throws. */
export async function requestLines(path: string, init: RequestInit,
                                   onLine: (line: unknown) => void): Promise<void> {
  let res: Response
  try {
    res = await fetch(`/api${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init.headers } })
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e
    return settle(0, '')
  }
  if (!res.ok || !res.body) return settle(res.status, await res.text().catch(() => ''))
  reportApi(res.status)
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let rest = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    const lines = (rest + value).split('\n')
    rest = lines.pop() ?? ''
    for (const line of lines) if (line.trim()) onLine(JSON.parse(line))
  }
  if (rest.trim()) onLine(JSON.parse(rest))
}

/** a POST or a DELETE with no body */
export const post = <T>(path: string) => request<T>(path, { method: 'POST' })
export const del = <T>(path: string) => request<T>(path, { method: 'DELETE' })

/* An answer as it is (not JSON), with the same errors as request(). `url` is a page
   address: '/api/...' or a picture's src. */
async function fetchRaw(url: string, init?: RequestInit): Promise<Response> {
  let res: Response
  try { res = await fetch(url, init) } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e
    return settle(0, '')
  }
  if (!res.ok) return settle(res.status, await res.text().catch(() => ''))
  return res
}

/** a file's text */
export const requestText = (path: string) => fetchRaw(`/api${path}`).then(r => r.text())
/** a file's raw bytes */
export const requestBytes = (path: string) => fetchRaw(`/api${path}`).then(r => r.arrayBuffer()).then(b => new Uint8Array(b))
export const requestBlob = (path: string, init: RequestInit) => fetchRaw(`/api${path}`, init).then(r => r.blob())

/* A multipart upload with progress (0 to 1) as it goes. Same errors as request(). */
export function upload<T>(path: string, form: FormData, onProgress?: (share: number) => void): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `/api${path}`)
    if (onProgress) xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded / e.total) }
    const done = (status: number, text: string) => settle<T>(status, text).then(resolve, reject)
    xhr.onload = () => void done(xhr.status, xhr.responseText)
    xhr.onerror = () => void done(0, '')
    xhr.send(form)
  })
}

async function settle<T>(status: number, text: string): Promise<T> {
  reportApi(status)
  if (status === 0) throw new ApiError('offline', OFFLINE, 0)
  const body = parseBody(text)
  if (status < 200 || status >= 300) {
    const e = (body && typeof body === 'object' ? (body as { error?: { code?: string; message?: string; detail?: Record<string, string> } }).error : null) ?? {}
    throw new ApiError(e.code ?? 'http_error', e.message ?? httpMessage(status), status, e.detail ?? {})
  }
  if (body === BAD) throw new ApiError('bad_response', `The server sent a response that is not JSON (HTTP ${status}).`, status)
  return body as T
}

const BAD = Symbol('bad json')
function parseBody(text: string): unknown {
  if (!text) return {}
  try { return JSON.parse(text) } catch { return BAD }
}

function httpMessage(status: number): string {
  if (status === 502 || status === 503 || status === 504) return `The server didn't answer in time (HTTP ${status}). Retry in a moment.`
  if (status === 404) return 'Not found (HTTP 404).'
  return `The server didn't respond as expected (HTTP ${status}).`
}

/** `login`: passkeys are on (a deployment setting), off for local runs; `methods`: the kinds of
    credential that exist; `setup`: none yet (the login page asks the setup code) */
export type AuthStatus = { login: boolean; methods: ('passkey' | 'password' | 'pin')[]; setup: boolean }

export const authApi = {
  status: () => request<AuthStatus>('/auth/status'),
}

/** POST /bulk-actions: one action on many, each through its own route (server/celine/api/bulk.py);
    what a rule refused comes back in `skipped` with its sentence */
export type BulkWhat = 'conversations' | 'documents' | 'folders' | 'notes' | 'chats' | 'terminals'
export type BulkDo = 'archive' | 'unarchive' | 'trash' | 'restore' | 'purge'
/** the kinds an "Archive older than" takes */
export type OlderKind = 'chat' | 'mission'
export const bulkApi = {
  run: (what: BulkWhat, action: BulkDo, ids: string[]) =>
    request<BulkResult>('/bulk-actions', json('POST', { what, action, ids })),
  /** the ones of `kind` not changed for `days` days, nothing done */
  older: (kind: OlderKind, days: number) =>
    request<{ ids: string[] }>('/bulk-actions', json('POST', { what: 'conversations', action: 'archive', kind, older_than: days, dry_run: true })),
  archiveOlder: (kind: OlderKind, days: number) =>
    request<BulkResult>('/bulk-actions', json('POST', { what: 'conversations', action: 'archive', kind, older_than: days })),
}

export const api = {
  configStatus: () => request<ConfigStatus>('/status'),
  /** every conversation (or every archived one, or every deleted one), pinned first, then
      the most recently updated (the server pages them) */
  listConversations: async (which?: 'archived' | 'trashed') => {
    const all: ConversationSummary[] = []
    let cursor: string | null = null
    do {
      const page: Page<'conversations', ConversationSummary> = await request(
        `/conversations?limit=500${which ? `&${which}=true` : ''}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`)
      all.push(...page.conversations)
      cursor = page.next
    } while (cursor)
    return all
  },
  /** the same snapshot as the feed's `llm.queue` event */
  llmQueue: () => request<QueueSnapshot>('/llm-queue'),
  /** one conversation's background jobs, running first, then the newest */
  jobs: (conversation: string, limit: number) =>
    request<Page<'jobs', Job>>(`/jobs?conversation=${encodeURIComponent(conversation)}&limit=${limit}`),
  cancelJob: (id: string) => post<{ cancelled: boolean }>(`/jobs/${id}/stop`),
  /** what a conversation's model can do, or before one exists the `model` it will use */
  chatModel: (conversation: string | null, model?: string | null) => {
    const q = new URLSearchParams()
    if (conversation) q.set('conversation', conversation)
    if (model) q.set('model', model)
    return request<ChatModel>(`/chat-model?${q}`)
  },
  /** words inside Celine's conversations (not the archive: that is kbApi.search) */
  search: (q: string) => request<{ hits: ConversationHit[] }>(`/messages?q=${encodeURIComponent(q)}`),
  renameConversation: (id: string, title: string) =>
    request<{ ok: boolean }>(`/conversations/${id}`, json('PATCH', { title })),
  pinConversation: (id: string, pinned: boolean) =>
    request<{ ok: boolean }>(`/conversations/${id}`, json('PATCH', { pinned })),
  /** out of the list with its sub-agents, every message kept; a message to it brings it back */
  archiveConversation: (id: string, archived: boolean) =>
    post<{ archived_at: string | null }>(`/conversations/${id}/${archived ? 'archive' : 'unarchive'}`),
  /** trashed with its sub-agents, every message kept; `restoreConversation` brings it back */
  deleteConversation: (id: string) =>
    del<{ ok: boolean }>(`/conversations/${id}`),
  restoreConversation: (id: string) =>
    post<{ ok: boolean }>(`/conversations/${id}/restore`),
  /** a picture on screen (its address) becomes its portrait */
  setPortrait: async (src: string) => {
    const form = new FormData()
    form.append('image', await (await fetchRaw(src)).blob(), 'portrait')
    return request<{ portrait: { url: string } | null }>('/agent/portrait', { method: 'PUT', body: form })
  },
  /** a trashed one gone for good, with its sub-agents and every row tied to them */
  purgeConversation: (id: string) =>
    del<{ ok: boolean }>(`/conversations/${id}?for_good=1`),
  /** a chat; `settings` are the choices made before the first message (model,
      thinking, tools). The server checks them all before it creates anything (a bad
      value is a 400) */
  createConversation: (settings?: NewConversationSettings) =>
    request<{ id: string; settings?: ConversationSettings }>('/conversations', json('POST', settings ?? {})),
}

type Role = 'user' | 'assistant' | 'tool' | 'system'

export type Attachment = {
  id: string
  filename: string
  kind: 'image' | 'audio' | 'text' | 'binary'
  sha256: string
  bytes: number
  media_type: string
  /** an image a model that can't see images got as text: the photo description, the
   *  model that wrote it and the model it was for (the note on the message) */
  description?: string | null
  described_by?: string | null
  described_for?: string | null
  /** a picture's size, so its box is there before it loads */
  width?: number | null
  height?: number | null
  /** the batch that made it: its gallery shows it */
  batch_id?: string | null
}

/** what the chat model can do now (GET /chat-model): `tools`, whether it takes tools at
 *  all; `blocked` when it can't see images and no Photo descriptions model answers */
export type ChatModel = { model: string; tools: boolean; sees: boolean; describer: string | null; blocked: boolean }

/** the line after a switch to a model that can't see images (event `images.notice`):
 *  `describer` writes the descriptions of the earlier images, or null when none can */
export type ImageNotice = { after_message_id: string; model: string; describer: string | null }

export type Message = {
  id: string
  parent_id: string | null
  role: Role
  content: string
  ordinal: number
  siblings: number
  created_at: string
  attachments?: Attachment[]
  reasoning?: string | null
  /** who wrote it: you as you typed it ('user'), it ('model'), or anything else that
   *  reached the conversation ('intercom': read `intercom` for who and what) */
  author?: 'user' | 'model' | 'intercom'
  /** on every user-role message: the intercom message it was delivered from */
  intercom?: Intercom | null
  /** its reply kept as a sample of its voice ("More like this") */
  sample?: boolean
  /** `quiet`: a reply of a turn that shows only its tool calls */
  meta: { quiet: boolean }
}

/** One intercom message: from `user`, `agent:<conversation id>` or `system:<name>`;
 *  `kind` says how it is headed. `from_name` is the sender's name as people read it,
 *  `mine` that the conversation showing it sent it itself. `due_at`: it was set
 *  earlier to land then (a reminder). */
export type Intercom = {
  id: number
  kind: Kind
  from: string
  to: string
  subject: string | null
  created_at: string
  due_at: string | null
  reply_to: number | null
  thread: number
  data: { mission?: string; changed?: boolean; time_limit?: boolean; via?: string; jobs?: string[] } & Record<string, unknown>
  from_name: string | null
  mine: boolean
  /** it starts a turn where it lands; false: it lands quietly and waits for no answer */
  wake: boolean
}

/** A background job, one shape everywhere (GET /jobs, GET /jobs/{id}). */
export type Job = {
  id: string
  conversation_id: string
  tool_name: string
  state: 'running' | 'done' | 'failed' | 'cancelled' | 'timeout' | 'interrupted'
  result: string | null
  job_message_id: string | null
  /** when the step was promoted to a job, `detach_after_seconds` in */
  created_at: string
  finished_at: string | null
  /** the call it ran: its id, the assistant message that holds it, when it was recorded */
  tool_call_id: string | null
  message_id: string | null
  started_at: string | null
  /** the call's result payload once the job settled (an image or music job: its batch); null while it runs */
  display: Record<string, unknown> | null
  /** the latest progress line while it runs */
  progress: string | null
  /** what the call was asked, when its arguments say so in one field (an image's prompt, an edit's instruction) */
  prompt: string | null
  /** on a stopped job: who stopped it. 'user' (the row or the panel), 'celine' (its stop tool), 'turn' (Stop on the reply) */
  stopped_by: 'user' | 'celine' | 'turn' | null
}

/** the LLM queue: what runs and waits on the models right now */
type QueueEntry = { turn_id: string | null; role: string; priority: 'interactive' | 'agent' | 'background'; model: string }
type QueueSnapshot = { running: QueueEntry[]; waiting: QueueEntry[] }

type ConversationHit = {
  conversation_id: string
  title: string
  role: Role
  created_at: string
  snippet: string
  /** the path that opens its conversation where it lives, the server's (homes.py) */
  link: string
}

export type TurnState =
  | 'queued' | 'preparing' | 'streaming' | 'tools'
  | 'done' | 'aborted' | 'failed' | 'interrupted' | 'capped'

/** whether a stopped or failed answer can be continued (docs/ARCHITECTURE.md "Continue") */
type Continuable = { ok: boolean; label: string | null; reason: string | null }
export type Turn = { id: string; state: TurnState; reason: string | null; reason_code?: string | null; cut?: string | null
  continues?: number; continuable?: Continuable | null
  /** a running turn's reply: waiting for a slot, loading the model, thinking, flowing or done */
  live?: Live | null }

/** the user's last message that has no answer while no turn runs (the server's): the
 *  turn that tried and how it ended (null when none did), why, in a sentence, and whether
 *  Continue picks up the words its reply said before it failed */
export type Unanswered = { message_id: string; turn_id: string | null; state: TurnState | null; reason: string; continues: boolean }

/** where the reply being written stands (the snapshot's `turn.live`, then the live events) */
export type Live = { message_id: string; phase: 'waiting' | 'loading' | 'thinking' | 'flowing' | 'done'
  ahead?: number; running?: number; model?: string
  /** waiting: the request ahead on its model, by name ("Self-review") */
  holder?: string | null
  /** 'no_model': no chat model can answer now; 'gpu_lease': an image generation holds
   *  the model's GPU; 'busy': the provider is busy (429, 503) and the model is asked
   *  again. `message` says it in words */
  reason?: string | null; message?: string | null }

/** a reply another model in the chain wrote, because the chosen one failed */
export type Switched = { model: string; instead_of: string; why: string | null }

/** GET /skills?conversation=: each skill and tool as one conversation has it */
export type SkillInfo = {
  name: string
  label: string
  summary: string
  description: string
  enabled_globally: boolean
  status: 'up' | 'starting' | 'down' | 'dead' | 'unset' | 'unconnected'
  error: string | null
  tools: { name: string; wire_name: string; label: string; summary: string; description: string
           mutating: boolean
           /** on in this conversation */
           enabled: boolean
           /** on by default (Settings) */
           default: boolean
           /** "here": this conversation's switch decides it */
           why: 'here' | 'default' }[]
}

/** one tool call of Celine's, in the snapshot and the tool.* events */
export type ToolCall = {
  id: string
  name: string
  arguments: string
  state: 'pending' | 'running' | 'ok' | 'error' | 'gated' | 'cancelled' | 'detached' | 'interrupted'
  /** the result's payload for the row, per tool. A shell call (shell__run, shell__wait,
   *  shell__type, shell__stop, shell__read): `terminal` (its name), `terminal_id` (the six characters
   *  of its address, the link), `owner`, `state` (a run: done, running, waiting, closed,
   *  not_started; a screen: prompt, running, waiting, fullscreen, closed; error),
   *  `program`, `pid`, `exit` (a run's, null until it ended), `seconds` (a run's real
   *  duration), `running_for` and `quiet_for` (seconds, while a program runs or waits),
   *  `paused`, `opened` (a new terminal), `cmd` and `log` (the run's id and log), `text`
   *  (the command, or the first line typed), `keys`, `read` (a shell__read); a listing
   *  carries `terminals` instead */
  display: Record<string, unknown> | null
  /** the row in words, the server's (skills/words.py) */
  words: ToolWords
  /** the complete result, as Celine got it (null while it runs) */
  result?: string | null
  /** the latest progress line while it runs (event tool.progress) */
  progress?: ToolProgress | null
  /** when it was recorded, for the elapsed time of a call still at work */
  started_at?: string | null
  /** when it ended, for the time a browser row shows */
  finished_at?: string | null
  /** the job it was handed to, once it outlived the detach window */
  job_id?: string | null
}

/** a call in words: `running` while it is at work ("Reading the note x"), `settled` once
 *  it ended ("Read the note x", "Could not read the note x", "Not run: ..."), `step` the
 *  tool alone at work ("Reading the note"), `where` the places a search was kept to */
export type ToolWords = { running: string; settled: string; step: string; where: string | null }

/** calls alike in a row, shown as one row (skills/words.py runs): its calls, its words
 *  ("Ran 8 commands in terminal main"), how many of them failed */
export type ToolRun = { ids: string[]; words: string; failed: number }
/** a turn's runs as a tool event carries them, all of them each time */
export type TurnRuns = { turn: string; list: ToolRun[] }

/** what a running tool says it is doing */
type ToolProgress = {
  note: string
}

/** older messages summarised for Celine (the snapshot's `compactions`, `context.compacted`) */
export type Compaction = {
  id: string; trigger: string; through_message_id: string; covers_messages: number
  text: string; tokens_before: number | null; tokens_after: number | null; focus: string | null; created_at: string
}

/** a sub-agent as the snapshot lists it (sub_agents agents_of) */
type SubAgent = { id: string; name: string; access: 'read' | 'work'; folder: string | null }

export type Snapshot = {
  conversation: {
    id: string; title: string; seq: number; archived_at: string | null
    agent_of: string | null; agents: SubAgent[]
    /** a sub-agent's name, null for any other */
    agent_name?: string | null
    /** a sub-agent's parent: its name and the path that opens it; null for any other */
    started_by?: { name: string; link: string | null } | null
    /** what it is: the place that shows it */
    kind?: ConversationSummary['kind']
    /** the app path that shows it: a chat, a mission, a task's or a run's page */
    link?: string | null
    /** the notes left out here (PATCH notes_off) */
    notes_off?: string[]
  }
  messages: Message[]
  tool_calls: Record<string, ToolCall[]>
  /** the runs of each turn, by turn id */
  runs: Record<string, ToolRun[]>
  compactions: Compaction[]
  turn: Turn | null
  /** what Regenerate answers, null when nothing waits for it */
  unanswered: Unanswered | null
  /** the live turns in the order they run */
  queue: Queued[]
  switched: Record<string, Switched>
  image_notices: ImageNotice[]
  /** Bastet's banners (lib/guard.ts) */
  guard: GuardBanner[]
  /** her requests that wait for you here, after the messages that made their calls */
  approvals: ChatApproval[]
  /** its plan here (event plan.state), null when it has none */
  plan: Plan | null
  seq: number
}

/** one step of its plan (server/celine/plans): exactly one is doing while any is open */
export type PlanStep = {
  title: string; state: 'todo' | 'doing' | 'done'; checkpoint: 'none' | 'ask'; model: string | null
  /** when it became doing, and when done (ISO); null before */
  started_at: string | null; done_at: string | null
}
/** its plan for a conversation, the Plan button in its head; `waiting` while it
    waits on your answer at a checkpoint */
export type Plan = { conversation_id: string; text: string; steps: PlanStep[]; waiting: boolean; updated_at: string }

/** a live turn of a conversation: running, or waiting for the one before it */
export type Queued = { turn_id: string }

/** one request of a reply's step, as it went out (GET .../requests) */
export type SentRequest = {
  step: number; model: string; provider: string; created_at: string
  prompt_tokens: number | null; completion_tokens: number | null; estimate: number | null; finish_reason: string | null
  /** ms from the model's slot to its first token; prompt tokens read from the server's cache */
  first_token_ms: number | null; cache_read: number | null
  body: { messages?: { role: string; content: unknown; name?: string; tool_name?: string }[]; tools?: { function?: { name?: string } }[] } & Record<string, unknown>
}

/** GET /conversations/{cid}/context: blocks in order persona, talk, about, notes, tools, history */
export type ContextView = {
  window: number
  blocks: { name: 'persona' | 'talk' | 'about' | 'notes' | 'tools' | 'history'; tokens: number }[]
  notes: { path: string; tokens: number; state: 'included' | 'off'; why: string | null }[]
  tools: number
  /** the Load automatically switch */
  auto_context: boolean
}

export const RUNNING: TurnState[] = ['queued', 'preparing', 'streaming', 'tools']

export type Siblings = {
  parent_id: string | null
  siblings: { id: string; role: Role; content: string; ordinal: number }[]
  index: number
  count: number
}

export function uploadAttachment(file: File): Promise<Attachment> {
  const form = new FormData()
  form.append('file', file, file.name)
  return upload<Attachment>('/attachments', form)
}

export const deleteAttachment = (id: string) =>
  del<{ removed: boolean }>(`/attachments/${id}`)

export type NotebookNode = {
  name: string
  path: string
  kind: 'dir' | 'file'
  bytes?: number
  protected?: boolean
  owner?: 'config' | 'marked' | 'generated' | null
  virtual?: boolean
  children?: NotebookNode[]
}

/** the notebook: the one notebook */
export const notebookApi = (() => {
  const e = encodeURIComponent
  /** a note's or folder's path in the URL, each part encoded, the slashes kept */
  const entry = (path: string) => `/notebook/entries/${path.split('/').map(e).join('/')}`
  return {
    tree: () => request<{ tree: NotebookNode[] }>(`/notebook/tree`),
    read: (path: string) =>
      request<{ path: string; content: string; shown: string; protected: boolean }>(
        entry(path)),
    /* the saved text comes back: the server renews its Last changed line; `shown` is it
       as it reads it (its name and the user's filled in) */
    write: (path: string, content: string) =>
      request<{ ok: boolean; content: string; shown: string }>(entry(path), json('PUT', { content })),
    mkdir: (path: string) =>
      request<{ ok: boolean }>(`/notebook/folders`, json('POST', { path })),
    move: (src: string, dst: string) =>
      request<{ ok: boolean }>(entry(src), json('PATCH', { path: dst })),
    /** into .trash, never deleted; `restoreTrashed` brings it back */
    remove: (path: string) =>
      del<{ ok: boolean }>(entry(path)),
    /** what was deleted, newest first: where each was */
    trash: () => request<{ trash: { id: string; path: string; kind: 'file' | 'dir' }[] }>(`/notebook/trash`),
    restoreTrashed: (id: string) =>
      post<{ ok: boolean; path: string }>(`/notebook/trash/${e(id)}/restore`),
    protect: (path: string, protected_: boolean) =>
      request<{ ok: boolean }>(entry(path), json('PATCH', { protected: protected_ })),
    /** the texts a note had before it changed, newest first */
    versions: (path: string) =>
      request<{ versions: { id: string; at: string }[] }>(`/notebook/versions?path=${e(path)}`),
    version: (path: string, id: string) =>
      request<{ id: string; at: string; content: string }>(
        `/notebook/versions/${e(id)}?path=${e(path)}`),
  }
})()

export const conversationApi = {
  retry: (id: string, message_id: string) =>
    request<{ turn_id: string }>(`/conversations/${id}/retry`,
      json('POST', { message_id })),
  /** a new version of the message; yours is answered (turn_id), its own is not (null) */
  edit: (id: string, message_id: string, text: string) =>
    request<{ message_id: string; turn_id: string | null; seq: number }>(`/conversations/${id}/messages/${message_id}/versions`,
      json('POST', { text })),
  /** its last reply carried on, as a new version of it; `refused` says why not */
  continueMessage: (id: string, message_id: string) =>
    post<{ message_id: string; turn_id: string; seq: number } | { refused: string }>(`/conversations/${id}/messages/${message_id}/continue`),
  /** its reply as a sample of its voice in its notebook (on), or out of it again */
  sample: (id: string, message_id: string, on: boolean) =>
    request<{ sample: boolean }>(`/conversations/${id}/messages/${message_id}/voice-sample`, { method: on ? 'PUT' : 'DELETE' }),
  /** your next message as the model writes it in your voice, from what you typed
   *  (`start`) on; nothing is sent */
  impersonate: (id: string, start = '') =>
    request<{ text: string }>(`/conversations/${id}/impersonate`,
      json('POST', { start })),
  version: (id: string, message_id: string) =>
    request<{ seq: number }>(`/conversations/${id}/active-version`,
      json('PUT', { message_id })),
  siblings: (id: string, message_id: string) =>
    request<Siblings>(`/conversations/${id}/messages/${message_id}/siblings`),
  /** what the model's server answered for this reply, as it reported it */
  raw: (id: string, message_id: string) =>
    request<{ raw: unknown }>(`/conversations/${id}/messages/${message_id}/raw`),
  /** every request that streamed into this step, as it went out */
  requests: (id: string, message_id: string) =>
    request<{ requests: SentRequest[] }>(`/conversations/${id}/messages/${message_id}/requests`),
  /** what the next request would hold, measured per block, and its notes with their state */
  context: (id: string) => request<ContextView>(`/conversations/${id}/context`),
  /** the notes left out of this conversation (the whole list) */
  setNotesOff: (id: string, notes_off: string[]) =>
    request<{ ok: true }>(`/conversations/${id}`, json('PATCH', { notes_off })),
  /** gone for good, with everything after it in its branch */
  remove: (id: string, message_id: string) =>
    del<{ message_ids: string[]; active_leaf_id: string | null }>(`/conversations/${id}/messages/${message_id}`),
  /** sent to the conversation; message_id, turn_id and seq are null while a turn of it
   *  runs (it reads it at its next step) */
  send: (id: string, text: string, attachments: string[] = []) =>
    request<{ intercom_id: number; message_id: string | null; turn_id: string | null; seq: number | null }>(
      `/conversations/${id}/messages`,
      json('POST', { text, attachments })),
  regenerate: (id: string) =>
    post<{ turn_id: string }>(`/conversations/${id}/regenerate`),
  /** carry on a stopped, interrupted or failed answer where it ended */
  continueTurn: (id: string, turnId: string, wait = false) =>
    request<{ turn_id: string; seq: number }>(`/conversations/${id}/turns/${turnId}/continue`,
      wait ? json('POST', { wait }) : { method: 'POST' }),
  /** a conversation's catalog */
  skills: (id: string) => request<{ skills: SkillInfo[] }>(`/skills?conversation=${id}`),
  retrySkill: (name: string) =>
    post<{ ok: boolean }>(`/skills/${name}/restart`),
  /** a skill or a tool on or off in this conversation; `client_seq` orders fast clicks */
  setTool: (id: string, target: string, enabled: boolean, client_seq: number) =>
    request<{ seq: number | null; applied: boolean }>(`/conversations/${id}/tool-settings/${target}`,
      json('PUT', { enabled, client_seq })),
  abort: (id: string) =>
    post<{ turn_id: string | null }>(`/conversations/${id}/stop`),
}
