/* Models: every role's models, the chat model first, then the providers
   (the servers your models run on: llama-swap, Ollama, OpenRouter, Anthropic, any
   OpenAI-compatible server). Each role has an ordered list: the first model that can
   answer does the work, the next one steps in when it can't. Any provider can fill any
   role. ComfyUI and its presets (images and music) are on their own page (Media.tsx).


   Every line of state comes from the server (backends/router.py): reachable or not,
   what is loaded, a model that could not be loaded. A key is typed once and never
   shown again; a provider without its key offers no models. Every change, here or in
   another tab, shows at once (SETTINGS_EVENTS).
   API: server/celine/api/settings.py (lib/settingsApi.ts). */
import { useMemo, useState, type CSSProperties } from 'react'
import { ArrowDown, ArrowUp, KeyRound, MoreHorizontal, Pencil, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { errorText } from '../../lib/format'
import { format, useNav } from '../../lib/nav'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import {
  SETTINGS_EVENTS, settingsApi, type Available, type Cap, type KindInfo, type ModelConfig,
  type OptionSchema, type ProviderKind, type ProviderModel, type ProviderView, type RoleView,
  LINK,
} from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useAutosave } from '../../lib/useAutosave'
import { useResource, type Resource } from '../../lib/useResource'
import { ModelChooser, contextWords, findModel, modelDetail, modelName } from '../../components/ModelChooser'
import { EMPTY_PROVIDER, ProviderFields, host, kindInfo, ready, type ProviderDraft } from '../../components/ProviderFields'
import {
  Button, Checkbox, Choice, ConfirmDialog, Dialog, EmptyState, Field, Form, type FormField, InlineError, Input, ListRow, Loaded, LoadingRow, MenuButton, Notice, NumberField, SearchField, Section, StateWord, Step, SubHead, Switch, useContextMenu, VirtualList, type MenuItem, type WordTone,
} from '../../ui'
import { Page } from './parts'
import { ROLE } from '../../lib/media'
import './settings.css'

/* Providers and roles load on their own: the page and the roles show at once, the
   providers' state (a reachability check) lands in place when it comes. */
export function Models({ providerId }: { providerId?: string }) {
  const providers = useResource(() => settingsApi.providers(), [], { events: SETTINGS_EVENTS })
  const roles = useResource(() => settingsApi.roles().then(r => r.roles), [], { events: SETTINGS_EVENTS })
  const reload = () => { providers.reload(); roles.reload() }
  if (providerId) {
    const data = providers.data
    if (!data) {
      return <Page title="Models"><Section><Loaded res={providers} /></Section></Page>
    }
    const p = data.providers.find(x => x.id === providerId && x.provider.kind !== 'comfyui')
    if (!p) return <Page title="Models"><EmptyState title="Not found">Nothing at this link.</EmptyState></Page>
    // a save's answer in place at once, so an option never flips back while the list reloads
    const put = (v: ProviderView) => providers.set(d => d && { ...d, providers: d.providers.map(x => (x.id === v.id ? v : x)) })
    return <ProviderPage p={p} providers={data.providers} kinds={data.kinds} cloudHelp={data.cloud_help} onChanged={reload} onSaved={put} />
  }
  return <ModelsHome providers={providers} roles={roles} onChanged={reload} />
}

/* ── words for state ── */
/** a provider's reachability as its word; `tone` warn when it needs a look */
export function reach(p: ProviderView): { tone: WordTone; word: string } {
  if (p.state.key_missing) return { tone: 'warn', word: LINK.unset }
  if (p.state.reachable === true) return { tone: 'muted', word: LINK.up }
  if (p.state.reachable === false) return { tone: 'warn', word: LINK.down }
  return { tone: 'muted', word: 'Not checked' }
}

/** the reach word in its text tone */
export function ReachWord({ p }: { p: ProviderView }) {
  const st = reach(p)
  return <StateWord tone={st.tone}>{st.word}</StateWord>
}

/* ── the chat model first, then the other roles, then the providers ── */
function ModelsHome({ providers, roles, onChanged }: {
  providers: Resource<Awaited<ReturnType<typeof settingsApi.providers>>>
  roles: Resource<Record<string, RoleView>>
  onChanged: () => void
}) {
  const [adding, setAdding] = useState(false)
  // ComfyUI is on Images & Music
  const list = (providers.data?.providers ?? []).filter(p => p.provider.kind !== 'comfyui')
  const r = roles.data
  // every role but images and music (Images & Music), the chat model first
  const shown = Object.entries(r ?? {}).filter(([k]) => k !== ROLE.images && k !== ROLE.music)
    .sort(([a], [b]) => Number(b === 'chat') - Number(a === 'chat'))
  return (
    <Page title="Models">
      <Section title="Providers" help={providers.data?.help} count={providers.data ? list.length : undefined}
               action={list.length > 0 && <Button icon={Plus} onClick={() => setAdding(true)}>Add provider</Button>}>
        {providers.error && !providers.data ? <InlineError message={providers.error} onRetry={providers.reload} />
          : !providers.data ? <LoadingRow label="Checking providers" />
          : list.length === 0 ? (
            <EmptyState title="No providers yet"
                   action={<Button variant="primary" icon={Plus} onClick={() => setAdding(true)}>Add provider</Button>} />
          ) : (
            <div className="rows">
              {list.map(p => (
                <ListRow key={p.id} href={format({ section: 'settings', page: 'models', sub: p.id })} title={p.provider.label}
                         subtitle={p.state.error ? <StateWord tone="warn">{p.state.error}</StateWord> : undefined}
                         meta={<span className="reach"><ReachWord p={p} /></span>} />
              ))}
            </div>
          )}
      </Section>
      <Section title="Roles">
        {r ? shown.map(([k, info]) => <RoleRow key={k} head role={k} info={info} providers={list} onChanged={onChanged} />)
          : <Loaded res={roles} />}
      </Section>
      {adding && providers.data && (
        <AddProvider kinds={providers.data.kinds} cloudHelp={providers.data.cloud_help} taken={providers.data.providers.map(p => p.id)}
                     onClose={() => setAdding(false)} onAdded={onChanged} />
      )}
    </Page>
  )
}

/** a role's list of models (`onSave` writes it, else the role):
    a row per model in the order they are tried (numbered when there are two or more), its "..." to move or remove it,
    then Add model under it */
export function RoleRow({ role, info, providers, onChanged, fallback, onSave, head = false }: {
  role: string; info: RoleView; providers: ProviderView[]; onChanged: () => void
  /** the role's name over its list (the Models page) */
  head?: boolean
  /** what an empty list does instead ("The chat model") */
  fallback?: string
  onSave?: (chain: string[]) => Promise<unknown>
}) {
  const online = useOnline()
  const ctx = useContextMenu()
  const [choosing, setChoosing] = useState(false)
  const save = useAction(async (chain: string[]) => {
    await (onSave ? onSave(chain) : settingsApi.putRole(role, chain))
    onChanged()
  })
  const chain = info.chain
  const busy = !online || save.busy
  const swap = (i: number) => void save.run([...chain.slice(0, i), chain[i + 1]!, chain[i]!, ...chain.slice(i + 2)])
  const menu = (i: number, label: string): MenuItem[] => [
    { label: 'Move up', icon: ArrowUp, disabled: busy || i === 0, onSelect: () => swap(i - 1) },
    { label: 'Move down', icon: ArrowDown, disabled: busy || i === chain.length - 1, onSelect: () => swap(i) },
    'sep',
    { label: 'Remove', icon: X, danger: true, disabled: busy, title: `Remove ${label} from ${info.label}`,
      onSelect: () => void save.run(chain.filter(x => x !== chain[i])) },
  ]
  const add = (
    <Button icon={Plus} loading={save.busy} disabled={!online || !providers.length}
            title={!online ? OFFLINE_TITLE : providers.length ? undefined : 'Add provider first'}
            onClick={() => setChoosing(true)}>Add model</Button>
  )
  return (
    <div className="chain">
      {head && <SubHead help={info.help}>{info.label}</SubHead>}
      {info.models.length > 0 ? (
        <div className="rows">
          {info.models.map((m, i) => {
            // the server's sentence: a model gone from its provider, else its provider's view of it
            const problem = m.problem ?? findModel(m.ref, providers)?.model.problem ?? null
            const items = menu(i, m.label)
            return (
              <ListRow key={m.ref} density="lg" leading={info.models.length > 1 ? <Step n={i + 1} /> : undefined} title={m.label} subtitle={m.provider_label}
                       detail={problem && <StateWord tone="warn">{problem}</StateWord>}
                       onContextMenu={e => ctx.open(e, items, m.label)} press={ctx.press(() => items, m.label)}
                       trailing={<MenuButton icon={MoreHorizontal} label="More" items={items} size="sm" />} />
            )
          })}
        </div>
      ) : fallback && <p className="chain__empty">{fallback}</p>}
      <div>{add}</div>
      {save.error && <InlineError compact message={save.error} />}
      {ctx.menu}
      {choosing && (
        <ModelChooser title={`Add model to ${info.label}`} providers={providers} needs={info.needs} skip={chain}
                      onPick={ref => { setChoosing(false); void save.run([...chain, ref]) }}
                      onClose={() => setChoosing(false)} />
      )}
    </div>
  )
}

/* ── one provider ── */

// lists of OpenRouter provider names: edited in var/settings/providers.toml
const FILE_ONLY = new Set(['order', 'only', 'ignore', 'quantizations', 'backup_models', 'thinking_levels'])

/** the options of one provider; `others`: the llama-swap and tabbyAPI providers it can share a GPU with */
export function optionSchema(opts: OptionSchema[], others: ProviderView[]): FormField[] {
  return opts.filter(o => !FILE_ONLY.has(o.key)).map(o => {
    const base = { key: o.key, label: o.label, help: o.help ?? undefined, default: o.default, where: 'state' as const,
                   zero: o.zero ?? undefined, needs: o.needs ?? undefined }
    if (o.key === 'shares_gpu_with') {
      return { ...base, type: 'choice',
               choices: [['', 'No other provider'] as [string, string],
                         ...others.map(x => [x.id, x.provider.label] as [string, string])] }
    }
    if (o.type === 'bool') return { ...base, type: 'bool' }
    if (o.choices) return { ...base, type: 'choice', choices: o.choices }
    return { ...base, type: o.type === 'int' || o.type === 'num' ? 'number' : 'text', unit: o.unit }
  })
}

function ProviderPage({ p, providers, kinds, cloudHelp, onChanged, onSaved }: {
  p: ProviderView; providers: ProviderView[]; kinds: KindInfo[]; cloudHelp: string; onChanged: () => void; onSaved: (p: ProviderView) => void
}) {
  const kind = kindInfo(kinds, p.provider.kind)
  const nav = useNav()
  const online = useOnline()
  const ctx = useContextMenu()
  const check = useAction(async () => { await settingsApi.test(p.id); onChanged() })
  const [adding, setAdding] = useState(false)
  const [remove, setRemove] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [dropping, setDropping] = useState<ProviderModel | null>(null)
  const cfg = p.provider
  const opts = useAutosave(async changes => {
    onSaved(await settingsApi.putProvider(p.id, { ...cfg, options: { ...(cfg.options ?? {}), ...changes } }))
    onChanged()
  })
  const schema = useMemo(() => optionSchema(kind?.options ?? [], providers.filter(x => x.provider.kind === 'llama-swap' || x.provider.kind === 'tabby')),
                         [kind, providers])
  const keyed = kind?.key === 'required' || p.key.state === 'set' || cfg.kind === 'openai'
  const open = p.models.find(m => m.name === editing)
  const menu = (m: ProviderModel): MenuItem[] => [
    { label: 'Edit', icon: Pencil, onSelect: () => setEditing(m.name) },
    'sep',
    { label: 'Remove', icon: Trash2, danger: true, disabled: !online || m.used_by.length > 0,
      title: m.used_by.length ? `Used for ${m.used_by.join(', ')}` : undefined, onSelect: () => setDropping(m) },
  ]

  return (
    <Page title={cfg.label} up={{ page: 'models', label: 'Models' }} detail={<ReachWord p={p} />}
          actions={<Button icon={RefreshCw} loading={check.busy} disabled={!online} onClick={() => void check.run()}>Check provider</Button>}
          more={[{ label: 'Edit', icon: Pencil, disabled: !online, onSelect: () => setRenaming(true) },
                 { label: 'Remove this provider', icon: Trash2, danger: true, disabled: p.used_by.length > 0 || !online,
                   title: p.used_by.length ? `Used for ${p.used_by.join(', ')}` : undefined, onSelect: () => setRemove(true) }]}>
      {(p.state.error || check.error) && <InlineError message={check.error ?? p.state.error} />}

      {keyed && <KeySection p={p} required={kind?.key === 'required'} onChanged={onChanged} />}

      <Section title="Models" count={p.models.length || undefined} action={p.models.length > 0 &&
        <Button icon={Plus} disabled={!online} onClick={() => setAdding(true)}>Add models</Button>}>
        {p.models.length === 0 ? (
          <EmptyState compact action={<Button variant="primary" icon={Plus} disabled={!online} onClick={() => setAdding(true)}>Add models</Button>}>
            No models yet
          </EmptyState>
        ) : (
          <div className="rows">
            {p.models.map(m => {
              const trouble = m.problem
              const facts = modelDetail(m)
              const stale = !trouble && m.context && m.server_context && m.context !== m.server_context
                ? `Context size ${Math.round(m.context / 1024)}k differs from the server` : null
              const items = menu(m)
              return (
                <ListRow key={m.ref} density="lg" title={modelName(m)} subtitle={facts || undefined} aside={m.used_by.join(', ') || undefined}
                         detail={(trouble || stale) && <StateWord tone="warn">{trouble || stale}</StateWord>}
                         onClick={() => setEditing(m.name)} onContextMenu={e => ctx.open(e, items, modelName(m))}
                         press={ctx.press(() => items, modelName(m))}
                         trailing={<MenuButton icon={MoreHorizontal} label="More" items={items} size="sm" />} />
              )
            })}
          </div>
        )}
      </Section>

      {schema.length > 0 && (
        <Section title="Options">
          <Form fields={schema} values={{ ...cfg.options, ...opts.pending }} disabled={!online}
                onChange={(k, v) => opts.set(k, v)} />
          {opts.error && <InlineError compact message={opts.error} onRetry={opts.flush} />}
        </Section>
      )}

      {ctx.menu}
      {adding && <AddModels p={p} onClose={() => setAdding(false)} onAdded={onChanged} />}
      {renaming && <ProviderDialog p={p} kinds={kinds} cloudHelp={cloudHelp} onClose={() => setRenaming(false)} onChanged={onChanged} />}
      {open && <ModelDialog p={p} m={open} onClose={() => setEditing(null)} onChanged={onChanged} />}
      {dropping && (
        <ConfirmDialog title={`Remove ${modelName(dropping)}?`} danger confirmLabel="Remove"
                       body={`It is removed from ${cfg.label} and from every role that uses it.`}
                       onClose={() => setDropping(null)}
                       onConfirm={async () => {
                         try { await settingsApi.deleteModel(p.id, dropping.name); setDropping(null); onChanged() }
                         catch (e) { return errorText(e) }
                       }} />
      )}
      {remove && (
        <ConfirmDialog title={`Remove ${cfg.label}?`} danger confirmLabel="Remove"
                       body="Its models are removed from every role, and its saved key is deleted."
                       onClose={() => setRemove(false)}
                       onConfirm={async () => {
                         try { await settingsApi.deleteProvider(p.id); onChanged(); nav.go({ section: 'settings', page: 'models' }) }
                         catch (e) { return errorText(e) }
                       }} />
      )}
    </Page>
  )
}

/* ── one model of a provider, edited in a dialog that saves each change itself ──
   Its name, context size in tokens, parallel slots (requests at once; empty is one), an
   OpenRouter model's pinned provider slug (deepinfra/fp4), an Anthropic model's max tokens
   (empty is 8192), and the parameters the app sends with every request to it:
   llama-swap and llama-server thinking (chat_template_kwargs.enable_thinking), Ollama the
   reasoning effort (params.reasoning_effort; "none" is no thinking). A conversation's own
   thinking choice replaces these for its requests (settings/thinking.py). */
const LLAMA = new Set(['llama-swap', 'tabby', 'llama-server'])
const SLOTS = 1, MAX_TOKENS = 8192

type Llama = { chat_template_kwargs?: Record<string, unknown>; [k: string]: unknown }

function hasParams(kind: string, m: ProviderModel): boolean {
  return (LLAMA.has(kind) || kind === 'ollama' || kind === 'openrouter' || kind === 'openai') && (m.caps ?? []).includes('chat')
}

/** the model's stored settings with its llama block changed; empty parts are dropped */
function withLlama(cfg: ModelConfig, change: (l: Llama) => Llama): ModelConfig {
  const next = change(structuredClone((cfg.llama ?? {}) as Llama))
  const kw = next.chat_template_kwargs
  if (kw && Object.keys(kw).length === 0) delete next.chat_template_kwargs
  const { llama: _old, ...rest } = cfg
  return Object.keys(next).length ? { ...rest, llama: next } : rest
}

function withParams(cfg: ModelConfig, key: string, value: unknown): ModelConfig {
  const params = { ...(cfg.params ?? {}) }
  if (value === undefined) delete params[key]
  else params[key] = value
  const { params: _old, ...rest } = cfg
  return Object.keys(params).length ? { ...rest, params } : rest
}

const whole = (t: string) => !t.trim() || /^[1-9]\d*$/.test(t.trim())

type ModelDraft = { label: string; context: string; slots: string; maxTokens: string; slug: string; caps: Cap[]; thinking: string; effort: string }

/** a switch or a choice saves at once, a name or a number when you leave its field (a
    number only while it is whole); Close keeps what was saved */
function ModelDialog({ p, m, onClose, onChanged }: { p: ProviderView; m: ProviderModel; onClose: () => void; onChanged: () => void }) {
  const online = useOnline()
  const kind = p.provider.kind
  const stored = (p.provider.models[m.name] ?? {}) as ModelConfig
  const llama = LLAMA.has(kind)
  const params = hasParams(kind, m)
  const kw = ((stored.llama ?? {}) as Llama).chat_template_kwargs?.enable_thinking
  const [d, setD] = useState<ModelDraft>(() => ({
    label: stored.label ?? '', context: stored.context ? String(stored.context) : '', slots: String(stored.slots ?? SLOTS),
    maxTokens: String(stored.max_tokens ?? MAX_TOKENS), slug: stored.provider ?? '', caps: stored.caps ?? m.caps ?? ['chat'],
    thinking: kw === true ? 'on' : kw === false ? 'off' : '',
    effort: typeof stored.params?.reasoning_effort === 'string' ? stored.params.reasoning_effort : '',
  }))
  const invalid = (t: string) => !whole(t)
  const ok = ![d.context, d.slots, d.maxTokens].some(invalid)
  const auto = useAutosave<ModelConfig>(async c => { await settingsApi.putModel(p.id, m.name, c.model!); onChanged() })
  const build = (x: ModelDraft): ModelConfig => {
    const num = (t: string, dflt?: number) => (t.trim() && Number(t) !== dflt ? Number(t) : undefined)
    let next: ModelConfig = { ...stored, label: x.label.trim() || undefined, context: num(x.context), slots: num(x.slots, SLOTS),
                              caps: x.caps.length ? x.caps : undefined,
                              max_tokens: kind === 'anthropic' ? num(x.maxTokens, MAX_TOKENS) : stored.max_tokens,
                              provider: kind === 'openrouter' ? x.slug.trim() || undefined : stored.provider }
    if (params && llama && !m.thinking_offered?.length) {
      next = withLlama(next, l => {
        const k = { ...(l.chat_template_kwargs ?? {}) }
        if (x.thinking) k.enable_thinking = x.thinking === 'on'
        else delete k.enable_thinking
        return { ...l, chat_template_kwargs: k }
      })
    } else if (params) next = withParams(next, 'reasoning_effort', x.effort || undefined)
    return Object.fromEntries(Object.entries(next).filter(([, v]) => v !== undefined)) as ModelConfig
  }
  const change = (patch: Partial<ModelDraft>, when: 'now' | 'blur' = 'now') => {
    const x = { ...d, ...patch }
    setD(x)
    if (![x.context, x.slots, x.maxTokens].some(invalid)) auto.set('model', build(x), when)
  }
  const text = (k: 'label' | 'context' | 'slots' | 'maxTokens' | 'slug') => ({
    value: d[k], disabled: !online, onChange: (v: string) => change({ [k]: v }, 'blur'), onBlur: auto.flush,
  })
  const words = m.thinking_offered ?? []
  const efforts = [{ value: '', label: 'Default' }, { value: 'none', label: 'Off' },
                   ...[...words, ...(d.effort && d.effort !== 'none' && !words.includes(d.effort) ? [d.effort] : [])].map(w => ({ value: w, label: w }))]
  return (
    <Dialog title={modelName(m)} onClose={onClose} size="sm" actions={<Button onClick={onClose}>Close</Button>}>
      <form className="dialog__form" onSubmit={e => { e.preventDefault(); auto.flush() }}>
        <Field label="Name">
          <Input {...text('label')} placeholder={m.name} onChange={e => change({ label: e.target.value }, 'blur')} />
        </Field>
        <NumberField label="Context size" unit="tokens" min={1} {...text('context')} invalid={invalid(d.context)}
                     hint={m.server_context ? `Server: ${m.server_context.toLocaleString('en-GB')}` : undefined} />
        <NumberField label="Parallel slots" min={1} {...text('slots')} invalid={invalid(d.slots)} />
        {kind === 'anthropic' && <NumberField label="Max tokens" min={1} {...text('maxTokens')} invalid={invalid(d.maxTokens)} />}
        {kind === 'openrouter' && (
          <Field label="Upstream provider">
            <Input mono {...text('slug')} placeholder="deepinfra/fp4" spellCheck={false} onChange={e => change({ slug: e.target.value }, 'blur')} />
          </Field>
        )}
        <Field label="Capabilities" group>
          <div className="settings__caps">
            {(['chat', 'tools', 'vision'] as const).map(c => (
              <Switch key={c} label={c.charAt(0).toUpperCase() + c.slice(1)}
                      checked={stated(m, c) ?? d.caps.includes(c)} disabled={!online || stated(m, c) !== null}
                      onChange={v => change({ caps: v ? [...d.caps, c] : d.caps.filter(x => x !== c) })} />
            ))}
          </div>
        </Field>
        {params && (llama && !m.thinking_offered?.length ? (
          <Field label="Thinking" group>
            <Choice label={`Thinking of ${modelName(m)}`} value={d.thinking} disabled={!online} onChange={v => change({ thinking: v })}
                    options={[{ value: '', label: 'Default' }, { value: 'on', label: 'On' }, { value: 'off', label: 'Off' }]} />
          </Field>
        ) : (
          <Field label="Thinking" group>
            <Choice label={`Thinking of ${modelName(m)}`} value={d.effort} disabled={!online} onChange={v => change({ effort: v })} options={efforts} />
          </Field>
        ))}
        {!ok ? <InlineError compact message="Enter a whole number." />
          : auto.error && <InlineError compact message={auto.error} onRetry={auto.flush} />}
      </form>
    </Dialog>
  )
}

/* A key is write-only: typed once, stored on the server, never shown again. */
/** a provider's type, address, Cloud and name: Save checks the address (Auto-detect asks the
    server what answers there), saves what it found and asks the provider's probe (its key:
    KeySection). Cloud is saved only once the user sets it; left alone, or after Use the
    default, the address says (null) */
function ProviderDialog({ p, kinds, cloudHelp, onClose, onChanged }: {
  p: ProviderView; kinds: KindInfo[]; cloudHelp: string; onClose: () => void; onChanged: () => void
}) {
  const online = useOnline()
  const [d, setD] = useState<ProviderDraft>({ kind: p.provider.kind, url: p.provider.base_url, key: '', label: p.provider.label })
  const [cloud, setCloud] = useState(p.provider.cloud ?? undefined)
  // p.cloud is the address's word only while the saved provider has no switch of its own
  const said = d.url.trim() === p.provider.base_url && p.provider.cloud == null ? p.cloud : 'mixed'
  const ok = !!d.url.trim()
  const save = useAction(async () => {
    const r = await settingsApi.testNew({ kind: d.kind || undefined, base_url: d.url.trim() })
    if (!r.kind) throw new Error(r.sentence)
    await settingsApi.putProvider(p.id, { ...p.provider, kind: r.kind, label: d.label.trim() || p.provider.label, base_url: r.base_url, cloud: cloud ?? null })
    await settingsApi.test(p.id)
    onChanged(); onClose()
  })
  return (
    <Dialog title={p.provider.label} onClose={onClose} size="sm" dismissable={!save.busy}
            actions={<>
              <Button variant="secondary" onClick={onClose} disabled={save.busy}>Cancel</Button>
              <Button variant="primary" loading={save.busy} disabled={!online || !ok} onClick={() => void save.run()}>Save</Button>
            </>}>
      <form className="dialog__form" onSubmit={e => { e.preventDefault(); if (online && ok) void save.run() }}>
        <ProviderFields kinds={kinds} value={d} onChange={setD} withKey={false}
                        cloud={{ checked: cloud ?? said, help: cloudHelp, onChange: setCloud,
                                 onReset: cloud === undefined ? undefined : () => setCloud(undefined) }} />
        {save.error && <InlineError compact message={save.error} />}
      </form>
    </Dialog>
  )
}

export function KeySection({ p, required, onChanged }: { p: ProviderView; required: boolean; onChanged: () => void }) {
  const online = useOnline()
  const [key, setKey] = useState('')
  const save = useAction(async () => { await settingsApi.setKey(p.id, key.trim()); setKey(''); onChanged() })
  const drop = useAction(async () => { await settingsApi.deleteKey(p.id); onChanged() })
  return (
    <Section title={required ? 'API key' : 'API key (optional)'}>
      <form className="row-actions" onSubmit={e => { e.preventDefault(); if (key.trim()) void save.run() }}>
        <Input type="password" autoComplete="off" spellCheck={false} value={key} onChange={e => setKey(e.target.value)}
               placeholder={p.key.state === 'set' ? 'New key' : 'Paste the key'} aria-label="API key" className="key-input" />
        <Button type="submit" variant="primary" icon={KeyRound} loading={save.busy} disabled={!key.trim() || !online}>Save key</Button>
        {p.key.state === 'set' && <Button variant="ghost" loading={drop.busy} disabled={!online} onClick={() => void drop.run()}>Remove key</Button>}
      </form>
      {(save.error || drop.error) && <InlineError compact message={save.error ?? drop.error} />}
    </Section>
  )
}

function AddModels({ p, onClose, onAdded }: { p: ProviderView; onClose: () => void; onAdded: () => void }) {
  const list = useResource(() => settingsApi.available(p.id), [p.id])
  const [q, setQ] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [zdr, setZdr] = useState(false)
  const [provider, setProvider] = useState('')
  const shown = useMemo(() => {
    const f = q.trim().toLowerCase()
    return (list.data?.models ?? []).filter(d => !d.added)
      .filter(d => !f || d.id.toLowerCase().includes(f) || (d.name ?? '').toLowerCase().includes(f))
  }, [list.data, q])
  // a model the server does not list: the name as typed
  const typed = q.trim()
  const unlisted = !!typed && !/[\s'"]/.test(typed) && !(list.data?.models ?? []).some(d => d.id === typed)
  const addTyped = useAction(async () => {
    await settingsApi.putModel(p.id, typed, { label: typed, ...(zdr ? { zdr: true } : {}), ...(provider.trim() ? { provider: provider.trim() } : {}) })
    onAdded(); onClose()
  })
  const add = useAction(async () => {
    for (const id of picked) {
      const d = list.data?.models.find(x => x.id === id)
      const m: ModelConfig = { label: d?.name || id, ...(d?.caps ? { caps: d.caps } : {}), ...(d?.context ? { context: d.context } : {}),
                               ...(d?.thinking_levels?.length ? { thinking_levels: d.thinking_levels } : {}),
                               ...(zdr ? { zdr: true } : {}),
                               ...(provider.trim() ? { provider: provider.trim() } : {}) }
      await settingsApi.putModel(p.id, id, m)
    }
    onAdded(); onClose()
  })
  return (
    <Dialog title={`Add models from ${p.provider.label}`} onClose={onClose} size="lg" dismissable={!add.busy}>
      <div className="dialog__form">
        <SearchField value={q} onChange={setQ} placeholder="Search models or type a model ID" autoFocus />
        {unlisted && (
          <Button variant="secondary" icon={Plus} loading={addTyped.busy} disabled={add.busy} onClick={() => void addTyped.run()}>
            Add "{typed}"
          </Button>
        )}
        {list.error && !list.data ? <InlineError compact message={list.error} onRetry={list.reload} />
          : !list.data ? <LoadingRow label="Loading" />
          : shown.length === 0 ? (unlisted ? null : <EmptyState compact>{q ? 'No models match' : 'All models from this provider are already added.'}</EmptyState>)
          : (
            <div className="boxed-list boxed-list--dialog" style={{ '--rows': shown.length } as CSSProperties}>
              <VirtualList items={shown} estimate={44} getKey={d => d.id} resetOn={q}
                           renderRow={(d: Available) => (
                             <label className="pick-row">
                               <Checkbox checked={picked.has(d.id)} ariaLabel={`Add ${d.name || d.id}`}
                                         onChange={v => setPicked(s => { const n = new Set(s); if (v) n.add(d.id); else n.delete(d.id); return n })} />
                               <span className="pick-row__name">{d.name || d.id}</span>
                               <span className="pick-row__meta">
                                 {[capWords(d.caps), d.context ? contextWords(d.context) : '', d.loaded ? 'loaded' : '',
                                                d.price?.in_per_m != null ? `$${d.price.in_per_m} / $${d.price.out_per_m ?? '?'} per M tokens` : '']
                                   .filter(Boolean).join(', ').replace(/^./, c => c.toUpperCase())}
                               </span>
                             </label>
                           )} />
            </div>
          )}
        {p.provider.kind === 'openrouter' && (
          <>
            <Switch label="Zero data retention" checked={zdr} disabled={add.busy} onChange={setZdr} />
            <Field label="Upstream provider">
              <Input mono value={provider} disabled={add.busy} placeholder="deepinfra/fp4" spellCheck={false}
                     onChange={e => setProvider(e.target.value)} />
            </Field>
          </>
        )}
        {(add.error || addTyped.error) && <InlineError compact message={add.error ?? addTyped.error} />}
        <div className="dialog__actions">
          <Button variant="secondary" onClick={onClose} disabled={add.busy}>Cancel</Button>
          <Button variant="primary" loading={add.busy} disabled={picked.size === 0} onClick={() => void add.run()}>
            {picked.size ? `Add ${picked.size === 1 ? '1 model' : `${picked.size} models`}` : 'Add'}
          </Button>
        </div>
      </div>
    </Dialog>
  )
}

/** capabilities as words: "chat, tools", "image generation" */
/** whether the server states the model has `c` (tools, vision); null when it does not say */
const stated = (m: ProviderModel, c: Cap): boolean | null =>
  m.server_caps && (c === 'tools' || c === 'vision') ? m.server_caps.includes(c) : null
const capWords = (caps: string[] | undefined) => (caps ?? []).map(c => c.replace(/_/g, ' ')).join(', ')

/* ── Add provider: check the address first, then save ── */
export function slug(label: string, taken: string[]): string {
  const base = label.toLowerCase().replace(/[^a-z0-9_.-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'provider'
  let id = base, n = 2
  while (taken.includes(id)) id = `${base}-${n++}`
  return id
}

function AddProvider({ kinds, cloudHelp, taken, onClose, onAdded }: {
  kinds: KindInfo[]; cloudHelp: string; taken: string[]; onClose: () => void; onAdded: () => void
}) {
  const nav = useNav()
  const online = useOnline()
  // d.kind '' is Auto-detect: the server says what answers, and its answer fills Type and Address
  const [d, setD] = useState<ProviderDraft>(EMPTY_PROVIDER)
  // Cloud as the user set it; unset, the check says what the address is and nothing is saved
  const [cloud, setCloud] = useState<boolean>()
  const [checked, setChecked] = useState<Awaited<ReturnType<typeof settingsApi.testNew>> | null>(null)
  const check = useAction(async () => {
    setChecked(null)
    const r = await settingsApi.testNew({ kind: d.kind || undefined, base_url: d.url.trim(), key: d.key.trim() || undefined })
    if (r.kind && !d.kind) setD(x => ({ ...x, kind: r.kind!, url: r.base_url }))
    setChecked(r)
    // a missing key is no reason to stop: the provider page's empty key field asks for it
    if ((r.ok || r.fix === 'add_key') && r.kind) await save(r.kind, r.base_url)
  })
  const save = async (k: ProviderKind, base_url: string) => {
    const at = kindInfo(kinds, k)
    const name = d.label.trim() || (at?.default_url ? at.label : host(base_url))
    const id = slug(name, taken)
    await settingsApi.putProvider(id, { kind: k, label: name, base_url, cloud, models: {} })
    if (d.key.trim()) await settingsApi.setKey(id, d.key.trim())
    onAdded(); onClose()
    nav.go({ section: 'settings', page: 'models', sub: id })
  }
  const anyway = useAction(async () => { if (checked?.kind) await save(checked.kind, checked.base_url) })
  return (
    <Dialog title="Add provider" onClose={onClose} size="md" dismissable={!check.busy && !anyway.busy}>
      <form className="dialog__form" onSubmit={e => { e.preventDefault(); void check.run() }}>
        <ProviderFields kinds={kinds} value={d} onChange={v => { setD(v); setChecked(null) }} autoFocus
                        cloud={{ checked: cloud ?? checked?.cloud ?? 'mixed', help: cloudHelp, onChange: setCloud,
                                 onReset: cloud === undefined ? undefined : () => setCloud(undefined) }} />
        {checked && !checked.ok && !check.busy && (
          <Notice tone="warn" action={checked.kind ? { label: 'Add anyway', onClick: () => void anyway.run(), busy: anyway.busy } : undefined}>
            {checked.sentence}
          </Notice>
        )}
        {(check.error || anyway.error) && <InlineError compact message={check.error ?? anyway.error} />}
        <div className="dialog__actions">
          <Button variant="secondary" onClick={onClose} disabled={check.busy}>Cancel</Button>
          <Button type="submit" variant="primary" loading={check.busy} disabled={!online || !ready(kinds, d)}>Check and add</Button>
        </div>
      </form>
    </Dialog>
  )
}
