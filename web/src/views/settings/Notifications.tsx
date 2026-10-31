/* Settings, Notifications: system notifications while the app's tab is hidden or not
   focused, and pushes while no page of it is open and focused (lib/notify.ts), saved on this
   device. The browser's permission on top: Allow notifications before it was asked, Blocked
   with Check again after a revoke; on plain http (no secure context) the browser has none,
   and the switches are off. Push: this device's switch, and every device that takes pushes
   (the server's list) with Remove. */
import { MonitorSmartphone, Trash2 } from 'lucide-react'
import { stamp } from '../../lib/format'
import {
  allow, checkAgain, KINDS, pushApi, setPrefs, subscribePush, unsubscribePush, useCanPush, useNotifyPrefs, usePermission,
  usePushEndpoint,
} from '../../lib/notify'
import { useAction } from '../../lib/useAction'
import { useResource } from '../../lib/useResource'
import { Button, Dot, IconButton, InlineError, ListRow, LoadingRow, Section, Switch } from '../../ui'
import { Page } from './parts'
import './settings.css'

export function Notifications() {
  const prefs = useNotifyPrefs()
  const perm = usePermission()
  const ask = useAction(allow)
  const devices = useResource(signal => pushApi.list(signal), [], { events: ev => ev.type === 'push.subscriptions' })
  const endpoint = usePushEndpoint()
  const canPush = useCanPush()
  const rows = devices.data ?? []
  const pushing = rows.some(d => d.endpoint === endpoint)
  const toggle = useAction((on: boolean) => (on ? subscribePush() : unsubscribePush(rows)))
  const remove = useAction((id: number) => pushApi.remove(id))
  const granted = perm === 'granted'
  const usable = perm !== 'insecure' && perm !== 'unsupported'
  return (
    <Page title="Notifications">
      <Section>
        {perm === 'insecure' && <p className="src-line">Notifications only work when the app opens over https.</p>}
        {perm === 'unsupported' && <p className="src-line">This browser does not support notifications.</p>}
        {perm === 'default' && (
          <div><Button variant="primary" loading={ask.busy} onClick={() => void ask.run()}>Allow notifications</Button></div>
        )}
        {perm === 'denied' && <>
          <div className="feature-line">
            <Dot tone="warn" text="Blocked" />
            <Button onClick={checkAgain}>Check again</Button>
          </div>
          <p className="src-line">Allow them in this site's settings in your browser.</p>
        </>}
        {granted && <Dot tone="ok" text="Allowed" />}
        <Switch label="Show notifications" checked={usable && prefs.on} disabled={!usable}
                onChange={on => (on && !granted ? void ask.run() : setPrefs({ on }))} />
        <Switch label="Push to this device" checked={usable && pushing} disabled={!usable || !canPush || toggle.busy || !devices.data}
                onChange={on => void toggle.run(on)} />
        {toggle.error && <InlineError compact message={toggle.error} />}
      </Section>
      <Section>
        {KINDS.map(k => (
          <Switch key={k.id} label={k.label} checked={usable && prefs[k.id]} disabled={!usable || !(prefs.on || pushing)}
                  onChange={v => setPrefs({ [k.id]: v })} />
        ))}
      </Section>
      {(rows.length > 0 || devices.error || !devices.data) && <Section title="Push">
        {devices.error && <InlineError message={devices.error} onRetry={devices.reload} />}
        {remove.error && <InlineError compact message={remove.error} />}
        {!devices.data && devices.loading && <LoadingRow />}
        <div className="rows">
          {rows.map(d => (
            <ListRow key={d.id} icon={MonitorSmartphone} title={d.device}
                     subtitle={`Created ${stamp(d.created_at)}`}
                     meta={d.endpoint === endpoint ? 'This device' : undefined} trailingAlways
                     trailing={<IconButton icon={Trash2} label="Remove" onClick={() => void remove.run(d.id)} />} />
          ))}
        </div>
      </Section>}
    </Page>
  )
}
