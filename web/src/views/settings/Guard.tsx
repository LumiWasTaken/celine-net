/* Settings, Guard (/settings/guard): the one place Bastet is set up. The feature's switch
   (features.toml guard) with its sentence while on and not ready, its mode while on (Warn or
   Enforce, the general setting), its two models (the Decision model is the guard role, the second
   look is guard.toml's chain) through the same form the onboarding's Guard card uses
   (WelcomeForms.tsx), its Rules (what each rule does: Off, Warn, Stop, Freeze, and the tools
   that ask you first; guard.toml), the Passes that hold (only while one does, each with End),
   and its own texts: Persona, Replies to <its name>, Messages to you, each a long text on a
   surface that opens in the large editor (BastetText below: History and Ask Celine, keys
   bastet:persona, bastet:talk, bastet:note; the shipped file is the default, Use the default
   in the editor's "..." while its own is in use). Every "?" text is the server's. Everything
   saves itself. API: lib/guard.ts guardApi. */
import { useRef, useState } from 'react'
import { Pencil, SearchX, Ticket } from 'lucide-react'
import { LongEditor, TextHistory, textHistory } from '../../components/editor'
import { guardApi, type BastetField, type GuardRules, type RuleAct } from '../../lib/guard'
import { useAgent } from '../../lib/agent'
import { format, useNav, type Route } from '../../lib/nav'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { settingsApi, type FeaturesView } from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useAutosave } from '../../lib/useAutosave'
import { useResource } from '../../lib/useResource'
import { Button, EmptyState, Field, InlineError, Input, ListRow, Loaded, LoadingRow, Section, Segmented, StatusLine, Switch } from '../../ui'
import { GeneralFields } from './General'
import { TextPreview } from './Agent'
import { FORMS, type SaveRef } from '../WelcomeForms'
import { Page } from './parts'
import './settings.css'

const guardPage: Route = { section: 'settings', page: 'guard' }
const editRoute = (key: string, history = false): Route => ({ section: 'settings', page: 'guard', edit: key, ...(history && { history }) })

/** Bastet's texts: their titles (the agent's name in one), and what to ask the agent for one */
const TEXTS: { field: BastetField; title: (name: string) => string; hint: string }[] = [
  { field: 'persona', title: () => 'Persona', hint: 'Make her warmer' },
  { field: 'talk', title: name => `Replies to ${name}`, hint: 'Make her replies shorter' },
  { field: 'note', title: () => 'Messages to you', hint: 'Make her messages plainer' },
]

const useTexts = () => useResource(() => guardApi.texts().then(r => r.texts), [], { events: ev => ev.type === 'guard.texts.changed' })

export function Guard() {
  const online = useOnline()
  const name = useAgent()?.name ?? ''
  const view = useResource<FeaturesView>(() => settingsApi.features(), [], { events: ev => ev.type === 'features.changed' })
  const texts = useTexts()
  const rules = useResource(() => guardApi.rules(), [], { events: ev => ev.type === 'settings.changed' })
  const passes = useResource(() => guardApi.passes().then(r => r.passes), [], { events: ev => ev.type === 'guard.passes' })
  const [form, setForm] = useState<unknown>(undefined)
  const saveRef: SaveRef = useRef(null)
  const put = useAutosave<boolean>(async changes => {
    if (view.data) view.set(await settingsApi.putFeatures({ ...view.data.features, ...changes }))
  })
  const feature = view.data?.catalog.find(f => f.id === 'guard')
  const on = !!view.data && (put.pending.guard ?? view.data.features.guard) !== false
  const help = rules.data?.help
  const Form = FORMS.guard!
  return (
    <Page title="Guard">
      <Section>
        {view.error && <InlineError message={view.error} onRetry={view.reload} />}
        {!feature ? <LoadingRow /> : <>
          <Switch checked={on} disabled={!online || feature.state === 'missing'} title={online ? undefined : OFFLINE_TITLE}
                  onChange={v => put.set('guard', v)} label={feature.label} help={feature.help} />
          {on && (feature.state === 'missing' || feature.state === 'unset') && <StatusLine tone="warn">{feature.detail || feature.needs}</StatusLine>}
        </>}
        {put.error && <InlineError compact message={put.error} onRetry={put.flush} />}
      </Section>
      <Section title="Mode" help={help?.mode}>
        <GeneralFields only={['guard']} />
      </Section>
      {feature && (
        <Section title="Models">
          <div className="stack">
            <Form feature={feature} value={form} onChange={setForm} saveRef={saveRef}
                  reprobe={async () => { await settingsApi.probeFeature('guard'); view.reload() }} />
          </div>
        </Section>
      )}
      <Section title="Rules" help={help?.rules}>
        {rules.data ? <Rules rules={rules.data} set={rules.set} disabled={!online || !on} /> : <Loaded res={rules} />}
      </Section>
      {!!passes.data?.length && (
        <Section title="Passes" help={help?.passes}>
          {passes.data.map(p => <PassRow key={p.id} id={p.id} title={p.label} line={p.line} piece={p.piece} />)}
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

/* ── what each rule does, one Segmented per rule (Off is its switch, else its `does`), and the
   tools that ask you first, saved when the field is left ── */
function Rules({ rules, set, disabled }: { rules: GuardRules; set: (r: GuardRules) => void; disabled: boolean }) {
  const save = useAction(async (tables: Record<string, Record<string, unknown>>) => set(await guardApi.putRules(tables)))
  const tools = rules.confirm.tools.join(', ')
  const [draft, setDraft] = useState<string | null>(null)
  const act = (rule: string, v: RuleAct) => void save.run({ [rule]: v === 'off' ? { on: false } : { on: true, does: v } })
  const keep = () => {
    if (draft === null || draft === tools) { setDraft(null); return }
    void save.run({ confirm: { tools: draft.split(',').map(t => t.trim()).filter(Boolean) } }).then(() => setDraft(null))
  }
  return (
    <div className="stack">
      {rules.rows.map(r => (
        <Field key={r.rule} label={r.label} group>
          <Segmented label={r.label} value={r.act} options={rules.acts} disabled={disabled || save.busy} onChange={v => act(r.rule, v)} />
        </Field>
      ))}
      <Field label="Ask you first" help={rules.help.confirm}>
        <Input value={draft ?? tools} disabled={disabled} onChange={e => setDraft(e.target.value)} onBlur={keep}
               onKeyDown={e => { if (e.key === 'Enter') keep() }} />
      </Field>
      {save.error && <InlineError compact message={save.error} />}
    </div>
  )
}

/* ── one pass that holds: its rule, where and until when, the piece it lets through; End ── */
function PassRow({ id, title, line, piece }: { id: number; title: string; line: string; piece: string | null }) {
  const end = useAction(() => guardApi.endPass(id))
  return (
    <>
      <ListRow icon={Ticket} title={title} subtitle={line} detail={piece ?? undefined} trailingAlways
               trailing={<Button size="sm" variant="ghost" loading={end.busy} onClick={() => void end.run()}>End</Button>} />
      {end.error && <InlineError compact message={end.error} />}
    </>
  )
}

/* ── one of Bastet's texts in the large editor, or its History; Restore this version puts
   that text back and saves it ── */
export function BastetText({ edit, history }: { edit: string; history: boolean }) {
  const nav = useNav()
  const name = useAgent()?.name ?? ''
  const texts = useTexts()
  const t = TEXTS.find(x => x.field === edit)
  if (!t) return <Page title=""><EmptyState icon={SearchX} title="Not found">Nothing at this link.</EmptyState></Page>
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
