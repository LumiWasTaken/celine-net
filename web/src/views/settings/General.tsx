/* General: when a settings file has an error, one quiet line and the list under it; You
   (your name, time zone, what you write about you), each saved when you leave it; Chats
   (Lock archived chats, Describe new chat photos), saved on change. How the app looks is
   Appearance; its name is its own (Her.tsx). Source code: the code this build runs, its
   commit on the link (the server's `build`, the AGPL's offer). Advanced: the time limits and
   sizes (the server's `advanced` fields; a skill's own under its label), each saved on change
   (lib/useAutosave). */
import { useEffect } from 'react'
import { ArrowUpRight } from 'lucide-react'
import type { ConfigStatus } from '../../lib/api'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { settingsApi } from '../../lib/settingsApi'
import { useAutosave } from '../../lib/useAutosave'
import { useResource } from '../../lib/useResource'
import { Button, Field, Form, type FormField, InlineError, Input, Section, TextArea } from '../../ui'
import { Page } from './parts'
import './settings.css'

export function General({ status }: { status: ConfigStatus | null }) {
  const problems = status?.problems ?? []
  return (
    <Page title="General">
      {problems.length > 0 && (
        <Section>
          <div className="config-line">
            <p className="src-line">{problems.length === 1 ? 'A settings file has an error.' : 'Settings files have errors.'}</p>
            <ul className="plain-list">
              {problems.map((p, i) => <li key={i}><code>{p.file}</code>: {p.message}</li>)}
            </ul>
          </div>
        </Section>
      )}
      <YouCard />
      <Section title="Chats">
        <GeneralFields only={['lock_archived', 'image_captions_auto']} />
      </Section>
      {status && (
        <Section title="Source code"
                 action={<Button variant="ghost" size="sm" icon={ArrowUpRight} href={status.build.source} target="_blank" rel="noopener">
                   {status.build.commit}
                 </Button>} />
      )}
    </Page>
  )
}

export function Advanced() {
  return (
    <Page title="Advanced">
      <GeneralFields advanced />
    </Page>
  )
}

/* The general settings from the server's schema: the ones named, or the Advanced ones
   (the app's own, then one section per skill, by the server's `group`). Several pages
   show a few of them; each save sends the whole object back (the API replaces it). */
export function GeneralFields({ only, advanced = false }: { only?: string[]; advanced?: boolean }) {
  const online = useOnline()
  const res = useResource(() => settingsApi.general(), [],
                          { events: ev => ev.type === 'settings.changed' })
  const auto = useAutosave(async changes => {
    const r = await settingsApi.putGeneral({ ...res.data?.general, ...changes })
    res.set(d => d && { ...d, general: r.general })
  })
  if (res.data === undefined && !res.error) return null
  if (!res.data) {
    const error = <InlineError compact message={res.error} onRetry={res.reload} />
    return advanced ? <Section>{error}</Section> : error
  }
  const schema = res.data.fields.filter(f => only ? only.includes(f.key) : advanced && f.advanced)
  if (schema.length === 0) return null
  const values = { ...res.data.general, ...auto.pending }
  const form = (fields: FormField[], withError = false) => (
    <div title={online ? undefined : OFFLINE_TITLE}>
      <Form fields={fields} values={values} disabled={!online} onChange={(k, v) => auto.set(k, v)} />
      {withError && auto.error && <InlineError compact message={auto.error} onRetry={auto.flush} />}
    </div>
  )
  if (!advanced) return form(schema, true)
  const groups = [...new Set(schema.map(f => f.group).filter((g): g is string => !!g))]
  return (
    <>
      <Section>{form(schema.filter(f => !f.group), true)}</Section>
      {groups.map(g => <Section key={g} title={g}>{form(schema.filter(f => f.group === g))}</Section>)}
    </>
  )
}

/* ── you: your name, your time zone (this browser's until one is saved: shown, and saved
   with the rest when you leave), what you write about you ── */
function YouCard() {
  const online = useOnline()
  const res = useResource(() => settingsApi.general(), [], { events: ev => ev.type === 'settings.changed' })
  const g = res.data?.general
  const auto = useAutosave(async changes => {
    const r = await settingsApi.putGeneral({ ...res.data?.general, ...changes })
    res.set(d => d && { ...d, general: r.general })
  })
  const zoneDefault = res.data?.fields.find(f => f.key === 'timezone')?.default
  const here = Intl.DateTimeFormat().resolvedOptions().timeZone
  // never saved and still the default zone: this browser's
  const prefill = !!g && !res.data?.saved && g.timezone === zoneDefault && !!here && here !== g.timezone
  useEffect(() => { if (prefill) auto.set('timezone', here, 'blur') }, [prefill]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!g) return <Section title="You">{res.error && <InlineError compact message={res.error} onRetry={res.reload} />}</Section>
  const value = (k: string) => String(auto.pending[k] ?? g[k] ?? '')
  const field = (k: string) => ({
    value: value(k), disabled: !online,
    onChange: (e: { target: { value: string } }) => auto.set(k, e.target.value, 'blur'),
    onBlur: auto.flush,
  })
  const enter = (e: React.KeyboardEvent) => { if (e.key === 'Enter') auto.flush() }
  return (
    <Section title="You">
      <div className="form" title={online ? undefined : OFFLINE_TITLE}>
        <Field label="Your name"><Input {...field('user_name')} maxLength={40} onKeyDown={enter} /></Field>
        <Field label="Time zone"><Input {...field('timezone')} placeholder="America/New_York" spellCheck={false} onKeyDown={enter} /></Field>
        <Field label="About you"><TextArea {...field('user_bio')} rows={4} maxLength={2000} /></Field>
        {auto.error && <InlineError compact message={auto.error} onRetry={auto.flush} />}
      </div>
    </Section>
  )
}
