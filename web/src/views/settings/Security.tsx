/* Security: the login's credentials in one list (a passkey, a password, a PIN: its kind as
   the icon, its label, when it was created, "This session" on the one this browser logged in
   with), Add passkey (the browser's prompt), Add password and Add PIN (a dialog each, the
   server's sentence on the field), Delete on each row (never the last one; when the server
   says logged_out the page goes to /). Then new recovery codes and the named tokens host
   scripts send on the intercom with (logging out is the door at the bottom of the rail).
   Listed only while the login is on. API: lib/passkey.ts securityApi. */
import { useState } from 'react'
import { Copy, Fingerprint, Hash, LockKeyhole, Plus, Trash2, type LucideIcon } from 'lucide-react'
import { stamp } from '../../lib/format'
import { securityApi, type Credential, type CredentialKind, type NamedToken } from '../../lib/passkey'
import { useAction } from '../../lib/useAction'
import { useResource } from '../../lib/useResource'
import {
  Button, ConfirmDialog, Dialog, Field, IconButton, InlineError, Input, ListRow, LoadingRow, PinInput, Section, copy,
} from '../../ui'
import { Page } from './parts'
import './settings.css'

const KIND: Record<CredentialKind, { icon: LucideIcon; word: string }> = {
  passkey: { icon: Fingerprint, word: 'Passkey' }, password: { icon: LockKeyhole, word: 'Password' }, pin: { icon: Hash, word: 'PIN' },
}
const PIN_LENGTH = 8
/** the server's seconds as a stamp */
const when = (t: number | string) => stamp(typeof t === 'number' ? t * 1000 : t)

export function Security() {
  const creds = useResource(() => securityApi.credentials(), [])
  const add = useAction(async () => { await securityApi.add(); creds.reload() })
  const [adding, setAdding] = useState<'password' | 'pin' | null>(null)
  const [deleting, setDeleting] = useState<Credential | null>(null)
  const [renewing, setRenewing] = useState(false)
  const [codes, setCodes] = useState<string[] | null>(null)
  const [copied, setCopied] = useState(false)
  const list = creds.data?.credentials ?? []
  const last = list.length === 1

  return (
    <Page title="Security">
      <div className="stack stack--loose">
        <Section title="Login"
                 action={<Button icon={Plus} loading={add.busy} onClick={() => void add.run()}>Add passkey</Button>}
                 more={[
                   { label: 'Add password', icon: LockKeyhole, onSelect: () => setAdding('password') },
                   { label: 'Add PIN', icon: Hash, onSelect: () => setAdding('pin') },
                 ]}>
          {creds.error && <InlineError message={creds.error} onRetry={creds.reload} />}
          {add.error && <InlineError compact message={add.error} />}
          {!creds.data && creds.loading && <LoadingRow />}
          <div className="rows">
            {list.map(c => (
              <ListRow key={c.id} icon={KIND[c.kind].icon} title={c.label || KIND[c.kind].word}
                       subtitle={`Created ${when(c.created_at)}`}
                       meta={c.this_session ? 'This session' : undefined} trailingAlways
                       trailing={<IconButton icon={Trash2} label="Delete" disabled={last} title={last ? 'The last one stays' : 'Delete'}
                                             onClick={() => setDeleting(c)} />} />
            ))}
          </div>
        </Section>

        <Section title="Recovery codes"
                 action={<Button onClick={() => setRenewing(true)}>Reset codes</Button>}>
          {codes && (
            <div className="recovery">
              <p className="src-line">Save these now. The app does not show them again.</p>
              <ol className="recovery__codes">{codes.map(c => <li key={c}>{c}</li>)}</ol>
              <div className="row-actions">
                <Button icon={Copy} onClick={() => void copy(codes.join('\n')).then(setCopied)}>
                  {copied ? 'Copied' : 'Copy'}
                </Button>
              </div>
            </div>
          )}
        </Section>

        <Tokens />
      </div>

      {adding && <AddSecret kind={adding} onClose={() => setAdding(null)} onAdded={() => { setAdding(null); creds.reload() }} />}

      {deleting && (
        <ConfirmDialog
          title={`Delete this ${KIND[deleting.kind].word.toLowerCase()}?`}
          body="The app logs out every browser that used it."
          confirmLabel="Delete" danger
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            const r = await securityApi.deleteCredential(deleting.id)
            if (r.logged_out) { location.replace('/'); return }
            setDeleting(null)
            creds.reload()
          }} />
      )}

      {renewing && (
        <ConfirmDialog title="Reset the recovery codes?" body="The old codes stop working."
                       confirmLabel="Reset codes"
                       onClose={() => setRenewing(false)}
                       onConfirm={async () => {
                         const r = await securityApi.newRecoveryCodes()
                         setCodes(r.recovery_codes)
                         setCopied(false)
                         setRenewing(false)
                       }} />
      )}
    </Page>
  )
}

/* a new password (at least 10 characters) or PIN (8 digits); the server's sentence on the field */
function AddSecret({ kind, onClose, onAdded }: { kind: 'password' | 'pin'; onClose: () => void; onAdded: () => void }) {
  const [secret, setSecret] = useState('')
  const save = useAction(async () => { await securityApi.addCredential(kind, secret); onAdded() })
  const ready = kind === 'pin' ? secret.length === PIN_LENGTH : secret.length >= 10
  const word = KIND[kind].word
  return (
    <Dialog title={`Add ${kind === 'pin' ? word : word.toLowerCase()}`} size="sm" onClose={onClose} dismissable={!save.busy}
            actions={<>
              <Button onClick={onClose} disabled={save.busy}>Cancel</Button>
              <Button variant="primary" loading={save.busy} disabled={!ready} onClick={() => void save.run()}>Add</Button>
            </>}>
      <form onSubmit={e => { e.preventDefault(); if (ready) void save.run() }}>
        <Field label={word} error={save.error} group={kind === 'pin'}>
          {kind === 'pin'
            ? <PinInput value={secret} onChange={setSecret} length={PIN_LENGTH} label={word} invalid={!!save.error} autoFocus />
            : <Input type="password" autoComplete="new-password" value={secret} invalid={!!save.error} autoFocus
                     onChange={e => setSecret(e.target.value)} />}
        </Field>
      </form>
    </Dialog>
  )
}

/* Named tokens: `Authorization: Bearer <token>` on /api/intercom only
   sends as system:<name> (docs/ARCHITECTURE.md). The token is shown once; revoking hides it. */
function Tokens() {
  const list = useResource(() => securityApi.tokens(), [])
  const [name, setName] = useState('')
  const [made, setMade] = useState<{ name: string; token: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const [revoking, setRevoking] = useState<NamedToken | null>(null)
  const make = useAction(async () => {
    setMade(await securityApi.makeToken(name.trim()))
    setCopied(false)
    setName('')
    list.reload()
  })
  return (
    <Section title="API tokens">
      {list.error && <InlineError message={list.error} onRetry={list.reload} />}
      {!list.data && list.loading && <LoadingRow />}
      <div className="rows">
        {(list.data?.tokens ?? []).map(t => (
          <div key={t.id} className="passkey-row">
            <div className="passkey-row__text">
              <span className="passkey-row__name">{t.name}</span>
              <span className="passkey-row__meta">Created {stamp(t.created_at)}</span>
            </div>
            <Button size="sm" onClick={() => setRevoking(t)}>Revoke</Button>
          </div>
        ))}
      </div>
      <form className="row-actions token-make" onSubmit={e => { e.preventDefault(); if (name.trim()) void make.run() }}>
        <Field label="Name" error={make.error}>
          <Input value={name} maxLength={40} spellCheck={false} placeholder="What it is for"
                 onChange={e => setName(e.target.value)} />
        </Field>
        <Button icon={Plus} type="submit" loading={make.busy} disabled={!name.trim()}>Create token</Button>
      </form>
      {made && (
        <div className="recovery">
          <p className="src-line">The token for {made.name}. Save it now. The app does not show it again.</p>
          <code className="recovery__codes token-once">{made.token}</code>
          <div className="row-actions">
            <Button icon={Copy} onClick={() => void copy(made.token).then(setCopied)}>
              {copied ? 'Copied' : 'Copy'}
            </Button>
          </div>
        </div>
      )}
      {revoking && (
        <ConfirmDialog title={`Revoke the token ${revoking.name}?`} body="Scripts that use it stop reaching the intercom."
                       confirmLabel="Revoke" danger
                       onClose={() => setRevoking(null)}
                       onConfirm={async () => {
                         await securityApi.revokeToken(revoking.id)
                         setRevoking(null)
                         list.reload()
                       }} />
      )}
    </Section>
  )
}
