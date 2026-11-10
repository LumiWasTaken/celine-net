/* Settings, Features (/settings/features): the server's tiers in its order (FeatureTiers),
   each under its SubHead: Core, its rows on and fixed (no switch to flip); Built-in
   and Plugins, one row per feature: its label (its `help` behind a "?"), its switch (off
   and greyed while the service is missing or not set up, its `needs` sentence under it and
   in a toast on a click), saved on each change (the whole object). A feature with a setup
   form opens it under its row (Edit), the same form the onboarding uses (WelcomeForms.tsx);
   its Check saves and asks the probe anew, the row follows. A form's draft is kept by
   feature id. "Check again" in the head asks every probe anew. The onboarding's Features
   step draws the same tiers with `about` as a second line, and a feature with a setup step
   switches on even while blocked (`free`): its step comes next. API: lib/settingsApi.ts
   settingsApi.features / putFeatures / probeFeature. */
import { ArrowUpRight, ChevronDown, ChevronUp, RefreshCw } from 'lucide-react'
import { format } from '../../lib/nav'
import { useRef, useState } from 'react'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { blocked, flip, waits } from '../../lib/featureSteps'
import { settingsApi, type Feature, type FeaturesView } from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useAutosave } from '../../lib/useAutosave'
import { useResource } from '../../lib/useResource'
import { Button, ConfirmDialog, cx, InlineError, Loaded, Section, StateWord, SubHead, Switch, toast } from '../../ui'
import { formOf, type SaveRef } from '../WelcomeForms'
import { Page } from './parts'
import './settings.css'

const MEDIA = new Set(['images', 'music'])

/** the probes of the catalog's features (or `only` those ids), asked now; the catalog with the answers */
export async function reprobe(catalog: Feature[], only?: string[]): Promise<Feature[]> {
  const ask = (f: Feature) => f.state !== 'none' && (!only || only.includes(f.id))
  const probes = await Promise.all(catalog.map(f => ask(f) ? settingsApi.probeFeature(f.id) : null))
  return catalog.map((f, i) => ({ ...f, ...(probes[i] ?? {}) }))
}

export function Features() {
  const online = useOnline()
  const res = useResource(() => settingsApi.features(), [], { events: ev => ev.type === 'settings.changed' })
  const [open, setOpen] = useState<string | null>(null)
  const [forms, setForms] = useState<Record<string, unknown>>({})
  const save = useAutosave<boolean>(async changes => {
    res.set(await settingsApi.putFeatures({ ...res.data?.features, ...changes }))
  })
  const check = useAction(async () => {
    if (!res.data) return
    const catalog = await reprobe(res.data.catalog)
    res.set(d => d && { ...d, catalog })
  })
  const reprobeOne = async (id: string) => {
    const p = await settingsApi.probeFeature(id)
    res.set(d => d && { ...d, catalog: d.catalog.map(x => x.id === id ? { ...x, ...p } : x) })
  }
  return (
    <Page title="Features"
          actions={<Button icon={RefreshCw} loading={check.busy} disabled={!online || !res.data} title={online ? undefined : OFFLINE_TITLE}
                           onClick={() => void check.run()}>Check again</Button>}>
      <Section>
        {res.data ? (
          <div title={online ? undefined : OFFLINE_TITLE}>
            <FeatureTiers view={{ ...res.data, features: { ...res.data.features, ...save.pending } }} disabled={!online}
                          onChange={f => Object.entries(f).forEach(([id, on]) => { if (on !== (save.pending[id] ?? res.data!.features[id])) save.set(id, on) })}
                          open={open} onOpen={setOpen} forms={forms} setForms={setForms} reprobe={reprobeOne} />
            {save.error && <InlineError compact message={save.error} onRetry={save.flush} />}
            {check.error && <InlineError compact message={check.error} />}
          </div>
        ) : <Loaded res={res} />}
      </Section>
    </Page>
  )
}

/** the server's tiers, each under its heading: the core's rows on and fixed, then each
    tier's features as switch rows; `onChange` gets the whole object. `about`: each row says
    what it gives (the onboarding); `free`: a feature with a setup step switches on even
    while blocked (the onboarding, whose step for it comes next). With `open`, a row with a
    setup form opens it (Edit) */
export function FeatureTiers({ view, disabled = false, about = false, free = false, onChange, open, onOpen, forms, setForms, reprobe }: {
  view: FeaturesView
  disabled?: boolean
  about?: boolean
  free?: boolean
  onChange: (features: Record<string, boolean>) => void
  open?: string | null
  onOpen?: (id: string | null) => void
  forms?: Record<string, unknown>
  setForms?: (f: Record<string, unknown>) => void
  reprobe?: (id: string) => Promise<void>
}) {
  return (
    <div className="feature-tiers">
      {view.tiers.map(t => (
        <div key={t.id} className="feature-tier">
          <SubHead>{t.label}</SubHead>
          {t.id === 'core' ? view.core.map(c => (
            <div key={c.id} className="feature-row">
              <Switch checked disabled help={about ? undefined : c.about} onChange={() => undefined}
                      label={<>{c.label}{about && <span className="feature-about">{c.about}</span>}</>} />
            </div>
          )) : view.catalog.filter(f => f.tier === t.id).map(f => {
            // a feature with a page of its own links to it (Guard; Images & Music and a
            // plugin's form page while on); a plugin's form lives only on its page
            const on = view.features[f.id] !== false
            const page = f.id === 'guard' ? 'guard' : MEDIA.has(f.id) && on ? 'media' : f.form && on ? f.form : null
            const Form = onOpen && !page && !f.form ? formOf(f) : undefined
            const isOpen = open === f.id
            return (
              <div key={f.id}>
                <div className="feature-line">
                  <FeatureSwitch feature={f} on={view.features[f.id] !== false} about={about} free={free && f.step}
                                 waiting={waits(view, f)} disabled={disabled} onChange={v => onChange(flip(view, f.id, v))} />
                  {onOpen && page && (
                    <Button variant="ghost" size="sm" icon={ArrowUpRight} href={format({ section: 'settings', page })}>
                      {page === 'guard' ? 'Open Guard' : page === 'media' ? 'Open Images & Music' : `Open ${f.label}`}
                    </Button>
                  )}
                  {Form && (
                    <Button variant="ghost" size="sm" icon={isOpen ? ChevronUp : ChevronDown} title="Edit" pressed={isOpen}
                            onClick={() => onOpen?.(isOpen ? null : f.id)}>Edit</Button>
                  )}
                </div>
                {Form && isOpen && (
                  <SetupForm Form={Form} feature={f} value={forms?.[f.id]} onChange={v => setForms?.({ ...forms, [f.id]: v })}
                             reprobe={() => reprobe?.(f.id) ?? Promise.resolve()} />
                )}
              </div>
            )
          })}
        </div>
      ))}
    </div>
  )
}

/** one setup form under its row (Settings): its own save ref, which its Check runs */
function SetupForm({ Form, feature, value, onChange, reprobe }: {
  Form: NonNullable<ReturnType<typeof formOf>>
  feature: Feature
  value: unknown
  onChange: (v: unknown) => void
  reprobe: () => Promise<void>
}) {
  const saveRef: SaveRef = useRef(null)
  return <div className="feature-setup"><Form feature={feature} value={value} onChange={onChange} saveRef={saveRef} reprobe={reprobe} /></div>
}

/** one feature's switch and its label; switched on but missing or not set up, the probe's
    sentence under it in the warn tone.
    Blocked (its service missing or not set up): off and greyed, its `needs` sentence under
    it, and a click shows it as a toast; `free` (the onboarding, a feature whose setup step
    comes next): it switches as any other. `waiting` (the feature it comes `after` is switched
    off, as flipped on this page): off and greyed as when blocked. With `about` the sentence is a second line and
    its "?" is `off_when`; without, its "?" is the server's `help`. A "?" never repeats what
    the row shows: an off row has none. A feature with `warn` (shares) asks first when
    switched on (Turn on); off needs no confirm */
export function FeatureSwitch({ feature: f, on, disabled = false, about = false, free = false, waiting = false, onChange }: {
  feature: Feature
  on: boolean
  disabled?: boolean
  about?: boolean
  free?: boolean
  /** the feature it comes after is switched off: off and greyed as when blocked */
  waiting?: boolean
  onChange: (on: boolean) => void
}) {
  const off = (blocked(f) && !free) || waiting
  // switched on but the probe found it missing or not set up: its sentence, in the warn tone
  const problem = on && !off && (f.state === 'missing' || f.state === 'unset') && f.detail
  const line = off ? f.needs : problem ? <StateWord tone="warn">{f.detail}</StateWord> : about ? f.about : ''
  const help = off ? undefined : (about ? f.off_when : f.help) || undefined
  const [asking, setAsking] = useState(false)
  return (
    <div className={cx('feature-row', off && 'feature-row--off')}
         onClick={off ? () => toast({ text: f.needs }) : undefined}>
      <Switch checked={!off && on} disabled={disabled || off} help={help}
              onChange={v => (v && f.warn ? setAsking(true) : onChange(v))}
              label={<>
                {f.label}
                {line && <span className="feature-about">{line}</span>}
              </>} />
      {asking && (
        <ConfirmDialog title={`Turn on ${f.label}?`} body={f.warn} confirmLabel="Turn on"
                       onClose={() => setAsking(false)} onConfirm={() => { setAsking(false); onChange(true) }} />
      )}
    </div>
  )
}
