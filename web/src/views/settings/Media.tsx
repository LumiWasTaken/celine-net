/* Images & Music (/settings/media), listed while the Images or the Music feature is on:
   the ComfyUI server (its Address, saved when the field is left; its key; its options,
   each saved on change; Check provider; Remove this provider), then the presets of each
   kind that is on (Presets.tsx). The one place ComfyUI is set up in Settings: the Models
   page leaves it out. Every state comes from the server (the provider's, each feature's
   probe). API: lib/settingsApi.ts, lib/media.ts. */
import { useEffect, useMemo, useState } from 'react'
import { RefreshCw, Trash2 } from 'lucide-react'
import { errorText } from '../../lib/format'
import { useFeatureOn } from '../../lib/features'
import { useOnline } from '../../lib/socket'
import { SETTINGS_EVENTS, settingsApi, type ProviderView } from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useResource } from '../../lib/useResource'
import { kindInfo } from '../../components/ProviderFields'
import { Button, ConfirmDialog, Dot, Field, Form, InlineError, Input, Loaded, Section } from '../../ui'
import { KeySection, optionSchema, providerProblem, reach } from './Models'
import { Page } from './parts'
import { Presets } from './Presets'
import './settings.css'

export function Media() {
  const online = useOnline()
  const providers = useResource(() => settingsApi.providers(), [], { events: SETTINGS_EVENTS })
  const roles = useResource(() => settingsApi.roles().then(r => r.roles), [], { events: SETTINGS_EVENTS })
  const imagesOn = useFeatureOn('images')
  const musicOn = useFeatureOn('music')
  const [removing, setRemoving] = useState(false)
  const reload = () => { providers.reload(); roles.reload() }
  const list = providers.data?.providers ?? []
  const comfy = list.find(p => p.provider.kind === 'comfyui')
  const check = useAction(async () => { if (comfy) await settingsApi.test(comfy.id); reload() })
  const st = comfy && reach(comfy)
  const problem = comfy && providerProblem(comfy)

  return (
    <Page title="Images & Music" detail={st && <Dot tone={st.tone} text={st.word} />}
          actions={comfy && <Button icon={RefreshCw} loading={check.busy} disabled={!online} onClick={() => void check.run()}>Check provider</Button>}
          more={comfy ? [{ label: 'Remove this provider', icon: Trash2, danger: true, disabled: comfy.used_by.length > 0 || !online,
                           title: comfy.used_by.length ? `Used for ${comfy.used_by.join(', ')}` : undefined, onSelect: () => setRemoving(true) }] : undefined}>
      {!providers.data ? <Section><Loaded res={providers} /></Section> : <>
        {(problem || check.error) && <InlineError message={check.error ?? problem} />}
        <Comfy p={comfy} others={list} kinds={providers.data.kinds} onChanged={reload} />
        {comfy && (kindInfo(providers.data.kinds, 'comfyui')?.key === 'required' || comfy.key.state === 'set') && (
          <KeySection p={comfy} required={kindInfo(providers.data.kinds, 'comfyui')?.key === 'required'} onChanged={reload} />
        )}
        {roles.data ? <>
          {imagesOn && <Presets kind="images" providers={list} roles={roles.data} onChanged={reload} />}
          {musicOn && <Presets kind="music" providers={list} roles={roles.data} onChanged={reload} />}
        </> : <Section><Loaded res={roles} /></Section>}
      </>}
      {removing && comfy && (
        <ConfirmDialog title={`Remove ${comfy.provider.label}?`} danger confirmLabel="Remove"
                       body="Its models are removed from every role, and its saved key is deleted."
                       onClose={() => setRemoving(false)}
                       onConfirm={async () => {
                         try { await settingsApi.deleteProvider(comfy.id); setRemoving(false); reload() }
                         catch (e) { return errorText(e) }
                       }} />
      )}
    </Page>
  )
}

/* ── the ComfyUI server: its Address (saved when the field is left or on Enter: the saved
   provider's, else a new one the server fills with every preset) and its options ── */
function Comfy({ p, others, kinds, onChanged }: {
  p: ProviderView | undefined; others: ProviderView[]; kinds: Parameters<typeof kindInfo>[0]; onChanged: () => void
}) {
  const online = useOnline()
  const saved = p?.provider.base_url ?? ''
  const [url, setUrl] = useState(saved)
  useEffect(() => { setUrl(saved) }, [saved])
  const save = useAction(async () => {
    const next = url.trim()
    if (!next || next === saved) return
    const v = await settingsApi.putComfyui(next, p)
    await settingsApi.test(v.id)
    onChanged()
  })
  const saveOpt = useAction(async (key: string, value: unknown) => {
    if (!p) return
    await settingsApi.putProvider(p.id, { ...p.provider, options: { ...(p.provider.options ?? {}), [key]: value } })
    onChanged()
  })
  const schema = useMemo(() => optionSchema(kindInfo(kinds, 'comfyui')?.options ?? [],
                                            others.filter(x => x.provider.kind === 'llama-swap' || x.provider.kind === 'tabby')),
                         [kinds, others])
  return (
    <Section title="ComfyUI">
      <Field label="Address" error={save.error}>
        <Input value={url} placeholder="http://192.168.1.20:8188" spellCheck={false} disabled={!online || save.busy}
               onChange={e => setUrl(e.target.value)} onBlur={() => void save.run()}
               onKeyDown={e => { if (e.key === 'Enter') void save.run(); if (e.key === 'Escape') setUrl(saved) }} />
      </Field>
      {p && schema.length > 0 && (
        <Form fields={schema} values={p.provider.options ?? {}} disabled={saveOpt.busy || !online}
              onChange={(k, v) => void saveOpt.run(k, v)} />
      )}
      {saveOpt.error && <InlineError compact message={saveOpt.error} />}
    </Section>
  )
}
