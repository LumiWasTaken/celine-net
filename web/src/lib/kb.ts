/* The archive's sources (the server calls each one a kb), their chats and messages,
   search, tasks and connectors. */
import { del, json, post, query, request } from './api'

type KbKind = 'chat' | 'documents'

export type Kb = {
  id: string
  kind: KbKind
  title: string
  platform?: string
  chats?: number
  messages?: number
  people?: number
  items?: number
  first_ts?: number | null
  last_ts?: number | null
  semantic?: { passages: number; embedded: number; ready: boolean }
}

type ChatKind = 'dm' | 'group' | 'channel'

export type Person = {
  id: number
  name: string
  source?: 'custom' | 'contact' | 'push' | 'display' | 'username' | 'phone' | 'unknown'
  phone?: string | null
  names?: { custom: string | null; contact: string | null; push: string | null; display?: string | null; username: string | null }
  messages?: number
  chats?: number
}

export type Chat = {
  id: number
  kb: string
  name: string
  kind: ChatKind
  parent: string | null
  /** in the trash: out of lists and search, it still opens */
  trashed: boolean
  messages: number
  last_ts: number | null
  last_preview: { sender: string | null; from_me: boolean; text: string } | null
  members?: Person[]
}

/** a chat deleted and ignored (GET /sources/{sid}/ignored-chats); `name` and `kind` as they were (a chat
 *  without a name: its phone number, or "Group" or "Channel" with the end of its id) */
type IgnoredChat = { key: string; kind: ChatKind; name: string; at: number }

export type MediaType =
  | 'photo' | 'video' | 'voice' | 'audio' | 'document' | 'sticker' | 'gif'
  | 'location' | 'contact' | 'file'

export type ArchiveMessage = {
  id: number
  ref: string
  chat: number
  ts: number
  from_me: boolean
  sender: { id: number; name: string; phone?: string | null } | null
  text: string
  kind: 'text' | 'media' | 'system'
  media: { type: MediaType; caption?: string | null; files?: MediaFile[] } | null
  reply_to: { id: number; sender: string; text: string } | null
  edited: boolean
  deleted_at: number | null
  /** earlier texts, oldest first; [] when never edited, or when the edit was not seen
      (a WhatsApp backup only says `edited`) */
  edits: MessageEdit[]
}

type MessageEdit = { ts: number; text: string }

/** a file of the message kept in the archive (GET /sources/{sid}/media/{sha256}); files the
    phone never gave are not listed */
export type MediaFile = { name: string | null; size: number | null; content_type: string | null; sha256: string }

type MessagePage = { messages: ArchiveMessage[]; has_before: boolean; has_after: boolean }

export type SearchHit = {
  ref: string
  kb: string
  id: number
  ts: number
  sk?: number
  chat?: { id: number; name: string; kind: ChatKind; parent?: string | null }
  sender?: { id: number; name: string } | null
  from_me?: boolean
  kind?: 'text' | 'media' | 'system'
  /** the media type only */
  media?: MediaType | null
  edited?: boolean
  deleted?: boolean
  snippet: string
  score?: number
}

/* ── word search: query understanding (chips, facets) ── */
type SearchTerm = { raw: string; tokens: string[]; prefix: boolean; phrase: boolean; negate: boolean; group: number }
type PersonCandidate = { kb: string; id: number; ref: string; name: string; source?: string; messages?: number }
type PersonMatch = { name: string; refs: string[]; candidates: PersonCandidate[]; ambiguous: boolean; match: 'exact' | 'prefix' | 'fuzzy' | 'none' }
type When = { from_ms: number | null; to_ms: number | null; label: string; start?: string; end?: string }
export type SearchChip = {
  kind: string; raw: string; value: string; label: string
  refs?: string[]; candidates?: PersonCandidate[]; ambiguous?: boolean; when?: When; error?: string
}
type SearchSuggestion = { kind: string; text: string; value: string; label: string; when?: When }
type ParsedQuery = {
  raw: string; text: string; terms: SearchTerm[]; kbs: string[] | null
  people: PersonMatch[]; chats: PersonMatch[]
  from: number | null; to: number | null
  has: string[]; is_me: boolean; is_deleted: boolean; is_edited: boolean
  chips: SearchChip[]; suggestions: SearchSuggestion[]
}
type SearchCorrected = { query: string; original: string; words: Record<string, string>; variants: Record<string, string[]> }
type SearchResult = {
  hits: SearchHit[]
  total: number
  total_capped: boolean
  next_cursor: string | null
  corrected?: SearchCorrected | null
  /** the query with a rare word swapped for a common close one; never applied */
  did_you_mean?: string | null
  top_hits?: SearchHit[]
  query?: ParsedQuery
  took_ms: number
  indexing?: boolean
}
type FacetKbCount = { kb: string; title: string; count: number; approximate: boolean }
type FacetChatCount = { ref: string; kb: string; id: number; name: string; kind: ChatKind; parent: string | null; count: number }
type FacetPersonCount = { ref: string | null; kb: string; id: number; name: string; me: boolean; count: number }
type FacetMonth = { month: string; count: number }
type SearchFacets = {
  total: number
  approximate: boolean
  kbs: FacetKbCount[]
  chats: FacetChatCount[]
  people: FacetPersonCount[]
  months: FacetMonth[]
  query?: ParsedQuery
  took_ms: number
}

/** unreachable: a connection or server did not answer; a normal end, not a failure */
type TaskState = 'running' | 'done' | 'failed' | 'cancelled' | 'unreachable'
export type Task = {
  id: string
  kb: string
  kind: string
  label: string
  state: TaskState
  started_at: number
  finished_at: number | null
  progress: { done: number; total: number | null; unit: string } | null
  result: Record<string, unknown> | null
  error: string | null
}

/** an automatic task (sync, semantic topic search index update, photo descriptions): the
    outcome of its last run, one entry per (kb, kind); `since` = when this state began */
export type Job = {
  kb: string
  kind: string
  label: string
  state: Exclude<TaskState, 'running'>
  since: number
  finished_at: number
  error: string | null
  /** a new run is checking again: the last outcome may no longer hold */
  running: boolean
}

export type DiscordTarget = {
  id: string
  name: string
  kind?: 'dm' | 'group'
  icon?: string | null
  sync: boolean
  synced_at?: number | null
  messages?: number | null
  category?: string | null
  /** a server channel that was not there when its include list was last saved */
  new?: boolean
}
export type DiscordGuild = { id: string; name: string; icon: string | null; channels: DiscordTarget[] | null }
type DiscordSession = {
  id: string
  kb: string
  state: 'starting' | 'waiting' | 'scanned' | 'done' | 'failed' | 'cancelled' | 'expired'
  qr_url: string | null
  qr_svg: string | null
  user: { id: string; name: string; avatar: string | null } | null
  error: string | null
  expires_at: number | null
}
type DiscordTargets = {
  account: { id: string; name: string } | null
  dms: DiscordTarget[]
  guilds: DiscordGuild[]
}

export type DiscordSubSettings = {
  /** the include list: only these channels are synced (threads follow their channel) */
  channels: string[]
  include_bots: boolean
  include_attachments: boolean
  backfill: 'none' | '30d' | 'all'
}
export type DiscordSubscription = {
  guild_id: string
  guild_name: string | null
  icon: string | null
  enabled: boolean
  /** draft: chosen, not started; on: syncing; off: stopped */
  state: 'draft' | 'on' | 'off'
  created_at: number
  started_at: number | null
  updated_at: number
  settings: DiscordSubSettings
  last_event_at: number | null
  events: number
  stats: { channels: number; messages: number; deleted: number; edited: number }
}
/* A connector (discord-link, wa-link): GET /api/sources/{sid}/connector. `unreachable` is a
   normal state, not a problem: the feed resumes by itself. */
type ConnectionState = 'connected' | 'needs_login' | 'unreachable'
export type ConnectionRecord = {
  state: ConnectionState
  since: number | null
  error: string | null
  last_sync: number | null
  next_sync: number | null
}
export type ConnectorState = {
  platform: string
  connection: ConnectionRecord
  account: { id: string; name: string | null } | null
  /** when a login that worked was lost (the phone disconnected, Discord refused it) */
  login_lost_at: number | null
  /** Discord: the QR login in progress */
  login: DiscordSession | null
  /** WhatsApp: a QR code to connect the phone is ready (GET .../connector/qr.png) */
  qr_at: number | null
}
export type ConnectorSummary = {
  kb: string
  platform: string
  title: string
  connection: ConnectionRecord
  account: { id: string; name: string | null } | null
  login_lost_at: number | null
}

/* ── people across sources: person records in var/celine.db, not a kb file ── */
/** an account in one source, as GET /people lists it; `handle` is the
    number or @username, null when the source has neither */
export type PersonAccount = Person & { ref: string; kb: string; handle: string | null; last_ts: number | null }
/** one person: a same person (several accounts, `same_person`) or a single account.
    `about` and `aliases` are what you wrote; `person` is the record id when there is one */
export type PersonEntry = {
  key: string; person: number | null; same_person: boolean; name: string
  about: string; aliases: string[]; messages: number; last_ts: number | null
  accounts: PersonAccount[]
}
type PersonRecord = { id: number | null; name: string; about: string; aliases: string[]; members: string[] }

type MessageCard = ArchiveMessage & { chat: Chat | { id: number; name: string; kind: ChatKind } }
/** a quoted piece of a document */
export type DocumentCard = { text: string; segment: { id: number; page?: number | null; speaker?: string | null }; item: { id: number; path: string; title: string; kind: string } }
export type RefCard = MessageCard | DocumentCard
export const isDocumentCard = (c: RefCard): c is DocumentCard => 'item' in c && !('chat' in c)
export type DocumentLink = { id: number; path: string; title: string; kind: string }


const enc = encodeURIComponent

export const kbApi = {
  list: () => request<{ kbs: Kb[] }>('/sources'),

  /** one source's chats, or with no source All chats: every source's in one list */
  chats: (kb: string | undefined, p: { q?: string; sort?: 'recent' | 'name' | 'count'; trashed?: boolean; limit?: number; offset?: number } = {}) =>
    request<{ chats: Chat[]; total: number }>(`${kb ? `/sources/${enc(kb)}` : '/archive'}/chats${query(p)}`),
  chat: (kb: string, id: number) => request<Chat>(`/sources/${enc(kb)}/chats/${id}`),
  patchChat: (kb: string, id: number, body: { name?: string | null; trashed?: boolean }) =>
    request<Chat>(`/sources/${enc(kb)}/chats/${id}`, json('PATCH', body)),

  messages: (kb: string, chat: number, p: { before?: number; after?: number; around?: number; limit?: number } = {}) =>
    request<MessagePage>(`/sources/${enc(kb)}/chats/${chat}/messages${query(p)}`),
  message: (kb: string, id: number) =>
    request<ArchiveMessage & { chat: Chat }>(`/sources/${enc(kb)}/messages/${id}`),
  /** a message's file, with the type and name the archive recorded (a picture shows
      inline, anything else downloads) */
  mediaUrl: (kb: string, message: number, f: MediaFile) => `/api/sources/${enc(kb)}/messages/${message}/media/${f.sha256}`,

  patchPerson: (kb: string, id: number, name: string | null) =>
    request<Person>(`/sources/${enc(kb)}/people/${id}`, json('PATCH', { name })),

  search: (p: {
    q: string; kb?: string; chat?: string; person?: string; from?: number; to?: number
    sort?: 'recent' | 'oldest' | 'best' | 'relevance'; limit?: number
    cursor?: string; top?: number; exact?: boolean; literal?: boolean
  }, signal?: AbortSignal) =>
    request<SearchResult>(`/archive/search${query(p)}`, { signal }),
  searchFacets: (p: { q: string; kb?: string; chat?: string; person?: string; from?: number; to?: number; top?: number; literal?: boolean }, signal?: AbortSignal) =>
    request<SearchFacets>(`/archive/search/facets${query(p)}`, { signal }),
  searchParse: (text: string, signal?: AbortSignal) =>
    request<ParsedQuery>(`/archive/search/parse?q=${enc(text)}`, { signal }),

  tasks: () => request<{ tasks: Task[]; jobs: Job[] }>('/tasks'),
  retryTask: (id: string) => post<{ task: string }>(`/tasks/${enc(id)}/retry`),
  cancelTask: (id: string) => post<{ ok?: boolean }>(`/tasks/${enc(id)}/stop`),
  /** remove one finished task from the list */
  dismissTask: (id: string) => del<{ ok?: boolean }>(`/tasks/${enc(id)}`),

  /** delete a chat from the archive and ignore it: no sync or import brings it back */
  deleteChat: (kb: string, chatId: number) =>
    del<{ deleted: number }>(`/sources/${enc(kb)}/chats/${chatId}`),
  ignoredChats: (kb: string) => request<{ chats: IgnoredChat[] }>(`/sources/${enc(kb)}/ignored-chats`),
  /** restore an ignored chat; it comes back from what the connection has (`task`) */
  restoreChat: (kb: string, key: string) =>
    request<{ task: string | null }>(`/sources/${enc(kb)}/ignored-chats/include`, json('POST', { key })),

  discordTargets: (kb: string) => request<DiscordTargets>(`/sources/${enc(kb)}/discord/targets`),
  discordChannels: (kb: string, guild: string) =>
    request<{ channels: DiscordTarget[] }>(`/sources/${enc(kb)}/discord/guilds/${enc(guild)}/channels`),
  /** include (true) or leave out (false) chats by Discord id; one included again is read
      from the start (`task`) */
  setDiscordTargets: (kb: string, set: Record<string, boolean>) =>
    request<{ changed: number; task: string | null }>(`/sources/${enc(kb)}/discord/targets`, json('PUT', { set })),
  discordLoginStart: (kb: string) => post<DiscordSession>(`/sources/${enc(kb)}/discord/login-sessions`),
  discordLoginCancel: (kb: string, sid: string) =>
    del<{ ok?: boolean }>(`/sources/${enc(kb)}/discord/login-sessions/${enc(sid)}`),
  discordLogout: (kb: string) => post<{ ok?: boolean }>(`/sources/${enc(kb)}/discord/logout`),
  discordSetToken: (kb: string, token: string) =>
    request<{ ok?: boolean }>(`/sources/${enc(kb)}/discord/token`, json('PUT', { token })),
  /** the first import (the onboarding): a task `import`, its progress the archive's message count */
  importMessages: (kb: string) => post<{ task: string }>(`/sources/${enc(kb)}/import`),
  discordSync: (kb: string) =>
    post<{ task: string }>(`/sources/${enc(kb)}/discord/sync`),

  discordSubscriptions: (kb: string) =>
    request<{ subscriptions: DiscordSubscription[]; defaults: DiscordSubSettings }>(`/sources/${enc(kb)}/discord/subscriptions`),
  /** saves the choices; never starts syncing (a new one is a draft). A running one
      answers a task when newly included channels are synced now. */
  putDiscordSubscription: (kb: string, guildId: string, body: { enabled?: false; settings?: Partial<DiscordSubSettings>; guild_name?: string; icon?: string }) =>
    request<{ subscription: DiscordSubscription; task: string | null }>(
      `/sources/${enc(kb)}/discord/subscriptions/${enc(guildId)}`, json('PUT', body)),
  startDiscordSubscription: (kb: string, guildId: string, body: { settings?: Partial<DiscordSubSettings>; guild_name?: string; icon?: string }) =>
    request<{ subscription: DiscordSubscription; task: string | null }>(
      `/sources/${enc(kb)}/discord/subscriptions/${enc(guildId)}/start`, json('POST', body)),
  deleteDiscordSubscription: (kb: string, guildId: string) =>
    del<{ ok?: boolean }>(`/sources/${enc(kb)}/discord/subscriptions/${enc(guildId)}`),

  connectors: () => request<{ connectors: ConnectorSummary[] }>('/connectors'),
  connector: (kb: string) => request<ConnectorState>(`/sources/${enc(kb)}/connector`),
  /** the QR code while connecting the phone; `at` changes with each new code */
  qrUrl: (kb: string, at: number) => `/api/sources/${enc(kb)}/connector/qr.png?at=${at}`,
}

/* [[kb:id]] and [[documents:segment]] quotes, and [[Some file]] links (GET /api/archive/refs). */
export const refsApi = {
  resolve: (refs: string[]) =>
    request<{ refs: Record<string, RefCard | null>; items?: Record<string, DocumentLink | null> }>(`/archive/refs?refs=${enc(refs.join(','))}`),
}

export const identitiesApi = {
  /** one entry per person from every chat source (or one) */
  people: (p: { q?: string; kb?: string }) => request<{ people: PersonEntry[] }>(`/people${query({ ...p, limit: 1000 })}`),
  /** mark accounts as the same person */
  link: (refs: string[], name: string) =>
    request<{ person: PersonRecord }>('/people', json('POST', { refs, name })),
  /** separate one account from its same person */
  unlink: (ref: string) =>
    del<{ person: number; deleted: boolean }>(`/people/${enc(ref)}/link`),
  /** the person of account `ref`: a same person's name, the about text, the aliases */
  setPerson: (ref: string, body: { name?: string; about?: string; aliases?: string[] }) =>
    request<{ person: PersonRecord }>(`/people/${enc(ref)}`, json('PATCH', body)),
}

/* ── message files ── */
const PICTURE = /^image\/(jpeg|png|webp|gif|avif|bmp)$/
const PICTURE_NAME = /\.(jpe?g|png|webp|gif|avif|bmp)$/i

export const isPicture = (f: MediaFile) =>
  f.content_type ? PICTURE.test(f.content_type) : PICTURE_NAME.test(f.name ?? '')

/** a chat message's text as it reads: a Discord custom emoji (<:name:id>) as :name:, its
    name possibly marked by a search */
export const chatText = (text: string) => text.replace(/<a?:((?:\w|<\/?mark>)+):\d+>/g, ':$1:')

/* ── references ── */
const MSG_REF_RE = /^([a-z0-9_-]+):(\d+)$/i
export function parseRef(ref: string): { kb: string; id: number } | null {
  const m = MSG_REF_RE.exec(ref.trim())
  return m ? { kb: m[1]!, id: Number(m[2]) } : null
}
