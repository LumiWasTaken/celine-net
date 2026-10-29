/* The Settings API (server/celine/api/settings.py): providers and their models,
   write-only keys, the models of each role, tool defaults, general
   settings, it (GET/PUT /her), and the chat model of one conversation.

   Every PUT takes the whole object and is idempotent; keys are never sent back. Every
   change reaches open pages at once: `settings.changed` after a save, `models.state`
   when a provider or model changes state (SETTINGS_EVENTS). */
import { del, json, post, request } from './api'
import { LOCALE } from './format'
import type { Her } from './her'
import type { ServerEvent } from './bus'
import type { FormField } from '../ui'

const enc = encodeURIComponent

/** reload anything that shows providers, models or roles on these server events */
export const SETTINGS_EVENTS = (ev: ServerEvent) => ev.type === 'settings.changed' || ev.type === 'models.state'

/* ── providers and models ─────────────────────────────────────────────────── */

export type ProviderKind = 'llama-swap' | 'tabby' | 'llama-server' | 'ollama' | 'openrouter' | 'anthropic' | 'openai' | 'comfyui'
export type Cap = 'chat' | 'tools' | 'vision' | 'embeddings' | 'rerank' | 'image_generation' | 'music_generation' | 'transcription' | 'speech'

export type ModelConfig = {
  label?: string
  caps?: Cap[]
  context?: number
  /** parallel slots: requests at once on this model; empty: one */
  slots?: number
  params?: Record<string, unknown>
  llama?: Record<string, unknown>
  reasoning_effort?: string
  thinking_levels?: string[]
  /** OpenRouter: only zero-data-retention endpoints */
  zdr?: boolean
  /** OpenRouter: the provider slug the model is pinned to (deepinfra/fp4) */
  provider?: string
  /** Anthropic: the longest reply in tokens; empty: 8192 */
  max_tokens?: number
}

/** what is stored for a provider (PUT takes this whole object) */
type ProviderConfig = {
  kind: ProviderKind
  label: string
  base_url: string
  options?: Record<string, unknown>
  models: Record<string, ModelConfig>
}

type KeyState = {
  state: 'set' | 'not set'
  set_at: string | null
  required: boolean
}

/** what the app knows about the server now (backends/router.py) */
type ProviderState = {
  /** null: not asked yet */
  reachable: boolean | null
  /** ms, when this reachability began */
  since: number | null
  /** why it is unreachable, one sentence */
  error: string | null
  /** the models loaded now; null when the server does not say */
  loaded: string[] | null
  starting: string[]
  /** the provider needs a key and none is saved: its models are not offered */
  key_missing: boolean
}

type ModelState =
  | { state: 'ok' }
  | { state: 'failed'; kind: 'could_not_load' | 'error'; message: string; at: number }

export type ProviderModel = ModelConfig & {
  name: string
  ref: string
  /** the role names it is used for, as the Models page shows them */
  used_by: string[]
  /** what the thinking control offers: the model's, else the provider's, else the kind's */
  thinking_offered?: string[]
  /** false while its provider has no key: not offered in any list */
  offered: boolean
  state: ModelState
  /** the context the server says it runs with; turns budget with it over `context` */
  server_context: number | null
}

export type ProviderView = {
  id: string
  provider: ProviderConfig
  version: string
  key: KeyState
  state: ProviderState
  models: ProviderModel[]
  /** the role names its models are used for */
  used_by: string[]
}

export type OptionSchema = { key: string; type: 'bool' | 'int' | 'num' | 'str' | 'list'; default: unknown; choices: string[] | null }
/** `default_url`: the kind lives at that one address (OpenRouter, Anthropic), no address field */
export type KindInfo = { kind: ProviderKind; label: string; key: 'required' | 'optional'; default_url: string | null; options: OptionSchema[] }

/** the answer of "Check and add" for an address not saved yet */
type Health = {
  ok: boolean
  level: 'ok' | 'needs_you'
  /** only when something is wrong */
  sentence?: string
  fix?: 'add_key' | 'replace_key'
  models?: number
  loaded?: string[]
}

export type Available = { id: string; name?: string; caps?: Cap[]; context?: number; loaded?: boolean; added: boolean
  thinking_levels?: string[]
  price?: { in_per_m?: number; out_per_m?: number } | null }

/* ── roles: an ordered list of models each; the first that can answer works ── */
export type RoleView = {
  label: string
  /** the capabilities every model in the list needs */
  needs: Cap[]
  chain: string[]
  models: { ref: string; label: string; provider: string; provider_label: string; key_missing: boolean }[]
  version: string
}

/* ── tools ── */
type ToolDefault = 'on' | 'off'
type ToolView = {
  wire_name: string; name: string; label: string; summary: string
  mutating: boolean; default: ToolDefault; set_in_settings: boolean
}
export type SkillView = {
  name: string; label: string; summary: string; source: 'built-in' | 'mcp'
  status: 'up' | 'starting' | 'down' | 'dead' | 'unset' | 'unconnected'; error: string | null
  default: ToolDefault | 'mixed'; set_in_settings: boolean
  tools: ToolView[]
}

/* ── general and sources: rendered from a field schema ── */
type EmailView = {
  fields: FormField[]
  email: Record<string, unknown>
  password: { state: 'set' | 'not set'; set_at: string | null }
  version: string
}

type SecretState = { state: 'set' | 'not set'; set_at: string | null }

type AskKind = 'vault' | 'gate' | 'her' | 'share'

/** one request waiting for you (server/celine/approvals.py): `vault` a tap, `gate` a
    tool Bastet asks you about, `her` an edit of herself, `share` a public share; every
    word is the server's, built from fixed sentences and the call's own data */
export type VaultAsk = {
  id: string
  kind: AskKind
  action: string
  facts: string[]
  why: string
  conversation: string
  /** the last tool that read from outside in that conversation before it asked */
  read: string | null
  /** null: it waits for you (your own turn), until you answer or Stop */
  expires_in: number | null
}

/** a request as its chat shows it, after the message that made the call (the snapshot's
    `approvals`, the events approval.asked and approval.closed); a gate is asked on
    Bastet's banner instead */
export type ChatApproval = {
  id: string
  kind: AskKind
  /** the kind's name, in bold */
  label: string
  line: string
  tool_call_id: string | null
  message_id: string | null
}

type VaultView = {
  fields: FormField[]
  vault: Record<string, unknown>
  client_secret: SecretState
  /** the master password is stored nowhere: this says whether it was typed since the
      last start */
  unlocked: boolean
  asks: VaultAsk[]
  /** the server's status line: Not set up, Locked, Unlocked, or what went wrong */
  status: string
  version: string
}

export type EmailCheck = {
  ok: boolean
  sentence: string
  folders: { name: string; messages: number | null; unread: number | null }[]
}

/** one conversation's own choices and what they come to (GET/PATCH /conversations/{cid}/settings) */
export type ConversationSettings = {
  /** picked for this conversation, or null: the default */
  model: string | null
  /** null: the model's own default */
  thinking: string | null
  /** this conversation's tool overrides: skill or tool → on/off */
  tools: Record<string, boolean>
  /** Bastet checks this chat (false: she skips it entirely) */
  guard: boolean
  effective: {
    model: string | null; label: string | null; provider_label: string | null
    /** what Default comes to now (the head of the conversation's role list); null: no model */
    default: { model: string; label: string; provider_label: string | null } | null
    chain: string[]
    /** the Guard feature is on; off, this chat's switch does nothing */
    guard: boolean
  }
}
export type ConversationPatch = { model?: string | null; thinking?: ConversationSettings['thinking']; tools?: Record<string, boolean | null>; guard?: boolean }

/* ── features: what it has (server/celine/settings/features.py, the plug) ── */
type FeatureState = 'ready' | 'missing' | 'unset' | 'none'
export type Feature = {
  id: string
  label: string
  needs: string
  /** the places (routes.tsx ids) and Settings pages it owns: off, they leave the rail and the list */
  places: string[]
  pages: string[]
  /** one sentence on what it gives you, and one short phrase on why someone turns it off */
  about: string
  off_when: string
  /** the onboarding's setup form for it, or "" */
  setup: string
  /** its form at /api/forms/<id> (a plugin's settings.toml), or "": its Settings page and its card's body */
  form: string
  /** the onboarding's card: "detect" when the service answered or has an address and did
      not, "always" (shares, the guard, routines, the connectors), "" never (email, vault: the Later
      card and Settings) */
  card: '' | 'detect' | 'always'
  /** the archives it fills (Discord, WhatsApp: its source id) */
  kbs: string[]
  /** the feature of that archive ("messages"): its card offers the switch and the first import */
  archive: string
  /** what switching it on asks first (shares), or "" */
  warn: string
  /** what the probe found; "none": nothing to detect */
  state: FeatureState
  detail: string
  /** the address the probe found the service at while none is saved: the form prefills it */
  url?: string
}
type Probe = { state: FeatureState; detail: string; url?: string }
export type FeaturesView = {
  features: Record<string, boolean>
  catalog: Feature[]
  saved: boolean
  /** the onboarding finished */
  done: boolean
  version: string
}
/** the Dot of a feature's state; null: nothing to show */
export const STATE_TONE: Record<FeatureState, 'ok' | 'error' | 'muted' | null> = { ready: 'ok', missing: 'error', unset: 'muted', none: null }

/* ── Bastet's rules and her services ── */
type GuardRules = { chain: { provider: string; model: string } & Record<string, unknown> } & Record<string, unknown>
type ServiceView = { url: string }
/** `keys`: the sha256 of each box's key, what its key.hash file holds */
type ServicesView = { services: Record<string, ServiceView>; keys: Record<string, { hash: string }>; version: string }

export const settingsApi = {
  features: () => request<FeaturesView>('/features'),
  /** the whole object; an id left out is on */
  putFeatures: (features: Record<string, boolean>, done?: boolean) =>
    request<FeaturesView>('/features', json('PUT', done === undefined ? { features } : { features, done })),
  /** one feature's state, asked now (no cache): "Check again" */
  probeFeature: (id: string) => request<Probe>(`/features/${enc(id)}/probe`),

  providers: () => request<{ providers: ProviderView[]; kinds: KindInfo[]; version: string }>('/providers'),
  putProvider: (pid: string, p: ProviderConfig) => request<ProviderView>(`/providers/${enc(pid)}`, json('PUT', p)),
  /** the onboarding's ComfyUI: the saved one's id and models when there is one (the address
      changes in place), else a new `comfyui` with no models: the server fills the preset roles */
  putComfyui: (base_url: string, saved?: ProviderView) =>
    request<ProviderView>(`/providers/${enc(saved?.id ?? 'comfyui')}`,
                          json('PUT', { kind: 'comfyui', label: saved?.provider.label ?? 'ComfyUI', base_url, models: saved?.provider.models ?? {} })),
  deleteProvider: (pid: string) => del<{ deleted: boolean }>(`/providers/${enc(pid)}`),
  /** "Check provider": the provider's state, asked now */
  test: (pid: string) => post<ProviderView>(`/providers/${enc(pid)}/test`),
  /** no kind: the server says what it is (`kind` in the answer; null when it could not tell) */
  testNew: (body: { kind?: ProviderKind; base_url?: string; key?: string }) =>
    request<Health & { kind: ProviderKind | null; base_url: string }>('/provider-checks', json('POST', body)),
  available: (pid: string, refresh = false) =>
    request<{ models: Available[]; total: number }>(`/providers/${enc(pid)}/available-models${refresh ? '?refresh=true' : ''}`),
  putModel: (pid: string, model: string, m: ModelConfig) =>
    request<{ ref: string }>(`/providers/${enc(pid)}/models/${model.split('/').map(enc).join('/')}`, json('PUT', m)),
  deleteModel: (pid: string, model: string) =>
    del<{ deleted: boolean }>(`/providers/${enc(pid)}/models/${model.split('/').map(enc).join('/')}`),

  /** write-only: the answer only says whether a key is set */
  setKey: (pid: string, key: string) => request<KeyState>(`/secrets/${enc(pid)}`, json('PUT', { key })),
  deleteKey: (pid: string) => del<KeyState>(`/secrets/${enc(pid)}`),

  roles: () => request<{ roles: Record<string, RoleView>; version: string }>('/roles'),
  /** a role's models in order; [] turns it off */
  putRole: (role: string, chain: string[]) => request<RoleView>(`/roles/${enc(role)}`, json('PUT', { chain })),

  /** Bastet's rules (guard.toml): `chain` is her second look, provider and model "" for none;
      the PUT merges the given tables into the saved rules */
  guardRules: () => request<GuardRules>('/guard/rules'),
  putGuardRules: (rules: Partial<GuardRules>) => request<GuardRules>('/guard/rules', json('PUT', rules)),

  /** its services (the shellbox, its browser, SearXNG): their addresses and the boxes' key hashes */
  services: () => request<ServicesView>('/services'),
  /** whole object: every service's url */
  putServices: (view: ServicesView, id: string, url: string) =>
    request<ServicesView>('/services', json('PUT', { services: { ...view.services, [id]: { url } } })),
  /** one service, asked now with what is saved */
  testService: (id: string) => post<Probe>(`/services/${enc(id)}/test`),

  tools: () => request<{ skills: SkillView[]; defaults: Record<string, ToolDefault>; version: string }>('/tool-defaults'),
  /** a skill or a tool; null: follow the skill / config again */
  putTool: (target: string, value: ToolDefault | null) =>
    request<{ skills: SkillView[] }>(`/tool-defaults/${enc(target)}`, json('PUT', { default: value })),

  /** `saved`: the general settings were saved at least once */
  general: () => request<{ fields: FormField[]; general: Record<string, unknown>; version: string; saved: boolean }>('/settings'),
  /** the whole object; a key left out goes back to its default */
  putGeneral: (general: Record<string, unknown>) =>
    request<{ general: Record<string, unknown> }>('/settings', json('PUT', { general })),

  email: () => request<EmailView>('/email'),
  /** the whole object; `password` left out keeps the saved one. The answer never holds it */
  putEmail: (email: Record<string, unknown>, password?: string) =>
    request<EmailView>('/email', json('PUT', password === undefined ? { email } : { email, password })),
  /** logs in with the saved account and counts the folders */
  checkEmail: () => post<EmailCheck>('/email/test'),

  vault: () => request<VaultView>('/vault'),
  /** the whole object; a secret left out keeps the saved one. The vault logs in again,
      locked; the answer never holds a secret */
  putVault: (vault: Record<string, unknown>, secrets: { client_secret?: string } = {}) =>
    request<VaultView>('/vault', json('PUT', { vault, ...secrets })),
  /** the master password, to unlock once; the server keeps it nowhere */
  unlockVault: (master_password: string) =>
    request<VaultView>('/vault/unlock', json('POST', { master_password })),
  lockVault: () => post<VaultView>('/vault/lock'),
  /** POST /approvals/{id}/answer: the answer to one request, from Settings or its chat */
  answerApproval: (id: string, ok: boolean) =>
    request<{ approvals: VaultAsk[] }>(`/approvals/${enc(id)}/answer`, json('POST', { ok })),

  conversationSettings: (cid: string) => request<ConversationSettings>(`/conversations/${enc(cid)}/settings`),
  /** merge-patch: a key left out stays, null goes back to the default */
  patchConversationSettings: (cid: string, patch: ConversationPatch) =>
    request<ConversationSettings>(`/conversations/${enc(cid)}/settings`, json('PATCH', patch)),
}

/* ── it (server/celine/api/agent.py): GET /her, PUT /her with If-Match ── */
export type Portrait = { url: string; media_type: string; size: number; updated_at: string }
/** its Banned words: each one as it is sent (" delve" mid-sentence), all at one Bias (-100 bans) */
export type Voice = { on: boolean; banned: string[]; bias: number }
export type HerView = Her & {
  persona: string
  /** how it talks, its own text, null for the shipped one (talk_shipped) */
  talk: string | null
  talk_shipped: string
  voice: Voice
  /** its voice as History keeps it (her:voice), one line per entry */
  voice_text: string
  version: string
}
export type HerPut = Partial<{ name: string; pronouns: string; color: string; persona: string; talk: string | null; voice: Voice }>

export const herApi = {
  get: () => request<HerView>('/agent'),
  /** `version`: the one its page was loaded at; a change made meanwhile (by Celine) is a
      version_conflict with the server's sentence, nothing written */
  put: (body: HerPut, version: string) =>
    request<HerView>('/agent', { ...json('PUT', body), headers: { 'If-Match': version } }),
  putPortrait: (file: File) => {
    const form = new FormData()
    form.append('image', file)
    return request<{ portrait: Portrait }>('/agent/portrait', { method: 'PUT', body: form })
  },
  deletePortrait: () => del<{ deleted: boolean }>('/agent/portrait'),
}

/* ── one order for tools everywhere (Settings, Tools and a conversation's panel):
   skills by label A to Z, and the tools inside each likewise ── */
const byLabel = (a: { label: string }, b: { label: string }) => a.label.localeCompare(b.label, LOCALE, { sensitivity: 'base' })
/** the one word set for anything it connects to: a source, a tool, a provider, the browser */
export const LINK = { up: 'Connected', down: 'Not connected', unset: 'Not set up' } as const
/** a skill's state in that word set; null while it works */
export const SKILL_WORD: Record<SkillView['status'], string | null> = {
  up: null, starting: null, down: LINK.down, dead: 'Stopped', unset: LINK.unset, unconnected: LINK.down,
}

/* Settings → Tools and a conversation's panel group skills the same way: On, Needs setup
   (shown off until it works), Off */
export const TOOL_GROUPS = [['on', 'On'], ['setup', 'Needs setup'], ['off', 'Off']] as const
export const toolGroup = (status: SkillView['status'], anyOn: boolean) =>
  SKILL_WORD[status] ? 'setup' : anyOn ? 'on' : 'off'

export function toolOrder<T extends { label: string; tools: { label: string }[] }>(skills: T[]): T[] {
  return [...skills].sort(byLabel).map(s => ({ ...s, tools: [...s.tools].sort(byLabel) }))
}

/* The source dot: filled, pulsing, amber, hollow. */
export type Level = 'ok' | 'working' | 'needs_you' | 'off'
