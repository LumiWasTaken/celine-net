/* Vault: its passwords on your Vaultwarden. The server's fields first, then the master
   password. The server fields save when you leave them;
   the client secret is typed once and never shown again. The master password is stored
   nowhere: you type it to unlock, and every restart or save locks the vault again. The agent's
   requests that need your tap wait here (server/celine/approvals.py); every word
   of them is the server's. The line on top is the server's word on the vault.
   API: server/celine/api/settings.py (lib/settingsApi.ts). */
import { useState } from 'react'
import { KeyRound, Lock, LockOpen } from 'lucide-react'
import { capital, usePronouns } from '../../lib/name'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { LINK, settingsApi, type VaultAsk } from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useAutosave } from '../../lib/useAutosave'
import { useResource } from '../../lib/useResource'
import { Button, Field, Form, InlineError, Input, Section } from '../../ui'
import { Page } from './parts'
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
  const answer = useAction(async (id: string, ok: boolean) => {
    await settingsApi.answerApproval(id, ok)
    res.reload()
  })
  if (res.data === undefined && !res.error) return <Page title="Vault"><div /></Page>
  if (!res.data) return <Page title="Vault"><InlineError compact message={res.error} onRetry={res.reload} /></Page>
  const saved = (s: { state: string }) => (s.state === 'set' ? 'Saved' : undefined)
  const error = saveSecret.error ?? unlock.error ?? lock.error ?? answer.error
  return (
    <Page title="Vault">
      {res.data.asks.map(a => <Ask key={a.id} ask={a} busy={answer.busy || !online}
                                   onAnswer={ok => void answer.run(a.id, ok)} />)}
      <Section>
        {res.data.status !== LINK.unset && <p className="src-line">{res.data.status}</p>}
        <div title={online ? undefined : OFFLINE_TITLE}>
          <Form fields={res.data.fields} values={{ ...res.data.vault, ...auto.pending }} disabled={!online}
                onChange={(k, v) => auto.set(k, v)} />
          {auto.error && <InlineError compact message={auto.error} onRetry={auto.flush} />}
        </div>
        <form className="key-form" onSubmit={e => { e.preventDefault(); if (secret) void saveSecret.run() }}>
          <Field label="Client secret">
            <Input type="password" autoComplete="off" spellCheck={false} value={secret}
                   onChange={e => setSecret(e.target.value)} placeholder={saved(res.data.client_secret)} />
          </Field>
          <Button type="submit" variant="primary" icon={KeyRound} loading={saveSecret.busy}
                  disabled={!secret || !online}>Save</Button>
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

/** one request: what it asks, the entry's data, and what it read from outside right
    before; every line is the server's */
function Ask({ ask, busy, onAnswer }: { ask: VaultAsk; busy: boolean; onAnswer: (ok: boolean) => void }) {
  const p = usePronouns()
  return (
    <Section>
      <p><strong>{capital(p.subject)} {p.asks} to {ask.action}.</strong></p>
      {ask.facts.map(f => <p key={f} className="src-line">{f}</p>)}
      <p className="src-line">{capital(p.possessive)} reason, {p.possessive} words: <em>{ask.why}</em></p>
      {ask.read && <p className="src-line">Right before, {p.subject} read from outside with {ask.read}.</p>}
      {ask.expires_in !== null && <p className="src-line">Waits {Math.ceil(ask.expires_in / 60)} min more.</p>}
      <div className="key-form">
        <Button variant="primary" disabled={busy} onClick={() => onAnswer(true)}>Approve</Button>
        <Button disabled={busy} onClick={() => onAnswer(false)}>Deny</Button>
      </div>
    </Section>
  )
}
