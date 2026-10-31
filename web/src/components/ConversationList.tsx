/* A place's conversations, as one virtual list:

     Running, Pinned, Today, Yesterday, ...   the server's group of each row (`group`); a
                    date group (`dated`) folds by its head, kept on this device;
                    each row a real link with the title, its last line, a bare time, a
                    dot for a failure or a wait (the server's `state`); a running row
                    shows the server's step words (the dots while it writes) and Stop
     sub-agents     always under the conversation that started them, no fold
     Found in messages   on a search: the titles the server ranked first, then the
                    messages with their snippet and the matched words marked

   A list may put what runs on top (`working`). On a shelf (Archive or Trash) the list is that shelf's
   rows, each with Restore and Move to Trash, or Restore and Delete permanently. With a
   `picker` the rows can be selected (ui/Select); a sub-agent goes with its parent. The
   row menu opens from "..." and on right-click; on touch the row has no "...", a long
   press opens it (Select is in it). */
import { useMemo, type MouseEvent, type ReactNode } from 'react'
import {
  Archive, ArchiveRestore, Bot, Download, Flag, Heart, Inbox, ListChecks, MessageCircle, MoreHorizontal, PenLine, Pin, PinOff, Square, Trash2,
  type LucideIcon,
} from 'lucide-react'
import { api, type ConversationSummary } from '../lib/api'
import type { ChatHit } from '../lib/chat'
import { listTime } from '../lib/format'
import { usePref } from '../lib/prefs'
import {
  Button, coarse, Dot, Dots, EmptyState, EventLine, ICON, IconButton, InlineEdit, ListRow, ListSection, MenuButton, ShelfRow, Snippet,
  VirtualList, useContextMenu, type DotTone, type MenuItem, type Picker, type Shelf,
} from '../ui'

/** one thing at work, on top of a list without the server's groups */
export type WorkItem = {
  key: string
  /** the conversation it opens */
  open: string
  /** the path that opens it, the server's (homes.py) */
  link: string
  name: string
  icon: LucideIcon
  /** what it does now, in words */
  step: string
  tone: 'running' | 'warn'
  stopLabel: string
  onStop: () => void
}

/** a group of rows under a head of its own, in place of the day groups */
export type ListGroup = { key: string; head: ReactNode; items: ConversationSummary[] }

/** the state a row's dot says, or none */
export type RowState = 'running' | 'failed' | 'needs_you'
/** each place's loudest state, for the rail */
export type Work = Partial<Record<'celine', RowState>>

/** the glyph of a conversation, the same on a row and in a head */
export const kindIcon = (c: Pick<ConversationSummary, 'kind'>): LucideIcon => (c.kind === 'mission' ? Flag : Heart)

const TONE: Record<RowState, DotTone> = { running: 'running', failed: 'error', needs_you: 'warn' }
const STATE: Record<string, RowState> = { running: 'running', error: 'failed', warn: 'needs_you' }
/** a row's state as the rail counts it: the server's, else the caller's */
export const rowState = (c: ConversationSummary): RowState | null => (c.state ? STATE[c.state.tone] ?? null : null)

type Row =
  | { type: 'group'; key: string; label: string; fold?: number }
  | { type: 'head'; key: string; node: ReactNode }
  | { type: 'work'; key: string; w: WorkItem }
  | { type: 'away'; key: string; text: string }
  | { type: 'conversation'; key: string; c: ConversationSummary; sub?: boolean }
  | { type: 'gone'; key: string; c: ConversationSummary }
  | { type: 'hit'; key: string; h: ChatHit }

const HEIGHT = { group: 36, head: 56, work: 68, away: 44, conversation: 68, gone: 68, hit: 76 } as const

const NO_ROWS: ConversationSummary[] = []
const NO_HITS: ChatHit[] = []
const NO_WORK: WorkItem[] = []
const NO_HEADS: { key: string; node: ReactNode }[] = []
const byTitle = (c: ConversationSummary, q: string) => c.title.toLowerCase().includes(q)

export function ConversationList({
  conversations, archived = NO_ROWS, deleted = NO_ROWS, shelf = 'list', picker, onRestore, hits = NO_HITS, titles, query,
  selected, renaming, onOpen, onRename, onStartRename, onPin, onArchive, onDelete, onStop, onExport, stateOf, working = NO_WORK,
  away = null, onAway, onClearSearch, groups, matches = byTitle, leadingOf, label = 'Conversations', deletedHeads = NO_HEADS,
}: {
  conversations: ConversationSummary[]
  /** the archived ones, on the Archive shelf */
  archived?: ConversationSummary[]
  /** the deleted ones, on the Trash shelf */
  deleted?: ConversationSummary[]
  shelf?: Shelf
  picker?: Picker
  onRestore?: (c: ConversationSummary) => void
  /** more in the trash before the conversations, each with its Restore */
  deletedHeads?: { key: string; node: ReactNode }[]
  /** messages that match the search, as the server ranked them */
  hits?: ChatHit[]
  /** the ids whose title matches the search, as the server ranked them; left out: `matches` */
  titles?: string[]
  query: string
  selected: string | null
  renaming: string | null
  /** a plain click on a row; the link itself handles middle-click and new tabs */
  onOpen: (id: string) => void
  onRename: (id: string, title: string) => void
  onStartRename: (id: string | null) => void
  onPin?: (c: ConversationSummary) => void
  onArchive?: (c: ConversationSummary, archived: boolean) => void
  /** Move to Trash (the page asks first) */
  onDelete: (id: string) => void
  /** stop the reply being written */
  onStop: (id: string) => void
  /** the conversation as a file */
  onExport?: (c: ConversationSummary) => void
  /** a list without the server's `state`: the caller's */
  stateOf?: (c: ConversationSummary) => RowState | null
  /** a list without the server's groups: what runs, on top */
  working?: WorkItem[]
  /** Clear search, where nothing matches */
  onClearSearch?: () => void
  /** "While you were away: ..." after a reconnect, until clicked */
  away?: string | null
  onAway?: () => void
  /** the place's own groups of the (searched) conversations; default: the server's, else the days */
  groups?: (list: ConversationSummary[], filtering: boolean) => ListGroup[]
  matches?: (c: ConversationSummary, q: string) => boolean
  leadingOf?: (c: ConversationSummary) => ReactNode
  label?: string
}) {
  const q = query.trim().toLowerCase()
  // the date groups folded on this device, by label
  const [folded, setFolded] = usePref<string[]>('conversations:folded', [])
  const rows = useMemo<Row[]>(() => {
    const ranked = (list: ConversationSummary[]) => {
      if (!q) return list
      if (!titles) return list.filter(c => matches(c, q))
      const byId = new Map(list.map(c => [c.id, c]))
      return titles.flatMap(id => byId.get(id) ?? [])
    }
    const out: Row[] = []
    if (shelf === 'trash') {
      for (const h of deletedHeads) out.push({ type: 'head', key: `dh:${h.key}`, node: h.node })
      for (const c of ranked(deleted)) out.push({ type: 'gone', key: `d:${c.id}`, c })
      return out
    }
    if (shelf === 'archive') {
      for (const c of ranked(archived)) out.push({ type: 'gone', key: `a:${c.id}`, c })
      return out
    }
    if (!q && (working.length || away)) {
      out.push({ type: 'group', key: 'g:running', label: 'Running' })
      if (away) out.push({ type: 'away', key: 'away', text: away })
      for (const w of working) out.push({ type: 'work', key: `w:${w.key}`, w })
    }
    const push = (c: ConversationSummary) => {
      out.push({ type: 'conversation', key: c.id, c })
      for (const kid of c.agents) out.push({ type: 'conversation', key: kid.id, c: kid, sub: true })
    }
    const inWork = new Set(working.map(w => w.open))
    const listed = ranked(q ? conversations : conversations.filter(c => !inWork.has(c.id)))
    if (q) {
      listed.forEach(push)
    } else if (groups) {
      for (const g of groups(listed, false)) {
        out.push({ type: 'head', key: `h:${g.key}`, node: g.head })
        g.items.forEach(push)
      }
    } else {
      let last = ''
      for (const c of listed) {
        if (c.group !== last) {
          last = c.group ?? ''
          out.push({ type: 'group', key: `g:${last}`, label: last,
                     fold: c.dated ? listed.filter(x => x.group === last).length : undefined })
        }
        if (!c.dated || !folded.includes(last)) push(c)
      }
    }
    if (q && hits.length) {
      out.push({ type: 'group', key: 'g:hits', label: 'Found in messages' })
      hits.forEach(h => out.push({ type: 'hit', key: `h:${h.message_id}`, h }))
    }
    return out
  }, [conversations, archived, deleted, shelf, hits, titles, q, working, away, groups, matches, deletedHeads, folded])
  if (picker) {
    picker.keys.current = rows.flatMap(r => (r.type === 'gone' || (r.type === 'conversation' && !r.sub) ? [r.c.id] : []))
  }

  const ctx = useContextMenu()
  const touch = useMemo(coarse, [])
  const actions = (c: ConversationSummary): MenuItem[] => {
    const running = c.state ? c.state.tone === 'running' : !!c.active_state
    return [
      { label: 'Open', icon: MessageCircle, onSelect: () => onOpen(c.id) },
      // after App's focus hand-back from the pressed item (App.tsx), which would blur the field
      { label: 'Rename', icon: PenLine, onSelect: () => { setTimeout(() => onStartRename(c.id)) } },
      ...(onPin && !c.agent_of ? [{ label: c.pinned ? 'Unpin' : 'Pin', icon: c.pinned ? PinOff : Pin, onSelect: () => onPin(c) }] : []),
      ...(onExport ? [{ label: 'Export', icon: Download, onSelect: () => onExport(c) }] : []),
      ...(running ? [{ label: 'Stop', icon: Square, onSelect: () => onStop(c.id) }] : []),
      'sep' as const,
      // a sub-agent goes with its parent
      ...(c.agent_of || !onArchive ? [] : [c.archived_at
        ? { label: 'Restore', icon: ArchiveRestore, onSelect: () => onArchive(c, false) }
        : { label: 'Archive', icon: Archive, disabled: running, title: running ? 'Stop the reply first' : undefined,
            onSelect: () => onArchive(c, true) }]),
      ...(picker && !c.agent_of ? [{ label: 'Select', icon: ListChecks, onSelect: () => picker.start(c.id) }] : []),
      'sep' as const,
      { label: 'Move to Trash', icon: Trash2, danger: true, disabled: running,
        title: running ? 'Stop the reply first' : undefined, onSelect: () => onDelete(c.id) },
    ]
  }
  const leading = (c: ConversationSummary) => {
    if (leadingOf) return leadingOf(c)
    if (c.kind === 'chat') return undefined
    const Icon = kindIcon(c)
    return <Icon size={ICON.lg} aria-hidden />
  }

  if (q && rows.length === 0) {
    return <EmptyState title="No conversations match"
                       action={onClearSearch && <Button onClick={onClearSearch}>Clear search</Button>} />
  }
  if (shelf !== 'list' && rows.length === 0) {
    return <EmptyState icon={shelf === 'trash' ? Trash2 : Inbox} title={shelf === 'trash' ? 'Trash is empty' : 'Archive is empty'} />
  }

  return (
    <>
      <VirtualList items={rows} getKey={r => r.key} estimate={i => HEIGHT[rows[i]!.type]} inset={8} label={label}
                   renderRow={r => {
                     if (r.type === 'group') {
                       if (r.fold == null) return <ListSection label={r.label} />
                       const open = !folded.includes(r.label)
                       return <ListSection label={r.label} open={open} count={r.fold}
                                           onToggle={() => setFolded(open ? [...folded, r.label] : folded.filter(l => l !== r.label))} />
                     }
                     if (r.type === 'head') return r.node
                     if (r.type === 'away') return <ListRow density="sm" title={r.text} tooltip={r.text} onClick={onAway} />
                     if (r.type === 'work') {
                       const w = r.w
                       return (
                         <ListRow density="lg" href={w.link} selected={w.open === selected}
                                  leading={<w.icon size={ICON.lg} aria-hidden />} title={w.name} tooltip={w.name}
                                  subtitle={<EventLine running quiet>{w.step}</EventLine>}
                                  onClick={click(() => onOpen(w.open))} trailingAlways
                                  trailing={<IconButton icon={Square} fill size="sm" label={w.stopLabel} onClick={w.onStop} />} />
                       )
                     }
                     if (r.type === 'hit') {
                       const h = r.h
                       return (
                         <ListRow density="lg" href={h.link} title={h.title || 'Untitled'} tooltip={h.title}
                                  body={<Snippet text={h.snippet} words={h.marks.map(([a, b]) => h.snippet.slice(a, b))} />}
                                  onClick={click(() => onOpen(h.conversation_id))} />
                       )
                     }
                     if (r.type === 'gone') {
                       const c = r.c
                       return (
                         <ShelfRow shelf={shelf === 'archive' ? 'archive' : 'trash'} density="lg" leading={leading(c)}
                                   title={c.title || 'Untitled'} pick={picker?.row(c.id)}
                                   onClick={shelf === 'archive' ? () => onOpen(c.id) : undefined}
                                   onSelect={picker && (() => picker.start(c.id))}
                                   subtitle={listTime(c.updated_at)}
                                   onRestore={() => (shelf === 'archive' ? onArchive?.(c, false) : onRestore?.(c))}
                                   onTrash={() => onDelete(c.id)} onDelete={() => api.purgeConversation(c.id)} />
                       )
                     }
                     const c = r.c
                     const name = c.title || 'Untitled'
                     if (renaming === c.id) {
                       return <InlineEdit value={c.title} label="Rename" onSave={t => onRename(c.id, t)} onCancel={() => onStartRename(null)} />
                     }
                     const menu = actions(c)
                     const state = c.state ?? null
                     const own = stateOf?.(c) ?? null
                     const running = state ? state.tone === 'running' : own === 'running'
                     const tone = running ? undefined : state ? TONE[STATE[state.tone]!] : own ? TONE[own] : undefined
                     if (r.sub) {
                       return (
                         <ListRow depth={1} icon={Bot} href={c.link} selected={c.id === selected} title={name} tooltip={c.last_line || name}
                                  dot={running ? 'running' : state ? TONE[STATE[state.tone]!] : own ? TONE[own] : undefined}
                                  meta={listTime(c.updated_at)} onClick={click(() => onOpen(c.id))}
                                  onContextMenu={e => ctx.open(e, menu, name)} press={touch ? ctx.press(() => menu, name) : undefined}
                                  trailing={!touch && <MenuButton icon={MoreHorizontal} label="More" size="sm" items={menu} />} />
                       )
                     }
                     return (
                       <ListRow density="lg" href={c.link} selected={c.id === selected} pick={touch && !picker?.on ? undefined : picker?.row(c.id)}
                                leading={leading(c)} title={name} tooltip={name}
                                subtitle={running
                                  ? state?.words ? <EventLine running quiet>{state.words}</EventLine> : <Dots />
                                  : c.last_line || undefined}
                                // its state dot sits beside its time, so every title starts at one edge
                                meta={running ? undefined : tone ? <Dot tone={tone} text={listTime(c.updated_at)} /> : listTime(c.updated_at)}
                                onClick={click(() => onOpen(c.id))} onContextMenu={e => ctx.open(e, menu, name)}
                                press={touch ? ctx.press(() => menu, name) : undefined} trailingAlways={running}
                                trailing={running
                                  ? <IconButton icon={Square} fill size="sm" label="Stop" onClick={() => onStop(c.id)} />
                                  : !touch && <MenuButton icon={MoreHorizontal} label="More" size="sm" items={menu} />} />
                     )
                   }} />
      {ctx.menu}
    </>
  )
}

/* A plain left click stays in the app through `fn`; any other click is the link's. */
export function click(fn: () => void) {
  return (e?: MouseEvent) => {
    if (!e || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    fn()
  }
}
