/* Terminals: its terminals and yours, then the ended ones day by day; the log of every
   command (Commands); one terminal is views/Terminal.tsx in the same place. The place's
   sidebar holds Terminals and Commands with their counts and its open terminals as Recent.
   A command opens its result with Open terminal, Reply (into the terminal page's message
   box) and Open conversation. With ?mission=<id> the place shows that mission's commands.
   URL: /terminals, /terminals?mission=<conversation id>. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  ArrowUpRight, CircleStop, Copy, CornerUpLeft, Flag, History, ListChecks, MessageCircle, MoreHorizontal, Plus, RotateCcw, ScrollText, SearchX,
  SquareTerminal, Trash2, User, X,
} from 'lucide-react'
import { useName } from '../lib/name'
import { clock, count, dayLabel, listTime } from '../lib/format'
import { format, useNav } from '../lib/nav'
import { bulkApi } from '../lib/api'
import {
  commandWords, isShellEvent, isYours, outcomeWords, rowText, stateTone, stateWord,
  terminalsApi, type CommandRow, type From, type ResultFilter, type TerminalRow,
} from '../lib/terminals'
import { useAction } from '../lib/useAction'
import { useResource } from '../lib/useResource'
import {
  Button, ConfirmDialog, copied, Dialog, Dot, GroupHead, IconButton, InlineError, ListRow, ListSection, Loaded, MenuButton, Notice, Place,
  PlaceHead, SelectBar, ShelfRow, Toolbar, useContextMenu, usePicker, VirtualList, type BulkAction, type MenuItem, type Shelf,
} from '../ui'
import './Terminals.css'

const terminalsNoun = (n: number) => count(n, 'terminal')
const commandsNoun = (n: number) => count(n, 'command')
const FROM: Record<Exclude<From, 'all'>, string> = { conversations: 'Conversations', missions: 'Missions', sub_agents: 'Sub-agents', you: 'You' }
const KIND: Record<string, Exclude<From, 'all'>> = { conversation: 'conversations', mission: 'missions', sub_agent: 'sub_agents' }
/** who a terminal is from: yours, else the kind of conversation that opened it */
const fromOf = (t: TerminalRow): Exclude<From, 'all'> | null => (isYours(t) ? 'you' : t.conversation ? KIND[t.conversation.kind] ?? 'conversations' : null)
const PAGE = 100

/** the page the place shows next time it mounts (the sidebar of a terminal's page asks for Commands) */
let startOn: 'terminals' | 'commands' = 'terminals'

/* ── the place: its sidebar, the same on the list and on a terminal ── */
export function TerminalsPlace({ page, onPage, open, children }: {
  page?: 'terminals' | 'commands'
  /** a sidebar link; left out, it goes to the list first */
  onPage?: (page: 'terminals' | 'commands') => void
  /** the terminal on screen */
  open?: string
  children: ReactNode
}) {
  const nav = useNav()
  const list = useResource(() => terminalsApi.list(), [], { events: ev => ev.type === 'terminal.state' })
  const log = useResource(() => terminalsApi.commands({ limit: 1 }), [], { events: isShellEvent })
  const rows = list.data?.terminals ?? []
  const create = useAction(async () => { const r = await terminalsApi.create(); nav.go({ section: 'terminals', terminal: r.id }) })
  const go = (p: 'terminals' | 'commands') => {
    if (onPage) onPage(p)
    else { startOn = p; nav.go({ section: 'terminals' }) }
  }
  return (
    <Place title="Terminals"
           action={<IconButton icon={Plus} label="New terminal" disabled={!!list.data?.down || create.busy}
                               title={list.data?.down ?? create.error ?? undefined} onClick={() => void create.run()} />}
           side={<>
             <ListRow density="sm" icon={SquareTerminal} title="Terminals" meta={list.data && rows.length} selected={page === 'terminals'} onClick={() => go('terminals')} />
             <ListRow density="sm" icon={ScrollText} title="Commands" meta={log.data?.total} selected={page === 'commands'} onClick={() => go('commands')} />
             {rows.some(t => !t.ended_at) && <ListSection label="Recent" />}
             {rows.filter(t => !t.ended_at).map(t => (
               <ListRow key={t.id} density="sm" leading={<Dot tone={stateTone(stateWord(t))} />} title={t.name} selected={t.id === open}
                        href={format({ section: 'terminals', terminal: t.id })} />
             ))}
           </>}>
      {children}
    </Place>
  )
}

export function Terminals() {
  const nav = useNav()
  // the mission the URL names; while another place is on screen, the one it showed last
  const last = useRef<string | undefined>(undefined)
  if (nav.route.section === 'terminals') last.current = nav.route.mission
  const mission = last.current
  const [page, setPage] = useState<'terminals' | 'commands'>(() => (mission ? 'commands' : startOn))
  useEffect(() => { startOn = 'terminals' }, [])
  useEffect(() => { if (mission) setPage('commands') }, [mission])
  return (
    <TerminalsPlace page={page} onPage={p => { setPage(p); if (mission) nav.go({ section: 'terminals' }) }}>
      {page === 'commands' ? <Commands mission={mission} /> : <TerminalsList />}
    </TerminalsPlace>
  )
}

/* ── its terminals and yours, the ended ones day by day; the Trash as a shelf ── */
function TerminalsList() {
  const nav = useNav()
  const her = useName()
  const [shelf, setShelf] = useState<Shelf>('list')
  const [q, setQ] = useState('')
  const [state, setState] = useState('all')
  const [from, setFrom] = useState<Exclude<From, 'all'> | null>(null)
  const [sort, setSort] = useState('Recent')
  const [closing, setClosing] = useState<TerminalRow | null>(null)
  const picker = usePicker(shelf)
  const ctx = useContextMenu()
  const terminals = useResource(() => terminalsApi.list(), [], { events: ev => ev.type === 'terminal.state' })
  const trashed = useResource(() => terminalsApi.list(undefined, true), [], { enabled: shelf === 'trash', events: ev => ev.type === 'terminal.state' })
  const act = useAction(async (fn: () => Promise<unknown>) => { await fn(); terminals.reload(); trashed.reload() })
  const create = useAction(async () => { const r = await terminalsApi.create(); nav.go({ section: 'terminals', terminal: r.id }) })
  const [clearing, setClearing] = useState(false)

  const all = terminals.data?.terminals ?? []
  const down = terminals.data?.down ?? null
  const needle = q.trim().toLowerCase()
  const lastAt = (t: TerminalRow) => t.ended_at ?? t.opened_at
  const shown = all.filter(t => (state === 'all' || (state === 'ended') === !!t.ended_at) && (!from || fromOf(t) === from)
                                && t.name.toLowerCase().includes(needle))
    .sort((a, b) => (sort === 'Name' ? a.name.localeCompare(b.name) : lastAt(b).localeCompare(lastAt(a))))
  type Row = { key: string; head: string; icon: typeof User; n: number } | { key: string; t: TerminalRow }
  const group = (title: string, icon: typeof User, these: TerminalRow[]): Row[] =>
    these.length ? [{ key: `g:${title}`, head: title, icon, n: these.length }, ...these.map(t => ({ key: t.id, t }))] : []
  const ended = shown.filter(t => t.ended_at)
  const days = [...new Set(ended.map(t => dayLabel(Date.parse(t.ended_at!))))]
  const rows: Row[] = [
    ...group(her || 'Celine', SquareTerminal, shown.filter(t => !t.ended_at && !isYours(t))),
    ...group('You', User, shown.filter(t => !t.ended_at && isYours(t))),
    ...days.flatMap(d => group(`Ended ${d === 'Today' || d === 'Yesterday' ? d.toLowerCase() : d}`, History, ended.filter(t => dayLabel(Date.parse(t.ended_at!)) === d))),
  ]
  const gone = (trashed.data?.terminals ?? []).filter(t => t.name.toLowerCase().includes(needle))
  picker.keys.current = shelf === 'trash' ? gone.map(t => t.id) : shown.map(t => t.id)

  const menu = (t: TerminalRow): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => nav.go({ section: 'terminals', terminal: t.id }) },
    ...(t.conversation && !isYours(t) ? [{ label: 'Open conversation', icon: MessageCircle, onSelect: () => nav.goPath(t.conversation!.link) }] : []),
    ...(!t.ended_at && !isYours(t) && stateWord(t) === 'Running'
      ? [{ label: 'Stop', icon: CircleStop, detail: t.live?.program ?? undefined, onSelect: () => void act.run(() => terminalsApi.control(t.id, 'stop')) }] : []),
    'sep',
    { label: 'Select', icon: ListChecks, onSelect: () => picker.start(t.id) },
    t.ended_at ? { label: 'Move to Trash', icon: Trash2, danger: true, onSelect: () => void act.run(() => terminalsApi.trash(t.id)) }
      : { label: 'Close terminal', icon: X, danger: true, onSelect: () => setClosing(t) },
  ]
  const bulk: BulkAction[] = shelf === 'trash'
    ? [{ label: 'Restore', icon: RotateCcw, done: 'restored', run: keys => bulk_('restore', keys) },
       { label: 'Delete permanently', icon: Trash2, danger: true, done: 'deleted', run: keys => bulk_('purge', keys),
         ask: n => ({ title: `Delete ${terminalsNoun(n)} permanently?`, confirm: 'Delete permanently', body: 'This cannot be undone.' }) }]
    : [{ label: 'Move to Trash', icon: Trash2, danger: true, done: 'moved', run: keys => bulk_('trash', keys),
         ask: n => ({ title: `Move ${terminalsNoun(n)} to Trash?`, confirm: 'Move to Trash' }) }]
  async function bulk_(action: 'trash' | 'restore' | 'purge', keys: string[]) {
    const r = await bulkApi.run('terminals', action, keys)
    terminals.reload(); trashed.reload()
    return r
  }

  const draw = (r: Row) => {
    if ('head' in r) return <GroupHead icon={r.icon} title={r.head} count={terminalsNoun(r.n)} />
    const t = r.t
    const word = stateWord(t)
    const f = fromOf(t)
    const items = menu(t)
    return (
      <ListRow dot={stateTone(word)} title={t.name} muted={!!t.ended_at} href={format({ section: 'terminals', terminal: t.id })}
               subtitle={subtitleOf(t)}
               column={t.conversation && !isYours(t) ? <><span>{t.conversation.title}</span>{f && f !== 'conversations' && <span>{FROM[f]}</span>}</> : ''}
               cell={word === 'Idle' || word === 'Ended' ? undefined : word} meta={t.ended_at ? clock(Date.parse(t.ended_at)) : listTime(lastAt(t))} pick={picker.row(t.id)} onContextMenu={e => ctx.open(e, items, t.name)}
               trailing={!picker.on && <MenuButton icon={MoreHorizontal} label="More" items={items} size="sm" />} />
    )
  }

  const nothing = shelf === 'list' ? !!terminals.data && all.length === 0 : !!trashed.data && trashed.data.terminals.length === 0
  return (
    <div className="list-page">
      <PlaceHead level={1} title="Terminals" count={shelf === 'list' ? all.length : trashed.data?.terminals.length}
                 search={{ value: q, onChange: setQ, placeholder: 'Search terminals' }} picker={nothing ? undefined : picker}
                 shelf={{ trash: true, value: shelf, onChange: setShelf }}
                 more={[{ label: 'Move ended terminals to Trash', icon: Trash2, danger: true, disabled: !all.some(t => t.ended_at), onSelect: () => setClearing(true) }]}
                 newLabel="New terminal" newDisabled={down ?? create.busy} onNew={() => void create.run()} />
      {(down || picker.on || (shelf === 'list' && !nothing)) && (
        <div className="list-page__top">
          {down && <Notice tone="warn" className="terms-notice" action={{ label: 'Open Setup', onClick: () => nav.go({ section: 'welcome', step: 'shellbox' }) }}>{down}</Notice>}
          {picker.on ? <SelectBar picker={picker} noun={terminalsNoun} actions={bulk} />
            : (shelf === 'list' && !nothing) && <Toolbar state={{ label: 'State', value: state, onChange: setState, options: [
                        { value: 'all', label: 'All', count: all.length },
                        { value: 'open', label: 'Running', count: all.filter(t => !t.ended_at).length },
                        { value: 'ended', label: 'Ended', count: all.filter(t => t.ended_at).length }] }}
                       filters={[{ label: 'From', icon: MessageCircle, picked: from ? FROM[from] : undefined, onClear: () => setFrom(null),
                                   items: (Object.keys(FROM) as Exclude<From, 'all'>[]).map(k => ({ label: FROM[k], current: k === from, onSelect: () => setFrom(k) })) }]}
                       sort={{ label: 'Sort', value: sort, options: [{ value: 'Recent', label: 'Recent' }, { value: 'Name', label: 'Name' }], onChange: setSort }} />}
        </div>
      )}
      {(create.error || act.error) && <div className="terms-error"><InlineError compact message={create.error ?? act.error} /></div>}
      {shelf === 'trash' ? (
        <Loaded page res={trashed} icon={Trash2} empty={nothing && { title: 'Trash is empty' }}>
          {() => (
            <div className="list-page__rows">
              <VirtualList items={gone} estimate={56} getKey={t => t.id} label="Trash"
                           renderRow={t => <ShelfRow title={t.name} subtitle={endedLine(t)} pick={picker.row(t.id)} onSelect={() => picker.start(t.id)}
                                                     onClick={() => nav.go({ section: 'terminals', terminal: t.id })}
                                                     onRestore={() => void act.run(() => terminalsApi.restore(t.id))}
                                                     onDelete={() => act.run(() => terminalsApi.remove(t.id))} />} />
            </div>
          )}
        </Loaded>
      ) : (
        <Loaded page res={terminals} icon={SquareTerminal}
                empty={nothing ? { title: 'No terminals yet', action: !down && <Button variant="primary" icon={SquareTerminal} onClick={() => void create.run()}>New terminal</Button> }
                  : rows.length === 0 && { icon: SearchX, title: 'No terminals match', action: <Button onClick={() => { setQ(''); setFrom(null); setState('all') }}>Clear search</Button> }}>
          {() => (
            <div className="list-page__rows">
              <VirtualList items={rows} estimate={i => { const r = rows[i]!; return 'head' in r ? 56 : subtitleOf(r.t) ? 68 : 44 }} getKey={r => r.key} renderRow={draw} label="Terminals"
                           resetOn={`${q}|${state}|${from}|${sort}`} />
            </div>
          )}
        </Loaded>
      )}
      {ctx.menu}
      {closing && <CloseTerminal t={closing} onClose={() => setClosing(null)} onClosed={terminals.reload} />}
      {clearing && <ConfirmDialog title="Move ended terminals to Trash?" confirmLabel="Move to Trash" danger onClose={() => setClearing(false)}
                                  onConfirm={async () => { await terminalsApi.trashEnded(); setClearing(false); terminals.reload() }} />}
    </div>
  )
}

/** when and how it ended, an exit only when it failed (the Trash, where no day head says when) */
function endedLine(t: TerminalRow): string {
  const at = t.ended_at ? listTime(t.ended_at) : ''
  return [t.ended_by === 'reset' ? `Ended by the morning reset ${at}` : `Ended ${at}`, t.exit ? `exit ${t.exit}` : ''].filter(Boolean).join(', ')
}

/** a row's second line: what runs, where it idles, or how it ended */
function subtitleOf(t: TerminalRow): string | undefined {
  if (t.ended_at) return endedHow(t)
  return (stateWord(t) !== 'Idle' && t.live?.program) || t.live?.cwd || undefined
}

/** how it ended, under its day head: the morning reset, an exit only when it failed */
function endedHow(t: TerminalRow): string | undefined {
  return [t.ended_by === 'reset' && 'By the morning reset', t.exit && `exit ${t.exit}`].filter(Boolean).join(', ') || undefined
}

/** Close terminal, asked first: the program in it stops */
export function CloseTerminal({ t, onClose, onClosed }: { t: TerminalRow; onClose: () => void; onClosed: () => void }) {
  return (
    <ConfirmDialog title={`Close terminal ${t.name}?`} confirmLabel="Close terminal" danger
                   body={t.live?.program ? `${t.live.program} stops.` : undefined} onClose={onClose}
                   onConfirm={async () => { try { await terminalsApi.control(t.id, 'close'); onClose(); onClosed() } catch (e) { return e instanceof Error ? e.message : String(e) } }} />
  )
}

/* ── every command in every terminal, newest first, day by day ── */
function Commands({ mission }: { mission?: string }) {
  const nav = useNav()
  const [q, setQ] = useState('')
  const [result, setResult] = useState<ResultFilter>('all')
  const [from, setFrom] = useState<From>('all')
  const [opened, setOpened] = useState<CommandRow | null>(null)
  const [pages, setPages] = useState(1)
  const ctx = useContextMenu()
  useEffect(() => setPages(1), [q, result, from, mission])
  const commands = useResource(async () => {
    const query = { q: q.trim(), from, result, mission, limit: PAGE }
    const out = await terminalsApi.commands(query)
    for (let n = 1; n < pages && out.next; n++) {
      const r = await terminalsApi.commands({ ...query, before: out.next })
      out.commands.push(...r.commands)
      out.next = r.next
    }
    return out
  }, [q, from, result, mission, pages], { events: isShellEvent, debounce: q ? 250 : 0 })
  // the counts of the state row, the server's
  const counts = useResource(async () => {
    const [all, running, failed] = await Promise.all((['all', 'in_progress', 'failed'] as const).map(r =>
      terminalsApi.commands({ from, mission, result: r, limit: 1 }).then(x => x.total)))
    return { all, running, failed }
  }, [from, mission], { events: isShellEvent })
  const rows = commands.data?.commands ?? []
  type Row = { key: string; day: string; n: number } | { key: string; c: CommandRow }
  const dayOf = (c: CommandRow) => dayLabel(Date.parse(c.at))
  const perDay = new Map<string, number>()
  for (const c of rows) perDay.set(dayOf(c), (perDay.get(dayOf(c)) ?? 0) + 1)
  const list: Row[] = rows.flatMap((c, i) => [
    ...(i === 0 || dayOf(rows[i - 1]!) !== dayOf(c) ? [{ key: `d:${dayOf(c)}`, day: dayOf(c), n: perDay.get(dayOf(c))! }] : []),
    { key: c.id, c },
  ])
  const menu = (c: CommandRow): MenuItem[] => [
    ...(c.terminal.id ? [{ label: 'Open terminal', icon: SquareTerminal, onSelect: () => nav.go({ section: 'terminals', terminal: c.terminal.id! }) }] : []),
    ...(c.from.link ? [{ label: 'Open conversation', icon: MessageCircle, onSelect: () => nav.goPath(c.from.link!) }] : []),
    ...(c.from.kind !== 'you' && c.terminal.id ? [{ label: 'Reply', icon: CornerUpLeft, onSelect: () => nav.go({ section: 'terminals', terminal: c.terminal.id!, reply: c.id }) }] : []),
    { label: 'Copy', icon: Copy, onSelect: () => copied(c.text) },
  ]
  const draw = (r: Row) => {
    if ('day' in r) return <GroupHead title={r.day} count={commandsNoun(r.n)} />
    const c = r.c
    const out = outcomeWords(c)
    const items = menu(c)
    return (
      <ListRow dot={out.tone === 'muted' ? 'muted' : out.tone === 'error' ? 'error' : out.tone === 'running' ? 'running' : 'ok'}
               title={<span className="mono">{commandWords(c)}</span>} tooltip={commandWords(c)}
               subtitle={`${c.terminal.name}, ${c.from.kind === 'you' ? 'You' : c.from.title ?? FROM[KIND[c.from.kind] ?? 'conversations']}`}
               cell={out.tone === 'ok' ? undefined : out.words} meta={clock(Date.parse(c.at))} onClick={() => setOpened(c)} onContextMenu={e => ctx.open(e, items, commandWords(c))}
               trailing={<MenuButton icon={MoreHorizontal} label="More" items={items} size="sm" />} />
    )
  }
  const none = !!commands.data && rows.length === 0 && !q.trim() && from === 'all' && result === 'all' && !mission
  return (
    <div className="list-page">
      <PlaceHead level={1} title="Commands" count={commands.data?.total}
                 search={none ? undefined : { value: q, onChange: setQ, placeholder: 'Search commands', loading: commands.loading }} />
      {!none && (
        <div className="list-page__top">
          <Toolbar state={{ label: 'State', value: result, onChange: v => setResult(v as ResultFilter), options: [
                     { value: 'all', label: 'All', count: counts.data?.all }, { value: 'in_progress', label: 'Running', count: counts.data?.running },
                     { value: 'failed', label: 'Failed', count: counts.data?.failed }] }}
                   filters={[
                     { label: 'From', icon: MessageCircle, picked: from !== 'all' ? FROM[from] : undefined, onClear: () => setFrom('all'),
                       items: (Object.keys(FROM) as Exclude<From, 'all'>[]).map(k => ({ label: FROM[k], current: k === from, onSelect: () => setFrom(k) })) },
                     ...(mission ? [{ label: 'Mission', icon: Flag, picked: 'Mission', items: [], onClear: () => nav.go({ section: 'terminals' }) }] : []),
                   ]} />
        </div>
      )}
      <Loaded page res={commands} icon={ScrollText}
              empty={none ? { title: 'No commands yet' }
                : rows.length === 0 && { icon: SearchX, title: 'No commands match', action: <Button onClick={() => { setQ(''); setFrom('all'); setResult('all') }}>Clear search</Button> }}>
        {data => (
          <div className="list-page__rows">
            <VirtualList items={list} estimate={i => ('day' in list[i]! ? 56 : 68)} getKey={r => r.key} renderRow={draw} label="Commands"
                         resetOn={`${q}|${from}|${result}|${mission}`}
                         onEndReached={() => { if (data.next && !commands.loading) setPages(n => n + 1) }} />
          </div>
        )}
      </Loaded>
      {ctx.menu}
      {opened && <CommandDialog c={opened} onClose={() => setOpened(null)} />}
    </div>
  )
}

/** a command's whole result; on the terminal page Reply quotes it into the message box */
export function CommandDialog({ c, onClose, onReply }: {
  c: CommandRow; onClose: () => void; onReply?: (c: CommandRow) => void
}) {
  const tid = c.terminal.id
  const text = rowText(c)
  const reply = c.from.kind !== 'you' && tid
  return (
    <Dialog size="lg" title={c.terminal.name} onClose={onClose}
            actions={<>
              {c.from.link && (
                <Button href={c.from.link}>Open conversation</Button>
              )}
              {!onReply && tid && <Button href={format({ section: 'terminals', terminal: tid })}>Open terminal</Button>}
              {reply && (onReply
                ? <Button variant="primary" icon={CornerUpLeft} onClick={() => { onReply(c); onClose() }}>Reply</Button>
                : <Button variant="primary" icon={CornerUpLeft} href={format({ section: 'terminals', terminal: tid, reply: c.id })}>Reply</Button>)}
            </>}>
      <pre className="code-block">{c.text.includes('\n') ? c.text : commandWords(c)}</pre>
      {text && <pre className="code-block">{text}</pre>}
    </Dialog>
  )
}

