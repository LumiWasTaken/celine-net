/* Confirm access: adding a way to log in, new recovery codes and a new token need a proof
   from the last minutes (server/celine/auth/routes.py). An action wrapped in `guard` that
   the server answers with proof_needed opens this dialog (the login's own ways: a passkey,
   the password, the PIN, whichever exist) and, once proved, runs once more; Cancel leaves
   the server's sentence on the action. */
import { useCallback, useState, type ReactNode } from 'react'
import { Fingerprint } from 'lucide-react'
import { ApiError, authApi } from '../../lib/api'
import { PROOF_NEEDED, securityApi } from '../../lib/passkey'
import { useAction } from '../../lib/useAction'
import { useResource } from '../../lib/useResource'
import { Button, Dialog, Field, InlineError, Input, LoadingRow, PinInput, Segmented } from '../../ui'

const PIN_LENGTH = 8
const WORD = { password: 'Password', pin: 'PIN' } as const

export function useProof(): { guard: <T>(fn: () => Promise<T>) => Promise<T>; dialog: ReactNode } {
  const [asking, setAsking] = useState<((proved: boolean) => void) | null>(null)
  const guard = useCallback(async <T,>(fn: () => Promise<T>): Promise<T> => {
    try {
      return await fn()
    } catch (e) {
      if (!(e instanceof ApiError && e.code === PROOF_NEEDED)) throw e
      const proved = await new Promise<boolean>(done => setAsking(() => done))
      setAsking(null)
      if (!proved) throw e
      return await fn()
    }
  }, [])
  return { guard, dialog: asking && <Prove onDone={asking} /> }
}

function Prove({ onDone }: { onDone: (proved: boolean) => void }) {
  const status = useResource(() => authApi.status(), [])
  const methods = status.data?.methods ?? []
  const typed = methods.filter((m): m is 'password' | 'pin' => m !== 'passkey')
  const [picked, setPicked] = useState<'password' | 'pin' | null>(null)
  const kind = picked ?? typed[0] ?? null
  const [secret, setSecret] = useState('')
  const passkey = useAction(async () => { await securityApi.proveWithPasskey(); onDone(true) })
  const send = useAction(async () => {
    try { await securityApi.prove(kind!, secret) } catch (e) { if (kind === 'pin') setSecret(''); throw e }
    onDone(true)
  })
  const busy = passkey.busy || send.busy
  const ready = kind === 'pin' ? secret.length === PIN_LENGTH : secret.length > 0
  return (
    <Dialog title="Confirm access" size="sm" onClose={() => onDone(false)} dismissable={!busy}
            actions={<>
              <Button onClick={() => onDone(false)} disabled={busy}>Cancel</Button>
              {kind && <Button variant="primary" loading={send.busy} disabled={!ready || passkey.busy} onClick={() => void send.run()}>Confirm</Button>}
            </>}>
      {status.error && <InlineError message={status.error} onRetry={status.reload} />}
      {!status.data && status.loading && <LoadingRow />}
      {methods.includes('passkey') && (
        <Button icon={Fingerprint} loading={passkey.busy} disabled={send.busy} onClick={() => void passkey.run()}>Use passkey</Button>
      )}
      {passkey.error && <InlineError compact message={passkey.error} />}
      {typed.length > 1 && (
        <Segmented label="Login" value={kind!} onChange={k => { setPicked(k); setSecret(''); send.clear() }}
                   options={typed.map(k => ({ value: k, label: WORD[k] }))} />
      )}
      {kind && (
        <form onSubmit={e => { e.preventDefault(); if (ready) void send.run() }}>
          <Field label={WORD[kind]} error={send.error} group={kind === 'pin'}>
            {kind === 'pin'
              ? <PinInput key="pin" value={secret} onChange={setSecret} length={PIN_LENGTH} label={WORD[kind]} invalid={!!send.error} autoFocus />
              : <Input key="password" type="password" autoComplete="current-password" value={secret} invalid={!!send.error} autoFocus
                       onChange={e => setSecret(e.target.value)} />}
          </Field>
        </form>
      )}
    </Dialog>
  )
}
