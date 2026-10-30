/* The onboarding (/welcome/<step>), a page without the rail, shown until it was finished once
   (GET /status `setup`). One card per step with Back and Next, "Step 4 of 7" with its
   percentage and a thin Progress bar above, Skip where a step may be left: it (name,
   pronouns, colour, its persona and how it talks: PUT /agent), you (your name, time zone,
   about you: PUT /settings), model (an address checked and saved as a provider, or the saved
   provider with that address again, one chat model picked, no Skip: PUT /providers/<id>,
   the server fills the roles), Features (the server's tiers, FeatureTiers: Core on and
   fixed, Base modules and User modules a switch each with its `about`; PUT /features), then
   one step per feature switched on there that has a setup step (`step`: a form, or a
   connection), in the catalog's order: its `about`, its state line, its setup form
   (WelcomeForms.tsx, its draft kept by feature id) whose Check saves and asks the probe
   anew; the features of a group (`group`: the shellbox and the Mac, "Her computer") share
   one step, a SubHead and a form each (GroupStep). Next saves the forms, asks their probes
   anew and saves the features: what is blocked then is saved off unless it was saved on before (toSave). Then done (All set, what
   is on, what was switched on but did not answer or is not set up, what is off, New conversation), which PUTs `done` when it is reached: Settings stays
   locked until then. A connector's step (Discord, WhatsApp) also links the account and
   offers its archive's switch and the first import (ConnectorSetup).
   The step count follows what is switched on. The page keeps the switches as flipped (a
   switch on whose service is missing stays on here, so its step stays), the server what
   could be saved. Until the setup is done every URL lands here and Settings is out of
   reach; afterwards Settings, Setup runs it again (Back to Settings in the top row, only
   when it was done before this run), every
   step prefilled from what is saved. The steps are derived from the catalog: no feature id
   lives here. Next saves the step, so a closed tab loses nothing before it; a 400 with a
   field shows on that field. */
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
import { useServerEvents, type ServerEvent } from '../lib/bus'
import { Button, Choice, Dot, Field, InlineError, Input, LoadingRow, Progress, Section, SubHead, TextArea } from '../ui'
import { FeatureSwitch, FeatureTiers, reprobe, stateTone } from './settings/Features'
import { blocked, firstTime, landing, stepping, stepsOf, toSave } from '../lib/featureSteps'
import { useConnector } from './settings/Connector'
import { LoginProblem } from './settings/DiscordSource'
import { TaskLine } from './settings/parts'
import { useTasks } from './settings/SourcePage'
import { Pairing } from './settings/WhatsAppSource'
import { PRONOUNS } from './settings/Her'
import { slug } from './settings/Models'
import { EMPTY_PROVIDER, ProviderFields, host, kindInfo, ready, type ProviderDraft } from '../components/ProviderFields'
import { formOf, type SaveRef } from './WelcomeForms'
import './Welcome.css'

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
  // the switches as the server last answered: a blocked one saved on stays saved on
  const stored = useRef<Record<string, boolean> | null>(null)
  const view = local ?? (res.data ? firstTime(res.data) : null)
  const steps = stepsOf(view)
  const found = steps.indexOf(step ?? 'her')
  // a step not on (a feature switched off, a group's feature): the URL is replaced by where
  // it lands; before the catalog came it may be a feature's: wait for it
  const lands = found < 0 && view && step ? landing(view, step) : null
  useEffect(() => { if (lands) nav.go({ section: 'welcome', step: lands }, { replace: true }) }, [lands]) // eslint-disable-line react-hooks/exhaustive-deps
  const waiting = found < 0
  const at = Math.max(0, found)
  const go = (i: number) => nav.go({ section: 'welcome', step: steps[Math.min(steps.length - 1, Math.max(0, i))] })
  const nav2 = { back: at > 0 ? () => go(at - 1) : undefined, next: () => go(at + 1) }
  const name = status?.owner.name ?? ''
  const id = steps[at]!
  const feature = view?.catalog.find(f => f.id === id)
  const members = view ? view.catalog.filter(f => f.group_label && f.group === id && stepping(view)(f)) : []
  /** PUT the features (toSave), so the server, the rail and the Features page agree; the
      answer replaces the view but for the switches as flipped here and the states probed */
  const save = async (v: FeaturesView, done?: boolean) => {
    const r = await settingsApi.putFeatures(toSave(v, stored.current ?? res.data?.features ?? {}), done)
    stored.current = r.features
    setLocal({ ...r, catalog: v.catalog, features: v.features })
  }
  const shared = { view, error: res.error, reload: res.reload, update: setLocal, save, ...nav2 }
  return (
    <div className="onb">
      <div className="onb__top">
        <div className="onb__steps">
          <span className="onb__count">Step {at + 1} of {steps.length}</span>
          <span className="onb__pct">{Math.round(((at + 1) / steps.length) * 100)}%</span>
        </div>
        <div className="onb__bar"><Progress value={at + 1} max={steps.length} label={`Step ${at + 1} of ${steps.length}`} /></div>
        {res.data?.done && <span className="onb__back"><Button variant="ghost" icon={ArrowLeft} href={format({ section: 'settings' })}>Back to Settings</Button></span>}
      </div>
      <div className="page onb__page">
        <div className="page__col onb__col">
          {waiting ? <Pending error={res.error} reload={res.reload} />
            : id === 'her' ? <HerStep key="her" {...nav2} />
            : id === 'you' ? <YouStep key="you" {...nav2} />
            : id === 'model' ? <ModelStep key="model" {...nav2} />
            : members.length ? (
              <GroupStep key={id} members={members} {...shared}
                         forms={forms} setForm={(fid, v) => setForms(x => ({ ...x, [fid]: v }))} />
            )
            : feature ? (
              <FeatureStep key={id} feature={feature} {...shared}
                           form={forms[feature.id]} setForm={v => setForms({ ...forms, [feature.id]: v })} />
            )
            : id === 'features' ? <FeaturesStep key="features" {...shared} />
            : <DoneStep key="done" {...shared} name={name} onSaved={onSaved} />}
        </div>
      </div>
    </div>
  )
}

type StepNav = { back?: () => void; next: () => void }

/** switched on in this run, or saved on, as the probe found it: ready (or nothing to
    detect), its service did not answer, or not set up */
const switched = (view: FeaturesView, f: Feature, state: 'on' | 'missing' | 'unset') =>
  view.features[f.id] !== false &&
    (state === 'on' ? f.state !== 'missing' && f.state !== 'unset' : f.state === state)
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

/* ── you: your name (empty until one is saved, the default its placeholder), your time
   zone (this browser's until one is saved), about you; an empty field is left out, so its
   default stays ── */
type You = { user_name: string; timezone: string; user_bio: string }

function YouStep({ back, next }: StepNav) {
  const res = useResource(() => settingsApi.general(), [])
  const [draft, setDraft] = useState<You | null>(null)
  const [field, setField] = useState<string | undefined>()
  const [error, setError] = useState<string | null>(null)
  const g = res.data?.general
  const fallback = (k: string) => String(res.data?.fields.find(f => f.key === k)?.default ?? '')
  const here = Intl.DateTimeFormat().resolvedOptions().timeZone
  const saved: You | null = g ? { user_name: String(g.user_name ?? ''), timezone: String(g.timezone ?? here ?? ''), user_bio: String(g.user_bio ?? '') } : null
  const d = draft ?? saved
  const set = (k: keyof You, v: string) => d && setDraft({ ...d, [k]: v })
  const onNext = async () => {
    if (!g || !d) return
    setField(undefined); setError(null)
    try { await settingsApi.putGeneral(Object.fromEntries(Object.entries({ ...g, ...d }).filter(([, v]) => v !== ''))) } catch (e) {
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
          <Input value={d.user_name} placeholder={fallback('user_name')} maxLength={40} invalid={!!err('user_name')} onChange={e => set('user_name', e.target.value)} autoFocus />
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
  save: Save
}
type Save = (v: FeaturesView, done?: boolean) => Promise<void>

/** settings of feature `id` were saved, here or elsewhere (the Mac's setup posting its
    Address): its state line asks the probe anew */
const savedFor = (ev: ServerEvent, id: string) =>
  ev.type === 'settings.changed' && Array.isArray(ev.what) && ev.what.includes(id)

/** the Loading or error of the features view while it is not here */
function Pending({ error, reload }: { error: string | null; reload: () => void }) {
  return error ? <InlineError compact message={error} onRetry={reload} /> : <LoadingRow label="Loading" />
}

/** a setup step's Next: its forms saved, their features' probes asked anew, the features saved */
async function saveStep(view: FeaturesView, save: Save, ids: string[], saves: SaveRef[]) {
  for (const r of saves) await r.current?.()
  await save({ ...view, catalog: await reprobe(view.catalog, ids) })
}

/* ── one feature switched on: its about line, its state and detail (Check again on a step
   without a form), then its setup form; Next saves the form, asks the probe anew, then
   saves the features ── */
function FeatureStep({ feature: f, view, error, reload, update, save, back, next, form, setForm }: Shared & {
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
    await saveStep(view, save, [f.id], [saveRef])
    next()
  }
  // the catalog's states came before the model step: a blocked step asks its probe once it opens
  useEffect(() => { if (view && blocked(f)) void check.run() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useServerEvents(ev => { if (savedFor(ev, f.id)) void check.run() })
  const Form = formOf(f)
  const tone = stateTone(f, true)
  return (
    <StepCard title={f.label} back={back} next={next} onNext={onNext} disabled={!view && (error ?? 'Loading')}
              head={<>
                {f.about && <p className="onb__about">{f.about}</p>}
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
        {Form && <Form feature={f} value={form} onChange={setForm} saveRef={saveRef} reprobe={() => check.run()} />}
        {f.archive && f.state === 'ready' && <ConnectorSetup feature={f} view={view} update={update} put={save} />}
      </>}
    </StepCard>
  )
}

/* ── a group's step ("Her computer": Container and Own computer): each feature of the group
   switched on, under its `choice` as a SubHead: its state line and its setup form. Next
   saves every form, asks their probes anew, then saves the features ── */
function GroupStep({ members, view, error, reload, update, save, back, next, forms, setForm }: Shared & {
  members: Feature[]
  forms: Record<string, unknown>
  setForm: (id: string, v: unknown) => void
}) {
  const refs = useRef(new Map<string, SaveRef>())
  const refOf = (id: string) => refs.current.get(id) ?? refs.current.set(id, { current: null }).get(id)!
  const onNext = async () => {
    if (!view) return
    await saveStep(view, save, members.map(f => f.id), members.map(f => refOf(f.id)))
    next()
  }
  return (
    <StepCard title={members[0]!.group_label} back={back} next={next} onNext={onNext} disabled={!view && (error ?? 'Loading')}>
      {!view ? <Pending error={error} reload={reload} />
        : members.map(f => <GroupPart key={f.id} feature={f} view={view} update={update} form={forms[f.id]}
                                      setForm={v => setForm(f.id, v)} saveRef={refOf(f.id)} />)}
    </StepCard>
  )
}

function GroupPart({ feature: f, view, update, form, setForm, saveRef }: {
  feature: Feature
  view: FeaturesView
  update: (v: FeaturesView) => void
  form: unknown
  setForm: (v: unknown) => void
  saveRef: SaveRef
}) {
  const check = useAction(async () => {
    const p = await settingsApi.probeFeature(f.id)
    update({ ...view, catalog: view.catalog.map(x => x.id === f.id ? { ...x, ...p } : x) })
  })
  useServerEvents(ev => { if (savedFor(ev, f.id)) void check.run() })
  const Form = formOf(f)
  const tone = stateTone(f, true)
  return <>
    <SubHead>{f.choice || f.label}</SubHead>
    {f.about && <p className="onb__about">{f.about}</p>}
    {tone && <span className="onb__state"><Dot tone={tone} /><span className="onb__detail">{f.detail}</span></span>}
    {check.error && <InlineError compact message={check.error} />}
    {Form && <Form feature={f} value={form} onChange={setForm} saveRef={saveRef} reprobe={() => check.run()} />}
  </>
}

/* ── a connector's step (Discord, WhatsApp) while its connector answers: the feature is
   saved on at once, so its connection opens; then its login while it needs one (the QR
   code, as on its Settings page), and once linked its archive's switch (`archive`:
   Messages, saved at once, its probe asked anew) and Import, the first import as a task
   whose line counts the archive's messages and says Done at its end. Skip leaves it to
   Settings, Features and the source's page ── */
function ConnectorSetup({ feature: f, view, update, put }: { feature: Feature; view: FeaturesView; update: (v: FeaturesView) => void; put: Save }) {
  const online = useOnline()
  const kid = f.kbs[0]!
  const conn = useConnector(kid, true)
  const tasks = useTasks()
  const save = useAction((v: FeaturesView) => put(v))
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

/* ── features: the tiers, a switch per feature, saved with Next; what is switched on
   decides the steps after it ── */
function FeaturesStep({ view, error, reload, update, save, back, next }: Shared) {
  const onNext = async () => {
    if (!view) return
    await save(view)
    next()
  }
  return (
    <StepCard title="Features" back={back} next={next} onNext={onNext} disabled={!view && (error ?? 'Loading')}>
      {view ? <FeatureTiers view={view} about free onChange={features => update({ ...view, features })} />
        : <Pending error={error} reload={reload} />}
    </StepCard>
  )
}

/* ── done: All set, what is on and off, New conversation. Reaching it finishes the setup
   (PUT /features with done), so Settings opens from here on ── */
function DoneStep({ view, error, reload, save, back, name, onSaved }: Shared & { name: string; onSaved: () => void }) {
  const finish = useAction(async () => {
    if (!view) return
    await save(view, true)
    onSaved()
  })
  const ready = !!view
  useEffect(() => { if (ready && !view.done) void finish.run() }, [ready]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Section title="All set">
      {!view ? <Pending error={error} reload={reload} /> : <>
        <p className="onb__line">{name} is ready.</p>
        <p className="onb__line">On: {labels(view, f => switched(view, f, 'on'))}</p>
        {labels(view, f => switched(view, f, 'missing')) !== 'None' && (
          <p className="onb__line">Off (did not answer): {labels(view, f => switched(view, f, 'missing'))}</p>
        )}
        {view.catalog.filter(f => switched(view, f, 'unset')).map(f => (
          <p key={f.id} className="onb__line">Not set up: {f.label}. {f.detail}</p>
        ))}
        <p className="onb__line">Off: {labels(view, f => view.features[f.id] === false)}</p>
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
