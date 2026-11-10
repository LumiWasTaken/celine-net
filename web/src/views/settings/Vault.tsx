/* Vault: its passwords on your Vaultwarden. The server's fields first, then the master
   password. The server fields save when you leave them, the client secret too (or on
   Enter); it is typed once and never shown again. The master password is stored
   nowhere: you type it to unlock, and every restart or save locks the vault again. The agent's
   requests that need your tap wait here as their cards (ApprovalCard). The line on top is
   the server's word on the vault.
   API: server/celine/api/settings.py (lib/settingsApi.ts). */
import { useState } from 'react'
import { Lock, LockOpen } from 'lucide-react'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { LINK, settingsApi } from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useAutosave } from '../../lib/useAutosave'
import { useResource } from '../../lib/useResource'
import { Button, Field, Form, InlineError, Input, Section } from '../../ui'
import { Page } from './parts'
import { ApprovalCard } from '../../components/ApprovalCard'
import './settings.css'

export function Vault() {
  const online = useOnline()
  const res = useResource(() => settingsApi.vault(), [],
                          { events: ev => ev.type === 'settings.changed' || ev.type === 'approvals.changed' })
  const [secret, setSecret] = useState('')
  const [master, setMaster] = useState('')
  const auto = useAutosave(async changes => {
    res.set(await settingsApi.putVault({ ...res.data?.vault, ...changes }))
  })
  const saveSecret = useAction(async () => {
    res.set(await settingsApi.putVault(res.data?.vault ?? {}, { client_secret: secret }))
    setSecret('')
  })
  const unlock = useAction(async () => {
    const typed = master
    setMaster('')
    res.set(await settingsApi.unlockVault(typed))
  })
  const lock = useAction(async () => { res.set(await settingsApi.lockVault()) })
  if (res.data === undefined && !res.error) return <Page title="Vault"><div /></Page>
  if (!res.data) return <Page title="Vault"><InlineError compact message={res.error} onRetry={res.reload} /></Page>
  const saved = (s: { state: string }) => (s.state === 'set' ? 'Saved' : undefined)
  const error = unlock.error ?? lock.error
  return (
    <Page title="Vault">
      {res.data.asks.map(a => <ApprovalCard key={a.id} ask={a} alone />)}
      <Section>
        {res.data.status !== LINK.unset && <p className="src-line">{res.data.status}</p>}
        <div title={online ? undefined : OFFLINE_TITLE}>
          <Form fields={res.data.fields} values={{ ...res.data.vault, ...auto.pending }} disabled={!online}
                onChange={(k, v) => auto.set(k, v)} />
          {auto.error && <InlineError compact message={auto.error} onRetry={auto.flush} />}
        </div>
        <form onSubmit={e => { e.preventDefault(); if (secret) void saveSecret.run() }}>
          <Field label="Client secret" error={saveSecret.error}>
            <Input type="password" autoComplete="off" spellCheck={false} value={secret} disabled={!online}
                   onChange={e => setSecret(e.target.value)} onBlur={() => { if (secret) void saveSecret.run() }}
                   placeholder={saved(res.data.client_secret)} />
          </Field>
        </form>
      </Section>
      <Section>
        {res.data.unlocked
          ? <Button icon={Lock} loading={lock.busy} disabled={!online} onClick={() => void lock.run()}>Lock</Button>
          : <form className="key-form" onSubmit={e => { e.preventDefault(); if (master) void unlock.run() }}>
              <Field label="Master password">
                <Input type="password" autoComplete="off" spellCheck={false} value={master}
                       onChange={e => setMaster(e.target.value)} />
              </Field>
              <Button type="submit" variant="primary" icon={LockOpen} loading={unlock.busy}
                      disabled={!master || !online}>Unlock</Button>
            </form>}
      </Section>
      {error && <InlineError compact message={error} />}
    </Page>
  )
}
