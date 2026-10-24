/* Settings, Notifications: system notifications while the app's tab is hidden or not
   focused (lib/notify.ts), saved on this device. The browser's permission on top: Allow
   notifications before it was asked, Blocked with Check again after a revoke; on plain
   http (no secure context) the browser has none, and the switches are off. */
import { allow, checkAgain, KINDS, setPrefs, useNotifyPrefs, usePermission } from '../../lib/notify'
import { useAction } from '../../lib/useAction'
import { Button, Dot, Section, Switch } from '../../ui'
import { Page } from './parts'
import './settings.css'

export function Notifications() {
  const prefs = useNotifyPrefs()
  const perm = usePermission()
  const ask = useAction(allow)
  const granted = perm === 'granted'
  const usable = perm !== 'insecure' && perm !== 'unsupported'
  return (
    <Page title="Notifications">
      <Section>
        {perm === 'insecure' && <p className="src-line">Notifications need https.</p>}
        {perm === 'unsupported' && <p className="src-line">This browser has no notifications.</p>}
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
      </Section>
      <Section>
        {KINDS.map(k => (
          <Switch key={k.id} label={k.label} checked={usable && prefs[k.id]} disabled={!usable || !prefs.on}
                  onChange={v => setPrefs({ [k.id]: v })} />
        ))}
      </Section>
    </Page>
  )
}
