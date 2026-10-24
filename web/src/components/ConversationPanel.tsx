/* The side panel of one conversation, a Drawer beside it with its tabs (the one place with
   tabs):

     Model    the chat model (a row, Change model opens the chooser) and thinking for this
              conversation; its background jobs
     Tools    Bastet for this chat (a switch, PATCH settings `guard`), then a block per
              skill with its switch, folding open to its tools' switches (ToolList)
     Pages    its pages and documents (ArtifactPanel)
     Context  what the next request holds, per block; its notes, each with a switch (PATCH
              notes_off); the tools it has loaded, each with its x; Add loads one by hand

   Celine.tsx holds the tab and the page on screen, so a chip, a card or its first write of
   a page can open them. For a new conversation (/new_chat) the same panel holds the
   choices until the first message creates it; Context appears once it exists.

   `ModelRow` is the model alone with a button that opens the chooser, for the heads of
   places that talk without this panel (a mission). */
import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeftRight, Plus, X } from 'lucide-react'
import { conversationApi, notebookApi, type ContextView, type Job, type NewConversationSettings, type NotebookNode } from '../lib/api'
import { AUTO_CONTEXT, AUTO_CONTEXT_TIP, contextApi } from '../lib/context'
import { errorText, num } from '../lib/format'
import { OFFLINE_TITLE, useOnline } from '../lib/socket'
import { useFeatureOn } from '../lib/features'
import { format } from '../lib/nav'
import { usePronouns } from '../lib/name'
import { SETTINGS_EVENTS, settingsApi, type ConversationSettings } from '../lib/settingsApi'
import { useAction } from '../lib/useAction'
import { useResource } from '../lib/useResource'
import { useConversationSettings } from '../lib/useConversationSettings'
import {
  Button, Choice, cx, Dialog, Drawer, EmptyState, IconButton, InlineError, ListRow, ListSection, Loaded, LoadingRow, Progress, SearchField,
  Switch, Tabs, VirtualList,
} from '../ui'
import { ModelChooser, findModel, modelName } from './ModelChooser'
import { ToolList } from './ToolList'
import { Jobs } from './Jobs'
import { ArtifactPanel } from './ArtifactPanel'
import './Composer.css'

export type PanelTab = 'model' | 'tools' | 'pages' | 'context'
const ALL_TABS: { value: PanelTab; label: string }[] = [
  { value: 'model', label: 'Model' }, { value: 'tools', label: 'Tools' }, { value: 'pages', label: 'Pages' }, { value: 'context', label: 'Context' },
]
const PANEL = 'Model, Tools, Pages, Context'

export function ConversationPanel({
  conversationId, jobs, open, onClose, draft, onDraft, live, tab: wanted, onTab, artifact, onArtifact, assistant, notesOff, turnKey,
}: {
  conversationId: string | null
  /** the conversation's background jobs, fetched by the view */
  jobs: { data: Job[] | undefined; error: string | null; reload: () => void }
  open: boolean
  onClose: () => void
  /** the new conversation's choices (when no conversation is open) */
  draft: NewConversationSettings
  onDraft: (next: NewConversationSettings) => void
  /** the notes left out of this conversation (its snapshot, then conversation.notes_off) */
  notesOff: string[]
  /** changes with every turn state: Context measures again */
  turnKey: string
  /** the latest settings pushed on the conversation's socket */
  live: ConversationSettings | null
  tab: PanelTab
  onTab: (tab: PanelTab) => void
  /** the page on the Pages tab, or null for the list */
  artifact: string | null
  onArtifact: (id: string | null) => void
  assistant: string
}) {
  const guardOn = useFeatureOn('guard')
  const tabs = conversationId ? ALL_TABS : ALL_TABS.filter(t => t.value !== 'context')
  const tab = tabs.some(t => t.value === wanted) ? wanted : 'model'
  return (
    <Drawer open={open} label={PANEL} onClose={onClose} fill={tab === 'pages'}
            title={<Tabs label={PANEL} value={tab} onChange={onTab} options={tabs} />}>
      {tab === 'model' && (
        <div className="chat-panel">
          {conversationId
            ? <ModelTab key={conversationId} cid={conversationId} enabled={open} live={live} />
            : <DraftModel draft={draft} onDraft={onDraft} enabled={open} />}
          {conversationId && <Jobs jobs={jobs.data ?? []} error={jobs.error} reload={jobs.reload} active={open} />}
        </div>
      )}
      {tab === 'tools' && (conversationId
        ? <>{guardOn && <GuardRow key={`g${conversationId}`} cid={conversationId} enabled={open} live={live} />}
            <ToolList key={conversationId} conversationId={conversationId} /></>
        : <>{guardOn && <BastetSwitch checked={draft.guard !== false} onChange={on => onDraft({ ...draft, guard: on })} />}
            <ToolList draft={{ value: draft.tools ?? {}, onChange: tools => onDraft({ ...draft, tools }) }} /></>)}
      {tab === 'pages' && (
        <ArtifactPanel conversationId={conversationId} artifact={artifact} onPick={onArtifact} assistant={assistant} active={open} />
      )}
      {tab === 'context' && conversationId && <ContextTab key={conversationId} cid={conversationId} notesOff={notesOff} turnKey={turnKey} enabled={open} />}
    </Drawer>
  )
}

/* ── the model as one row whose button opens the chooser (ModelChooser: every chat model
   by provider, whether it is loaded, a search), then the six thinking words. The word is
   stored as it is; the server sends the nearest effort the model accepts. ── */
const THINK = ['default', 'off', 'low', 'medium', 'high', 'max'] as const
const THINK_LABEL: Record<string, string> = { default: 'Default', off: 'Off', low: 'Low', medium: 'Medium', high: 'High', max: 'Max', xhigh: 'Max' }

/** the model in use as a row; Change model opens the chooser, with Default first */
export function ModelPick({ name, tooltip, current, fallback, disabled, onModel }: {
  /** what the row says: the picked model's name, or "Default, <the model it comes to>" */
  name: string; tooltip?: string
  current: string | null; fallback: string | null | undefined; disabled: boolean
  onModel: (ref: string | null) => void
}) {
  const [choosing, setChoosing] = useState(false)
  const providers = useResource(() => settingsApi.providers().then(r => r.providers), [], { enabled: choosing, events: SETTINGS_EVENTS })
  const pick = (ref: string | null) => { setChoosing(false); if (ref !== current) onModel(ref) }
  return <>
    <ListRow density="sm" title={name} tooltip={tooltip ?? name} trailingAlways
             trailing={<IconButton icon={ArrowLeftRight} label="Change model" disabled={disabled} onClick={() => setChoosing(true)} />} />
    {choosing && (providers.data ? (
      <ModelChooser title="Change model" providers={providers.data} needs={['chat']} current={current}
                    extra={{ label: fallback ? `Default, ${fallback}` : 'Default', onPick: () => pick(null) }}
                    onPick={pick} onClose={() => setChoosing(false)} />
    ) : <Loaded res={providers} compact inline label="Loading models" />)}
  </>
}

function Thinking({ thinking, onThinking, disabled }: { thinking: string | null; onThinking: (t: string | null) => void; disabled: boolean }) {
  const current = thinking ?? 'default'
  const list = [...THINK] as string[]
  if (thinking && !list.includes(thinking)) list.push(thinking)
  return (
    <div>
      <ListSection label="Thinking" />
      <Choice label="Thinking" list value={current} disabled={disabled}
              onChange={v => onThinking(v === 'default' ? null : v)}
              options={list.map(w => ({ value: w, label: THINK_LABEL[w] || (w.charAt(0).toUpperCase() + w.slice(1)) }))} />
    </div>
  )
}

/** a conversation's model as its ModelPick shows it: the name ("Default, X" while none is
    picked) and what Default comes to (the server's `effective.default`) */
function pickOf(m: ConversationSettings, online: boolean) {
  const name = m.effective.label ?? m.effective.model ?? ''
  return { name: m.model ? name : `Default, ${name}`, tooltip: online ? m.effective.provider_label ?? name : OFFLINE_TITLE,
           current: m.model, fallback: m.effective.default?.label }
}

/* an open conversation: GET/PATCH /conversations/{cid}/settings */
function ModelTab({ cid, enabled, live }: { cid: string; enabled: boolean; live: ConversationSettings | null }) {
  const online = useOnline()
  const { res, patch } = useConversationSettings(cid, enabled, live)
  const m = res.data
  if (!m) return <Loaded res={res} compact inline />
  if (!m.effective.model) return <NoChatModel />
  const disabled = !online || patch.busy
  return <>
    <ModelPick {...pickOf(m, online)} disabled={disabled} onModel={model => void patch.run({ model })} />
    <Thinking thinking={m.thinking} disabled={disabled} onThinking={thinking => void patch.run({ thinking })} />
    {patch.error && <InlineError compact message={patch.error} />}
  </>
}

/* Bastet for this chat: one switch, the server's `guard` (conversation.settings event) */
function GuardRow({ cid, enabled, live }: { cid: string; enabled: boolean; live: ConversationSettings | null }) {
  const online = useOnline()
  const { res, patch } = useConversationSettings(cid, enabled, live)
  if (!res.data) return null
  // off in General: this chat's switch shows off and does nothing (the server says which)
  const global = res.data.effective.guard
  return <>
    <BastetSwitch checked={global && res.data.guard} disabled={!global || !online || patch.busy}
                  title={!global ? 'Bastet is off in Settings' : online ? undefined : OFFLINE_TITLE}
                  onChange={guard => void patch.run({ guard })} />
    {patch.error && <InlineError compact message={patch.error} />}
  </>
}

/* Bastet's switch, drawn as the head of a skill's block (ToolBlock) */
function BastetSwitch({ checked, disabled, title, onChange }: {
  checked: boolean; disabled?: boolean; title?: string; onChange: (on: boolean) => void
}) {
  return (
    <div className="tool-block">
      <div className="tool-block__head">
        <Switch className="tool-block__switch" label={<span className="tool-block__name">Bastet</span>}
                checked={checked} disabled={disabled} title={title} onChange={onChange} />
      </div>
    </div>
  )
}

/* a new conversation: the choices, kept until the first message */
function DraftModel({ draft, onDraft, enabled }: {
  draft: NewConversationSettings; onDraft: (d: NewConversationSettings) => void; enabled: boolean
}) {
  const roles = useResource(() => settingsApi.roles().then(r => r.roles), [], { enabled, events: SETTINGS_EVENTS })
  const providers = useResource(() => settingsApi.providers().then(r => r.providers), [],
                                { enabled: enabled && !!draft.model, events: SETTINGS_EVENTS })
  const def = roles.data?.chat?.models[0]
  if (roles.data && !def && !draft.model) return <NoChatModel />
  const picked = draft.model ? findModel(draft.model, providers.data ?? []) : null
  const name = draft.model ? (picked ? modelName(picked.model) : draft.model) : def ? `Default, ${def.label}` : 'Default'
  return <>
    <ModelPick name={name} current={draft.model ?? null} fallback={def?.label} disabled={false}
               onModel={model => onDraft({ ...draft, model })} />
    <Thinking thinking={draft.thinking ?? null} disabled={false} onThinking={thinking => onDraft({ ...draft, thinking })} />
  </>
}

/* No chat model is set yet: one line and the way there */
function NoChatModel() {
  return (
    <ListRow title="No chat model yet." trailingAlways
             trailing={<Button size="sm" href={format({ section: 'settings', page: 'models' })}>Choose a model</Button>} />
  )
}

/* ── the model alone in another place's head, with the chooser behind its button ── */
export function ModelRow({ cid, enabled = true, live, head = false }: {
  cid: string; enabled?: boolean; live: ConversationSettings | null; head?: boolean
}) {
  const online = useOnline()
  const { res, patch } = useConversationSettings(cid, enabled, live)
  const m = res.data
  if (!m) return head ? null : <Loaded res={res} compact inline />
  if (!m.effective.model) return head ? null : <NoChatModel />
  return (
    <>
      <ModelPick {...pickOf(m, online)} disabled={!online || patch.busy} onModel={model => void patch.run({ model })} />
      {patch.error && <InlineError compact message={patch.error} />}
    </>
  )
}

/* ── Context: the tokens of the window in use, per block; its notes, each with its state
   and a switch that leaves it out of this conversation (PATCH notes_off, the whole list);
   the tools it has loaded, each with its x. Measured again after every turn state, every
   change of the list and every `context.changed` of this conversation. Tokens only. ── */
type Block = ContextView['blocks'][number]['name']
const BLOCK: Record<Exclude<Block, 'persona' | 'talk'>, string> = {
  about: 'About you', notes: 'Notebook', tools: 'Tools', history: 'Conversation', warning: 'Summary',
}
/** a block's name, the ones about it in its pronouns ("Who he is", "How they talk") */
function blockWord(b: Block, subject: string): string {
  const they = subject === 'they'
  if (b === 'persona') return `Who ${subject} ${they ? 'are' : 'is'}`
  if (b === 'talk') return `How ${subject} ${they ? 'talk' : 'talks'}`
  return BLOCK[b]
}

/** the tokens, and how many turns ago a turn last used it (nothing for this turn) */
const carriedWords = (tokens: number, since: number | undefined) =>
  `${num(tokens)} tokens${since ? `, used ${since} ${since === 1 ? 'turn' : 'turns'} ago` : ''}`

function noteWord(n: ContextView['notes'][number], off: boolean, since: number | undefined): string {
  if (off) return 'Left out here'
  if (n.state === 'included') return carriedWords(n.tokens, since)
  if (n.state === 'left_out') return `Left out, ${n.why || 'over the share'}`
  return `Off, ${n.why || 'by its own line'}`
}

function ContextTab({ cid, notesOff, turnKey, enabled }: {
  cid: string; notesOff: string[]; turnKey: string; enabled: boolean
}) {
  const { subject } = usePronouns()
  const ctx = useResource(() => conversationApi.context(cid), [cid, turnKey, notesOff.join('\n')],
    { enabled, events: ev => (ev.type === 'context.changed' && ev.conversation === cid)
      || (ev.type === 'conversation.auto_context' && ev.id === cid) })
  const [error, setError] = useState<string | null>(null)
  // the list sent last: two quick switches both count before the server's event comes back
  const sent = useRef(notesOff)
  useEffect(() => { sent.current = notesOff }, [notesOff])
  const unload = useAction(async (name: string) => { await conversationApi.unloadSkill(cid, name); ctx.reload() })
  const auto = useAction(async (on: boolean) => { await contextApi.setAuto(cid, on); ctx.reload() })
  const [loading, setLoading] = useState<'skill' | 'note' | null>(null)
  const c = ctx.data
  if (!c) return <Loaded res={ctx} compact inline />
  const used = c.blocks.reduce((n, b) => n + b.tokens, 0)
  const since = new Map(c.loaded.notes.map(n => [n.path, n.since]))
  const toggle = async (path: string, on: boolean) => {
    setError(null)
    const next = on ? sent.current.filter(p => p !== path) : [...sent.current.filter(p => p !== path), path]
    sent.current = next
    try { await conversationApi.setNotesOff(cid, next) } catch (e) { setError(errorText(e)) }
  }
  return (
    <div className="chat-panel">
      <ListRow density="sm" title={AUTO_CONTEXT} tooltip={AUTO_CONTEXT_TIP} trailingAlways
               trailing={<Switch hideLabel label={AUTO_CONTEXT} title={AUTO_CONTEXT_TIP} checked={c.auto_context} disabled={auto.busy}
                                 onChange={on => void auto.run(on)} />} />
      {auto.error && <InlineError compact message={auto.error} />}
      <div role="list" aria-label="Context">
        <div className="chat-ctx">
          <span className={cx('chat-ctx__total', used > c.window && 'chat-ctx__total--over')}><b>{num(used)}</b> of {num(c.window)} tokens</span>
          <Progress value={used} max={c.window} label="Context" />
        </div>
        {c.blocks.filter(b => b.tokens > 0).map(b => <ListRow key={b.name} density="sm" title={blockWord(b.name, subject)} meta={num(b.tokens)} />)}
      </div>
      <div className="chat-ctx__list" role="list" aria-label="Notebook">
        <ListSection label="Notebook" />
        {error && <InlineError compact message={error} />}
        {c.notes.map(n => {
          const off = notesOff.includes(n.path)
          // a note off by its own load line stays off whatever the switch says
          const own = n.state === 'off' && !off
          return (
            <ListRow key={n.path} density="sm" title={n.path.replace(/\.md$/, '')} tooltip={n.path} subtitle={noteWord(n, off, since.get(n.path))}
                     muted={off || n.state !== 'included'} trailingAlways
                     trailing={<Switch hideLabel checked={!off} disabled={own} label={n.path} onChange={on => void toggle(n.path, on)} />} />
          )
        })}
        <Button variant="ghost" className="chat-ctx__add" icon={Plus} title="Add a note" onClick={() => setLoading('note')}>Add</Button>
      </div>
      <div className="chat-ctx__list" role="list" aria-label="Tools">
        <ListSection label="Tools" />
        {unload.error && <InlineError compact message={unload.error} />}
        {c.loaded.skills.map(k => (
          <ListRow key={k.name} density="sm" title={k.label} subtitle={carriedWords(k.tokens, k.since)} trailingAlways
                   trailing={!k.core && <IconButton icon={X} size="sm" label={`Remove ${k.label}`} loading={unload.busy}
                                                    onClick={() => void unload.run(k.name)} />} />
        ))}
        <Button variant="ghost" className="chat-ctx__add" icon={Plus} title="Add tools" onClick={() => setLoading('skill')}>Add</Button>
      </div>
      {loading && (
        <LoadDialog cid={cid} what={loading} skip={loading === 'skill' ? c.loaded.skills.map(k => k.name) : c.loaded.notes.map(n => n.path)}
                    onDone={() => { setLoading(null); ctx.reload() }} onClose={() => setLoading(null)} />
      )}
    </div>
  )
}

/* loading by hand: every skill of this conversation, or every note of its notebook, with a
   search; one click loads it for its next step */
function LoadDialog({ cid, what, skip, onDone, onClose }: {
  cid: string; what: 'skill' | 'note'; skip: string[]; onDone: () => void; onClose: () => void
}) {
  const [q, setQ] = useState('')
  const items = useResource<{ key: string; title: string; detail: string }[]>(async () => {
    if (what === 'skill') {
      const r = await conversationApi.skills(cid)
      return r.skills.filter(k => k.status === 'up').map(k => ({ key: k.name, title: k.label, detail: k.summary }))
    }
    const files: { key: string; title: string; detail: string }[] = []
    const walk = (ns: NotebookNode[]) => ns.forEach(n => (n.kind === 'dir' ? walk(n.children ?? [])
      : files.push({ key: n.path, title: n.path.replace(/\.md$/, ''), detail: '' })))
    walk((await notebookApi.tree()).tree)
    return files
  }, [cid, what])
  const load = useAction(async (key: string) => {
    await (what === 'skill' ? contextApi.loadSkill(cid, key) : contextApi.loadNote(cid, key))
    onDone()
  })
  const shown = useMemo(() => {
    const f = q.trim().toLowerCase()
    return (items.data ?? []).filter(i => !skip.includes(i.key) && (!f || i.title.toLowerCase().includes(f) || i.key.toLowerCase().includes(f)))
  }, [items.data, q, skip])
  return (
    <Dialog title={what === 'skill' ? 'Add tools' : 'Add a note'} onClose={onClose} size="md">
      <SearchField value={q} onChange={setQ} placeholder="Search" autoFocus />
      {load.error && <InlineError compact message={load.error} />}
      {items.error ? <InlineError compact message={items.error} onRetry={items.reload} />
        : !items.data ? <LoadingRow />
        : shown.length === 0 ? <EmptyState compact title={q ? 'Nothing matches' : 'Everything is loaded'} />
        : <VirtualList items={shown} getKey={i => i.key} estimate={() => 44} label={what === 'skill' ? 'Tools' : 'Notebook'}
                       renderRow={i => <ListRow title={i.title} subtitle={i.detail || undefined} onClick={() => void load.run(i.key)} />} />}
    </Dialog>
  )
}
