/* The connection parts of the Discord and WhatsApp pages: the connection's state
   (GET /api/sources/{sid}/connector, fresh on source.state events and every few seconds
   while a login is in progress) and, for a connection with a chat it writes to
   (WhatsApp's "Message yourself"), the bridged chat: the marker its messages start with
   and this app's switch. */
import { useEffect, useState } from 'react'
import { useName } from '../../lib/name'
import { kbApi, type BridgeState, type ConnectorState } from '../../lib/kb'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { useAction } from '../../lib/useAction'
import { useResource, type Resource } from '../../lib/useResource'
import { Field, InlineError, Input, Section, Switch } from '../../ui'
import './settings.css'

/** the connection's state; while it waits for a login it looks again every 3 seconds
    (a new QR code, a scan) */
export function useConnector(kid: string, active: boolean): Resource<ConnectorState> {
  const r = useResource(() => kbApi.connector(kid), [kid, active], {
    events: ev => (ev.type === 'source.state' || ev.type === 'kb.changed') && ev.kb === kid,
  })
  const waiting = active && r.data?.connection.state === 'needs_login'
  const { reload } = r
  useEffect(() => {
    if (!waiting) return
    const t = setInterval(reload, 3000)
    return () => clearInterval(t)
  }, [waiting, reload])
  return r
}

export function BridgeSection({ kid, b, onChanged }: {
  kid: string
  b: BridgeState
  onChanged: (s: ConnectorState) => void
}) {
  const online = useOnline()
  const toggle = useAction(async (on: boolean) => onChanged(await kbApi.setBridge(kid, { on })))
  const name = useName()

  return (
    <Section title="Bridged chat">
      <Marker kid={kid} saved={b.marker ?? ''} onChanged={onChanged} />
      <Switch label={`${name} answers in your ${b.chat_name ?? 'bridged'} chat`.trim()} checked={b.on}
              disabled={!online || toggle.busy || (!b.on && !b.chat)}
              title={!online ? OFFLINE_TITLE : undefined}
              onChange={on => void toggle.run(on)} />
      {toggle.error && <InlineError compact message={toggle.error} />}
      {b.on && b.error && <InlineError compact message={b.error} />}
    </Section>
  )
}

/* ── the marker its messages start with ── */
function Marker({ kid, saved, onChanged }: { kid: string; saved: string; onChanged: (s: ConnectorState) => void }) {
  const online = useOnline()
  const name = useName()
  const [draft, setDraft] = useState(saved)
  useEffect(() => setDraft(saved), [saved])
  const save = useAction(async () => onChanged(await kbApi.setBridge(kid, { marker: draft })))
  // an empty marker would make its messages look like yours: it goes back to the saved one
  const commit = () => { if (!draft.trim()) setDraft(saved); else if (draft !== saved) void save.run() }
  return (
    <Field label="Replies start with" error={save.error}>
      <Input value={draft} maxLength={40} disabled={!online || save.busy} placeholder={name ? `${name}:` : undefined}
             onChange={e => setDraft(e.target.value)} onBlur={commit}
             onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); commit() } }} />
    </Field>
  )
}
