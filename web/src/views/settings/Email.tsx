/* Email: the one account it manages over IMAP: reads, marks, moves and deletes. The server fields save when
   you leave them; the password is typed once and never shown again. "Check email"
   logs in with what is saved and counts the folders; the page tests once when it opens
   with a password saved, so the state line is never blank.
   API: server/celine/api/settings.py (lib/settingsApi.ts). */
import { useEffect, useState } from 'react'
import { KeyRound, RefreshCw } from 'lucide-react'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { LINK, settingsApi, type EmailCheck } from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useAutosave } from '../../lib/useAutosave'
import { useResource } from '../../lib/useResource'
import { Button, Field, InlineError, Input, Notice, Section } from '../../ui'
import { Page, SchemaForm } from './parts'
import './settings.css'

export function Email() {
  const online = useOnline()
  // whose mailbox it is: its own
  const title = 'Email'
  const res = useResource(() => settingsApi.email(), [],
                          { events: ev => ev.type === 'settings.changed' })
  const [password, setPassword] = useState('')
  const [checked, setChecked] = useState<EmailCheck | null>(null)
  const auto = useAutosave(async changes => {
    res.set(await settingsApi.putEmail({ ...res.data?.email, ...changes }))
    setChecked(null)
  })
  const savePassword = useAction(async () => {
    res.set(await settingsApi.putEmail(res.data?.email ?? {}, password))
    setPassword('')
    setChecked(null)
  })
  const check = useAction(async () => setChecked(await settingsApi.checkEmail()))
  const saved = res.data?.password.state === 'set'
  useEffect(() => { if (saved) void check.run() }, [saved]) // eslint-disable-line react-hooks/exhaustive-deps
  if (res.data === undefined && !res.error) return <Page title={title}><div /></Page>
  if (!res.data) return <Page title={title}><InlineError compact message={res.error} onRetry={res.reload} /></Page>
  const set = res.data.password.state === 'set'
  return (
    <Page title={title}
          actions={<Button icon={RefreshCw} loading={check.busy} disabled={!online} onClick={() => void check.run()}>Check email</Button>}>
      <Section>
        {set && <p className="src-line">{checked ? (checked.ok ? LINK.up : LINK.down) : check.error ? LINK.down : 'Testing'}</p>}
        <div title={online ? undefined : OFFLINE_TITLE}>
          <SchemaForm schema={res.data.fields} values={{ ...res.data.email, ...auto.pending }} disabled={!online}
                      onChange={(k, v) => auto.set(k, v)} />
        </div>
        <form className="key-form" onSubmit={e => { e.preventDefault(); if (password) void savePassword.run() }}>
          <Field label="Password">
            <Input type="password" autoComplete="off" spellCheck={false} value={password}
                   onChange={e => setPassword(e.target.value)} placeholder={set ? 'Saved' : undefined} />
          </Field>
          <Button type="submit" variant="primary" icon={KeyRound} loading={savePassword.busy}
                  disabled={!password || !online}>Save password</Button>
        </form>
        {auto.error && <InlineError compact message={auto.error} onRetry={auto.flush} />}
        {savePassword.error && <InlineError compact message={savePassword.error} />}
        {checked && !checked.ok && <Notice tone="warn">{checked.sentence}</Notice>}
        {checked?.ok && (
          <>
            <p className="src-line">{checked.sentence}</p>
            <ul className="plain-list">
              {checked.folders.map(f => <li key={f.name}>{f.name}: {f.messages ?? '?'} messages, {f.unread ?? '?'} unread</li>)}
            </ul>
          </>
        )}
        {check.error && <InlineError compact message={check.error} />}
      </Section>
    </Page>
  )
}
