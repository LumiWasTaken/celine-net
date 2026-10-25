/* The agent's place: the chats. The kit's Place: the conversation list in
   its sidebar with its own head (Search, Select, Archive, Trash, More, New; a drawer from
   the left on a phone), the chat beside it: the head with its face, its name and its mood,
   the transcript in the reading column, the composer as the one raised object, and the side
   panel (Model, Tools, Pages, Context). It is one section of the app and stays mounted
   while you are elsewhere, so a running reply and a draft survive switching. The server
   sends every group, count, row state and its mood; the page draws them. */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { ArrowDown, Archive, Bell, Compass, Download, Images, LoaderCircle, PanelRight, Pencil, Pin, PinOff, Plus, RotateCcw, Trash2, TriangleAlert, X } from 'lucide-react'
import {
  api, conversationApi,
  type ConfigStatus, type ConversationSummary, type Message, type NewConversationSettings, type Job,
} from '../lib/api'
import { emit, useServerEvents } from '../lib/bus'
import { ShowArtifact } from '../lib/artifacts'
import { chatApi, useMood, type ChatHit } from '../lib/chat'
import { ConversationList, rowState, type RowState, type Work } from '../components/ConversationList'
import { ConversationPanel, type PanelTab } from '../components/ConversationPanel'
import { ReminderLines, Reminders, pendingOf } from '../components/Reminders'
import { HerFace, MoodText } from '../components/Talk'
import { PlanButton } from '../components/Plan'
import { useHer } from '../lib/her'
import { count, errorText } from '../lib/format'
import { useNav } from '../lib/nav'
import { setPageName } from '../lib/title'
import { AgentsContext, useAgents } from '../lib/agents'
import { isOpen } from '../lib/missions'
import { fileUrl } from '../lib/media'
import { FirstRun } from './settings/FirstRun'
import { ConversationReminders, remindersApi } from '../lib/reminders'
import { snapshotAgain, useOnline, OFFLINE_TITLE } from '../lib/socket'
import { useConversation } from '../lib/useConversation'
import { useResource } from '../lib/useResource'
import { useStickToBottom } from '../lib/useStickToBottom'
import { Transcript, type Pending } from '../components/Transcript'
import { SideHost } from '../components/MessageActions'
import { Composer, type ComposerHandle, type Note } from '../components/Composer'
import { waitWords } from '../components/Status'
import { PhotoViewer } from '../components/PhotoViewer'
import { GalleryContext, collectGeneratedImages, madeDetails, type Made } from '../components/Batch'
import {
  Button, ConfirmDialog, EmptyState, IconButton, InlineEdit, InlineError, LoadingRow, Notice, Place, PlaceHead, SelectBar,
  coarse, isNarrow, StatusLine, cx, usePicker, type Shelf,
} from '../ui'
import { usePref } from '../lib/prefs'
import { bulkActions, useArchiveOlder } from '../components/Bulk'

/** the events that move a conversation in or out of a shelf, or change its row */
const LIST_EVENTS = new Set(['conversation.title', 'conversation.created', 'conversation.deleted',
  'conversation.restored', 'conversation.purged', 'conversation.pinned', 'conversation.archived', 'conversation.unarchived',
  'version.changed', 'message.purged', 'mission.state', 'frozen.changed'])

const place = 'celine' as const
const chatsNoun = (n: number) => count(n, 'conversation')
const exportUrl = (id: string) => `/api/conversations/${encodeURIComponent(id)}/export`

export function Celine({ active, status, sidebarOpen, onSidebar, onWork }: {
  active: boolean
  status: ConfigStatus | null
  /** the list over the chat on a phone */
  sidebarOpen: boolean
  onSidebar: (open: boolean) => void
  /** what each place has going on (the rail's dots) */
  onWork: (work: Work) => void
}) {
  const nav = useNav()
  const who = useHer()
  const mood = useMood()
  const placeName = who?.name ?? ''
  // the side panel and its tab, kept on this device; on a phone it lies over the chat, so
  // it starts closed there
  const [panelKept, keepPanel] = usePref('panel', false)
  const [drawerOpen, setDrawerShown] = useState(() => panelKept && !isNarrow())
  const setDrawerOpen = useCallback((open: boolean) => { setDrawerShown(open); if (!isNarrow()) keepPanel(open) }, [keepPanel])
  const [activeSide, setActiveSide] = useState<string | null>(null)
  const [panelTab, setPanelTab] = usePref<PanelTab>('panel:tab', 'model')
  // the page on the Pages tab (null: the list); a chip, a card or its first write shows one
  const [artifact, setArtifact] = useState<string | null>(null)
  const showArtifact = useCallback((id: string) => {
    setActiveSide(null)
    setArtifact(id)
    setPanelTab('pages')
    setDrawerOpen(true)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const openPanel = (t: PanelTab) => {
    setActiveSide(null)
    setPanelTab(t)
    setDrawerOpen(true)
  }
  const handleSetActiveSide = useCallback((key: string | null) => {
    if (key) {
      setDrawerOpen(false)
    }
    setActiveSide(key)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // the new conversation's choices, until the first message creates it
  const [newSettings, setNewSettings] = useState<NewConversationSettings>({})

  // the shelves: the list always, Archive and Trash once opened; asked again on every
  // event that moves a row, and when a reply starts or ends
  const [shelf, setShelf] = useState<Shelf>('list')
  const listEvents = (ev: { type: string; state?: unknown }) => LIST_EVENTS.has(ev.type)
    || (ev.type === 'turn.state' && ['queued', 'done', 'aborted', 'failed', 'interrupted', 'capped'].includes(ev.state as string))
  const list = useResource(() => chatApi.list('list'), [], { events: listEvents })
  // the archived ones always: one opened by its link is still its own, with Restore
  const archived = useResource(() => chatApi.list('archive'), [], { events: listEvents })
  const trashed = useResource(() => chatApi.list('trash'), [], { events: listEvents, enabled: shelf === 'trash' })
  const shown = shelf === 'archive' ? archived : shelf === 'trash' ? trashed : list
  const rows = list.data?.rows
  const picker = usePicker(shelf)
  const older = useArchiveOlder('chat', chatsNoun)
  const [actionError, setActionError] = useState<string | null>(null)
  const attempt = useCallback(async (fn: () => Promise<unknown>) => {
    setActionError(null)
    try { await fn() } catch (e) { setActionError(errorText(e)) }
  }, [])

  // the URL decides which conversation is open: /chat/<id>, or /new_chat for none
  const routeConversation = nav.route.section === place ? nav.route.conversation ?? null : undefined
  const [selected, setSelectedState] = useState<string | null>(routeConversation ?? null)
  useEffect(() => { if (routeConversation !== undefined) setSelectedState(routeConversation) }, [routeConversation])
  const setSelected = useCallback((id: string | null, opts?: { replace?: boolean }) => {
    setSelectedState(id)
    nav.go({ section: place, conversation: id ?? undefined }, opts)
  }, [nav])

  // Search: the titles on the shelf, then the messages, both as the server ranks them
  const [query, setQuery] = useState('')
  const q = query.trim()
  const found = useResource(signal => (q ? chatApi.search(q, shelf, signal) : Promise.resolve({ titles: [], hits: [] as ChatHit[] })),
                            [q, shelf], { debounce: 200 })

  const composer = useRef<ComposerHandle>(null)
  const [note, setNote] = useState<Note | null>(null)
  // your message on screen the moment you press Send, until the server's copy arrives
  const [pending, setPending] = useState<(Pending & { cid: string | null; intercomId?: number; turnId?: string | null }) | null>(null)
  const [dragging, setDragging] = useState(false)
  // Activity and Thoughts open beside the conversation, like the side panel
  const [side, setSide] = useState<HTMLDivElement | null>(null)
  useEffect(() => {
    const clear = () => setDragging(false)
    window.addEventListener('drop', clear)
    window.addEventListener('dragend', clear)
    return () => { window.removeEventListener('drop', clear); window.removeEventListener('dragend', clear) }
  }, [])
  const transcript = useRef<HTMLDivElement>(null)
  const jumpRef = useRef<(opts?: unknown) => void>(() => {})
  const agents = useAgents()
  // this conversation's background jobs: the head's count and the panel's list
  const jobs = useResource(
    () => (selected ? api.jobs(selected, 20).then(r => r.jobs) : Promise.resolve([] as Job[])), [selected],
    { events: ev => ev.type === 'job.state' && (ev as { conversation?: string }).conversation === selected })
  const jobList = (jobs.data ?? []).filter(j => j.conversation_id === selected)
  const runningJobs = jobList.filter(j => j.state === 'running').length
  const [renaming, setRenaming] = useState<string | null>(null)
  const [confirmTrash, setConfirmTrash] = useState<string | null>(null)
  const [remindersOpen, setRemindersOpen] = useState(false)
  // the messages scheduled to this conversation; null while another conversation's list is here
  const reminders = useResource(
    async () => ({ cid: selected, list: selected ? await remindersApi.list(selected) : [] }), [selected],
    { events: ev => ev.type === 'intercom.state' && (ev as { conversation_id?: string }).conversation_id === selected })
  const reminderList = reminders.data?.cid === selected ? reminders.data.list : null
  const pendingReminders = useMemo(() => pendingOf(reminderList), [reminderList])
  const conversationReminders = useMemo(() => ({ list: reminderList, reload: reminders.reload }), [reminderList, reminders.reload])
  const convo = useConversation(selected)

  // every row of its own, sub-agents too
  const every = useMemo(() => [...rows ?? [], ...archived.data?.rows ?? [], ...trashed.data?.rows ?? []].flatMap(c => [c, ...c.agents]),
                        [rows, archived.data, trashed.data])
  const current = every.find(c => c.id === selected) ?? null
  const name = placeName
  useEffect(() => { if (active) setPageName(name) }, [active, name])

  // a conversation another place shows (a mission, a task, a run, their sub-agents)
  // moves to the place the server names
  const snap = convo.conversation
  useEffect(() => {
    if (!active || !selected || current || !snap || snap.id !== selected) return
    if (snap.link && snap.link !== `/chat/${selected}`) nav.goPath(snap.link, { replace: true })
  }, [active, selected, current, snap, nav])

  const galleryImages = useMemo(
    () => (convo.messages ? collectGeneratedImages(convo.messages as Message[], convo.toolCalls) : []),
    [convo.messages, convo.toolCalls],
  )
  const [galleryIndex, setGalleryIndex] = useState<number | null>(null)
  const [standalonePhoto, setStandalonePhoto] = useState<Made | null>(null)
  useEffect(() => { setGalleryIndex(null); setStandalonePhoto(null); setArtifact(null); setNote(null) }, [selected])
  const openImage = useCallback((idOrSha: string, fallback?: Made) => {
    const idx = galleryImages.findIndex(x => x.img.id === idOrSha || x.img.sha256 === idOrSha)
    if (idx !== -1) { setStandalonePhoto(null); setGalleryIndex(idx); return }
    if (fallback) { setGalleryIndex(null); setStandalonePhoto(fallback) }
  }, [galleryImages])

  // a /compact you ran: its outcome arrives later as an event
  const awaitingCompact = useRef(false)
  const outcome = convo.compactOutcome
  useEffect(() => {
    if (!outcome || !awaitingCompact.current) return
    awaitingCompact.current = false
    setNote(outcome.kind === 'compacted' ? { tone: 'success', text: 'Compacted.' }
      : outcome.kind === 'failed' ? { tone: 'error', text: "Couldn't compact right now." }
      : { tone: 'success', text: outcome.reason ?? 'Nothing to compact yet.' })
  }, [outcome])

  // after a reconnect: what finished meanwhile, once, on top of the list
  const [away, setAway] = useState<string | null>(null)
  useServerEvents(ev => {
    if (ev.type === 'ui.new') newConversation()
    if (ev.type === 'changes') {
      jobs.reload()
      const done = (Array.isArray(ev.jobs) ? ev.jobs : []) as { state: string }[]
      const ok = done.filter(j => j.state === 'done').length
      const bad = done.filter(j => j.state !== 'done' && j.state !== 'cancelled').length
      if (ok || bad) setAway(`While you were away: ${ok} finished${bad ? `, ${bad} failed` : ''}`)
    }
    // its first write of a new page in the open conversation shows it beside the chat
    if (ev.type === 'artifact.updated' && ev.n === 1 && !ev.yours && ev.conversation === selected && typeof ev.id === 'string') {
      showArtifact(ev.id)
    }
  })

  const rename = useCallback((id: string, title: string) => {
    setRenaming(null)
    if (title) void attempt(() => api.renameConversation(id, title))
  }, [attempt])
  const togglePin = (c: ConversationSummary) => attempt(() => api.pinConversation(c.id, !c.pinned))
  const archive = (c: ConversationSummary, on: boolean) => attempt(() => api.archiveConversation(c.id, on))
  const toTrash = async (id: string) => {
    await api.deleteConversation(id)
    setConfirmTrash(null)
    if (selected === id) setSelected(null, { replace: true })
  }
  const restore = (c: ConversationSummary) => attempt(() => api.restoreConversation(c.id))
  const exportOne = (c: ConversationSummary) => { window.location.href = exportUrl(c.id) }

  // New goes to /new_chat: nothing is created until the first message
  const newConversation = useCallback(() => {
    setSelected(null)
    setDrawerOpen(false)
    setNewSettings({})
    onSidebar(false)
    if (!coarse()) composer.current?.focus()
  }, [setSelected, onSidebar]) // eslint-disable-line react-hooks/exhaustive-deps
  const toNew = useCallback(() => setSelected(null, { replace: true }), [setSelected])
  const onCompact = useCallback(() => { awaitingCompact.current = true }, [])

  // the first send creates it with those settings; its URL replaces /new_chat at once
  const createConversation = useCallback(async () => {
    const { id } = await api.createConversation(newSettings)
    setPending(p => p && { ...p, cid: id })
    setSelected(id, { replace: true })
    setNewSettings({})
    list.reload()
    return id
  }, [setSelected, newSettings, list])

  // your message, on screen at once with the dots; the requests follow. A message not sent
  // says why, and the Composer puts back what you typed
  const post = useCallback(async (text: string, ready: string[], names: string[]) => {
    setNote(null)
    setPending({ cid: selected, text, files: names })
    jumpRef.current({ smooth: false })
    try {
      const id = selected ?? await createConversation()
      const r = await conversationApi.send(id, text, ready)
      setPending(p => p && { ...p, cid: id, intercomId: r.intercom_id, turnId: r.turn_id })
    } catch (e) {
      setPending(null)
      setNote({ tone: 'error', text: errorText(e) })
      throw e
    }
  }, [selected, createConversation])
  // the server's copy of your message (with its reply under way when one started) takes
  // the place of the one shown at once
  useEffect(() => {
    if (pending?.intercomId !== undefined && (!pending.turnId || convo.turn?.id === pending.turnId)
        && convo.messages.some(m => m.intercom?.id === pending.intercomId)) setPending(null)
  }, [pending, convo.messages, convo.turn?.id])

  // stopped on screen at once; the server confirms within a second
  const { markStopping } = convo
  const stop = useCallback(async () => {
    if (!selected) return
    markStopping(true)
    await attempt(async () => {
      try {
        const r = await conversationApi.abort(selected)
        if (!r.turn_id) markStopping(false)
      } catch (e) { markStopping(false); throw e }
    })
  }, [selected, attempt, markStopping])
  const stopOther = useCallback((id: string) => {
    if (id === selected) void stop()
    else void attempt(() => conversationApi.abort(id))
  }, [selected, stop, attempt])
  const regenerate = useCallback(async () => { if (selected) await conversationApi.regenerate(selected) }, [selected])

  // Escape closes the side panel, then the list over the chat; Alt+Up and Alt+Down step
  // through the list
  const stepOrder = useRef<string[]>([])
  stepOrder.current = (rows ?? []).map(c => c.id)
  useEffect(() => {
    if (!active) return
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return
      if (e.key === 'Escape') {
        if (drawerOpen) setDrawerOpen(false)
        else if (sidebarOpen && selected) onSidebar(false)
      }
      if (e.altKey && !e.ctrlKey && !e.metaKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
        const ids = stepOrder.current
        if (!ids.length) return
        e.preventDefault()
        const at = selected ? ids.indexOf(selected) : -1
        const next = e.key === 'ArrowDown' ? Math.min(ids.length - 1, at + 1) : Math.max(0, at - 1)
        if (ids[next] && ids[next] !== selected) setSelected(ids[next]!)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [active, drawerOpen, sidebarOpen, selected, setSelected, onSidebar]) // eslint-disable-line react-hooks/exhaustive-deps

  // what the rail shows for its place: waiting for you over failed over running (the
  // server's row states), and its sub-agents at work
  const work = useMemo<Work>(() => {
    const rank: Record<RowState, number> = { running: 1, failed: 2, needs_you: 3 }
    let top: RowState | null = null
    const mark = (st: RowState | null) => { if (st && (!top || rank[st] > rank[top])) top = st }
    for (const c of rows ?? []) {
      mark(rowState(c) ?? (c.mission_name && isOpen(c.mission_status) ? 'running' : null))
      c.agents.forEach(a => mark(rowState(a)))
    }
    if (agents.some(a => a.working && (rows ?? []).some(c => c.id === a.parent))) mark('running')
    return top ? { celine: top } : {}
  }, [rows, agents])
  useEffect(() => { onWork(work) }, [work, onWork])

  const online = useOnline()
  const offline = convo.reconnecting || !online
  // back online: what failed meanwhile is asked again, so no stale error stays
  const wasOnline = useRef(online)
  useEffect(() => {
    if (online && !wasOnline.current) { list.reload(); archived.reload(); trashed.reload(); jobs.reload(); reminders.reload() }
    wasOnline.current = online
  }, [online]) // eslint-disable-line react-hooks/exhaustive-deps
  const shownPending = pending && pending.cid === selected ? pending : null
  const loading = !!selected && !list.data && !current && !shownPending
  const missing = !!selected && !!list.data && !current && !shownPending && !convo.conversation
  const empty = !selected && !shownPending
  const drawn = !!(current || shownPending || (selected && convo.conversation))
  const lastMessage = convo.messages.at(-1)
  const { onScroll: onTranscriptScroll, unseen, jump: jumpToLatest } = useStickToBottom(
    transcript, [lastMessage?.content, lastMessage?.reasoning, lastMessage?.id, convo.toolCalls, pendingReminders.length],
    [selected], [selected, convo.messages.length > 0, drawn])
  jumpRef.current = jumpToLatest

  const noteLine = note && (
    <div className="chat-note">
      {note.tone === 'error' ? <InlineError message={note.text} compact /> : <StatusLine tone="success">{note.text}</StatusLine>}
      <IconButton icon={X} size="sm" label="Dismiss" onClick={() => setNote(null)} />
    </div>
  )
  const open = (id: string) => { setSelected(id); onSidebar(false) }
  const conversationTitle = current?.title || convo.conversation?.title || ''
  const isArchived = current ? !!current.archived_at : !!convo.conversation?.archived_at
  const headMore = current ? [
    { label: 'Rename', icon: Pencil, onSelect: () => setRenaming(current.id) },
    ...(!current.agent_of ? [{ label: current.pinned ? 'Unpin' : 'Pin', icon: current.pinned ? PinOff : Pin, onSelect: () => void togglePin(current) }] : []),
    { label: 'Reminders', icon: Bell, onSelect: () => setRemindersOpen(true) },
    ...(galleryImages.length ? [{ label: 'Images', icon: Images, onSelect: () => setGalleryIndex(galleryImages.length - 1) }] : []),
    { label: 'Export', icon: Download, onSelect: () => exportOne(current) },
    'sep' as const,
    ...(isArchived || current.agent_of ? [] : [{ label: 'Archive', icon: Archive, onSelect: () => void archive(current, true) }, 'sep' as const]),
    { label: 'Move to Trash', icon: Trash2, danger: true, onSelect: () => setConfirmTrash(current.id) },
  ] : []

  // the head's buttons beside its name: the panel, More, then "running" and Restore as they show
  const headActs = 1 + (current ? 1 : 0) + (runningJobs > 0 ? 1 : 0) + (isArchived && current ? 1 : 0) + (convo.plan?.steps.length ? 1 : 0)

  const listHead = <>
    <PlaceHead title={placeName} count={shown.data?.count}
               search={{ value: query, onChange: setQuery, label: 'Search conversations', loading: !!q && found.loading }}
               picker={picker} shelf={{ archive: true, trash: true, value: shelf, onChange: setShelf }}
               more={[older.item, { label: 'Go to', icon: Compass, onSelect: () => emit({ type: 'ui.goto' }) }]}
               newLabel="New conversation" onNew={newConversation} />
    {older.dialog}
    <SelectBar picker={picker} noun={chatsNoun} actions={bulkActions({ what: 'conversations', shelf, noun: chatsNoun, archive: true })} />
  </>
  const listBody = (
    <div className="chat-list">
      {shown.error && <InlineError message={shown.error} onRetry={shown.reload} />}
      {found.error && <InlineError message={found.error} onRetry={found.reload} />}
      {actionError && <InlineError message={actionError} compact />}
      {!shown.data ? <LoadingRow /> : shelf === 'list' && !rows?.length && !q ? (
        <EmptyState action={<Button variant="primary" icon={Plus} onClick={newConversation}>New conversation</Button>} />
      ) : (
        <ConversationList conversations={rows ?? []} archived={archived.data?.rows} deleted={trashed.data?.rows} shelf={shelf}
                          picker={picker} query={query} titles={q ? found.data?.titles ?? [] : undefined} hits={q ? found.data?.hits : undefined}
                          onClearSearch={() => setQuery('')} selected={selected} renaming={renaming}
                          onOpen={open} onRename={rename} onStartRename={setRenaming} onPin={c => void togglePin(c)}
                          onArchive={(c, on) => void archive(c, on)} onRestore={c => void restore(c)} onDelete={setConfirmTrash}
                          onStop={stopOther} onExport={exportOne} away={away} onAway={() => setAway(null)} />
      )}
    </div>
  )

  return (
    <ShowArtifact.Provider value={showArtifact}>
    <AgentsContext.Provider value={agents}>
    <Place title={placeName} label="Conversations" open={sidebarOpen} onOpen={onSidebar} head={listHead} side={listBody}>
      <div className="chat">
        <main className={cx('chat-main', dragging && 'chat-main--drop')} style={{ '--acts': headActs } as CSSProperties}
              onDragOver={e => { if (!e.dataTransfer.types.includes('Files')) return; e.preventDefault(); setDragging(true) }}
              onDragLeave={() => setDragging(false)}
              onDrop={e => {
                setDragging(false)
                if (!e.dataTransfer.files.length) return
                e.preventDefault(); composer.current?.addFiles(e.dataTransfer.files)
              }}>
          <PlaceHead level={1}
                     lead={<HerFace who={who} size="sm" speaking={convo.running} mood={mood} />}
                     title={
                       <span className="chat-who">
                         {name}<MoodText mood={mood} />
                         {renaming === current?.id ? (
                           <InlineEdit value={conversationTitle} label="Rename conversation" onCancel={() => setRenaming(null)}
                                       onSave={t => { setRenaming(null); if (t && t !== conversationTitle) rename(current.id, t) }} />
                         ) : (
                           conversationTitle && <span className="chat-head__title">{conversationTitle}</span>
                         )}
                       </span>
                     }
                     titleAttr={conversationTitle || undefined}
                     actions={<>
                       <PlanButton plan={convo.plan} />
                       {runningJobs > 0 && (
                         <Button icon={LoaderCircle} variant="ghost" className="chat-jobs" title={`${count(runningJobs, 'background job')} running`}
                                 onClick={() => openPanel('model')}>{`${runningJobs} running`}</Button>
                       )}
                       {/* an archived conversation says so by its one way back */}
                       {isArchived && current && <Button icon={RotateCcw} onClick={() => void archive(current, false)}>Restore</Button>}
                       <IconButton icon={PanelRight} label="Model, Tools, Pages, Context" pressed={drawerOpen} iconSize="lg"
                                   onClick={() => (drawerOpen ? setDrawerOpen(false) : openPanel(panelTab))} />
                     </>}
                     more={headMore} />
          {offline && (
            <div className="chat-notice">
              <Notice tone="warn" action={selected ? { label: 'Retry', onClick: () => snapshotAgain(selected) } : undefined}>{OFFLINE_TITLE}</Notice>
            </div>
          )}

          {drawn ? (
            <div className="chat-scroll" ref={transcript} onScroll={onTranscriptScroll}>
              <GalleryContext.Provider value={{ images: galleryImages, openImage }}>
                <SideHost.Provider value={{ host: side, active: activeSide, setActive: handleSetActiveSide }}>
                <ConversationReminders.Provider value={conversationReminders}>
                  <Transcript conversationId={selected}
                              messages={convo.messages}
                              toolCalls={convo.toolCalls} turn={convo.turn}
                              running={convo.running} error={convo.error}
                              pending={shownPending} compactions={convo.compactions} onRetry={regenerate}
                              live={convo.live} switched={convo.switched}
                              imageNotices={convo.imageNotices} banners={convo.banners} approvals={convo.approvals} forming={convo.forming}
                              waiting={waitWords(convo.waiting, convo.live)} queue={convo.queue}
                              note={null} assistant={name}
                              user={status?.user ?? ''} />
                </ConversationReminders.Provider>
                </SideHost.Provider>
              </GalleryContext.Provider>
              <ReminderLines list={pendingReminders} />
            </div>
          ) : loading ? (
            <div className="chat-scroll">{list.error && <EmptyState><InlineError message={list.error} onRetry={list.reload} /></EmptyState>}</div>
          ) : missing ? (
            <div className="chat-scroll chat-scroll--empty">
              <EmptyState icon={TriangleAlert} title="Not found"
                          action={<Button variant="primary" icon={Plus} onClick={newConversation}>New conversation</Button>} />
            </div>
          ) : (
            <div className="chat-scroll chat-scroll--empty">
              <div className="chat-greeting">
                <HerFace who={who} size="lg" mood={mood} />
                <div className="chat-greeting__name">{name}</div>
                <MoodText mood={mood} />
              </div>
              <FirstRun />
            </div>
          )}

          <Composer handle={composer} conversationId={selected} exists={!!current} active={active && !drawerOpen} name={name}
                    running={convo.running} offline={offline} sending={!!pending} pending={!!shownPending}
                    empty={empty} hidden={missing} closed={null} settings={convo.settings}
                    draftSettings={newSettings} onDraftSettings={setNewSettings} onModelPanel={() => openPanel('model')}
                    onSend={post} onStop={() => void stop()} setNote={setNote} onCompact={onCompact}
                    toNew={toNew} attempt={attempt} onReminders={current ? () => setRemindersOpen(true) : undefined}
                    notices={noteLine}
                    setup={current && <FirstRun />}
                    jump={unseen && <Button size="sm" icon={ArrowDown} onClick={jumpToLatest}>New messages</Button>} />
        </main>

        <div ref={setSide} className="chat-side" />
        <ConversationPanel conversationId={selected}
                           jobs={{ data: jobList, error: jobs.error, reload: jobs.reload }}
                           open={drawerOpen} onClose={() => setDrawerOpen(false)}
                           draft={newSettings} onDraft={setNewSettings} live={convo.settings}
                           notesOff={convo.conversation?.notes_off ?? []} turnKey={`${convo.turn?.id}:${convo.turn?.state}`}
                           tab={panelTab} onTab={setPanelTab} artifact={artifact} onArtifact={setArtifact}
                           assistant={name} />
      </div>
    </Place>
      {remindersOpen && current && (
        <Reminders conversationId={current.id} list={pendingReminders} reload={reminders.reload} onClose={() => setRemindersOpen(false)} />
      )}
      {confirmTrash && (
        <ConfirmDialog title="Move to Trash?" danger confirmLabel="Move to Trash"
                       body={<>"{every.find(c => c.id === confirmTrash)?.title || 'Untitled'}" goes to Trash, where Restore brings it back.</>}
                       onClose={() => setConfirmTrash(null)} onConfirm={() => toTrash(confirmTrash)} />
      )}
      {standalonePhoto && (
        <PhotoViewer src={fileUrl(standalonePhoto)} name={standalonePhoto.filename} prompt={standalonePhoto.prompt}
                     details={madeDetails(standalonePhoto)} onClose={() => setStandalonePhoto(null)} />
      )}
      {galleryIndex !== null && galleryImages[galleryIndex] && (
        <PhotoViewer src={fileUrl(galleryImages[galleryIndex]!.img)} name={galleryImages[galleryIndex]!.img.filename}
                     prompt={galleryImages[galleryIndex]!.img.prompt} details={madeDetails(galleryImages[galleryIndex]!.img)}
                     onClose={() => setGalleryIndex(null)}
                     onPrev={galleryIndex > 0 ? () => setGalleryIndex(galleryIndex - 1) : undefined}
                     onNext={galleryIndex < galleryImages.length - 1 ? () => setGalleryIndex(galleryIndex + 1) : undefined}
                     index={galleryIndex} total={galleryImages.length} />
      )}
    </AgentsContext.Provider>
    </ShowArtifact.Provider>
  )
}
