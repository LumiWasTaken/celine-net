/* The setup forms, one per `setup` id of the feature catalog (Welcome.tsx and Settings,
   Features pick them by it): comfyui (one address for Images and Music: the saved ComfyUI
   provider's to start with; Music says "Same ComfyUI as Images" once Images has one), email
   (the account, Check email logs in), vault (the server fields and the client secret), search
   (what an embedding and a reranker model give and how to run one, then the Topic search and
   Reranking models out of what the saved providers offer, a pick saves at once), guard (Bastet's
   Decision model, the `small` role, and its Second look model, PUT /guard/rules, each with what
   it does and what happens without it; a pick saves at once; no chat model at all: the way to
   one), service (the shellbox and its browser: Address and the
   read-only Key hash with Copy, what the box's key.hash file holds, shown while the box does
   not take the key), address (SearXNG: the
   address alone) and routines (every routine as a row: its switch, its label and summary, its
   time; a switch saves at once, a time when the field is left: PUT /schedules/<name> with the
   whole routine). Check is the one action of a form: it saves, then asks the card's probe
   anew (`reprobe`), so the card's or row's state line follows. Every form keeps its draft in
   the caller's state (so Back keeps what was typed) and hands its save to `saveRef`, which
   the onboarding's Next runs before it saves the features. All through the Settings
   endpoints. */
import { Copy } from 'lucide-react'
import { useEffect, useRef, useState, type MutableRefObject, type ReactElement } from 'react'
import { format, useNav } from '../lib/nav'
import { OFFLINE_TITLE, useOnline } from '../lib/socket'
import { routinesApi, type Schedule } from '../lib/routines'
import { LINK, settingsApi, type Available, type Cap, type Feature, type ProviderView, type RoleView } from '../lib/settingsApi'
import { useAction } from '../lib/useAction'
import { useResource } from '../lib/useResource'
import { Button, Choice, Dot, Field, InlineError, Input, LoadingRow, Switch, copied } from '../ui'
import { reach } from './settings/Models'
import { SchemaForm } from './settings/parts'
import './Welcome.css'
import { speechApi, type Direction, type SpeechEndpoint } from '../lib/speechApi'
import { SpeechTest } from '../components/SpeechTest'

export type SaveRef = MutableRefObject<(() => Promise<void>) | null>
type FormProps<T> = {
  feature: Feature
  value: T | undefined
  onChange: (v: T) => void
  saveRef: SaveRef
  /** the card's probe, asked anew: its state line follows what the form just saved */
  reprobe: () => Promise<unknown>
}

/** the form of a `setup` id; an id not here shows no form */
export const FORMS: Record<string, (p: FormProps<any>) => ReactElement> = {
  comfyui: ComfyuiForm, email: EmailForm, vault: VaultForm, search: SearchForm, guard: GuardForm, service: ServiceForm, address: AddressForm,
  routines: RoutinesForm, speech: SpeechForm,
}

type SpeechDraft = { endpoint: SpeechEndpoint; key: string; clear: boolean }
function SpeechForm({ feature, value, onChange, saveRef, reprobe }: FormProps<SpeechDraft>) {
  const direction: Direction = feature.id === 'speech_input' ? 'input' : 'output'
  const res = useResource(signal => speechApi.settings(direction, signal), [direction])
  const [models, setModels] = useState<string[]>([])
  const d = value ?? (res.data ? { endpoint: res.data.endpoint, key: '', clear: false } : null)
  const discovering = useRef<AbortController | null>(null)
  const latest = useRef(d); latest.current = d
  useEffect(() => {
    setModels([])
    return () => discovering.current?.abort()
  }, [d?.endpoint.url, direction])
  const save = async () => {
    if (!d || !res.data) return
    const saved = await speechApi.save(direction, d.endpoint, res.data.version, d.clear ? null : d.key || undefined)
    res.set(saved)
    onChange({ ...d, key: '', clear: false })
  }
  saveRef.current = save
  const action = useAction(async () => { await save(); await reprobe() })
  const discover = useAction(async () => {
    discovering.current?.abort()
    const c = new AbortController(); discovering.current = c
    await save()
    if (c.signal.aborted) return
    const found = await speechApi.models(direction, c.signal)
    if (c.signal.aborted) return
    setModels(found.models)
    if (!found.models.length) throw new Error('The provider returned no speech models.')
    const current = latest.current
    if (found.models.length === 1 && current && !current.endpoint.model) onChange({ ...current, endpoint: { ...current.endpoint, model: found.models[0] } })
  })
  if (!d) return res.error ? <InlineError compact message={res.error} onRetry={res.reload} /> : <LoadingRow label="Loading" />
  return <>
    <SchemaForm plain schema={(res.data?.fields ?? []).filter(f => f.key !== 'model')} values={d.endpoint}
      onChange={(k, v) => onChange({ ...d, endpoint: { ...d.endpoint, [k]: v } })} />
    <Field label="API key"><Input type="password" autoComplete="off" value={d.key}
      placeholder={res.data?.key.state === 'set' ? 'Saved' : 'Optional'}
      onChange={e => onChange({ ...d, key: e.target.value, clear: false })} /></Field>
    {res.data?.key.state === 'set' && <Switch label="Clear saved key" checked={d.clear}
      onChange={clear => onChange({ ...d, clear, key: '' })} />}
    <CheckRow label="Discover models" busy={discover.busy} disabled={!d.endpoint.url?.trim() || action.busy}
      error={discover.error} onClick={() => void discover.run()} />
    {models.length > 0 && <Choice label="Available speech models" value={d.endpoint.model ?? ''}
      options={Array.from(new Set([d.endpoint.model, ...models].filter((v): v is string => !!v))).map(v => ({ value: v, label: v }))}
      onChange={model => onChange({ ...d, endpoint: { ...d.endpoint, model } })} />}
    <Field label="Model"><Input value={d.endpoint.model ?? ''} placeholder="Discover or enter a model ID"
      onChange={e => onChange({ ...d, endpoint: { ...d.endpoint, model: e.target.value } })} /></Field>
    <CheckRow label="Save" busy={action.busy} disabled={!d.endpoint.url?.trim() || discover.busy}
      error={action.error} onClick={() => void action.run()} />
    <SpeechTest direction={direction} identity={JSON.stringify(d.endpoint)}
      ready={!!d.endpoint.url?.trim() && !!d.endpoint.model?.trim() && !action.busy && !discover.busy} save={save} />
  </>
}

/** the Check row: the one button of a form, and the error of its last run */
function CheckRow({ label = 'Check', busy, disabled, error, onClick }: {
  label?: string; busy: boolean; disabled: boolean; error: string | null; onClick: () => void
}) {
  const online = useOnline()
  return (
    <>
      <div className="onb__check">
        <Button loading={busy} disabled={!online || disabled} title={online ? undefined : OFFLINE_TITLE} onClick={onClick}>{label}</Button>
      </div>
      {error && <InlineError compact message={error} />}
    </>
  )
}

/* ── comfyui: one address, Images and Music share the draft; the saved ComfyUI provider's
   address to start with; Check saves it (PUT that provider, a new `comfyui` when none is
   saved) and asks the probe. Music with an address typed on Images: one muted line ── */
type Comfy = { base_url: string }

function ComfyuiForm({ feature, value, onChange, saveRef, reprobe }: FormProps<Comfy>) {
  const providers = useResource(() => settingsApi.providers(), [])
  const saved = providers.data?.providers.find(p => p.provider.kind === 'comfyui')
  const d = value ?? (providers.data ? { base_url: saved?.provider.base_url ?? '' } : null)
  const save = async () => {
    if (d?.base_url.trim() && d.base_url.trim() !== saved?.provider.base_url) {
      await settingsApi.putComfyui(d.base_url.trim(), saved)
      providers.reload()
    }
  }
  saveRef.current = save
  const check = useAction(async () => { await save(); await reprobe() })
  if (!d) return providers.error ? <InlineError compact message={providers.error} onRetry={providers.reload} /> : <LoadingRow label="Loading" />
  if (feature.id === 'music' && d.base_url.trim()) return <p className="onb__line">Same ComfyUI as Images</p>
  return (
    <>
      <Field label="Address">
        <Input value={d.base_url} placeholder="http://192.168.1.20:8188" spellCheck={false}
               onChange={e => onChange({ base_url: e.target.value })} />
      </Field>
      <CheckRow busy={check.busy} disabled={!d.base_url.trim()} error={check.error} onClick={() => void check.run()} />
    </>
  )
}

/* ── email: the server's fields and the password; Check email saves, logs in, and asks the
   probe ── */
type Mail = { email: Record<string, unknown>; password: string; checked: { ok: boolean; sentence: string } | null }

function EmailForm({ value, onChange, saveRef, reprobe }: FormProps<Mail>) {
  const online = useOnline()
  const res = useResource(() => settingsApi.email(), [])
  const d = value ?? (res.data ? { email: res.data.email, password: '', checked: null } : null)
  const save = async () => {
    if (!d) return
    await settingsApi.putEmail(d.email, d.password || undefined)
  }
  const test = useAction(async () => {
    if (!d) return
    await save()
    const r = await settingsApi.checkEmail()
    res.reload()
    onChange({ ...d, password: '', checked: { ok: r.ok, sentence: r.sentence } })
    await reprobe()
  })
  saveRef.current = save
  if (!d) return res.error ? <InlineError compact message={res.error} onRetry={res.reload} /> : <LoadingRow label="Loading" />
  const set = res.data?.password.state === 'set'
  return (
    <>
      <SchemaForm schema={res.data?.fields ?? []} values={d.email} disabled={!online} plain
                  onChange={(k, v) => onChange({ ...d, email: { ...d.email, [k]: v }, checked: null })} />
      <Field label="Password">
        <Input type="password" autoComplete="off" spellCheck={false} value={d.password} placeholder={set ? 'Saved' : undefined}
               onChange={e => onChange({ ...d, password: e.target.value, checked: null })} />
      </Field>
      <div className="onb__check">
        <Button loading={test.busy} disabled={!online || !(d.password || set)} title={online ? undefined : OFFLINE_TITLE}
                onClick={() => void test.run()}>Check email</Button>
        <span className="onb__state">
          {d.checked && <Dot tone={d.checked.ok ? 'ok' : 'error'} text={d.checked.ok ? LINK.up : d.checked.sentence || LINK.down} />}
        </span>
      </div>
      {test.error && <InlineError compact message={test.error} />}
    </>
  )
}

/* ── vault: the server's fields (each one's help as its tooltip) and the client secret;
   Check saves (PUT /vault) and asks the probe ── */
type Vault = { vault: Record<string, unknown>; secret: string }

function VaultForm({ value, onChange, saveRef, reprobe }: FormProps<Vault>) {
  const online = useOnline()
  const res = useResource(() => settingsApi.vault(), [])
  const d = value ?? (res.data ? { vault: res.data.vault, secret: '' } : null)
  const save = async () => {
    if (d) await settingsApi.putVault(d.vault, d.secret ? { client_secret: d.secret } : {})
  }
  const check = useAction(async () => {
    if (!d) return
    await save()
    res.reload()
    onChange({ ...d, secret: '' })
    await reprobe()
  })
  saveRef.current = save
  if (!d) return res.error ? <InlineError compact message={res.error} onRetry={res.reload} /> : <LoadingRow label="Loading" />
  return (
    <>
      <SchemaForm schema={res.data?.fields ?? []} values={d.vault} disabled={!online} plain
                  onChange={(k, v) => onChange({ ...d, vault: { ...d.vault, [k]: v } })} />
      <Field label="Client secret">
        <Input type="password" autoComplete="off" spellCheck={false} value={d.secret}
               placeholder={res.data?.client_secret.state === 'set' ? 'Saved' : undefined}
               onChange={e => onChange({ ...d, secret: e.target.value })} />
      </Field>
      <CheckRow busy={check.busy} disabled={false} error={check.error} onClick={() => void check.run()} />
    </>
  )
}

/* ── the model pickers: one heading per saved provider, its models under it, out of the
   saved ones and what it offers but has not added yet ("+pid/model": added on save) ── */
type Offer = { p: ProviderView; available: Available[] }

/** every saved provider with what it offers but has not added yet */
const useOffers = () => useResource(async () => {
  const { providers } = await settingsApi.providers()
  return Promise.all(providers.map(async p => {
    const available = await settingsApi.available(p.id).then(a => a.models, () => [] as Available[])
    return { p, available } as Offer
  }))
}, [])

/** the Choice options of one capability: None first when `none`, then each provider's saved
    models and its offered ones ("+pid/model": added on save), a model once */
const modelOptions = (offers: Offer[], cap: Cap, none: boolean) => [
  ...(none ? [{ value: '', label: 'None' }] : []),
  ...offers.flatMap(({ p, available }) => {
    const saved = p.models.filter(m => m.offered && (m.caps ?? []).includes(cap)).map(m => ({ value: m.ref, label: m.name }))
    const have = new Set(p.models.map(m => m.ref.slice(p.id.length + 1)))
    const more = available.filter(a => !a.added && !have.has(a.id) && (a.caps ?? []).includes(cap))
      .map(a => ({ value: `+${p.id}/${a.id}`, label: a.name || a.id }))
    const all = [...saved, ...more]
    return all.length ? [{ group: p.provider.label, options: all }] : []
  }),
]

/** a pick's ref: a "+pid/model" pick is added first */
async function addPick(offers: Offer[] | undefined, want: string, cap: Cap) {
  if (!want.startsWith('+')) return want
  const [pid, ...rest] = want.slice(1).split('/')
  const id = rest.join('/')
  const a = offers?.find(o => o.p.id === pid)?.available.find(m => m.id === id)
  return (await settingsApi.putModel(pid!, id, { label: a?.name || id, caps: a?.caps ?? [cap] })).ref
}

/* ── search: Topic search (embeddings) and Reranking (rerank), one model each or none; a
   pick saves at once (PUT /roles/{role}), then the card's state line is asked anew ── */
const ROLES: { role: string; cap: Cap }[] = [{ role: 'embeddings', cap: 'embeddings' }, { role: 'reranker', cap: 'rerank' }]
type Picks = Partial<Record<string, string>>

function SearchForm({ value, onChange, saveRef, reprobe }: FormProps<Picks>) {
  const online = useOnline()
  const roles = useResource(() => settingsApi.roles(), [])
  const offers = useOffers()
  const picks = value ?? {}
  const current = (role: string) => roles.data?.roles[role]?.chain[0] ?? ''
  const picked = (role: string) => picks[role] ?? current(role)
  // the saved picks fill the draft once, so Back shows them as they are
  useEffect(() => { if (!value && roles.data) onChange(Object.fromEntries(ROLES.map(r => [r.role, current(r.role)]))) }, [roles.data]) // eslint-disable-line react-hooks/exhaustive-deps
  saveRef.current = null
  const pick = useAction(async (role: string, cap: Cap, want: string) => {
    onChange({ ...picks, [role]: want })
    const ref = await addPick(offers.data, want, cap)
    await settingsApi.putRole(role, ref ? [ref] : [])
    roles.reload()
    await reprobe()
  })
  if (!roles.data || !offers.data) {
    const err = roles.error ?? offers.error
    return err ? <InlineError compact message={err} onRetry={() => { roles.reload(); offers.reload() }} /> : <LoadingRow label="Loading" />
  }
  return (
    <>
      <p className="onb__about">
        An embedding model lets Search find messages by topic, not only by their words, and a reranker model puts the best
        matches first. Any OpenAI-compatible server can run them, for example Ollama: run <code>ollama pull nomic-embed-text</code> on
        the server from Connect a model and it shows up here. You can also pick them later in Settings, Models, under Topic search and Reranking.
      </p>
      {ROLES.map(({ role, cap }) => {
        const info: RoleView | undefined = roles.data!.roles[role]
        if (!info) return null
        return (
          <Field key={role} label={info.label} group>
            <Choice list label={info.label} value={picked(role)} disabled={!online || pick.busy} options={modelOptions(offers.data!, cap, true)}
                    onChange={v => void pick.run(role, cap, v)} />
          </Field>
        )
      })}
      {pick.error && <InlineError compact message={pick.error} />}
    </>
  )
}

/* ── guard: Bastet's Decision model (the `small` role: PUT /roles/small) and her Second look
   model (guard.toml [chain]: PUT /guard/rules {chain: {provider, model}}, "" for None), each
   out of the chat models the saved providers offer, with two sentences under each (what it
   does, what Bastet does without it) and the state of its provider; a pick saves at once,
   then the card's state line is asked anew. With no chat model saved at all: a line and
   the way to one (Connect a model in the onboarding, Open Models in Settings) ── */
type Guard = { small: string; chain: string }

/** the picked model's state: its provider's reach, or that it is gone */
function ModelState({ offers, value }: { offers: Offer[]; value: string }) {
  if (!value) return null
  const ref = value.replace(/^\+/, '')
  const o = offers.find(x => ref.startsWith(`${x.p.id}/`))
  if (!o || (!value.startsWith('+') && !o.p.models.some(m => m.ref === ref))) return <Dot tone="warn" text="Not on its provider any more." />
  const { tone, word } = reach(o.p)
  return <Dot tone={tone} text={`${o.p.provider.label}: ${word}`} />
}

function GuardForm({ value, onChange, saveRef, reprobe }: FormProps<Guard>) {
  const online = useOnline()
  const nav = useNav()
  const roles = useResource(() => settingsApi.roles(), [])
  const rules = useResource(() => settingsApi.guardRules(), [])
  const offers = useOffers()
  const small = roles.data?.roles.small?.chain[0] ?? ''
  const chain = rules.data?.chain?.model ? `${rules.data.chain.provider}/${rules.data.chain.model}` : ''
  const d = value ?? { small, chain }
  // the saved picks fill the draft once, so Back shows them as they are
  useEffect(() => { if (!value && roles.data && rules.data !== undefined) onChange({ small, chain }) }, [roles.data, rules.data]) // eslint-disable-line react-hooks/exhaustive-deps
  const save = async (want: Guard) => {
    if (want.small !== small) {
      const ref = await addPick(offers.data, want.small, 'chat')
      await settingsApi.putRole('small', ref ? [ref] : [])
    }
    if (want.chain !== chain) {
      const ref = await addPick(offers.data, want.chain, 'chat')
      const [provider, ...rest] = ref.split('/')
      await settingsApi.putGuardRules({ chain: { provider: ref ? provider! : '', model: rest.join('/') } })
    }
  }
  saveRef.current = () => save(d)
  const pick = useAction(async (want: Guard) => {
    onChange(want)
    await save(want)
    roles.reload(); rules.reload()
    await reprobe()
  })
  // Guard is on once a decision model exists: with none saved, the first chat model is picked and saved
  const first = offers.data ? modelOptions(offers.data, 'chat', false).flatMap(o => ('options' in o ? o.options : [o]))[0]?.value : undefined
  useEffect(() => {
    if (roles.data && !small && first && !pick.busy) void pick.run({ small: first, chain: value?.chain ?? chain })
  }, [roles.data, first]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!roles.data || !offers.data || rules.data === undefined) {
    const err = roles.error ?? offers.error ?? rules.error
    return err ? <InlineError compact message={err} onRetry={() => { roles.reload(); offers.reload(); rules.reload() }} /> : <LoadingRow label="Loading" />
  }
  if (!first) {
    const welcome = nav.route.section === 'welcome'
    return (
      <div className="onb__check">
        <p className="onb__line">No chat model yet. Bastet needs one to decide.</p>
        {welcome ? <Button onClick={() => nav.go({ section: 'welcome', step: 'model' })}>Connect a model</Button>
          : <Button href={format({ section: 'settings', page: 'models' })}>Open Models</Button>}
      </div>
    )
  }
  return (
    <>
      <Field label="Decision model" group hint={<>
        Decides quickly whether a risky step needs a check (it is also the Titles and descriptions model).
        Without one, Watch lets every step run, and On holds a risky step until one answers.
      </>}>
        <Choice list label="Decision model" value={d.small} disabled={!online || pick.busy} options={modelOptions(offers.data, 'chat', false)}
                onChange={v => void pick.run({ ...d, small: v })} />
        <ModelState offers={offers.data} value={d.small} />
      </Field>
      <Field label="Second look model" group hint={<>
        A stronger model looks again at what the decision model stopped, before you are told: if it disagrees, you are not told and any freeze from it is lifted.
        With None, or when it does not answer, the first decision stands, and your note says why.
      </>}>
        <Choice list label="Second look model" value={d.chain} disabled={!online || pick.busy} options={modelOptions(offers.data, 'chat', true)}
                onChange={v => void pick.run({ ...d, chain: v })} />
        <ModelState offers={offers.data} value={d.chain} />
      </Field>
      {pick.error && <InlineError compact message={pick.error} />}
    </>
  )
}

/* ── service: the shellbox and its browser (Address, and the Key hash the box's key.hash
   file must hold, read-only with Copy), SearXNG (the address alone): Check saves (PUT
   /services) and asks the probe; Next saves the same way. The address starts as the saved
   one, else the one the probe found the service at (`feature.url`) ── */
const SERVICE: Record<string, string> = { shellbox: 'shellbox', browser: 'browser', web: 'searxng' }
/** an example address per service, on its own port (docker-compose.yml) */
const EXAMPLE: Record<string, string> = { shellbox: 'http://192.168.1.20:8770', browser: 'http://192.168.1.20:8771', searxng: 'http://192.168.1.20:8888' }
type Service = { url: string }

function ServiceForm(p: FormProps<Service>) { return <ServiceFields {...p} withKey /> }
function AddressForm(p: FormProps<Service>) { return <ServiceFields {...p} withKey={false} /> }

function ServiceFields({ feature, value, onChange, saveRef, reprobe, withKey }: FormProps<Service> & { withKey: boolean }) {
  const id = SERVICE[feature.id] ?? feature.id
  const res = useResource(() => settingsApi.services(), [])
  const saved = res.data?.services[id]
  const hash = res.data?.keys[id]?.hash ?? ''
  const d = value ?? (res.data !== undefined ? { url: saved?.url || feature.url || '' } : null)
  const save = async () => {
    if (!d) return
    const view = res.data ?? await settingsApi.services()
    if (d.url.trim() === (saved?.url ?? '')) return
    await settingsApi.putServices(view, id, d.url.trim())
    res.reload()
  }
  const check = useAction(async () => { await save(); await reprobe() })
  saveRef.current = save
  if (!d) return res.error ? <InlineError compact message={res.error} onRetry={res.reload} /> : <LoadingRow label="Loading" />
  return (
    <>
      <Field label="Address">
        <Input value={d.url} placeholder={EXAMPLE[id]} spellCheck={false}
               onChange={e => onChange({ url: e.target.value })} />
      </Field>
      {withKey && feature.state !== 'ready' && (
        <Field label="Key hash">
          <span className="onb__hash">
            <Input value={hash} readOnly mono spellCheck={false} title="Put this in the box's key.hash file" />
            <Button icon={Copy} disabled={!hash} title="Put this in the box's key.hash file" onClick={() => copied(hash)}>Copy</Button>
          </span>
        </Field>
      )}
      <CheckRow busy={check.busy} disabled={!d.url.trim()} error={check.error} onClick={() => void check.run()} />
    </>
  )
}

/* ── routines: every routine as a row (its switch, its label with its summary under it, its
   time): a flipped switch saves at once, a changed time when its field is left, Next saves
   what is still pending. The time is a field with HH:MM when the cron's minute and hour are
   plain numbers (the other cron fields stay); any other cron is read-only text ── */
type Rows = Record<string, { on: boolean; time: string }>
const HHMM = /^([01]?\d|2[0-3]):([0-5]\d)$/
/** minute and hour are plain numbers: a time to type */
const plainCron = (cron: string) => cron.split(' ').slice(0, 2).every(x => /^\d+$/.test(x))
/** "HH:MM" when minute and hour are plain numbers, else the cron line */
const timeOf = (cron: string) => {
  const [m, h] = cron.split(' ')
  return plainCron(cron) ? `${h!.padStart(2, '0')}:${m!.padStart(2, '0')}` : cron
}
/** the cron with the typed time's minute and hour, the rest as it was; a cron line as typed */
const cronOf = (cron: string, time: string) => {
  const m = HHMM.exec(time.trim())
  return m ? [Number(m[2]), Number(m[1]), ...cron.split(' ').slice(2)].join(' ') : time.trim()
}
const rowsOf = (list: Schedule[]): Rows => Object.fromEntries(list.map(s => [s.name, { on: s.on, time: timeOf(s.cron) }]))
const changed = (s: Schedule, r: { on: boolean; time: string }) => r.on !== s.on || cronOf(s.cron, r.time) !== s.cron

function RoutinesForm({ value, onChange, saveRef }: FormProps<Rows>) {
  const online = useOnline()
  const res = useResource(() => routinesApi.schedules().then(r => r.schedules), [])
  const list = res.data
  const rows = value ?? (list ? rowsOf(list) : null)
  const put = (s: Schedule, r: Rows[string]) => {
    const { label, summary, prompt, after, partner, partner_prompt, rounds, note, role, idle_only, wall_minutes, daytime } = s
    return routinesApi.put(s.name, { label, summary, prompt, after, partner, partner_prompt, rounds, note, role, idle_only,
                                     wall_minutes, daytime, cron: cronOf(s.cron, r.time), on: r.on })
  }
  /** every changed row (or `only` that one, as `row` says it now) is PUT; throws on a refusal */
  const saveAll = async (only?: string, row?: Rows[string]) => {
    if (!list || !rows) return
    for (const s of list) {
      const r = only === s.name ? row : rows[s.name]
      if ((!only || s.name === only) && r && changed(s, r)) await put(s, r)
    }
    res.reload()
  }
  const save = useAction(saveAll)
  saveRef.current = () => saveAll()
  if (!rows || !list) return res.error ? <InlineError compact message={res.error} onRetry={res.reload} /> : <LoadingRow label="Loading" />
  return (
    <>
      {list.map(s => {
        const r = rows[s.name] ?? { on: s.on, time: timeOf(s.cron) }
        const set = (patch: Partial<Rows[string]>) => {
          const next = { ...r, ...patch }
          onChange({ ...rows, [s.name]: next })
          return next
        }
        return (
          <div key={s.name} className="onb__routine">
            <Switch checked={r.on} disabled={!online || save.busy} onChange={on => void save.run(s.name, set({ on }))}
                    label={<>{s.title}{s.summary && <span className="feature-about">{s.summary}</span>}</>} />
            {plainCron(s.cron)
              ? <Input mono className="onb__time" value={r.time} aria-label="Time" spellCheck={false} disabled={!online}
                       onChange={e => set({ time: e.target.value })} onBlur={() => void save.run(s.name, r)} />
              : <span className="onb__cron mono">{s.cron}</span>}
          </div>
        )
      })}
      {save.error && <InlineError compact message={save.error} />}
    </>
  )
}
