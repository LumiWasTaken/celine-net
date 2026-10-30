/* Images and music on Images & Music (Media.tsx): one card per preset (comfyui/presets.py,
   GET /api/generation-presets?kind=), shipped and the user's own. The presets it uses come
   first, in its order, which up and down change (the Image generation or Music generation
   role; the first of a task is its default); its switch takes a preset in or out. A card
   opens in place: its editor (the server's fields, each saved on its own change: a text
   when it is left) with Replace workflow, Download workflow and Reset to defaults (an
   edited shipped one) or Remove (the user's own), and a form with one field per parameter
   that makes a batch (POST /api/batches). Upload workflow (the head) makes a new preset
   from a workflow in API format with placeholders; the server says what is missing.
   Below the cards, the newest batches, yours and its own, each a gallery following its
   events on the global feed. */
import { useState } from 'react'
import { ArrowDown, ArrowUp, Download, FileUp, Play, Star, Trash2, Undo2, Upload } from 'lucide-react'
import { uploadAttachment } from '../../lib/api'
import { useServerEvents } from '../../lib/bus'
import { clock, errorText } from '../../lib/format'
import { batchEvent, fileUrl, mediaApi, ROLE, tileUrl, workflowUrl, type Batch, type Kind, type Param, type Preset } from '../../lib/media'
import { useOnline } from '../../lib/socket'
import { SETTINGS_EVENTS, STATE_TONE, settingsApi, type Cap, type ProviderView, type RoleView } from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useResource } from '../../lib/useResource'
import { BatchGallery, StopBatch, doneWords, madeImages, type Made } from '../../components/Batch'
import {
  Button, Card, CardGrid, Choice, ConfirmDialog, Dialog, Dot, Field, FileButton, Form, Picks, IconButton, InlineError, Input, ListRow, LoadingRow, NumberField, Player, Section, Step, SubHead, Switch, TextArea,
} from '../../ui'
import './settings.css'

const TASK: Record<Preset['task'], string> = { generate: 'new images', edit: 'edits', music: 'music' }

export function Presets({ kind, providers, roles, onChanged }: {
  kind: Kind; providers: ProviderView[]; roles: Record<string, RoleView>; onChanged: () => void
}) {
  const online = useOnline()
  const list = useResource(() => mediaApi.presets(kind), [kind], { events: SETTINGS_EVENTS })
  const probe = useResource(() => settingsApi.probeFeature(kind), [kind], { events: SETTINGS_EVENTS })
  const recent = useRecent(kind)
  const [open, setOpen] = useState<string | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const role = ROLE[kind]
  const chain = roles[role]?.chain ?? []
  const save = useAction(async (next: string[]) => { await settingsApi.putRole(role, next); onChanged(); list.reload() })
  // note: the first ComfyUI provider that has the preset hosts it, else the first one
  const comfy = providers.filter(p => p.provider.kind === 'comfyui')
  const use = useAction(async (p: Preset) => {
    const host = comfy.find(x => p.id in x.provider.models) ?? comfy[0]!
    if (!(p.id in host.provider.models)) await settingsApi.putModel(host.id, p.id, { label: p.label, caps: [role as Cap] })
    await save.run([...chain, `${host.id}/${p.id}`])
  })
  const swap = (i: number) => void save.run([...chain.slice(0, i), chain[i + 1]!, chain[i]!, ...chain.slice(i + 2)])
  const presets = [...(list.data?.presets ?? [])]
    .sort((a, b) => (a.order ?? Infinity) - (b.order ?? Infinity) || a.label.localeCompare(b.label))
  const busy = !online || save.busy || use.busy
  const shownPreset = presets.find(p => p.id === open)
  const changed = (p: Preset) => { list.set(d => d && { presets: d.presets.map(x => (x.id === p.id ? p : x)) }); onChanged() }

  return (
    <Section title={kind === 'images' ? 'Images' : 'Music'}
             action={<FileButton icon={Upload} accept=".json,application/json" disabled={!online} onFiles={([f]) => { if (f) setFile(f) }}>Upload workflow</FileButton>}>
      {probe.data && probe.data.state !== 'none' && <Dot tone={STATE_TONE[probe.data.state] ?? 'off'} text={probe.data.detail} />}
      {list.error && !list.data ? <InlineError message={list.error} onRetry={list.reload} />
        : !list.data ? <LoadingRow label="Loading presets" /> : (
        <CardGrid className="presets">
          {presets.map(p => {
            const at = p.ref ? chain.indexOf(p.ref) : -1
            const facts = [p.family, TASK[p.task], p.size && `${p.size.width}x${p.size.height}`].filter(Boolean).join(', ')
            return (
              <Card key={p.id} title={p.label} subtitle={facts} selected={open === p.id}
                    meta={at >= 0 ? <span title={`Used ${at === 0 ? 'first' : `as number ${at + 1}`}`}><Step n={at + 1} /></span> : undefined}
                    onClick={() => setOpen(o => (o === p.id ? null : p.id))}
                    menu={[
                      ...(at >= 0 ? [
                        { label: 'Make default', icon: Star, disabled: busy || at === 0, onSelect: () => void save.run([p.ref!, ...chain.filter(r => r !== p.ref)]) },
                        { label: 'Move up', icon: ArrowUp, disabled: busy || at === 0, onSelect: () => swap(at - 1) },
                        { label: 'Move down', icon: ArrowDown, disabled: busy || at === chain.length - 1, onSelect: () => swap(at) },
                        'sep' as const,
                      ] : []),
                      { label: 'Download workflow', icon: Download, onSelect: () => { window.location.href = workflowUrl(p.id) } },
                    ]}
                    actions={<Switch label="On" checked={at >= 0} disabled={busy || (at < 0 && comfy.length === 0)}
                                     onChange={on => void (on ? use.run(p) : save.run(chain.filter(r => r !== p.ref)))} />}>
                {p.good_at && <span className="preset__about">{p.good_at}</span>}
              </Card>
            )
          })}
        </CardGrid>
      )}
      {shownPreset && (
        <div className="preset-open">
          <Editor key={shownPreset.id} preset={shownPreset} onChanged={changed}
                  onRemoved={() => { setOpen(null); list.reload(); onChanged() }} />
          <Generate key={`go:${shownPreset.id}`} preset={shownPreset} sources={(recent.data?.batches ?? []).flatMap(madeImages)}
                    onStarted={recent.add} />
        </div>
      )}
      {(save.error || use.error) && <InlineError compact message={save.error ?? use.error} />}
      <Recent kind={kind} batches={recent.data?.batches} error={recent.error} reload={recent.reload} />
      {file && <UploadDialog kind={kind} file={file} onClose={() => setFile(null)}
                             onAdded={p => { setOpen(p.id); list.reload(); onChanged() }} />}
    </Section>
  )
}

/* ── one preset's settings: the server's fields, each saved on its change; its workflow
   replaced or downloaded; an edited shipped one reset, an own one removed ── */
function Editor({ preset: p, onChanged, onRemoved }: { preset: Preset; onChanged: (p: Preset) => void; onRemoved: () => void }) {
  const online = useOnline()
  const [removing, setRemoving] = useState(false)
  const edit = useAction(async (key: string, value: unknown) => onChanged(await mediaApi.edit(p.id, { ...p.values, [key]: value })))
  const replace = useAction(async (f: File) => onChanged(await mediaApi.replaceWorkflow(p.id, await f.text())))
  const reset = useAction(async () => onChanged(await mediaApi.reset(p.id)))
  const error = edit.error ?? replace.error ?? reset.error
  return (
    <div className="preset-open__edit">
      <Form fields={p.fields} values={p.values} disabled={!online || edit.busy} onChange={(k, v) => void edit.run(k, v)} />
      {error && <InlineError compact message={error} />}
      <div className="preset-open__actions">
        <FileButton icon={FileUp} accept=".json,application/json" disabled={!online || replace.busy}
                    onFiles={([f]) => { if (f) void replace.run(f) }}>Replace workflow</FileButton>
        <Button icon={Download} href={workflowUrl(p.id)}>Download workflow</Button>
        {p.origin === 'edited' && <Button icon={Undo2} loading={reset.busy} disabled={!online} onClick={() => void reset.run()}>Reset to defaults</Button>}
        {p.origin === 'own' && <Button icon={Trash2} disabled={!online} onClick={() => setRemoving(true)}>Remove</Button>}
      </div>
      {removing && (
        <ConfirmDialog title={`Remove ${p.label}?`} danger confirmLabel="Remove" body="Its workflow is deleted."
                       onClose={() => setRemoving(false)}
                       onConfirm={async () => {
                         try { await mediaApi.remove(p.id); setRemoving(false); onRemoved() }
                         catch (e) { return errorText(e) }
                       }} />
      )}
    </div>
  )
}

/* ── a workflow file the user picked: its name, then Upload; the server checks its
   placeholders and says each one that is missing ── */
function UploadDialog({ kind, file, onClose, onAdded }: { kind: Kind; file: File; onClose: () => void; onAdded: (p: Preset) => void }) {
  const online = useOnline()
  const [label, setLabel] = useState(file.name.replace(/\.json$/i, ''))
  const go = useAction(async () => { const p = await mediaApi.upload(kind, label.trim(), await file.text()); onAdded(p); onClose() })
  const ok = online && !!label.trim()
  return (
    <Dialog title="Upload workflow" onClose={onClose} size="sm" dismissable={!go.busy}
            actions={<>
              <Button variant="secondary" onClick={onClose} disabled={go.busy}>Cancel</Button>
              <Button variant="primary" loading={go.busy} disabled={!ok} onClick={() => void go.run()}>Upload</Button>
            </>}>
      <form className="dialog__form" onSubmit={e => { e.preventDefault(); if (ok) void go.run() }}>
        <Field label="Name">
          <Input value={label} autoFocus onChange={e => setLabel(e.target.value)} />
        </Field>
        {go.error && <InlineError compact message={go.error} />}
      </form>
    </Dialog>
  )
}

/* ── the newest batches of a kind, kept as the server says them ── */
function useRecent(kind: Kind) {
  const res = useResource(() => mediaApi.batches(kind), [kind], { events: ev => ev.type === 'open' })
  const add = (b: Batch) => res.set(d => {
    const list = d?.batches ?? []
    return { batches: list.some(x => x.id === b.id) ? list.map(x => (x.id === b.id ? b : x)) : [b, ...list] }
  })
  useServerEvents(ev => { const b = batchEvent(ev, kind); if (b) add(b) })
  return { ...res, add }
}

/* the newest few, then Show more: the page stays a settings page, not a gallery */
const STEP = { images: 4, music: 8 } as const

function Recent({ kind, batches, error, reload }: { kind: Kind; batches?: Batch[]; error: string | null; reload: () => void }) {
  const [playing, setPlaying] = useState<{ src: string; title: string; detail: string } | null>(null)
  const [limit, setLimit] = useState<number>(STEP[kind])
  if (error && !batches) return <InlineError compact message={error} onRetry={reload} />
  if (!batches?.length) return null
  const more = (total: number) => total > limit && (
    <div><Button onClick={() => setLimit(l => l + STEP[kind])}>Show more</Button></div>
  )
  const link = (b: Batch) => b.link ?? undefined
  if (kind === 'music') {
    // a row per track: its title (or what it was asked for), the style, its time; Play opens the player
    const rows = batches.flatMap(b => b.items.map(i => ({ b, i })))
    return (
      <div className="batches">
        <SubHead>Recent</SubHead>
        <div className="rows">
          {rows.slice(0, limit).map(({ b, i }) => {
            const time = clock(Date.parse(b.created_at))
            const done = i.state === 'done' && i.attachment
            const title = done ? i.title ?? i.attachment!.filename : i.style ?? ''
            const words = i.state === 'failed' ? i.error : i.state === 'stopped' ? 'Stopped' : i.state === 'done' ? i.style : i.note
            return (
              <ListRow key={`${b.id}:${i.index}`} density="lg" href={link(b)} title={title} subtitle={done ? i.style : words} meta={time}
                       tooltip={i.style} trailingAlways
                       trailing={done
                         ? <IconButton icon={Play} size="sm" label={`Play ${title}`}
                                       onClick={() => setPlaying({ src: fileUrl(i.attachment!), title, detail: time })} />
                         : <StopBatch batch={b} />} />
            )
          })}
        </div>
        {more(rows.length)}
        {playing && <Player key={playing.src} src={playing.src} title={playing.title} detail={playing.detail} onClose={() => setPlaying(null)} />}
      </div>
    )
  }
  return (
    <div className="batches">
      <SubHead>Recent</SubHead>
      <CardGrid className="batches__grid">
      {batches.slice(0, limit).map(b => {
        const first = b.items[0]
        const what = first?.prompt ?? first?.style ?? ''
        const time = clock(Date.parse(b.created_at))
        const href = link(b)
        return (
          <div key={b.id} className="card batches__one">
            <div className="batches__head">
              {href ? <a className="batches__time" href={href}>{time}</a> : <span className="batches__time">{time}</span>}
              <span className="batches__what" title={what}>{what}</span>
              {b.items.length > 1 && <span className={`batches__count batches__count--${b.state}`}>{doneWords(b)}</span>}
              <StopBatch batch={b} />
            </div>
            <BatchGallery batch={b} />
          </div>
        )
      })}
      </CardGrid>
      {more(batches.length)}
    </div>
  )
}

/* ── the form: one field per parameter; empty is the preset's own value ── */
type Values = Record<string, string | boolean>
const PICK = 112      // a picture choice's width (.choice-tiles--picture: 7rem)
const NUMBER = new Set<Param['type']>(['integer', 'number'])

function initial(p: Preset): Values {
  const v: Values = {}
  for (const prm of p.parameters) {
    if (prm.type === 'boolean') v[prm.name] = prm.default === true
    else if (prm.type === 'choice') v[prm.name] = String(prm.default ?? prm.choices?.[0] ?? '')
    else v[prm.name] = ''
  }
  return v
}

function Generate({ preset: p, sources, onStarted }: { preset: Preset; sources: Made[]; onStarted: (b: Batch) => void }) {
  const online = useOnline()
  const [values, setValues] = useState<Values>(() => initial(p))
  const [count, setCount] = useState('')
  const [loras, setLoras] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [uploaded, setUploaded] = useState<Made[]>([])
  const set = (k: string, v: string | boolean) => setValues(x => ({ ...x, [k]: v }))
  const shown = p.parameters.filter(prm => prm.name !== 'model' || p.catalog?.models)
  const main = p.kind === 'images' ? 'prompt' : 'style'
  const ready = String(values[main] ?? '').trim() !== '' && (p.task !== 'edit' || picked.length > 0)

  const go = useAction(async () => {
    const item: Record<string, unknown> = p.kind === 'images' ? { preset: p.id } : {}
    for (const prm of shown) {
      const v = values[prm.name]
      if (v === '' || v === undefined) continue
      item[prm.name] = prm.type === 'integer' ? parseInt(String(v), 10) : prm.type === 'number' ? Number(v) : v
    }
    picked.forEach((id, n) => { item[n ? `source${n + 1}` : 'source'] = id })
    const l = loras.split(',').map(s => s.trim()).filter(Boolean)
    if (l.length) item.loras = l
    onStarted(await mediaApi.start(p.kind, [item], parseInt(count, 10) || 1))
  })
  const upload = useAction(async (f: File) => {
    const a = await uploadAttachment(f)
    setUploaded(u => [{ id: a.id, sha256: a.sha256, filename: a.filename, width: 0, height: 0 }, ...u])
    setPicked(s => (s.length < (p.images ?? 1) ? [...s, a.id] : [a.id]))
  })

  const field = (prm: Param) => <ParamField key={prm.name} prm={prm} value={values[prm.name]!} onChange={v => set(prm.name, v)} />
  return (
    <form className="generate" noValidate onSubmit={e => { e.preventDefault(); if (ready) void go.run() }}>
      {p.task === 'edit' && (
        <Field label="Image" error={upload.error} group>
          <div className="generate__sources">
            <FileButton icon={Upload} label="Upload" accept="image/*" disabled={upload.busy}
                        onFiles={([f]) => { if (f) void upload.run(f) }} />
            <Picks label="Image" values={picked} onChange={setPicked} max={p.images ?? 1}
                   options={[...uploaded, ...sources].map(img => ({ value: img.id, label: img.filename, picture: tileUrl(img, PICK) }))} />
          </div>
        </Field>
      )}
      {shown.filter(prm => !NUMBER.has(prm.type)).map(field)}
      {p.catalog && p.catalog.max_loras > 0 && (
        <Field label="LoRAs">
          <Input mono spellCheck={false} value={loras} onChange={e => setLoras(e.target.value)} placeholder="name:0.8, name" />
        </Field>
      )}
      <div className="generate__numbers">
        {shown.filter(prm => NUMBER.has(prm.type)).map(field)}
        <NumberField label="How many" min={1} value={count} onChange={setCount} />
      </div>
      <div className="generate__go">
        <Button type="submit" variant="primary" loading={go.busy} disabled={!online || !ready}>Generate</Button>
      </div>
      {go.error && <InlineError compact message={go.error} />}
    </form>
  )
}

function ParamField({ prm, value, onChange }: { prm: Param; value: string | boolean; onChange: (v: string | boolean) => void }) {
  if (prm.type === 'boolean') return <Switch label={prm.label} checked={value === true} onChange={onChange} />
  if (prm.type === 'choice') {
    return (
      <Field label={prm.label} group>
        <Choice label={prm.label} value={String(value)} onChange={onChange}
                options={(prm.choices ?? []).map(c => ({ value: c, label: c }))} />
      </Field>
    )
  }
  if (prm.type === 'integer' || prm.type === 'number') {
    return (
      <NumberField label={prm.label} min={prm.min} max={prm.max} step={prm.step ?? (prm.type === 'integer' ? 1 : undefined)}
                   value={String(value)} onChange={onChange} />
    )
  }
  const long = prm.name === 'prompt' || prm.name === 'style' || prm.name === 'lyrics' || prm.name === 'negative'
  return (
    <Field label={prm.label}>
      {long
        ? <TextArea rows={prm.name === 'prompt' || prm.name === 'style' ? 4 : 3} value={String(value)}
                    onChange={e => onChange(e.target.value)} />
        : <Input mono={prm.name === 'model'} spellCheck={prm.name !== 'model'} value={String(value)}
                 placeholder={prm.default != null ? String(prm.default) : ''} onChange={e => onChange(e.target.value)} />}
    </Field>
  )
}
