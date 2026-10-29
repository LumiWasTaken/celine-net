/* Choose a model: one dialog, used for a role's list (Settings, Models) and for one
   conversation (its side panel). Grouped by provider, in the providers' order. Only
   models that can do the role's work are listed, and none of a provider that still needs its key.
   A llama-swap model says whether it is loaded now, or that it loads first and about how
   long the last load took (lib/loaded). A provider with a problem says it under its name. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Check } from 'lucide-react'
import type { Cap, ProviderModel, ProviderView } from '../lib/settingsApi'
import { useLoaded, type Loaded } from '../lib/loaded'
import { Dialog, EmptyState, ICON, ListRow, ListSection, SearchField, VirtualList, type VirtualListHandle } from '../ui'
import './ModelChooser.css'

export const modelName = (m: ProviderModel) => m.label || m.name

/** "64k context" */
export const contextWords = (n: number) => `${n >= 1024 ? `${Math.round(n / 1024)}k` : n} context`

const capWords = (caps: string[] | undefined) => (caps ?? []).map(c => c.replace(/_/g, ' ')).join(', ')

/** what tells two similar models apart: the capabilities, the context */
export const modelDetail = (m: ProviderModel) => {
  const n = m.server_context ?? m.context
  return [capWords(m.caps_now), n ? contextWords(n) : ''].filter(Boolean).join(', ').replace(/^./, c => c.toUpperCase())
}

/** "Loaded", or "Not loaded, about 12 s" with what its last load took */
function loadWord(ref: string, loaded: Loaded | undefined): string | null {
  const m = loaded?.models.find(x => x.ref === ref)
  if (!m) return null
  if (m.loaded) return 'Loaded'
  return m.load_seconds != null ? `Not loaded, about ${Math.round(m.load_seconds)} s` : 'Not loaded'
}

/** the model and its provider for a model ref ("provider/model"), or null */
export function findModel(ref: string, providers: ProviderView[]): { model: ProviderModel; provider: ProviderView } | null {
  for (const p of providers) {
    const m = p.models.find(x => x.ref === ref)
    if (m) return { model: m, provider: p }
  }
  return null
}

export function ModelChooser({ title, providers, needs, current, skip, onPick, onClose, extra }: {
  title: string
  providers: ProviderView[]
  /** what the role needs (chat needs chat) */
  needs?: Cap[]
  current?: string | null
  /** refs not to offer (already in the role's list) */
  skip?: string[]
  onPick: (id: string) => void
  onClose: () => void
  /** "Default, My model" for a conversation: the model it comes to in its label */
  extra?: { label: string; onPick: () => void }
}) {
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const loaded = useLoaded()
  const list = useRef<VirtualListHandle>(null)
  const rows = useMemo<Row[]>(() => {
    const f = q.trim().toLowerCase()
    const out: Row[] = extra ? [{ type: 'extra' }] : []
    for (const p of providers) {
      const models = p.models.filter(m => m.offered && !skip?.includes(m.ref))
        .filter(m => !needs || needs.every(c => m.caps_now.includes(c)))
        .filter(m => !f || modelName(m).toLowerCase().includes(f) || m.name.toLowerCase().includes(f))
      if (!models.length) continue
      out.push({ type: 'provider', p })
      if (p.state.reachable === false && p.state.error) out.push({ type: 'problem', p })
      for (const m of models) out.push({ type: 'model', m })
    }
    return out
  }, [providers, needs, skip, q, extra])
  const picks = rows.map((r, i) => (r.type === 'provider' || r.type === 'problem' ? -1 : i)).filter(i => i >= 0)
  useEffect(() => { setActive(picks[0] ?? 0); list.current?.scrollToIndex(0) }, [q]) // eslint-disable-line react-hooks/exhaustive-deps

  const choose = (r: Row | undefined) => {
    if (r?.type === 'extra') extra?.onPick()
    else if (r?.type === 'model') onPick(r.m.ref)
  }
  const move = (dir: 1 | -1) => {
    const at = picks.indexOf(active)
    const next = picks[Math.min(picks.length - 1, Math.max(0, (at < 0 ? -1 : at) + dir))]
    if (next === undefined) return
    setActive(next); list.current?.scrollToIndex(next)
  }
  const models = rows.filter(r => r.type === 'model').length

  return (
    <Dialog title={title} onClose={onClose} size="md">
      <div className="dialog__form">
        <SearchField value={q} onChange={setQ} placeholder="Search" autoFocus
                     onKeyDown={e => {
                       if (e.key === 'ArrowDown') { e.preventDefault(); move(1) }
                       else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1) }
                       else if (e.key === 'Enter') { e.preventDefault(); choose(rows[active]) }
                     }} />
        <div className="chooser">
          {models === 0 && <EmptyState compact title={q ? 'No models match' : 'No model for this role yet'} />}
          <VirtualList items={rows} handleRef={list}
                       getKey={(r, i) => r.type === 'model' ? r.m.ref : r.type === 'provider' ? `p:${r.p.id}` : r.type === 'problem' ? `e:${r.p.id}` : `x:${i}`}
                       estimate={i => (rows[i]?.type === 'provider' ? 36 : rows[i]?.type === 'model' ? 56 : rows[i]?.type === 'problem' ? 28 : 44)}
                       renderRow={(r, i) => {
                         if (r.type === 'provider') {
                           return <ListSection label={r.p.provider.label} />
                         }
                         if (r.type === 'problem') {
                           return <p className="chooser__problem">{r.p.state.error}</p>
                         }
                         const on = r.type === 'extra' ? !current : r.m.ref === current
                         const swap = r.type === 'model' ? loadWord(r.m.ref, loaded) : null
                         return (
                           <ListRow selected={i === active} title={r.type === 'extra' ? extra!.label : modelName(r.m)}
                                    subtitle={r.type === 'model' ? modelDetail(r.m) || undefined : undefined}
                                    meta={<>{swap}{on && <Check size={ICON.sm} aria-label="Current" />}</>}
                                    onClick={() => choose(r)} />
                         )
                       }} />
        </div>
      </div>
    </Dialog>
  )
}

type Row = { type: 'extra' } | { type: 'provider'; p: ProviderView } | { type: 'problem'; p: ProviderView } | { type: 'model'; m: ProviderModel }
