/* A form served by /api/forms/{id} (lib/forms.ts) as a Settings page: its note, the app's
   address when the view asks for it, its fields saving themselves, its actions (the view
   is read again after one). A plugin with a settings.toml gets this page, named
   after it. FormActions is shared with the onboarding's card (WelcomeForms.tsx). */
import { useState } from 'react'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { useForm, type ActionResult, type FormAction, type FormLine, type FormView, type UseForm } from '../../lib/forms'
import { useAction } from '../../lib/useAction'
import { useAutosave } from '../../lib/useAutosave'
import { Button, Facts, Field, Form, InlineError, Input, Notice, Section } from '../../ui'
import { AddressField } from './General'
import { Page } from './parts'
import './settings.css'

export function FormPage({ id, label }: { id: string; label: string }) {
  const online = useOnline()
  const form = useForm(id)
  const auto = useAutosave(changes => form.save(changes))
  const d = form.res.data
  if (d === undefined && !form.res.error) return <Page title={label}><div /></Page>
  if (!d) return <Page title={label}><InlineError compact message={form.res.error} onRetry={form.res.reload} /></Page>
  const shown = form.shown(auto.pending)
  return (
    <Page title={label}>
      <Section>
        <FormState view={d} />
        {d.address && <AddressField />}
        <div title={online ? undefined : OFFLINE_TITLE}>
          <Form fields={d.fields} values={shown.values} secrets={shown.secrets} disabled={!online}
                onChange={(k, v) => auto.set(k, v)} />
        </div>
        {auto.error && <InlineError compact message={auto.error} onRetry={auto.flush} />}
        <FormActions form={form} disabled={!online || Object.keys(auto.pending).length > 0} />
      </Section>
    </Page>
  )
}

/** what the server says of the form's device: its note, its facts, what is off */
export function FormState({ view: d }: { view: FormView }) {
  return <>
    {d.note && <p className="src-line">{d.note}</p>}
    {d.facts && <Facts items={d.facts} />}
    {d.lines && <Lines lines={d.lines} />}
  </>
}

/** the server's lines in words, each one line of tinted text with its icon (a compact
    Notice), never a dot */
function Lines({ lines }: { lines: FormLine[] }) {
  return <div className="form__lines">
    {lines.map((l, i) => <Notice key={i} compact tone={NOTICE[l.tone]}>{l.text}</Notice>)}
  </div>
}
const NOTICE = { ok: 'success', warn: 'warn', error: 'error' } as const

/** a form's actions, each a button (with the one text it asks for), its answer and its
    lines in words (Lines).
    `before`: what runs first (the onboarding saves its draft); `after`: what follows (its
    card's probe, asked anew) */
export function FormActions({ form, disabled, before, after }: {
  form: UseForm
  disabled: boolean
  before?: () => Promise<void>
  after?: () => Promise<unknown>
}) {
  // a plugin's actions are its code, which runs only while it is on
  const actions = form.res.data?.running ? form.res.data.actions : []
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [result, setResult] = useState<ActionResult | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const run = useAction(async (a: FormAction) => {
    setBusy(a.id)
    try {
      await before?.()
      setResult(await form.run(a.id, a.input ? inputs[a.id] ?? '' : undefined))
      setInputs(x => ({ ...x, [a.id]: '' }))
      form.res.reload()
      await after?.()
    } finally { setBusy(null) }
  })
  if (!actions.length) return null
  return <>
    {actions.filter(a => a.input).map(a => (
      <Field key={a.id} label={a.input!}>
        <Input type="password" autoComplete="off" spellCheck={false} value={inputs[a.id] ?? ''}
               onChange={e => setInputs(x => ({ ...x, [a.id]: e.target.value }))} />
      </Field>
    ))}
    <div className="form__control">
      {actions.map(a => (
        <Button key={a.id} loading={busy === a.id} disabled={disabled || !!busy || (!!a.input && !inputs[a.id])}
                onClick={() => void run.run(a)}>{a.label}</Button>
      ))}
    </div>
    {result?.sentence && <Lines lines={[{ text: result.sentence, tone: result.ok ? 'ok' : 'error' }]} />}
    {result?.lines && <Lines lines={result.lines} />}
    {run.error && <InlineError compact message={run.error} />}
  </>
}
