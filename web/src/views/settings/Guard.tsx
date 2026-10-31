/* Settings, Guard (/settings/guard): the one place Bastet is set up. The feature's switch
   (features.toml guard), its mode while on (Watch or On, the general setting) with one line
   for each mode and for Off, its two models (the decision model is the Titles role, the
   second look is guard.toml's chain) through the same form the onboarding's Guard card uses
   (WelcomeForms.tsx), and its own texts: Persona, Replies to <its name>, Notes to you, each a
   long text on a surface that opens in the large editor (BastetText below: History and Ask
   Celine, keys bastet:persona, bastet:talk, bastet:note; the shipped file is the default,
   Use the default in the editor's "..." while its own is in use). Everything saves itself.
   API: lib/guard.ts guardApi.texts (GET /guard/texts, PUT /guard/texts/{field}). */
import { useRef, useState } from 'react'
import { Pencil, SearchX } from 'lucide-react'
import { LongEditor, TextHistory, textHistory } from '../../components/editor'
import { guardApi, type BastetField } from '../../lib/guard'
import { useHer } from '../../lib/her'
import { format, useNav, type Route } from '../../lib/nav'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { settingsApi, type FeaturesView } from '../../lib/settingsApi'
import { useAutosave } from '../../lib/useAutosave'
import { useResource } from '../../lib/useResource'
import { Button, Dot, EmptyState, InlineError, Loaded, LoadingRow, Section, Switch } from '../../ui'
import { stateTone } from './Features'
import { GeneralFields } from './General'
import { TextPreview } from './Her'
import { FORMS, type SaveRef } from '../WelcomeForms'
import { Page } from './parts'
import './settings.css'

const guardPage: Route = { section: 'settings', page: 'guard' }
const editRoute = (key: string, history = false): Route => ({ section: 'settings', page: 'guard', edit: key, ...(history && { history }) })

/** Bastet's texts: their titles (her name in one), and what to ask Celine for one */
const TEXTS: { field: BastetField; title: (her: string) => string; hint: string }[] = [
  { field: 'persona', title: () => 'Persona', hint: 'Make her warmer' },
  { field: 'talk', title: her => `Replies to ${her}`, hint: 'Make her replies shorter' },
  { field: 'note', title: () => 'Notes to you', hint: 'Make her notes plainer' },
]

const useTexts = () => useResource(() => guardApi.texts().then(r => r.texts), [], { events: ev => ev.type === 'guard.texts.changed' })

export function Guard() {
  const online = useOnline()
  const name = useHer()?.name ?? ''
  const view = useResource<FeaturesView>(() => settingsApi.features(), [], { events: ev => ev.type === 'features.changed' })
  const texts = useTexts()
  const [form, setForm] = useState<unknown>(undefined)
  const saveRef: SaveRef = useRef(null)
  const put = useAutosave<boolean>(async changes => {
    if (view.data) view.set(await settingsApi.putFeatures({ ...view.data.features, ...changes }))
  })
  const feature = view.data?.catalog.find(f => f.id === 'guard')
  const on = !!view.data && (put.pending.guard ?? view.data.features.guard) !== false
  const Form = FORMS.guard!
  return (
    <Page title="Guard">
      <Section>
        {view.error && <InlineError message={view.error} onRetry={view.reload} />}
        {!feature ? <LoadingRow /> : (
          <div className="feature-line">
            <Switch checked={on} disabled={!online || feature.state === 'missing'}
                    title={online ? feature.off_when : OFFLINE_TITLE} onChange={v => put.set('guard', v)}
                    label={<><span className="feature-name">{feature.about}</span></>} />
            {(() => { const tone = stateTone(feature, on); return tone ? <Dot tone={tone} text={feature.detail || feature.needs} /> : null })()}
          </div>
        )}
        {put.error && <InlineError compact message={put.error} onRetry={put.flush} />}
      </Section>
      <Section title="Mode">
        <div className={on ? undefined : 'is-off'}>
          <GeneralFields only={['guard']} />
          <span className="feature-about">Watch: every step runs at once, and Bastet writes down in Events what she would have done.</span>
          <span className="feature-about">On: Bastet checks a risky step before it runs, and stops it, freezes {name} or asks you first, as her rules say.</span>
          <span className="feature-about">Off (the switch above): Bastet does not run. Nothing is checked and nothing is written to Events.</span>
        </div>
      </Section>
      {feature && (
        <Section title="Models">
          <div className="feature-setup">
            <Form feature={feature} value={form} onChange={setForm} saveRef={saveRef}
                  reprobe={async () => { await settingsApi.probeFeature('guard'); view.reload() }} />
          </div>
        </Section>
      )}
      {TEXTS.map(t => (
        <Section key={t.field} title={t.title(name)} action={<Button icon={Pencil} href={format(editRoute(t.field))}>Edit</Button>}>
          {texts.data ? <TextPreview text={texts.data[t.field].text} to={editRoute(t.field)} empty={t.title(name)} />
            : <Loaded res={texts} />}
        </Section>
      ))}
    </Page>
  )
}

/* ── one of Bastet's texts in the large editor, or its History; Restore this version puts
   that text back and saves it ── */
export function BastetText({ edit, history }: { edit: string; history: boolean }) {
  const nav = useNav()
  const name = useHer()?.name ?? ''
  const texts = useTexts()
  const t = TEXTS.find(x => x.field === edit)
  if (!t) return <Page title=""><EmptyState icon={SearchX} title="Not found">No text at this link.</EmptyState></Page>
  if (!texts.data) return <Page title="">{<Loaded res={texts} />}</Page>
  const now = texts.data[t.field]
  const title = t.title(name)
  const of = `bastet:${t.field}`
  const save = async (text: string | null) => texts.set((await guardApi.putText(t.field, text)).texts)
  const crumbs = [{ label: 'Settings', href: format({ section: 'settings' }) }, { label: 'Guard', href: format(guardPage) }]
  if (history) {
    return (
      <TextHistory crumbs={[...crumbs, { label: title, href: format(editRoute(edit)) }]} source={textHistory(of)}
                   current={now.text} onRestore={text => save(text).then(() => nav.go(editRoute(edit)))} />
    )
  }
  return (
    <LongEditor crumbs={crumbs} title={title} of={of} history={format(editRoute(edit, true))} hint={t.hint}
                more={now.own ? [{ label: 'Use the default', onSelect: () => void save(null).then(() => nav.go(guardPage)) }] : []}
                texts={[{ key: 'text', label: title, value: now.text }]}
                autosave onSave={async v => { await save(v.text ?? '') }} />
  )
}
