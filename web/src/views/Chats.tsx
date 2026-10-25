/* Messages: the WhatsApp and Discord archive, as a messenger. The place's sidebar holds
   All messages and each source with its count of chats. All messages, or a source's chats, as a list (Search, Sort, the Trash with
   Restore and Delete permanently, Select for many, Rename and Move to Trash from a row's
   menu), and a chat that pages both ways around the message it opened at, day by day,
   the name over each message. A message's menu: Copy, Copy message link, Ask Celine, Go to
   the original message, Versions, Rename the person. Photos open in the Viewer. A message
   link /m/<source>/<message> finds its chat and replaces the URL. Its column and marks
   are styled in Search.css, the reader a search result opens in.
   URL: /chats (All messages), /chats/<source>, /chats/<source>/<chat>[/<message>], /m/<source>/<message>. */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useName } from '../lib/name'
import {
  ArrowUpRight, BookHeart, Contact, Copy, CornerUpLeft, File, FileText, History, Image, ImagePlay, Info, Link, ListChecks, MapPin,
  MessagesSquare, Mic, MoreHorizontal, Music, Pencil, RotateCcw, Search, SearchX, Sticker, Trash2, Video, type LucideIcon,
} from 'lucide-react'
import { chatText, isPicture, kbApi, type ArchiveMessage, type Chat, type Kb, type MediaType } from '../lib/kb'
import { useServerEvents } from '../lib/bus'
import { useDebounced } from '../lib/useDebounced'
import { askCeline, copyMessageLink } from '../lib/ask'
import { format, useNav, type Route } from '../lib/nav'
import { bytes, clock, count, dayLabel, errorText, fullDate, listTime, num, sameDay } from '../lib/format'
import { bulkActions } from '../components/Bulk'
import {
  Avatar, Button, ConfirmDialog, Dialog, Divider, EmptyState, EventLine, ICON, IconButton, InlineError, Input, ListRow, LoadingRow,
  MenuButton, Message, Picture, Place, PlaceHead, Quote, SelectBar, ShelfRow, Toolbar, VirtualList, sourceIcon, toast, useContextMenu,
  cx, usePicker, type MenuItem, type Shelf, type VirtualListHandle, copied
} from '../ui'
import './Search.css'

const chatsNoun = (n: number) => count(n, 'chat')

export function Chats({ active }: { active: boolean }) {
  const nav = useNav()
  // the page the URL names; while another place is on screen, the one it showed last
  const last = useRef<Extract<Route, { section: 'chats' }>>({ section: 'chats' })
  if (nav.route.section === 'chats') last.current = nav.route
  const route = last.current
  const [kbs, setKbs] = useState<Kb[] | null>(null)
  const [kbsError, setKbsError] = useState<string | null>(null)
  const [resolveError, setResolveError] = useState<string | null>(null)

  const loadKbs = useCallback(async () => {
    try { setKbs((await kbApi.list()).kbs); setKbsError(null) } catch (e) { setKbsError(errorText(e)) }
  }, [])
  useEffect(() => { void loadKbs() }, [loadKbs])
  useServerEvents(ev => { if (ev.type === 'kb.changed') void loadKbs() })

  const chatKbs = useMemo(() => (kbs ?? []).filter(k => k.kind === 'chat'), [kbs])
  const kbId = route.kb
  const kb = chatKbs.find(k => k.id === kbId)

  // /m/<kb>/<id>: a bare message link. Find its chat, then replace the URL.
  useEffect(() => {
    if (!route.resolve || !route.kb || !route.message) return
    setResolveError(null)
    kbApi.message(route.kb, route.message)
      .then(m => nav.go({ section: 'chats', kb: route.kb, chat: m.chat.id, message: m.id }, { replace: true }))
      .catch(e => setResolveError(errorText(e)))
  }, [route, nav])

  const chatId = route.chat
  const messageId = !route.resolve ? route.message : undefined
  const nothing = !!kbs && chatKbs.every(k => !k.messages)

  return (
    <Place title="Messages"
           side={[
             <ListRow key="" density="sm" icon={MessagesSquare} title="All messages" meta={nothing || !kbs ? undefined : chatKbs.reduce((n, k) => n + (k.chats ?? 0), 0)}
                      selected={!kbId && !chatId} href={format({ section: 'chats' })} />,
             ...chatKbs.map(k => (
             <ListRow key={k.id} density="sm" icon={sourceIcon(k.platform ?? k.id) ?? MessagesSquare} title={k.title} meta={nothing ? undefined : k.chats}
                      selected={k.id === kbId && !chatId} href={format({ section: 'chats', kb: k.id })} />
           ))]}>
      {kbsError || nothing ? (
          <div className="list-page">
            <PlaceHead level={1} title="Messages" />
            <div className="list-page__empty">
              {kbsError ? <EmptyState icon={MessagesSquare} title={kbsError} action={<Button onClick={() => void loadKbs()}>Retry</Button>} />
                : <EmptyState icon={MessagesSquare} title="No messages yet"
                              action={<Button variant="primary" href={format({ section: 'settings', page: 'sources' })}>Open Settings</Button>} />}
            </div>
          </div>
        )
        : resolveError ? <div className="list-page__note"><InlineError message={resolveError} /></div>
        : kb && chatId ? <Thread key={`${kb.id}:${chatId}`} kb={kb} chatId={chatId} jump={messageId} />
        : <ChatList key={kbId ?? ''} kbs={chatKbs} kbId={kbId} active={active} />}
    </Place>
  )
}

/* ── list ── */

type Sort = 'recent' | 'name' | 'count'
const PAGE = 200
const SORTS: { value: Sort; label: string }[] = [{ value: 'recent', label: 'Recent' }, { value: 'name', label: 'Name' }, { value: 'count', label: 'Messages' }]

function ChatList({ kbs, kbId, active }: { kbs: Kb[]; kbId?: string; active: boolean }) {
  const nav = useNav()
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<Sort>('recent')
  const [shelf, setShelf] = useState<Shelf>('list')
  const trash = shelf === 'trash'
  const picker = usePicker(`${kbId}:${shelf}`)
  const [chats, setChats] = useState<Chat[]>([])
  const [total, setTotal] = useState(0)
  // true until the first page is in, so nothing flashes before it
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  const [renaming, setRenaming] = useState<Chat | null>(null)
  const [trashing, setTrashing] = useState<Chat | null>(null)
  const list = useRef<VirtualListHandle>(null)
  const ctx = useContextMenu()
  const [rowError, setRowError] = useState<string | null>(null)
  const act = (p: Promise<unknown>) => p.then(() => { setRowError(null); setVersion(v => v + 1) }).catch(e => setRowError(errorText(e)))
  const open = (c: Chat) => nav.go({ section: 'chats', kb: c.kb, chat: c.id })
  const menu = (c: Chat): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => open(c) },
    { label: 'Search', icon: Search, onSelect: () => nav.go({ section: 'search', kb: c.kb, chat: { ref: `${c.kb}:${c.id}`, name: c.name } }) },
    { label: 'Rename', icon: Pencil, onSelect: () => setRenaming(c) },
    'sep',
    { label: 'Select', icon: ListChecks, onSelect: () => picker.start(`${c.kb}:${c.id}`) },
    { label: 'Move to Trash', icon: Trash2, danger: true, onSelect: () => setTrashing(c) },
  ]
  useServerEvents(ev => { if (ev.type === 'kb.changed' && (!kbId || ev.kb === kbId)) setVersion(v => v + 1) })

  const typed = useDebounced(q, q ? 150 : 0)
  const load = useCallback(async (offset: number, signal?: { cancelled: boolean }) => {
    setLoading(true)
    try {
      const r = await kbApi.chats(kbId, { q: typed.trim() || undefined, sort, trashed: trash, limit: PAGE, offset })
      if (signal?.cancelled) return
      setChats(c => (offset === 0 ? r.chats : [...c, ...r.chats])); setTotal(r.total); setError(null)
    } catch (e) {
      if (!signal?.cancelled) setError(errorText(e))
    } finally {
      if (!signal?.cancelled) setLoading(false)
    }
  }, [kbId, typed, sort, trash])
  useEffect(() => {
    const signal = { cancelled: false }
    void load(0, signal)
    return () => { signal.cancelled = true }
  }, [load, version])
  useEffect(() => { if (active) list.current?.scrollToIndex(0) }, [kbId]) // eslint-disable-line react-hooks/exhaustive-deps
  const more = () => { if (!loading && chats.length < total) void load(chats.length) }
  picker.keys.current = chats.map(c => `${c.kb}:${c.id}`)
  const done = !loading || chats.length > 0
  // the first page failed: the list's own EmptyState with Retry, as in every place
  const failed = chats.length === 0 ? error : null

  return (
    <div className="list-page">
      <PlaceHead level={1} title={trash ? 'Trash' : 'Messages'} count={!failed && done && (total || !q) ? total : undefined}
                 search={{ value: q, onChange: setQ, placeholder: 'Search chats', loading: loading && !!q }}
                 picker={chats.length ? picker : undefined} shelf={{ trash: true, value: shelf, onChange: setShelf }} />
      {!failed && (picker.on || (!trash && kbs.length > 0)) && (
        <div className="list-page__top">
          {picker.on ? <SelectBar picker={picker} noun={chatsNoun} actions={bulkActions({ what: 'chats', shelf, noun: chatsNoun })} />
            : <Toolbar state={kbs.length > 1 ? { label: 'Source', value: kbId ?? '', onChange: k => nav.go({ section: 'chats', kb: k || undefined }),
                                                 options: [{ value: '', label: 'All', count: kbs.reduce((n, k) => n + (k.chats ?? 0), 0) },
                                                           ...kbs.map(k => ({ value: k.id, label: k.title, count: k.chats }))] } : undefined}
                       sort={{ label: 'Sort', value: sort, onChange: v => setSort(v as Sort), options: SORTS }} />}
        </div>
      )}
      {!failed && (error ?? rowError) && <div className="list-page__note"><InlineError message={(error ?? rowError)!} onRetry={error ? () => void load(0) : undefined} /></div>}
      {!done ? <LoadingRow label="Loading chats" />
        : chats.length === 0 ? (
          <div className="list-page__empty">
            {failed ? <EmptyState icon={MessagesSquare} title={failed} action={<Button onClick={() => void load(0)}>Retry</Button>} />
              : q ? <EmptyState icon={SearchX} title="No chats match" action={<Button onClick={() => setQ('')}>Clear search</Button>} />
              : <EmptyState icon={trash ? Trash2 : MessagesSquare} title={trash ? 'Trash is empty' : 'No chats yet'} />}
          </div>
        ) : (
          <div className="list-page__rows">
            <VirtualList handleRef={list} items={chats} estimate={68} overscan={12} getKey={c => c.id} onEndReached={more} label="Messages" resetOn={`${kbId}|${shelf}`}
                         after={loading ? <LoadingRow label="Loading more" /> : null}
                         renderRow={c => {
                           const key = `${c.kb}:${c.id}`
                           if (c.trashed) {
                             return <ShelfRow density="lg" title={c.name} subtitle={c.parent ?? `${num(c.messages)} messages`}
                                              pick={picker.row(key)} onSelect={() => picker.start(key)}
                                              leading={<Avatar name={c.name} seed={key} size={40} kind={c.kind} />}
                                              onClick={() => open(c)}
                                              onRestore={() => void act(kbApi.patchChat(c.kb, c.id, { trashed: false }))}
                                              onDelete={() => act(kbApi.deleteChat(c.kb, c.id))} />
                           }
                           const who = c.last_preview ? (c.last_preview.from_me ? 'You' : c.kind !== 'dm' ? c.last_preview.sender : null) : null
                           return (
                             <ListRow density="lg" title={c.name} tooltip={c.name} leading={<Avatar name={c.name} seed={key} size={40} kind={c.kind} />}
                                      subtitle={c.last_preview ? <>{who && <span className="search-who">{who}: </span>}{c.last_preview.text || 'Attachment'}</> : undefined}
                                      column={c.parent ?? kbs.find(k => k.id === c.kb)?.title ?? ''} cell={`${num(c.messages)} ${c.messages === 1 ? 'message' : 'messages'}`}
                                      meta={c.last_ts ? listTime(c.last_ts) : ''}
                                      href={format({ section: 'chats', kb: c.kb, chat: c.id })} onClick={e => { if (!e.ctrlKey && !e.metaKey) { e.preventDefault(); open(c) } }}
                                      pick={picker.row(key)} onContextMenu={e => ctx.open(e, menu(c), c.name)} press={ctx.press(() => menu(c), c.name)}
                                      trailing={!picker.on && <MenuButton icon={MoreHorizontal} label="More" items={menu(c)} size="sm" />} />
                           )
                         }} />
          </div>
        )}
      {ctx.menu}
      {renaming && <RenameChat c={renaming} onClose={() => setRenaming(null)} onDone={() => setVersion(v => v + 1)} />}
      {trashing && (
        <ConfirmDialog title={`Move ${trashing.name} to Trash?`} danger confirmLabel="Move to Trash" body="Restore brings it back."
                       onClose={() => setTrashing(null)}
                       onConfirm={async () => {
                         try { await kbApi.patchChat(trashing.kb, trashing.id, { trashed: true }) } catch (e) { return errorText(e) }
                         setTrashing(null); setVersion(v => v + 1)
                       }} />
      )}
    </div>
  )
}

/* a chat's name in this archive: yours, or its own again */
function RenameChat({ c, onClose, onDone }: { c: Pick<Chat, 'kb' | 'id' | 'name'>; onClose: () => void; onDone: (c: Chat) => void }) {
  const [name, setName] = useState(c.name)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const save = async (value: string | null) => {
    setSaving(true)
    try { onDone(await kbApi.patchChat(c.kb, c.id, { name: value })); onClose() } catch (e) { setError(errorText(e)) } finally { setSaving(false) }
  }
  return (
    <Dialog title="Rename" size="sm" onClose={onClose} dismissable={!saving}
            actions={<><Button onClick={onClose} disabled={saving}>Cancel</Button>
              <Button variant="primary" loading={saving} disabled={!name.trim()} onClick={() => void save(name.trim())}>Save</Button></>}>
      <form onSubmit={e => { e.preventDefault(); if (name.trim()) void save(name.trim()) }}>
        <Input autoFocus value={name} onChange={e => setName(e.target.value)} aria-label="Name" />
      </form>
      {error && <InlineError message={error} compact />}
    </Dialog>
  )
}

/* ── one chat ── */

const LIMIT = 80
const KEEP = 1200            // messages kept in the DOM; the far end is dropped past this
const EDGE = 600             // px from an edge that triggers the next page

type Page = { msgs: ArchiveMessage[]; hasBefore: boolean; hasAfter: boolean }

function Thread({ kb, chatId, jump }: { kb: Kb; chatId: number; jump?: number }) {
  const nav = useNav()
  const [chat, setChat] = useState<Chat | null>(null)
  const [page, setPage] = useState<Page | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [edgeError, setEdgeError] = useState<{ dir: 'before' | 'after'; message: string } | null>(null)
  const [highlight, setHighlight] = useState<number | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [person, setPerson] = useState<{ id: number; name: string } | null>(null)
  const [versions, setVersions] = useState<ArchiveMessage | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [trashing, setTrashing] = useState(false)
  const scroller = useRef<HTMLDivElement>(null)
  const busy = useRef<'before' | 'after' | null>(null)
  const anchor = useRef<{ id: number; top: number } | null>(null)
  const pendingScroll = useRef<{ kind: 'bottom' } | { kind: 'message'; id: number } | null>(null)

  const loadChat = useCallback(async () => {
    try { setChat(await kbApi.chat(kb.id, chatId)) } catch (e) { setError(errorText(e)) }
  }, [kb.id, chatId])
  useEffect(() => { void loadChat() }, [loadChat])

  const loadInitial = useCallback(async (around?: number) => {
    setError(null)
    try {
      const r = await kbApi.messages(kb.id, chatId, around ? { around, limit: LIMIT } : { limit: LIMIT })
      pendingScroll.current = around ? { kind: 'message', id: around } : { kind: 'bottom' }
      anchor.current = null
      setPage({ msgs: r.messages, hasBefore: r.has_before, hasAfter: r.has_after })
    } catch (e) { setError(errorText(e)) }
  }, [kb.id, chatId])

  // first load, and every jump to a message that is not on screen yet
  const loadedIds = useRef<Set<number>>(new Set())
  useEffect(() => {
    if (jump && loadedIds.current.has(jump)) {
      pendingScroll.current = { kind: 'message', id: jump }
      setHighlight(null)
      requestAnimationFrame(() => scrollToPending())
      return
    }
    void loadInitial(jump)
  }, [jump, loadInitial])
  useEffect(() => { loadedIds.current = new Set(page?.msgs.map(m => m.id)) }, [page])

  function rememberAnchor() {
    const el = scroller.current
    if (!el) return
    const box = el.getBoundingClientRect()
    for (const n of el.querySelectorAll<HTMLElement>('[data-mid]')) {
      const r = n.getBoundingClientRect()
      if (r.bottom > box.top) { anchor.current = { id: Number(n.dataset.mid), top: r.top - box.top }; return }
    }
  }
  function scrollToPending() {
    const el = scroller.current
    const p = pendingScroll.current
    if (!el || !p) return
    pendingScroll.current = null
    if (p.kind === 'bottom') { el.scrollTop = el.scrollHeight; return }
    const node = el.querySelector<HTMLElement>(`[data-mid="${p.id}"]`)
    if (node) { node.scrollIntoView({ block: 'center' }); setHighlight(p.id) }
  }
  // after every page change: keep the anchor (older ones above, the far end dropped) or do the pending scroll
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el || !page) return
    if (pendingScroll.current) { anchor.current = null; scrollToPending(); return }
    const a = anchor.current
    if (!a) return
    anchor.current = null
    const node = el.querySelector<HTMLElement>(`[data-mid="${a.id}"]`)
    if (!node) return
    el.scrollTop += node.getBoundingClientRect().top - el.getBoundingClientRect().top - a.top
  }, [page])

  const loadMore = useCallback(async (dir: 'before' | 'after') => {
    if (!page || busy.current) return
    const edge = dir === 'before' ? page.msgs[0] : page.msgs.at(-1)
    if (!edge) return
    busy.current = dir
    setEdgeError(null)
    try {
      const r = await kbApi.messages(kb.id, chatId, dir === 'before' ? { before: edge.id, limit: LIMIT } : { after: edge.id, limit: LIMIT })
      rememberAnchor()
      setPage(p => {
        if (!p) return p
        let msgs = dir === 'before' ? [...r.messages, ...p.msgs] : [...p.msgs, ...r.messages]
        let { hasBefore, hasAfter } = p
        if (dir === 'before') hasBefore = r.has_before; else hasAfter = r.has_after
        if (msgs.length > KEEP) {
          if (dir === 'before') { msgs = msgs.slice(0, KEEP); hasAfter = true } else { msgs = msgs.slice(msgs.length - KEEP); hasBefore = true }
        }
        return { msgs, hasBefore, hasAfter }
      })
    } catch (e) {
      setEdgeError({ dir, message: errorText(e) })
    } finally {
      busy.current = null
    }
  }, [page, kb.id, chatId])

  const onScroll = useCallback(() => {
    const el = scroller.current
    if (!el || !page || edgeError) return
    if (el.scrollTop < EDGE && page.hasBefore) void loadMore('before')
    else if (el.scrollHeight - el.scrollTop - el.clientHeight < EDGE && page.hasAfter) void loadMore('after')
  }, [page, loadMore, edgeError])

  // new messages in this archive: the newest when the end of the chat is loaded (a view at
  // the bottom stays at the bottom)
  useServerEvents(ev => {
    if (ev.type !== 'kb.changed' || ev.kb !== kb.id) return
    void loadChat()
    if (!page || page.hasAfter || busy.current) return
    const el = scroller.current
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 48) pendingScroll.current = { kind: 'bottom' }
    void loadMore('after')
  })
  // a short page may not fill the screen; keep loading until it scrolls or runs out
  useEffect(() => {
    const el = scroller.current
    if (!el || !page || busy.current || edgeError) return
    if (el.scrollHeight <= el.clientHeight + 10) {
      if (page.hasBefore) void loadMore('before')
      else if (page.hasAfter) void loadMore('after')
    }
  }, [page, loadMore, edgeError])
  useEffect(() => {
    if (highlight === null) return
    const t = setTimeout(() => setHighlight(null), HIGHLIGHT_MS)
    return () => clearTimeout(t)
  }, [highlight])

  const setTrashed = async (trashed: boolean) => {
    try {
      setChat(await kbApi.patchChat(kb.id, chatId, { trashed }))
      if (trashed) nav.go({ section: 'chats', kb: kb.id }, { replace: true })
    } catch (e) { setError(errorText(e)) }
  }
  const openAt = (id: number) => nav.go({ section: 'chats', kb: kb.id, chat: chatId, message: id })
  const search = () => chat && nav.go({ section: 'search', kb: kb.id, chat: { ref: `${kb.id}:${chat.id}`, name: chat.name } })
  const msgs = page?.msgs ?? []
  const group = chat ? chat.kind !== 'dm' : true

  return (
    <div className="search-page">
      <PlaceHead level={1} title={chat?.name ?? ''} titleAttr={chat?.name} detail={chat?.parent ?? (chat?.trashed ? 'Trash' : undefined)}
                 crumbs={[{ label: kb.title, href: format({ section: 'chats', kb: kb.id }) }]}
                 lead={<Avatar name={chat?.name} seed={`${kb.id}:${chatId}`} size={32} kind={chat?.kind} />}
                 actions={<IconButton icon={Search} label="Search" disabled={!chat} onClick={search} />}
                 more={chat?.trashed ? [{ label: 'Restore', icon: RotateCcw, onSelect: () => void setTrashed(false) },
                                        'sep', { label: 'Delete permanently', icon: Trash2, danger: true, onSelect: () => setDeleting(true) }]
                   : [{ label: 'Rename', icon: Pencil, disabled: !chat, onSelect: () => setRenaming(true) },
                      'sep', { label: 'Move to Trash', icon: Trash2, danger: true, disabled: !chat, onSelect: () => setTrashing(true) }]} />
      <div className="page search-chat" ref={scroller} onScroll={onScroll}>
        <div className="search-chat__col">
          {error && <InlineError message={error} onRetry={() => { void loadChat(); void loadInitial(jump) }} />}
          {edgeError?.dir === 'before' && <InlineError message={edgeError.message} onRetry={() => void loadMore('before')} />}
          {page && !page.hasBefore && msgs.length > 0 && <Divider>Start of the chat</Divider>}
          {page?.hasBefore && edgeError?.dir !== 'before' && <LoadingRow label="Loading older messages" />}
          {!page && !error && <LoadingRow label="Loading messages" />}
          {page && msgs.length === 0 && <EmptyState compact icon={MessagesSquare} title="No messages yet" />}
          {msgs.map((m, i) => {
            const prev = msgs[i - 1]
            return (
              <div key={m.id} className="search-chat__item">
                {(!prev || !sameDay(prev.ts, m.ts)) && <Divider>{dayLabel(m.ts)}</Divider>}
                <ChatMessage m={m} kb={kb.id} group={group} highlight={highlight === m.id} onOriginal={openAt}
                             onPerson={setPerson} onVersions={setVersions} />
              </div>
            )
          })}
          {page?.hasAfter && edgeError?.dir !== 'after' && <LoadingRow label="Loading newer messages" />}
          {edgeError?.dir === 'after' && <InlineError message={edgeError.message} onRetry={() => void loadMore('after')} />}
        </div>
      </div>
      {page?.hasAfter && (
        <div className="search-chat__latest"><Button onClick={() => void loadInitial()}>New messages</Button></div>
      )}
      {renaming && chat && <RenameChat c={chat} onClose={() => setRenaming(false)} onDone={setChat} />}
      {trashing && chat && (
        <ConfirmDialog title={`Move ${chat.name} to Trash?`} danger confirmLabel="Move to Trash" body="Restore brings it back."
                       onClose={() => setTrashing(false)} onConfirm={async () => { await setTrashed(true); setTrashing(false) }} />
      )}
      {deleting && chat && (
        <ConfirmDialog title={`Delete ${chat.name} permanently?`} confirmLabel="Delete permanently" danger body="This cannot be undone."
                       onClose={() => setDeleting(false)}
                       onConfirm={async () => {
                         try { await kbApi.deleteChat(kb.id, chatId) } catch (e) { return errorText(e) }
                         nav.go({ section: 'chats', kb: kb.id }, { replace: true })
                       }} />
      )}
      {person && <PersonRename kb={kb.id} person={person} onClose={() => setPerson(null)}
                               onDone={() => { setPerson(null); void loadInitial(page?.msgs[Math.floor(msgs.length / 2)]?.id) }} />}
      {versions && (
        <Dialog title="Versions" size="md" onClose={() => setVersions(null)} actions={<Button onClick={() => setVersions(null)}>Close</Button>}>
          <div className="search-chat__versions">
            {versions.edits.length === 0 && <EmptyState compact icon={History} title="No earlier versions" />}
            {versions.edits.map((e, i) => <Message key={i} name={listTime(e.ts)}>{e.text || 'Attachment'}</Message>)}
            <Message name="Current version">{versions.text}</Message>
          </div>
        </Dialog>
      )}
    </div>
  )
}

// how long a message you jumped to stays marked (tokens.css --dur-highlight)
const HIGHLIGHT_MS = 2400

const MEDIA: Record<MediaType, { label: string; icon: LucideIcon }> = {
  photo: { label: 'Photo', icon: Image }, video: { label: 'Video', icon: Video }, voice: { label: 'Voice message', icon: Mic },
  audio: { label: 'Audio', icon: Music }, document: { label: 'File', icon: FileText }, sticker: { label: 'Sticker', icon: Sticker },
  gif: { label: 'GIF', icon: ImagePlay }, location: { label: 'Location', icon: MapPin }, contact: { label: 'Contact', icon: Contact },
  file: { label: 'File', icon: File },
}

function ChatMessage({ m, kb, group, highlight, onOriginal, onPerson, onVersions }: {
  m: ArchiveMessage; kb: string; group: boolean; highlight: boolean
  onOriginal: (id: number) => void
  onPerson: (p: { id: number; name: string }) => void
  onVersions: (m: ArchiveMessage) => void
}) {
  const name = useName()
  const nav = useNav()
  if (m.kind === 'system') {
    return <div data-mid={m.id} className={highlight ? 'search-chat__hit' : undefined}>
      <EventLine quiet icon={Info} meta={<span title={fullDate(m.ts)}>{clock(m.ts)}</span>}>{m.text || 'System notice'}</EventLine>
    </div>
  }
  const who = m.from_me ? 'You' : m.sender?.name ?? ''
  const items: MenuItem[] = [
    { label: 'Copy', icon: Copy, onSelect: () => copied(m.text) },
    { label: 'Copy message link', icon: Link, onSelect: () => void copyMessageLink(m.ref).then(ok => ok && toast({ text: 'Copied' })) },
    { label: `Ask ${name || 'Celine'}`, icon: BookHeart, onSelect: () => askCeline(nav, '', [m.ref]) },
    ...(m.reply_to ? [{ label: 'Go to the original message', icon: CornerUpLeft, onSelect: () => onOriginal(m.reply_to!.id) }] : []),
    ...(m.edited ? [{ label: 'Versions', icon: History, onSelect: () => onVersions(m) }] : []),
    ...(m.sender && !m.from_me ? ['sep' as const, { label: `Rename ${m.sender.name}`, icon: Pencil, onSelect: () => onPerson({ id: m.sender!.id, name: m.sender!.name }) }] : []),
  ]
  const media = m.media ? MEDIA[m.media.type] ?? MEDIA.file : null
  const files = m.media?.files ?? []
  const photos = files.filter(isPicture)
  const others = files.filter(f => !isPicture(f))
  const gallery = photos.map(f => ({ src: kbApi.mediaUrl(kb, m.id, f), name: m.media?.caption || f.name || media?.label || 'Photo' }))
  const text = chatText(m.kind === 'media' ? (m.media?.caption || m.text) : m.text)
  const Icon = media?.icon
  return (
    <div data-mid={m.id} className={cx(highlight && 'search-chat__hit', highlight && m.from_me && 'search-chat__msg--mine')}>
      <Message mine={m.from_me} face={m.from_me ? undefined : <Avatar name={who} seed={m.sender?.id} size={28} />}
               name={!m.from_me && group ? who : undefined}
               time={<span title={fullDate(m.ts)}>{clock(m.ts)}</span>} menu={items}
               head={m.edited || m.deleted_at ? <>{m.edited && <span>Edited</span>}{m.deleted_at && <span title={fullDate(m.deleted_at)}>Deleted</span>}</> : undefined}
               before={(m.reply_to || photos.length > 0 || others.length > 0) && <>
                 {m.reply_to && <Quote who={m.reply_to.sender} onClick={() => onOriginal(m.reply_to!.id)}>{chatText(m.reply_to.text) || 'Attachment'}</Quote>}
                 {photos.length > 0 && (
                   <div className="search-chat__photos">
                     {gallery.map((g, i) => <Picture key={g.src} src={g.src} name={g.name} gallery={gallery} at={i} ratio="4 / 3" />)}
                   </div>
                 )}
                 {others.map(f => (
                   <Button key={f.sha256} size="sm" icon={Icon ?? File} href={kbApi.mediaUrl(kb, m.id, f)}>
                     {`${f.name ?? media?.label ?? 'File'}${f.size != null ? `, ${bytes(f.size)}` : ''}`}
                   </Button>
                 ))}
               </>}>
        {media && files.length === 0 && Icon ? <span className="search-chat__media"><Icon size={ICON.sm} aria-hidden />{text || media.label}</span>
          : text ? <span className="search-chat__text">{text}</span> : null}
      </Message>
    </div>
  )
}

function PersonRename({ kb, person, onClose, onDone }: { kb: string; person: { id: number; name: string }; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(person.name)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const save = async (value: string | null) => {
    setSaving(true)
    try { await kbApi.patchPerson(kb, person.id, value); onDone() } catch (e) { setError(errorText(e)) } finally { setSaving(false) }
  }
  return (
    <Dialog title={`Rename ${person.name}`} size="sm" onClose={onClose} dismissable={!saving}
            actions={<><Button disabled={saving} onClick={() => void save(null)}>Use their own name</Button>
              <Button variant="primary" loading={saving} onClick={() => void save(name.trim() || null)}>Save</Button></>}>
      <form onSubmit={e => { e.preventDefault(); void save(name.trim() || null) }}>
        <Input autoFocus value={name} onChange={e => setName(e.target.value)} aria-label="Name" />
      </form>
      {error && <InlineError message={error} compact />}
    </Dialog>
  )
}
