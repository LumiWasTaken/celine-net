/* The onboarding (/welcome/<step>), a page without the rail, shown until it was finished once
   (GET /status `setup`). One card per step with Back and Next, "Step 4 of 16" with its
   percentage and a thin Progress bar above, Skip where a step may be left: it (name, pronouns, colour, its persona and how it
   talks: PUT /agent), you (your name, time zone, about you: PUT /settings), model (an address
   checked and saved as a provider, or the saved provider with that address again, one chat
   model picked, no Skip: PUT /providers/<id>, the server fills the roles), then the fold: one
   card per feature the server's `card` policy shows for what its probe found (showsCard:
   "detect" when the service answered or has an address and did not, "always" for Shares, the guard,
   Routines and the connectors, whose `needs` says how to run them; email and vault never): its `about` under the title, its switch (greyed while the service
   is missing; a feature shown off is saved off; a service that answers while nothing is saved
   yet is shown on), its state line (grey once the switch is off), then its setup form
   (WelcomeForms.tsx, its draft kept by feature id) whose Check saves and asks the probe anew
   (Check again in the head only on a card without a form), one card "Features" with the
   plain switches and their `about` lines (PUT /features), one card "Later" with every feature
   that got no card (off, greyed, "Turn on later in Settings": Settings, Features opens the
   same form), then done (All set, what is on and off, New conversation), which PUTs `done`
   when it is reached: Settings stays locked until then. A connector's card (Discord,
   WhatsApp) also links the account and offers its archive's switch and the first import
   (ConnectorSetup).
   The step count follows the folded list. Until the setup is done every URL lands here and
   Settings is out of reach; afterwards Settings, Setup runs it again (Back to Settings in the
   top row), every step prefilled from what is saved. The steps are derived from the catalog:
   no feature id lives here. Next saves the step, so a closed tab loses nothing before it; a
   400 with a field shows on that field. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowLeft, ArrowRight, RefreshCw } from 'lucide-react'
import { ApiError, type ConfigStatus } from '../lib/api'
import { kbApi } from '../lib/kb'
import { onboardingModel } from '../lib/onboardingModel'
import { previewAppearance, saveAppearance, type Accent } from '../lib/appearance'
import { COLORS } from '../lib/her'
import { format, useNav } from '../lib/nav'
import { OFFLINE_TITLE, useOnline } from '../lib/socket'
import {
  herApi, settingsApi, type Available, type Feature, type FeaturesView, type KindInfo, type ModelConfig,
  type ProviderKind,
} from '../lib/settingsApi'
import { useAction } from '../lib/useAction'
import { useResource } from '../lib/useResource'
import { Button, Choice, Dot, Field, InlineError, Input, LoadingRow, Progress, Section, TextArea } from '../ui'
import { FeatureRows, FeatureSwitch, blocked, isPlain, showsCard, stateTone } from './settings/Features'
import { useConnector } from './settings/Connector'
import { LoginProblem } from './settings/DiscordSource'
import { TaskLine } from './settings/parts'
import { useTasks } from './settings/SourcePage'
import { Pairing } from './settings/WhatsAppSource'
import { PRONOUNS } from './settings/Her'
import { slug } from './settings/Models'
import { EMPTY_PROVIDER, ProviderFields, host, kindInfo, ready, type ProviderDraft } from '../components/ProviderFields'
import { FORMS, type SaveRef } from './WelcomeForms'
import './Welcome.css'

const FIXED = ['her', 'you', 'model']
/** a feature with no card and no plain switch: the Later card's */
const isLater = (f: Feature) => !showsCard(f) && !isPlain(f)
/** the step ids in order: the fixed three, the feature cards (their feature id), Features,
    Later (when a feature got no card), done */
const stepsOf = (view: FeaturesView | null) => {
  const catalog = view?.catalog ?? []
  return [...FIXED, ...catalog.filter(showsCard).map(f => f.id), 'features', ...(catalog.some(isLater) ? ['later'] : []), 'done']
}

/** the view before anything was saved: a service that answers is shown on (the server
    keeps what needs a service off until Setup saves it), never one that asks first (`warn`) */
const firstTime = (v: FeaturesView): FeaturesView =>
  v.saved ? v : { ...v, features: { ...v.features, ...Object.fromEntries(v.catalog.filter(f => f.state === 'ready' && !f.warn).map(f => [f.id, true])) } }

/** the field a 400 names, else nothing */
const fieldOf = (e: unknown) => (e instanceof ApiError ? e.detail.field : undefined)

export function Welcome({ status, onSaved }: { status: ConfigStatus | null; onSaved: () => void }) {
  const nav = useNav()
  const step = nav.route.section === 'welcome' ? nav.route.step : undefined
  const res = useResource(() => settingsApi.features(), [])
  // the features and their states as this page knows them: the server's, then every
  // switch flipped and every Check again on top; the forms' drafts, so Back keeps them
  const [local, setLocal] = useState<FeaturesView | null>(null)
  const [forms, setForms] = useState<Record<string, unknown>>({})
  const view = local ?? (res.data ? firstTime(res.data) : null)
  const steps = stepsOf(view)
  const found = steps.indexOf(step ?? 'her')
  // an unknown step before the catalog came may be a feature card: wait for it
  const waiting = found < 0 && !view && !res.error
  const at = Math.max(0, found)
  const go = (i: number) => nav.go({ section: 'welcome', step: steps[Math.min(steps.length - 1, Math.max(0, i))] })
  const nav2 = { back: at > 0 ? () => go(at - 1) : undefined, next: () => go(at + 1) }
  const name = status?.owner.name ?? ''
  const id = steps[at]!
  const feature = view?.catalog.find(f => f.id === id)
  const plain = (view?.catalog ?? []).filter(isPlain).map(f => f.id)
  const later = (view?.catalog ?? []).filter(isLater).map(f => f.id)
  const shared = { view, error: res.error, reload: res.reload, update: setLocal, ...nav2 }
  return (
    <div className="onb">
      <div className="onb__top">
        <div className="onb__steps">
          <span className="onb__count">Step {at + 1} of {steps.length}</span>
          <span className="onb__pct">{Math.round(((at + 1) / steps.length) * 100)}%</span>
        </div>
        <div className="onb__bar"><Progress value={at + 1} max={steps.length} label={`Step ${at + 1} of ${steps.length}`} /></div>
        {status?.setup && <span className="onb__back"><Button variant="ghost" icon={ArrowLeft} href={format({ section: 'settings' })}>Back to Settings</Button></span>}
      </div>
      <div className="page onb__page">
        <div className="page__col onb__col">
          {waiting ? <LoadingRow label="Loading" />
            : id === 'her' ? <HerStep key="her" {...nav2} />
            : id === 'you' ? <YouStep key="you" {...nav2} />
            : id === 'model' ? <ModelStep key="model" {...nav2} />
            : feature ? (
              <FeatureStep key={id} feature={feature} {...shared}
                           form={forms[feature.id]} setForm={v => setForms({ ...forms, [feature.id]: v })} />
            )
            : id === 'features' ? <FeaturesStep key="features" {...shared} only={plain} />
            : id === 'later' ? <LaterStep key="later" {...shared} only={later} />
            : <DoneStep key="done" {...shared} name={name} onSaved={onSaved} />}
        </div>
      </div>
    </div>
  )
}

type StepNav = { back?: () => void; next: () => void }

/** a feature is on: its switch, and nothing blocks it */
const isOn = (view: FeaturesView, f: Feature) => view.features[f.id] !== false && !blocked(f)
/** the labels of the features `pick` keeps, in the catalog's order; "None" for none */
const labels = (view: FeaturesView, pick: (f: Feature) => boolean) =>
  view.catalog.filter(pick).map(f => f.label).join(', ') || 'None'

/* ── one step's card: its fields, then Back, Skip (when the step may be left) and Next,
   which saves and goes on; a save's error sits at the foot ── */
function StepCard({ title, head, back, skip, onNext, disabled, children }: StepNav & {
  title: ReactNode
  /** under the title, fixed like it: a feature's state line */
  head?: ReactNode
  skip?: () => void
  /** saves; a rejection stays at the foot (and on its field, through the step's own state) */
  onNext: () => Promise<void>
  /** Next cannot run yet, with its reason */
  disabled?: string | false | null
  children: ReactNode
}) {
  const online = useOnline()
  const save = useAction(onNext)
  const why = !online ? OFFLINE_TITLE : disabled || undefined
  return (
    <Section title={title} className="onb__card">
      {head}
      <div className="onb__body">{children}</div>
      <div className="onb__foot ui-section__foot">
        {back && <Button variant="ghost" icon={ArrowLeft} onClick={back} disabled={save.busy}>Back</Button>}
        <span className="onb__gap">{save.error && <InlineError compact message={save.error} />}</span>
        {skip && <Button variant="ghost" onClick={skip} disabled={save.busy}>Skip</Button>}
        <Button variant="primary" iconEnd={ArrowRight} loading={save.busy} disabled={!!why} title={why}
                onClick={() => void save.run()}>Next</Button>
      </div>
    </Section>
  )
}

/* ── it: name, pronouns, colour, its persona and how it talks (the shipped text until
   it has its own) ── */
type HerDraft = { name: string; pronouns: string; color: string; persona: string; talk: string }

function HerStep({ back, next }: StepNav) {
  const her = useResource(() => herApi.get(), [])
  const [draft, setDraft] = useState<HerDraft | null>(null)
  const [field, setField] = useState<string | undefined>()
  const [error, setError] = useState<string | null>(null)
  const h = her.data
  const d = draft ?? (h ? { name: h.name, pronouns: h.pronouns, color: h.color, persona: h.persona, talk: h.talk ?? h.talk_shipped } : null)
  const set = (k: keyof HerDraft, v: string) => d && setDraft({ ...d, [k]: v })
  // her colour is the app's accent: shown at once, saved with Appearance's own path
  useEffect(() => { if (draft) previewAppearance({ accent: draft.color as Accent }) }, [draft?.color]) // eslint-disable-line react-hooks/exhaustive-deps
  const onNext = async () => {
    if (!h || !d) return
    setField(undefined); setError(null)
    try {
      await herApi.put(d, h.version)
      await saveAppearance({ accent: d.color as Accent })
    } catch (e) {
      setField(fieldOf(e)); setError(e instanceof Error ? e.message : String(e))
      throw e
    }
    next()
  }
  const err = (k: string) => (field === k ? error : null)
  return (
    <StepCard title={h?.name ?? ''} back={back} next={next} onNext={onNext} disabled={!d && (her.error ?? 'Loading')}>
      {!d ? (her.error ? <InlineError compact message={her.error} onRetry={her.reload} /> : <LoadingRow label="Loading" />) : <>
        <Field label="Name" error={err('name')}>
          <Input value={d.name} maxLength={40} invalid={!!err('name')} onChange={e => set('name', e.target.value)} autoFocus />
        </Field>
        <Field label="Pronouns" group error={err('pronouns')}>
          <Choice label="Pronouns" value={d.pronouns} options={PRONOUNS} onChange={v => set('pronouns', v)} />
        </Field>
        <Field label="Colour" group error={err('color')}>
          <Choice label="Colour" value={d.color} onChange={v => set('color', v)}
                  options={COLORS.map(k => ({ value: k, label: k[0]!.toUpperCase() + k.slice(1), swatch: `var(--char-${k})` }))} />
        </Field>
        <div className="onb__texts">
          <Field label="Persona" className="onb__textfield" error={err('persona')}>
            <TextArea fill className="input onb__text" value={d.persona} invalid={!!err('persona')} onChange={e => set('persona', e.target.value)} />
          </Field>
          <Field label="Example lines" className="onb__textfield" error={err('talk')}>
            <TextArea fill className="input onb__text" value={d.talk} invalid={!!err('talk')} onChange={e => set('talk', e.target.value)} />
          </Field>
        </div>
      </>}
    </StepCard>
  )
}

/* ── you: your name, your time zone (this browser's until one is saved), about you ── */
type You = { user_name: string; timezone: string; user_bio: string }

function YouStep({ back, next }: StepNav) {
  const res = useResource(() => settingsApi.general(), [])
  const [draft, setDraft] = useState<You | null>(null)
  const [field, setField] = useState<string | undefined>()
  const [error, setError] = useState<string | null>(null)
  const g = res.data?.general
  const zoneDefault = res.data?.fields.find(f => f.key === 'timezone')?.default
  const here = Intl.DateTimeFormat().resolvedOptions().timeZone
  const saved: You | null = g ? { user_name: String(g.user_name ?? ''), timezone: String(g.timezone ?? ''), user_bio: String(g.user_bio ?? '') } : null
  const d = draft ?? (saved && !res.data?.saved && saved.timezone === zoneDefault && here ? { ...saved, timezone: here } : saved)
  const set = (k: keyof You, v: string) => d && setDraft({ ...d, [k]: v })
  const onNext = async () => {
    if (!g || !d) return
    setField(undefined); setError(null)
    try { await settingsApi.putGeneral({ ...g, ...d }) } catch (e) {
      setField(fieldOf(e)); setError(e instanceof Error ? e.message : String(e))
      throw e
    }
    next()
  }
  const err = (k: string) => (field === k ? error : null)
  return (
    <StepCard title="You" back={back} next={next} skip={next} onNext={onNext} disabled={!d && (res.error ?? 'Loading')}>
      {!d ? (res.error ? <InlineError compact message={res.error} onRetry={res.reload} /> : <LoadingRow label="Loading" />) : <>
        <Field label="Your name" error={err('user_name')}>
          <Input value={d.user_name} maxLength={40} invalid={!!err('user_name')} onChange={e => set('user_name', e.target.value)} autoFocus />
        </Field>
        <Field label="Time zone" error={err('timezone')}>
          <Input value={d.timezone} placeholder="America/New_York" spellCheck={false} invalid={!!err('timezone')} onChange={e => set('timezone', e.target.value)} />
        </Field>
        <Field label="About you" error={err('user_bio')}>
          <TextArea rows={3} value={d.user_bio} maxLength={2000} placeholder="Anything worth knowing about you" invalid={!!err('user_bio')}
                    onChange={e => set('user_bio', e.target.value)} />
        </Field>
      </>}
    </StepCard>
  )
}

/* ── model: one address, checked and saved as a provider (the saved provider with that
   address, when there is one: no second provider for the same server); its chat models, one
   picked. Run again: the first saved provider with a chat model fills the step (its address,
   its name, that model), Next ready ── */
type Saved = { id: string; kind: ProviderKind; label: string; base_url: string; models: Record<string, ModelConfig> }

/** the same server: scheme, host and path alike, case and a trailing slash aside */
const sameUrl = (a: string, b: string) => a.trim().replace(/\/+$/, '').toLowerCase() === b.trim().replace(/\/+$/, '').toLowerCase()

function ModelStep({ back, next }: StepNav) {
  const online = useOnline()
  const providers = useResource(() => settingsApi.providers(), [])
  // d.kind '' is Auto-detect: the server says what answers, and its answer fills Type and Address
  const [d, setD] = useState<ProviderDraft>(EMPTY_PROVIDER)
  const edit = (v: ProviderDraft) => { setD(v); setChecked(null) }
  const [checked, setChecked] = useState<Awaited<ReturnType<typeof settingsApi.testNew>> | null>(null)
  const [saved, setSaved] = useState<Saved | null>(null)
  const [models, setModels] = useState<Available[] | null>(null)
  const [model, setModel] = useState('')
  const kinds: KindInfo[] = providers.data?.kinds ?? []
  const kindLabel = (k: ProviderKind | null) => kindInfo(kinds, k)?.label ?? k ?? ''
  // what is saved fills the step once, while nothing was typed
  const list = providers.data?.providers
  useEffect(() => {
    if (!list || d.url || saved) return
    const have = list.find(p => Object.values(p.provider.models).some(m => !m.caps || m.caps.includes('chat')))
    if (!have) return
    const chat: Available[] = Object.entries(have.provider.models).filter(([, m]) => !m.caps || m.caps.includes('chat'))
      .map(([mid, m]) => ({ id: mid, name: m.label, caps: m.caps, context: m.context, thinking_levels: m.thinking_levels, added: true }))
    setD({ kind: have.provider.kind, url: have.provider.base_url, key: '', label: have.provider.label })
    setSaved({ id: have.id, kind: have.provider.kind, label: have.provider.label, base_url: have.provider.base_url, models: have.provider.models })
    setModels(chat)
    setModel(chat[0]!.id)
  }, [list]) // eslint-disable-line react-hooks/exhaustive-deps
  const check = useAction(async () => {
    setChecked(null); setSaved(null); setModels(null); setModel('')
    const r = await settingsApi.testNew({ kind: d.kind || undefined, base_url: d.url.trim(), key: d.key.trim() || undefined })
    setChecked(r)
    if (!r.kind) return
    if (!d.kind) setD(x => ({ ...x, kind: r.kind!, url: r.base_url }))
    if (!r.ok) return
    const all = providers.data?.providers ?? []
    const have = all.find(p => sameUrl(p.provider.base_url, r.base_url))
    let id: string
    if (have) {
      id = have.id
      setSaved({ id, kind: have.provider.kind, label: have.provider.label, base_url: have.provider.base_url, models: have.provider.models })
    } else {
      const name = d.label.trim() || host(r.base_url)
      id = slug(name, all.map(p => p.id))
      await settingsApi.putProvider(id, { kind: r.kind, label: name, base_url: r.base_url, models: {} })
      providers.reload()
      setSaved({ id, kind: r.kind, label: name, base_url: r.base_url, models: {} })
    }
    if (d.key.trim()) await settingsApi.setKey(id, d.key.trim())
    const list = await settingsApi.available(id)
    const chat = list.models.filter(m => !m.caps || m.caps.includes('chat'))
    setModels(chat)
    if (chat.length === 1) setModel(chat[0]!.id)
  })
  const onNext = async () => {
    if (!saved || !model) return
    const m = models?.find(x => x.id === model)
    const cfg = onboardingModel(model, m, saved.models[model])
    await settingsApi.putProvider(saved.id, { kind: saved.kind, label: saved.label, base_url: saved.base_url, models: { ...saved.models, [model]: cfg } })
    next()
  }
  return (
    <StepCard title="Connect a model" back={back} next={next} onNext={onNext} disabled={!model && 'Choose a model'}>
      <ProviderFields kinds={kinds} value={d} onChange={edit} autoFocus />
      <div className="onb__check">
        <Button loading={check.busy} disabled={!online || !ready(kinds, d)} title={online ? undefined : OFFLINE_TITLE}
                onClick={() => void check.run()}>Check</Button>
        <span className="onb__state">
          {checked && <Dot tone={checked.ok ? 'ok' : 'warn'} text={[kindLabel(checked.kind), checked.sentence].filter(Boolean).join(': ')} />}
        </span>
      </div>
      {check.error && <InlineError compact message={check.error} />}
      {saved && (
        <Field label="Model" group>
          {!models ? <LoadingRow label="Loading" />
            : models.length === 0 ? <InlineError compact message="No chat model found." onRetry={() => void check.run()} />
            : <Choice list label="Model" value={model} onChange={setModel}
                      options={models.map(m => ({ value: m.id, label: m.name || m.id }))} />}
        </Field>
      )}
    </StepCard>
  )
}

/* ── the feature steps share the features view, its loading and its updates ── */
type Shared = StepNav & {
  view: FeaturesView | null
  error: string | null
  reload: () => void
  update: (v: FeaturesView) => void
}

/** the Loading or error of the features view while it is not here */
function Pending({ error, reload }: { error: string | null; reload: () => void }) {
  return error ? <InlineError compact message={error} onRetry={reload} /> : <LoadingRow label="Loading" />
}

/** PUT the features: what the page shows off (a blocked switch too) is saved off, so the
    server, the rail and the Features page agree; the answer replaces the view, the states this
    page probed stay */
async function putFeatures(view: FeaturesView, update: (v: FeaturesView) => void, done?: boolean) {
  const features = { ...view.features }
  for (const f of view.catalog) if (blocked(f)) features[f.id] = false
  const r = await settingsApi.putFeatures(features, done)
  update({ ...r, catalog: view.catalog })
}

/* ── one feature: its about line, its switch, its state and detail (Check again), then
   its setup form; Next saves the form, then the features ── */
function FeatureStep({ feature: f, view, error, reload, update, back, next, form, setForm }: Shared & {
  feature: Feature
  form: unknown
  setForm: (v: unknown) => void
}) {
  const online = useOnline()
  const saveRef: SaveRef = useRef(null)
  const check = useAction(async () => {
    if (!view) return
    const p = await settingsApi.probeFeature(f.id)
    update({ ...view, catalog: view.catalog.map(x => x.id === f.id ? { ...x, ...p } : x) })
  })
  const onNext = async () => {
    if (!view) return
    await saveRef.current?.()
    await putFeatures(view, update)
    next()
  }
  // the catalog's states came before the model step: a blocked card asks its probe once it opens
  useEffect(() => { if (view && blocked(f)) void check.run() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const Form = FORMS[f.setup]
  const on = !!view && view.features[f.id] !== false
  const tone = stateTone(f, on)
  return (
    <StepCard title={f.label} back={back} next={next} onNext={onNext} disabled={!view && (error ?? 'Loading')}
              head={<>
                {f.about && <p className="onb__about">{f.about}</p>}
                {view && (
                  <FeatureSwitch feature={f} on={on} aboutShown disabled={!online}
                                 onChange={v => update({ ...view, features: { ...view.features, [f.id]: v } })} />
                )}
                {view && tone && (
                  <div className="onb__check">
                    <span className="onb__state"><Dot tone={tone} /><span className="onb__detail">{f.detail}</span></span>
                    {!Form && (
                      <Button variant="ghost" icon={RefreshCw} loading={check.busy} disabled={!online} title={online ? undefined : OFFLINE_TITLE}
                              onClick={() => void check.run()}>Check again</Button>
                    )}
                  </div>
                )}
              </>}>
      {!view ? <Pending error={error} reload={reload} /> : <>
        {check.error && <InlineError compact message={check.error} />}
        {Form && (on || blocked(f)) && <Form feature={f} value={form} onChange={setForm} saveRef={saveRef} reprobe={() => check.run()} />}
        {f.archive && on && f.state === 'ready' && <ConnectorSetup feature={f} view={view} update={update} />}
      </>}
    </StepCard>
  )
}

/* ── a connector's card (Discord, WhatsApp) while its connector answers: the feature is
   saved on at once, so its connection opens; then its login while it needs one (the QR
   code, as on its Settings page), and once linked its archive's switch (`archive`:
   Messages, saved at once, its probe asked anew) and Import, the first import as a task
   whose line counts the archive's messages and says Done at its end. Skip leaves it to
   Settings, Features and the source's page ── */
function ConnectorSetup({ feature: f, view, update }: { feature: Feature; view: FeaturesView; update: (v: FeaturesView) => void }) {
  const online = useOnline()
  const kid = f.kbs[0]!
  const conn = useConnector(kid, true)
  const tasks = useTasks()
  const save = useAction((v: FeaturesView) => putFeatures(v, update))
  const start = useAction(async () => { await kbApi.importMessages(kid); tasks.reload() })
  const archive = view.catalog.find(x => x.id === f.archive)
  const recheck = useAction(async () => {
    if (!archive) return
    const p = await settingsApi.probeFeature(archive.id)
    update({ ...view, catalog: view.catalog.map(x => x.id === archive.id ? { ...x, ...p } : x) })
  })
  useEffect(() => { void save.run(view) }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // the connection opens a moment after the save: look again until it answers
  const has = !!conn.data
  const { reload } = conn
  useEffect(() => {
    if (has) return
    const t = setInterval(reload, 3000)
    return () => clearInterval(t)
  }, [has, reload])
  const st = conn.data
  const connected = st?.connection.state === 'connected'
  useEffect(() => { if (connected) void recheck.run() }, [connected]) // eslint-disable-line react-hooks/exhaustive-deps
  const task = tasks.data?.find(t => t.kb === kid && t.kind === 'import')
  const error = save.error ?? recheck.error ?? start.error
  return <>
    {!st ? <LoadingRow label="Checking the connection" />
      : st.connection.state === 'needs_login'
        ? (st.platform === 'discord' ? <LoginProblem kid={kid} st={st} onChanged={reload} /> : <Pairing kid={kid} st={st} />)
      : !connected ? <p className="onb__line">{st.connection.error}</p>
      : <>
        {archive && (
          <FeatureSwitch feature={archive} on={view.features[archive.id] !== false && !blocked(archive)} disabled={!online || save.busy}
                         onChange={v => void save.run({ ...view, features: { ...view.features, [archive.id]: v } })} />
        )}
        {task ? <TaskLine task={task} onChanged={tasks.reload} />
          : <div><Button loading={start.busy} disabled={!online} title={online ? undefined : OFFLINE_TITLE} onClick={() => void start.run()}>Import</Button></div>}
      </>}
    {error && <InlineError compact message={error} />}
  </>
}

/* ── features: the plain switches, saved once with Next ── */
function FeaturesStep({ view, error, reload, update, back, next, only }: Shared & { only: string[] }) {
  const onNext = async () => {
    if (!view) return
    await putFeatures(view, update)
    next()
  }
  return (
    <StepCard title="Features" back={back} next={next} onNext={onNext} disabled={!view && (error ?? 'Loading')}>
      {view ? <FeatureRows view={view} only={only} about onChange={features => update({ ...view, features })} />
        : <Pending error={error} reload={reload} />}
    </StepCard>
  )
}

/* ── done: All set, what is on and off, New conversation. Reaching it finishes the setup
   (PUT /features with done), so Settings opens from here on ── */
function DoneStep({ view, error, reload, update, back, name, onSaved }: Shared & { name: string; onSaved: () => void }) {
  const finish = useAction(async () => {
    if (!view) return
    await putFeatures(view, update, true)
    onSaved()
  })
  const ready = !!view
  useEffect(() => { if (ready && !view.done) void finish.run() }, [ready]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Section title="Design your agent">
      {!view ? <Pending error={error} reload={reload} /> : <>
        <p className="onb__line">All set. {name} is ready.</p>
        <p className="onb__line">On: {labels(view, f => isOn(view, f))}</p>
        {labels(view, f => !isOn(view, f) && f.state === 'missing') !== 'None' && (
          <p className="onb__line">Off (did not answer): {labels(view, f => !isOn(view, f) && f.state === 'missing')}</p>
        )}
        <p className="onb__line">Off: {labels(view, f => !isOn(view, f) && f.state !== 'missing')}</p>
        <p className="onb__line">You can change all of this later in Settings.</p>
        {finish.error && <InlineError compact message={finish.error} onRetry={() => void finish.run()} />}
      </>}
      <div className="onb__foot ui-section__foot">
        <Button variant="ghost" icon={ArrowLeft} onClick={back}>Back</Button>
        <span className="onb__gap" />
        <Button variant="primary" iconEnd={ArrowRight} href="/new_chat" disabled={finish.busy || !view?.done}>New conversation</Button>
      </div>
    </Section>
  )
}

/* ── later: every feature that got no card, off and greyed ("Turn on later in Settings");
   nothing to save ── */
function LaterStep({ view, error, reload, back, next, only }: Shared & { only: string[] }) {
  return (
    <StepCard title="Later" back={back} next={next} onNext={async () => next()} disabled={!view && (error ?? 'Loading')}>
      {view ? <FeatureRows view={view} only={only} later onChange={() => undefined} />
        : <Pending error={error} reload={reload} />}
    </StepCard>
  )
}
