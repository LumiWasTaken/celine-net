/* The box you write in, under a conversation: the kit's ComposerBox, the one raised object
   of the chat. Your draft (kept per conversation in this browser), slash commands and
   their list above the box, files (paste, drop, Attach) and quotes as chips in it, "+"
   with Attach, Impersonate and Reminders, and one button: Send, or Stop while it writes
   (Send again once you type: it waits for its next step). What you send goes out through
   `onSend`; a command runs here and says only what the server says. The page reaches in
   through `handle`: focus the box, add files dropped anywhere on it, read the draft. */
import { useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type Dispatch, type ReactNode, type Ref, type SetStateAction } from 'react'
import { Bell, LoaderCircle, Paperclip, Plus, Quote, TriangleAlert, Undo2, UserPen, X } from 'lucide-react'
import {
  api, conversationApi, deleteAttachment, uploadAttachment,
  type Attachment, type ChatModel, type NewConversationSettings,
} from '../lib/api'
import { takePrefill, useServerEvents, type Prefill } from '../lib/bus'
import { artifactRef } from '../lib/artifacts'
import { ArtifactChip, RefChip } from './Markdown'
import { errorText } from '../lib/format'
import { getJson, getPref, setJson, setPref } from '../lib/prefs'
import { SETTINGS_EVENTS, settingsApi, type ConversationSettings, type ProviderView } from '../lib/settingsApi'
import { OFFLINE_TITLE } from '../lib/socket'
import { THINK_WORD, commandsApi, menuRows, parseCommand, useCommands } from '../lib/commands'
import { Button, Chip, coarse, ComposerBox, IconButton, Menu, Notice, cx, curve, ms, reduced, type MenuItem } from '../ui'
import './Composer.css'
import { SpeechControls } from './Speech'

type Upload = {
  key: string
  name: string
  state: 'uploading' | 'ready' | 'failed'
  image: boolean
  att?: Attachment
  error?: string
}

/** the models offered for chat, from every provider */
const chatModels = (providers: ProviderView[]) =>
  providers.flatMap(p => p.models.filter(m => m.offered && (m.caps?.includes('chat') ?? true)))

/** what a slash command said, or why it failed: a quiet line above the box */
export type Note = { tone: 'success' | 'error'; text: string }

/** a chip above the text: `label` shows, `text` goes out first with your next message */
export type ComposerTag = { label: string; text: string }

/** the most commands listed above the box at once; typing narrows them */
const COMMANDS_SHOWN = 8

const noop = () => {}
const NO_DRAFT: NewConversationSettings = {}

export type ComposerHandle = {
  focus: () => void
  /** files dropped anywhere on the conversation */
  addFiles: (files: FileList | File[]) => void
  /** what is typed now */
  draft: () => string
  /** puts words into the box (a send that failed elsewhere) */
  put: (text: string) => void
}

/** the browser's file picker, with no element on the page */
function pickFiles(onFiles: (files: File[]) => void) {
  const input = document.createElement('input')
  input.type = 'file'
  input.multiple = true
  input.onchange = () => { if (input.files?.length) onFiles(Array.from(input.files)) }
  input.click()
}

export function Composer({
  handle, conversationId: selected, exists, active, name, running, offline, sending, pending, empty, hidden, closed,
  settings, draftSettings: newSettings = NO_DRAFT, onDraftSettings: setNewSettings = noop, onModelPanel = noop, onSend, onStop,
  setNote, onCompact = noop, toNew = noop, attempt, onReminders, notices, setup, jump, placeholder, noToolsText,
  takesPrefill = true, tag = null, onTag = noop,
}: {
  handle: Ref<ComposerHandle>
  conversationId: string | null
  /** a conversation of the list is open (not a new one) */
  exists: boolean
  /** its place is on screen */
  active: boolean
  /** who you write to */
  name: string
  running: boolean
  offline: boolean
  /** a message of yours is on its way (here or in another conversation): nothing else goes */
  sending: boolean
  /** yours here is on screen and not yet the server's */
  pending: boolean
  /** its greeting above it: the box glides down once the first message is sent */
  empty: boolean
  /** no conversation at this link: no box */
  hidden: boolean
  /** nothing new is written here: this sentence in place of the box */
  closed: string | null
  /** the conversation's settings (the latest `conversation.settings`) */
  settings: ConversationSettings | null
  /** a new conversation's choices before its first message (/think, /model) */
  draftSettings?: NewConversationSettings
  onDraftSettings?: Dispatch<SetStateAction<NewConversationSettings>>
  /** /model without a name: the side panel on its models */
  onModelPanel?: () => void
  /** your message with its attachments (ids, file names); rejects when it was not sent */
  onSend: (text: string, attachments: string[], names: string[]) => Promise<void>
  onStop: () => void
  setNote: Dispatch<SetStateAction<Note | null>>
  /** /compact ran: its outcome arrives later */
  onCompact?: () => void
  /** "Ask her" from elsewhere: to a new conversation */
  toNew?: () => void
  /** a click that fails says so */
  attempt: (fn: () => Promise<unknown>) => Promise<void>
  /** Reminders behind "+" (its chats) */
  onReminders?: () => void
  /** lines above the box (a lost login) */
  notices?: ReactNode
  /** above the box: what is still to set up (the first model) */
  setup?: ReactNode
  /** "New messages" when you scrolled up and more arrived: on the box's top edge */
  jump?: ReactNode
  /** the box's placeholder (default: "Message <name>") */
  placeholder?: string
  /** the line when the model that answers takes no tools (default: it can't search) */
  noToolsText?: (model: string) => string
  /** "Ask her" from elsewhere lands in this box (its place's, not a mission's) */
  takesPrefill?: boolean
  /** a chip that goes out first with the next message (a project's Edit); `onTag(null)`
      takes it away, after a send too */
  tag?: ComposerTag | null
  onTag?: (tag: null) => void
}) {
  const [draft, setDraft] = useState('')
  // messages and pages an "Ask Celine" brought along: chips in the box, sent with the text
  const [draftRefs, setDraftRefs] = useState<string[]>([])
  const text = useRef<HTMLTextAreaElement>(null)
  const pendingPrefill = useRef<Prefill | null>(null)
  const [uploads, setUploads] = useState<Upload[]>([])
  const serverCommands = useCommands(selected, settings)
  // /model's list, fetched when its list opens
  const [models, setModels] = useState<{ cid: string | null; list: string[] } | null>(null)
  // what the model that answers can do (asked again when the models change): no tools is
  // one line above the box; an image it can't see is a line too, and Send stays off
  const [chatModel, setChatModel] = useState<ChatModel | null>(null)
  const [modelsVersion, setModelsVersion] = useState(0)
  const hasImage = uploads.some(u => u.state === 'ready' && u.att?.kind === 'image')
  const draftModel = selected ? null : newSettings.model ?? null
  const conversationModel = settings?.model
  useEffect(() => {
    let live = true
    api.chatModel(selected, draftModel).then(
      r => { if (live) setChatModel(r) },
      () => { if (live) setChatModel(null) })
    return () => { live = false }
  }, [selected, draftModel, conversationModel, modelsVersion])
  const imageBlock = hasImage && chatModel?.blocked ? chatModel : null
  const noTools = chatModel && chatModel.model && !chatModel.tools ? chatModel : null

  useServerEvents(ev => {
    if (SETTINGS_EVENTS(ev)) setModels(null)       // /model's list follows Settings at once
    if (ev.type === 'models.state' || SETTINGS_EVENTS(ev)) setModelsVersion(v => v + 1)
    // "Ask her" from search or a message: a client-only event on the same bus. Starts a
    // fresh conversation in its place with the text as a draft and never sends it.
    if (ev.type === 'celine.prefill' && takesPrefill) prefill()
    // from the side panel: added to this conversation's draft, nothing else moves
    if (ev.type === 'celine.draft' && active) {
      const t = typeof ev.text === 'string' ? ev.text : ''
      const refs = Array.isArray(ev.refs) ? ev.refs.filter((r): r is string => typeof r === 'string') : []
      if (t) setDraft(d => (d.trim() ? `${d.trimEnd()}\n\n${t}` : t))
      setDraftRefs(list => [...list, ...refs.filter(r => !list.includes(r))])
    }
  })
  // From a conversation the reset below takes the draft when the new one opens; with the
  // new conversation already open the draft goes straight into the box
  const prefill = () => {
    const p = takePrefill()
    if (p === null) return
    if (selected === null) { pendingPrefill.current = null; setDraft(p.text); setDraftRefs(p.refs) }
    else pendingPrefill.current = p
    toNew()
    if (!coarse()) setTimeout(() => text.current?.focus(), 0)
  }

  // focus after the selection rendered (not on a touch screen: the keyboard would cover it)
  useEffect(() => { if (active && !coarse()) text.current?.focus() }, [selected, active])

  useEffect(() => {
    const p = pendingPrefill.current
    pendingPrefill.current = null
    setDraftRefs(!selected && p ? p.refs : [])
    if (!selected) { setDraft(p ? p.text : ''); setUploads([]); return }
    setDraft(getPref(`draft:${selected}`) ?? '')
    setUploads(getJson(`draft-files:${selected}`, []))
  }, [selected])
  // sent before this view was first opened (after the reset above, which would clear it)
  useEffect(() => { if (takesPrefill) prefill() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // each change is written at once: a switch right after typing keeps it
  useEffect(() => {
    if (!selected) return
    setPref(`draft:${selected}`, draft)
  }, [draft]) // eslint-disable-line react-hooks/exhaustive-deps
  // the files stay with the draft until it is sent or they are removed (the server keeps
  // a draft's file for 30 days, attachments.KEEP)
  useEffect(() => {
    if (!selected) return
    const ready = uploads.filter(u => u.state === 'ready')
    setJson(`draft-files:${selected}`, ready.length ? ready : null)
  }, [uploads]) // eslint-disable-line react-hooks/exhaustive-deps

  // the conversation open now: an upload ends in the one it started in
  const current = useRef(selected)
  current.current = selected
  const addFiles = useCallback(async (files: FileList | File[]) => {
    const from = selected
    const here = () => current.current === from
    for (const file of Array.from(files)) {
      const key = crypto.randomUUID()
      setUploads(u => [...u, { key, name: file.name, state: 'uploading', image: file.type.startsWith('image/') }])
      try {
        const att = await uploadAttachment(file)
        if (here()) setUploads(u => u.map(x => x.key === key ? { ...x, state: 'ready', att } : x))
        else if (from) {
          // switched away: the file joins the draft it was dropped on
          const kept = getJson<Upload[]>(`draft-files:${from}`, [])
          setJson(`draft-files:${from}`, [...kept, { key, name: file.name, state: 'ready', image: file.type.startsWith('image/'), att }])
        } else deleteAttachment(att.id).catch(() => {})
      } catch (e) {
        if (here()) setUploads(u => u.map(x => x.key === key ? { ...x, state: 'failed', error: errorText(e) } : x))
      }
    }
  }, [selected])

  const removeUpload = useCallback((key: string) => {
    const gone = uploads.find(x => x.key === key)
    if (gone?.att) deleteAttachment(gone.att.id).catch(() => {})
    setUploads(u => u.filter(x => x.key !== key))
  }, [uploads])

  useImperativeHandle(handle, () => ({
    focus: () => text.current?.focus(),
    addFiles: files => void addFiles(files),
    draft: () => draft,
    put: setDraft,
  }), [addFiles, draft])

  /* a slash command before the first message: /think and /model set the new
     conversation's choices (checked like the server would), the others need a conversation */
  const draftCommand = useCallback(async (cmd: string, args: string) => {
    const word = args.split(/\s+/)[0] ?? ''
    if (cmd === 'think') {
      if (!word) { setNote({ tone: 'success', text: `Thinking: ${newSettings.thinking ?? 'the model default'}.` }); return true }
      if (!THINK_WORD.test(word)) { setNote({ tone: 'error', text: 'Thinking is off, default, or one word such as low or high.' }); return false }
      const level = word.toLowerCase() === 'default' ? null : word
      setNewSettings(s => ({ ...s, thinking: level }))
      setNote({ tone: 'success', text: `Thinking: ${level ?? 'the model default'}, from the next turn.` })
      return true
    }
    if (cmd === 'model') {
      if (!word) { onModelPanel(); return true }
      if (word.toLowerCase() === 'default') {
        setNewSettings(s => ({ ...s, model: null }))
        setNote({ tone: 'success', text: 'Model: the default, from the next turn.' })
        return true
      }
      const providers = (await settingsApi.providers()).providers
      const w = word.toLowerCase()
      const hit = chatModels(providers)
        .find(m => [m.ref, m.name, m.label ?? ''].some(x => x.toLowerCase() === w))
      if (!hit) { setNote({ tone: 'error', text: `No chat model "${word}" in Settings, Models.` }); return false }
      setNewSettings(s => ({ ...s, model: hit.ref }))
      setNote({ tone: 'success', text: `Model: ${hit.label ?? hit.name}, from the next turn.` })
      return true
    }
    setNote({ tone: 'error', text: `/${cmd} needs a conversation. Send a first message.` })
    return false
  }, [newSettings.thinking, setNewSettings, setNote, onModelPanel])

  // you sent from this box: the next switch from empty to a conversation glides
  const sent = useRef(false)
  const send = useCallback(async (transcriptDraft?: string) => {
    const typed = (transcriptDraft ?? draft).trim()
    const refs = draftRefs
    const said = [typed, refs.map(r => `[[${r}]]`).join(' ')].filter(Boolean).join('\n\n')
    const out = tag ? [tag.text, said].filter(Boolean).join('\n\n') : said
    // a slash command runs here instead of being sent
    const cmd = uploads.length === 0 && !tag ? parseCommand(said, serverCommands) : null
    if (cmd) {
      const { command, args } = cmd
      setNote(null)
      try {
        if (!selected) { if (await draftCommand(command.name, args)) setDraft(''); return }
        if (command.when === 'idle' && running) {
          setNote({ tone: 'error', text: `${name} is still answering. Run /${command.name} once the reply is done.` })
          return
        }
        const r = await commandsApi.run(selected, command.name, args)
        setDraft('')
        if (command.name === 'compact') onCompact()
        // a command that lands something in the conversation says nothing: that is its answer
        if (r.message) setNote({ tone: 'success', text: r.message })
      } catch (e) { setNote({ tone: 'error', text: errorText(e) }) }
      return
    }
    const readyUploads = uploads.filter(u => u.state === 'ready')
    const ready = readyUploads.map(u => u.att!.id)
    if (uploads.some(u => u.state === 'uploading') || imageBlock) return
    // while it answers it is held by the server and reaches it at its next step
    if ((!said && ready.length === 0) || sending) return
    const before = uploads
    sent.current = true
    setDraft('')
    setDraftRefs([])
    setUploads([])
    try {
      await onSend(out, ready, readyUploads.map(u => u.name))
      onTag(null)
    } catch {
      // what you typed comes back (the reason is the page's note)
      sent.current = false
      setDraft(typed)
      setDraftRefs(refs)
      setUploads(before)
    }
  }, [draft, draftRefs, selected, running, sending, uploads, serverCommands, draftCommand, imageBlock, name, onSend, onCompact, setNote, tag, onTag])

  // Impersonate: the model writes your next message into the box, on from what you typed;
  // nothing is sent, and what you typed comes back with one tap (`typed`)
  const [impersonating, setImpersonating] = useState(false)
  const [typed, setTyped] = useState<string | null>(null)
  const impersonate = useCallback(async () => {
    if (!selected) return
    setImpersonating(true)
    await attempt(async () => {
      const r = await conversationApi.impersonate(selected, draft.trim())
      if (r.text) { setTyped(draft.trim() ? draft : null); setDraft(r.text); text.current?.focus() }
    })
    setImpersonating(false)
  }, [selected, draft, attempt])
  useEffect(() => { setTyped(null) }, [selected])

  // /model's list offers the models it can switch to
  const wantsModels = /^\/model\s/i.test(draft)
  useEffect(() => {
    if (!wantsModels || (models && models.cid === selected)) return
    let live = true
    const load = selected
      ? commandsApi.run(selected, 'model', '').then(r => r.models ?? [])
      : settingsApi.providers().then(r => chatModels(r.providers).map(m => m.ref))
    void load.then(list => { if (live) setModels({ cid: selected, list }) }).catch(() => { if (live) setModels({ cid: selected, list: [] }) })
    return () => { live = false }
  }, [wantsModels, selected, models])

  const commands = menuRows(draft, serverCommands, models?.cid === selected ? models.list : null).slice(0, COMMANDS_SHOWN)
  const command = /^\/\S*$/.test(draft.trim()) && commands.length > 0
  const ready = !pending && !command && !uploads.some(u => u.state === 'uploading') && !imageBlock
    && (!!draft.trim() || draftRefs.length > 0 || uploads.some(u => u.state === 'ready'))

  // "+": its items, in a new conversation and in one
  const plusBtn = useRef<HTMLButtonElement>(null)
  const [plusOpen, setPlusOpen] = useState(false)
  const plusItems: MenuItem[] = [
    { label: 'Attach', icon: Paperclip, onSelect: () => pickFiles(f => void addFiles(f)) },
    ...(exists ? [{ label: 'Impersonate', icon: UserPen, disabled: running || offline,
      title: running ? 'Wait until the reply is written' : offline ? OFFLINE_TITLE : undefined,
      onSelect: () => void impersonate() }] : []),
    ...(onReminders ? [{ label: 'Reminders', icon: Bell, onSelect: onReminders }] : []),
  ]

  // the first message of a new conversation: the box glides from the middle to the bottom
  // instead of jumping there. Only after you sent: opening a chat never glides.
  const inner = useRef<HTMLDivElement>(null)
  const glide = useRef<{ empty: boolean; top: number | null }>({ empty, top: null })
  useLayoutEffect(() => {
    const el = inner.current
    const top = el ? el.getBoundingClientRect().top : null
    const was = glide.current
    if (el && top !== null && was.top !== null && was.empty && !empty && sent.current && Math.abs(was.top - top) > 4 && !reduced()) {
      el.animate([{ transform: `translateY(${was.top - top}px)` }, { transform: 'none' }],
                 { duration: ms('--dur-slow'), easing: curve('--ease') })
    }
    if (!empty) sent.current = false
    glide.current = { empty, top }
  })

  // a conversation that is not found takes no messages: its page offers New conversation
  if (hidden) return null
  const chips = uploads.length > 0 || draftRefs.length > 0 || !!tag
  return (
    <div className={cx('chat-composer', empty && 'chat-composer--empty')}>
      <div className="chat-col chat-col--composer" ref={inner}>
        {/* floats on the box's top edge: appearing never moves it */}
        <div className={cx('chat-composer__float', commands.length > 0 && 'chat-composer__float--start')}>
          {commands.length > 0 ? (
            // each command by its usage, one line
            <Menu inline label="Commands" onClose={() => setDraft('')}
                  items={commands.map(r => ({ label: r.label, onSelect: () => { setDraft(r.fill); text.current?.focus() } }))} />
          ) : jump}
        </div>
        {noTools && (
          <Notice tone="warn">{noToolsText ? noToolsText(noTools.model) : `${noTools.model} can't use tools, so ${name} can't search.`}</Notice>
        )}
        {imageBlock && <Notice tone="warn">{imageBlock.model} can't see images.</Notice>}
        {notices}
        {setup}
        {typed !== null && (
          <div>
            <Button variant="ghost" size="sm" icon={Undo2} onClick={() => { setDraft(typed); setTyped(null) }}>Put back what I typed</Button>
          </div>
        )}
        {exists && closed ? <Notice>{closed}</Notice> : (
          <ComposerBox value={draft} inputRef={text}
                       onChange={v => { setDraft(v); setTyped(null); setNote(n => (n?.tone === 'success' ? null : n)) }}
                       onSend={() => void send()} onStop={onStop} running={running && !draft.trim()} ready={ready}
                       disabled={offline ? OFFLINE_TITLE : imageBlock ? 'Remove the image to send the message.' : undefined}
                        placeholder={placeholder ?? `Message ${name}`.trim()}
                        tools={<SpeechControls conversation={selected} active={active && !hidden} disabled={offline || sending}
                          onTranscript={(words, autoSend) => {
                            const combined = [draft.trim(), words].filter(Boolean).join('\n')
                            setDraft(combined)
                            if (autoSend) void send(combined)
                            else text.current?.focus()
                          }} />}
                       files={chips && <>
                         {tag && <Chip icon={Quote} title={tag.text} removeLabel={`Remove ${tag.label}`} onRemove={() => onTag(null)}>{tag.label}</Chip>}
                         {draftRefs.map(r => (
                           <span key={r} className="chat-ref">
                             {artifactRef(r) ? <ArtifactChip id={artifactRef(r)!} /> : <RefChip refId={r} />}
                             <IconButton icon={X} size="sm" label={artifactRef(r) ? 'Remove the page' : 'Remove the quote'}
                                         onClick={() => setDraftRefs(list => list.filter(x => x !== r))} />
                           </span>
                         ))}
                         {uploads.map(u => (
                           <Chip key={u.key} tone={u.state === 'failed' ? 'error' : 'neutral'}
                                 icon={u.state === 'uploading' ? LoaderCircle : u.state === 'failed' ? TriangleAlert : Paperclip}
                                 className={u.state === 'uploading' ? 'chip--busy' : undefined}
                                 title={u.error ?? u.name} removeLabel={`Remove ${u.name}`} onRemove={() => removeUpload(u.key)}>{u.name}</Chip>
                         ))}
                       </>}
                       start={<>
                         <IconButton ref={plusBtn} icon={Plus} label="Add" iconSize="lg" pressed={plusOpen} loading={impersonating}
                                     aria-haspopup="menu" aria-expanded={plusOpen} onClick={() => setPlusOpen(o => !o)} />
                       </>}
                       onKeyDown={e => {
                         if (e.key === 'Tab' && commands.length > 0) { e.preventDefault(); setDraft(commands[0]!.fill); return }
                         if (e.key === 'Enter' && !e.shiftKey && command) { e.preventDefault(); setDraft(commands[0]!.fill); return }
                         // Escape with nothing typed stops its reply
                         if (e.key === 'Escape' && !draft && running) { e.preventDefault(); onStop() }
                       }}
                       onPaste={e => {
                         const files = Array.from(e.clipboardData.files)
                         if (files.length) { e.preventDefault(); void addFiles(files) }
                       }} />
        )}
      </div>
      {plusOpen && plusBtn.current && <Menu anchor={plusBtn.current} label="Add" items={plusItems} onClose={() => setPlusOpen(false)} />}
    </div>
  )
}
