/* Settings, Features (/settings/features): one row per feature in the server's catalog
   order: the Dot of what its probe found (the detail sentence as the tooltip), its label
   (its `about` as the row's tooltip), its switch (off and greyed while the service is
   missing or not set up, its `needs` sentence under it and in a toast on a click), saved
   on each change (the whole object). A feature with a setup form opens it under its row
   (Edit), the same form the onboarding used (WelcomeForms.tsx), so "later" is one click;
   its Check saves and asks the probe anew, the row's Dot follows. A form's draft is kept
   by feature id, one per feature. "Check again" in the head
   asks every probe anew. The onboarding draws the same rows (FeatureRows) for the plain
   switches, with `about` as a second line, and for the Later card (`later`: off, greyed,
   "Turn on later in Settings"). API: lib/settingsApi.ts settingsApi.features /
   putFeatures / probeFeature. */
import { ArrowUpRight, ChevronDown, ChevronUp, RefreshCw } from 'lucide-react'
import { format } from '../../lib/nav'
import { useRef, useState } from 'react'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { STATE_TONE, settingsApi, type Feature, type FeaturesView } from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useAutosave } from '../../lib/useAutosave'
import { useResource } from '../../lib/useResource'
import { Button, ConfirmDialog, cx, Dot, InlineError, Loaded, Section, Switch, toast } from '../../ui'
import { formOf, type SaveRef } from '../WelcomeForms'
import { Page } from './parts'
import './settings.css'

/** a feature's switch cannot be on: its service is missing or not set up */
export const blocked = (f: Feature) => f.state === 'missing' || f.state === 'unset'
/** the onboarding gives it a card: the server's policy on what the probe found */
export const showsCard = (f: Feature) =>
  f.card === 'always' || (f.card === 'detect' && (f.state === 'ready' || f.state === 'missing'))
/** the Dot of a feature's state: grey once its switch is off, whatever the probe found */
export const stateTone = (f: Feature, on: boolean) => (f.state === 'ready' && !on ? 'muted' : STATE_TONE[f.state ?? 'none'])
/** a plain switch: nothing to detect, nothing to set up */
export const isPlain = (f: Feature) => !f.setup && !f.form && f.state === 'none'
const LATER = 'Turn on later in Settings'
const MEDIA = new Set(['images', 'music'])

/** every feature's probe, asked now; the catalog with the answers */
export async function reprobe(catalog: Feature[]): Promise<Feature[]> {
  const probes = await Promise.all(catalog.map(f => f.state === 'none' ? null : settingsApi.probeFeature(f.id)))
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
            <FeatureRows view={{ ...res.data, features: { ...res.data.features, ...save.pending } }} disabled={!online}
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

/** the catalog (or `only` those ids) as switch rows; `onChange` gets the whole object;
    `about`: each row says what its feature gives (the onboarding); `later`: the Later card's
    rows. With `open`, a row with a setup form opens it (Edit) */
export function FeatureRows({ view, only, disabled = false, about = false, later = false, onChange, open, onOpen, forms, setForms, reprobe }: {
  view: FeaturesView
  only?: string[]
  disabled?: boolean
  about?: boolean
  later?: boolean
  onChange: (features: Record<string, boolean>) => void
  open?: string | null
  onOpen?: (id: string | null) => void
  forms?: Record<string, unknown>
  setForms?: (f: Record<string, unknown>) => void
  reprobe?: (id: string) => Promise<void>
}) {
  return (
    <div className="form">
      {view.catalog.filter(f => !only || only.includes(f.id)).map(f => {
        // a feature with a page of its own links to it (Guard; Images & Music while on)
        const page = f.id === 'guard' ? 'guard' : MEDIA.has(f.id) && view.features[f.id] !== false ? 'media' : null
        const Form = onOpen && !page ? formOf(f) : undefined
        const isOpen = open === f.id
        return (
          <div key={f.id}>
            <div className="feature-line">
              <FeatureSwitch feature={f} on={view.features[f.id] !== false} about={about} later={later}
                             disabled={disabled} onChange={v => onChange({ ...view.features, [f.id]: v })} />
              {onOpen && page && (
                <Button variant="ghost" size="sm" icon={ArrowUpRight} href={format({ section: 'settings', page })}>
                  {page === 'guard' ? 'Open Guard' : 'Open Images & Music'}
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

/** one feature's switch: the state Dot (grey once the switch is off) and the label.
    Blocked (its service missing or not set up): off and greyed, its `needs` sentence under
    it, and a click shows it as a toast. `later`: the Later card's row, off and greyed with
    "Turn on later in Settings" under it. With `about` the sentence is a second line and
    the tooltip is `off_when`; without, the tooltip is `about`. `aboutShown`: the card
    already shows `about`, its state line and its `needs`, so the tooltip is `off_when`,
    the Dot says nothing more and there is no second line. A tooltip never repeats what
    the row shows: an off row has none. A feature with `warn` (shares) asks first when
    switched on (Turn on); off needs no confirm */
export function FeatureSwitch({ feature: f, on, disabled = false, about = false, aboutShown = false, later = false, label, onChange }: {
  feature: Feature
  on: boolean
  /** its words in place of its label (a group card: its `choice`) */
  label?: string
  disabled?: boolean
  about?: boolean
  aboutShown?: boolean
  later?: boolean
  onChange: (on: boolean) => void
}) {
  const tone = stateTone(f, on)
  const off = later || blocked(f)
  const line = later ? LATER : blocked(f) ? (aboutShown ? '' : f.needs) : about ? f.about : ''
  const title = off ? undefined : (about || aboutShown ? f.off_when : f.about) || undefined
  const [asking, setAsking] = useState(false)
  return (
    <div className={cx('feature-row', off && 'feature-row--off')}
         onClick={blocked(f) && !later ? () => toast({ text: f.needs }) : undefined}>
      <Switch checked={!blocked(f) && on} disabled={disabled || off} title={title}
              onChange={v => (v && f.warn ? setAsking(true) : onChange(v))}
              label={<>
                {tone && <Dot tone={tone} label={aboutShown || later ? undefined : f.detail || undefined} />}
                {tone && ' '}{label ?? f.label}
                {line && <span className="feature-about">{line}</span>}
              </>} />
      {asking && (
        <ConfirmDialog title={`Turn on ${f.label}?`} body={f.warn} confirmLabel="Turn on"
                       onClose={() => setAsking(false)} onConfirm={() => { setAsking(false); onChange(true) }} />
      )}
    </div>
  )
}
