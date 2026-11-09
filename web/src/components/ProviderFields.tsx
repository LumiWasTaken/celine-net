/* The fields of a model provider, the same in Setup's Connect a model step and in Settings,
   Models (Add provider, a provider's edit dialog): Type first (Auto-detect, then every kind
   the server lists but ComfyUI), Address, Cloud (where the caller passes `cloud`; Use the
   default beside it while the user set it), API key,
   Name. A hosted kind (one address of its own: OpenRouter, Anthropic) fills the address and
   needs a key; a local kind picked after it clears that address; Auto-detect leaves the
   address alone. The user runs Check (its own logic per place): with Auto-detect the
   server's answer picks the type and the address. */
import { RotateCcw } from 'lucide-react'
import { Choice, Field, IconButton, Input, Switch } from '../ui'
import type { KindInfo, ProviderKind } from '../lib/settingsApi'

export type ProviderDraft = { kind: ProviderKind | ''; url: string; key: string; label: string }
export const EMPTY_PROVIDER: ProviderDraft = { kind: '', url: '', key: '', label: '' }
const ORDER: ProviderKind[] = ['ollama', 'llama-server', 'llama-swap', 'tabby', 'openai', 'openrouter', 'anthropic']

export const host = (url: string) => { try { return new URL(url).host } catch { return url } }
export const kindInfo = (kinds: KindInfo[], kind: ProviderKind | '' | null) => kinds.find(k => k.kind === kind)
/** Check may run: an address, and a key when the kind needs one */
export const ready = (kinds: KindInfo[], d: ProviderDraft) =>
  !!d.url.trim() && (kindInfo(kinds, d.kind)?.key !== 'required' || !!d.key.trim())

/** the Cloud switch: what the server said (ProviderView.cloud, a check's `cloud`) or the user
    set; 'mixed' while the server has not said for this address. `onReset`, given while the
    user set it: Use the default, back to what the address says */
export type CloudSwitch = { checked: boolean | 'mixed'; help: string; onChange: (v: boolean) => void; onReset?: () => void }

export function ProviderFields({ kinds, value: d, onChange, cloud, withKey = true, autoFocus = false }: {
  kinds: KindInfo[]
  value: ProviderDraft
  onChange: (d: ProviderDraft) => void
  /** the Cloud switch under Address */
  cloud?: CloudSwitch
  /** the key field; off where the key has its own section */
  withKey?: boolean
  autoFocus?: boolean
}) {
  const info = kindInfo(kinds, d.kind)
  const hosted = !!info?.default_url
  const pick = (kind: ProviderKind | '') => {
    const to = kindInfo(kinds, kind)
    const url = to?.default_url ?? (kind && info?.default_url && d.url.trim() === info.default_url ? '' : d.url)
    onChange({ ...d, kind, url })
  }
  const typed = d.url.trim() ? host(d.url.includes('://') ? d.url.trim() : `http://${d.url.trim()}`) : ''
  return (
    <>
      <Field label="Type" group>
        <Choice list label="Type" value={d.kind} onChange={pick}
                options={[{ value: '' as const, label: 'Auto-detect' },
                          ...ORDER.flatMap(k => { const i = kindInfo(kinds, k); return i ? [{ value: i.kind, label: i.label }] : [] })]} />
      </Field>
      <Field label="Address">
        <Input value={d.url} placeholder="http://192.168.1.20:11434" spellCheck={false} autoFocus={autoFocus}
               onChange={e => onChange({ ...d, url: e.target.value })} />
      </Field>
      {cloud && (
        <span className="form__control">
          <Switch label="Cloud" help={cloud.help} checked={cloud.checked} onChange={cloud.onChange} />
          {cloud.onReset && <IconButton icon={RotateCcw} size="sm" label="Use the default" onClick={cloud.onReset} />}
        </span>
      )}
      {withKey && (
        <Field label={info?.key === 'required' ? 'API key' : 'API key (optional)'}>
          <Input type="password" autoComplete="off" value={d.key} placeholder={hosted ? 'sk-...' : 'Key'}
                 onChange={e => onChange({ ...d, key: e.target.value })} />
        </Field>
      )}
      <Field label="Name">
        <Input value={d.label} placeholder={hosted ? info?.label ?? '' : typed || 'Home server'}
               onChange={e => onChange({ ...d, label: e.target.value })} />
      </Field>
    </>
  )
}
