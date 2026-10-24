/* The vault is locked: a banner across the top, Unlock opens Settings, Vault; the X puts
   it away on this device for a day. */
import { LockKeyhole } from 'lucide-react'
import { Notice } from '../ui'
import { useNav } from '../lib/nav'
import { usePref } from '../lib/prefs'

const DAY = 24 * 60 * 60 * 1000

export function VaultBanner({ locked }: { locked: boolean }) {
  const nav = useNav()
  const [until, setUntil] = usePref('vault-banner-until', 0)
  if (!locked || Date.now() < until) return null
  return (
    <Notice tone="warn" icon={LockKeyhole} className="vault-banner" title="Vault locked"
            action={{ label: 'Unlock', onClick: () => nav.go({ section: 'settings', page: 'vault' }) }}
            onDismiss={() => setUntil(Date.now() + DAY)} dismissLabel="Dismiss for a day" />
  )
}
