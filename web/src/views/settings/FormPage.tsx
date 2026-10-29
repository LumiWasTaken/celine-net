/* A form served by /api/forms/{id} (lib/forms.ts) as a Settings page: its note, its fields
   saving themselves, its actions. A plugin with a settings.toml gets this page, named
   after it. FormActions is shared with the onboarding's card (WelcomeForms.tsx). */
import { useState } from 'react'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { useForm, type ActionResult, type FormAction, type UseForm } from '../../lib/forms'
import { useAction } from '../../lib/useAction'
import { useAutosave } from '../../lib/useAutosave'
import { Button, Dot, Field, Form, InlineError, Input, Section } from '../../ui'
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
        {d.note && <p className="src-line">{d.note}</p>}
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

/** a form's actions, each a button (with the one text it asks for), its answer as a Dot.
    `before`: what runs first (the onboarding saves its draft); `after`: what follows (its
    card's probe, asked anew) */
export function FormActions({ form, disabled, before, after }: {
  form: UseForm
  disabled: boolean
  before?: () => Promise<void>
  after?: () => Promise<unknown>
}) {
  const actions = form.res.data?.actions ?? []
  return <>{actions.map(a => <ActionRow key={a.id} a={a} form={form} disabled={disabled} before={before} after={after} />)}</>
}

function ActionRow({ a, form, disabled, before, after }: {
  a: FormAction; form: UseForm; disabled: boolean; before?: () => Promise<void>; after?: () => Promise<unknown>
}) {
  const [input, setInput] = useState('')
  const [result, setResult] = useState<ActionResult | null>(null)
  const run = useAction(async () => {
    await before?.()
    setResult(await form.run(a.id, a.input ? input : undefined))
    setInput('')
    await after?.()
  })
  return <>
    {a.input && (
      <Field label={a.input}>
        <Input type="password" autoComplete="off" spellCheck={false} value={input} onChange={e => setInput(e.target.value)} />
      </Field>
    )}
    <div className="form__control">
      <Button loading={run.busy} disabled={disabled || (!!a.input && !input)}
              onClick={() => void run.run()}>{a.label}</Button>
      {result && <Dot tone={result.ok ? 'ok' : 'error'} text={result.sentence} />}
    </div>
    {run.error && <InlineError compact message={run.error} />}
  </>
}
