/* Search: word search across every place on every keystroke. No model is involved. One page,
   no place sidebar. Before you type: the recent searches, and of the Toolbar only Search in
   and Source. Then the results grouped by place, each head with the server's count: its chats,
   Missions, Routines, Notebook, Documents and the Intercom (GET /search, each result opens
   at the server's link), then Messages, the chat archive, which pages on. Search in picks the
   places (this device keeps the ones taken out); Source, Person, Chat, Type and Date narrow
   Messages and leave the other places out, Sort orders Messages.
   People and dates are words in the box (from:, after:), shown as chips under it; a rare
   word's swap is offered, never applied. A result opens in its chat with its message
   marked (the messages around it); its menu copies its link or asks Celine about it.

   Four requests, each debounced and aborted: the other places (60 ms), the archive hits
   (60 ms), the counts per source, chat and person (220 ms, over the same filtered set) and
   query understanding for the chips (150 ms, text only). */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useName } from '../lib/name'
import {
  ArrowUpRight, BookHeart, CalendarDays, Copy, Eraser, History, ListFilter, MessagesSquare, Plus, Search as SearchIcon, SearchX, Shapes, User,
  type LucideIcon,
} from 'lucide-react'
import {
  chatText, kbApi, type MediaType, type SearchChip, type SearchHit,
} from '../lib/kb'
import { api, type ConfigStatus, type PlaceHit, type SearchPlace } from '../lib/api'
import { useDebounced } from '../lib/useDebounced'
import { useResource } from '../lib/useResource'
import { askCeline, copyMessageLink } from '../lib/ask'
import { format, useNav, type SearchFilters, type Section } from '../lib/nav'
import { errorText, listTime, num } from '../lib/format'
import { getJson, setJson, usePref } from '../lib/prefs'
import { PLACES, TITLE } from '../routes'
import {
  Avatar, Button, Chip, EmptyState, GroupHead, IconButton, InlineError, ListRow, ListSection, LoadingRow, MenuButton, PlaceHead,
  coarse, Snippet, toast, Toolbar, useContextMenu, VirtualList, type MenuItem, type VirtualListHandle,
} from '../ui'
import './Search.css'

const PAGE = 50
const RECENTS_KEY = 'search:recent'
type Sort = 'best' | 'recent' | 'oldest'
const SORT_LABEL: Record<Sort, string> = { best: 'Best matches', recent: 'Recent', oldest: 'Oldest' }
/** the places Search looks in, in the order of their groups; `chats` is Messages, the archive */
const IN: Section[] = ['celine', 'missions', 'routines', 'notebook', 'documents', 'intercom', 'chats']
const OUT_KEY = 'search:out'
const icon = (p: Section): LucideIcon => PLACES.find(x => x.id === p)?.icon ?? SearchIcon
const results = (n: number) => `${num(n)} ${n === 1 ? 'result' : 'results'}`
const TYPES: [MediaType, string][] = [['photo', 'Photo'], ['video', 'Video'], ['voice', 'Voice message'], ['audio', 'Audio'], ['document', 'File'],
                                      ['sticker', 'Sticker'], ['gif', 'GIF'], ['location', 'Location'], ['contact', 'Contact']]

function loadRecents(): string[] {
  const v = getJson<unknown>(RECENTS_KEY, [])
  return Array.isArray(v) ? v.filter(x => typeof x === 'string') : []
}
const saveRecents = (list: string[]) => setJson(RECENTS_KEY, list)

/* A chip's `raw` is the exact substring the parser matched, so replacing or removing it in
   place keeps everything else the user typed. */
const removeFromQuery = (q: string, raw: string) => q.replace(raw, '').replace(/\s+/g, ' ').trim()
const withHas = (q: string, type: MediaType | null) =>
  `${q.replace(/(^|\s)has:\S+/g, ' ').trim()}${type ? ` has:${type}` : ''}`.trim()
// Date: a during: word in the box, as Type is a has: word
const DATES = [['today', 'Today'], ['"this week"', 'This week'], ['"this month"', 'This month'], ['"this year"', 'This year']] as const
const withDate = (q: string, date: string | null) =>
  `${q.replace(/(^|\s)during:("[^"]*"|\S+)/g, ' ').trim()}${date ? ` during:${date}` : ''}`.trim()

/** the server's snippet (escaped text with the words found in <mark>) as the kit's Snippet */
function Found({ html }: { html: string }) {
  const text = html.replace(/<(?!\/?mark>)[^>]*>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')
  const parts = chatText(text).split(/<mark>|<\/mark>/)
  return <Snippet text={parts.join('')} words={parts.filter((_, i) => i % 2)} />
}

export function Search({ active, status }: { active: boolean; status: ConfigStatus | null }) {
  const name = useName()
  const nav = useNav()
  const route: SearchFilters = nav.route.section === 'search' ? nav.route : {}
  const [q, setQ] = useState(route.q ?? '')
  const [kb, setKb] = useState(route.kb ?? '')
  const [chat, setChat] = useState(route.chat ?? null)
  const [person, setPerson] = useState(route.person ?? null)
  const [sort, setSort] = useState<Sort>('best')
  const [group, setGroup] = useState('None')
  const [literal, setLiteral] = useState(false)

  // a link to /search?… (a chat's Search, Back, a bookmark) sets the filters
  const routeKey = nav.route.section === 'search' ? JSON.stringify(nav.route) : null
  const ownUrl = useRef<string | null>(null)
  useEffect(() => {
    if (!routeKey || routeKey === ownUrl.current) return
    const r = JSON.parse(routeKey) as SearchFilters
    setQ(r.q ?? ''); setKb(r.kb ?? ''); setChat(r.chat ?? null); setPerson(r.person ?? null)
    setLiteral(false)
  }, [routeKey])

  const [recents, setRecents] = useState<string[]>(loadRecents)
  const [cursor, setCursor] = useState(0)   // the keyboard-selected hit
  const input = useRef<HTMLInputElement>(null)
  const list = useRef<VirtualListHandle>(null)
  const ctx = useContextMenu()
  // Search in: every place that is on, but the ones taken out on this device
  const [left, setLeft] = usePref<string[]>(OUT_KEY, [])
  const offered = IN.filter(p => !status?.places_off.includes(p))
  const included = offered.filter(p => !left.includes(p))
  const archive = included.includes('chats')
  const placeWord = (p: Section) => (p === 'celine' ? name || 'Chats' : TITLE[p])
  // Source, Chat and Person narrow Messages, so the other places are left out
  const others = archive && (kb || chat || person) ? '' : included.filter(p => p !== 'chats').join(',')
  const sources = useResource(() => kbApi.list().then(r => r.kbs), [])
  const kbs = useMemo(() => sources.data ?? [], [sources.data])
  const chatKbs = useMemo(() => kbs.filter(k => k.kind === 'chat'), [kbs])

  // back on the page: the old words are selected, so typing replaces them
  useEffect(() => { if (active && !coarse()) { input.current?.focus(); input.current?.select() } }, [active])
  // the URL follows the words and filters (/search?q=…), so a search can be bookmarked
  useEffect(() => {
    if (!active) return
    const r = { section: 'search' as const, q: q.trim() || undefined, kb: kb || undefined, chat: chat ?? undefined, person: person ?? undefined }
    ownUrl.current = JSON.stringify(r)
    nav.go(r, { replace: true })
  }, [q, kb, chat, person, active]) // eslint-disable-line react-hooks/exhaustive-deps

  const params = useMemo(() => ({ q: archive ? q.trim() : '', kb: kb || undefined, chat: chat?.ref, person: person?.ref, sort, literal }),
                         [q, kb, chat, person, sort, literal, archive])
  // the other places, every keystroke
  const there = q.trim() && others ? q.trim() : ''
  const elsewhere = useResource(signal => (there ? api.search(there, others.split(',') as SearchPlace[], signal) : Promise.resolve(null)),
                                [there, others], { debounce: 60 })
  const groups = useMemo(() => (there ? elsewhere.data?.places ?? [] : []), [there, elsewhere.data])

  // hits, every keystroke; `paging`: the later page loading, if one is
  const found = useResource(signal => (params.q ? kbApi.search({ ...params, limit: PAGE, top: 0 }, signal) : Promise.resolve(null)), [params], { debounce: 60 })
  const page = params.q ? found.data : null
  // eslint-disable-next-line react-hooks/exhaustive-deps -- no page: a new empty list, nothing to keep
  const hits = page?.hits ?? []
  const nextCursor = page?.next_cursor ?? null
  const paging = useRef<AbortController | null>(null)
  const [pageError, setPageError] = useState<string | null>(null)
  useEffect(() => { setCursor(0); setPageError(null); paging.current?.abort(); paging.current = null }, [params, there, others])
  const loading = (found.loading && !!params.q) || (elsewhere.loading && !!there)
  const error = (params.q ? found.error : null) ?? (there ? elsewhere.error : null) ?? pageError
  const retry = () => { setPageError(null); found.reload(); elsewhere.reload() }

  // the counts per source, chat and person, over the same filtered set
  const counts = useResource(signal => (params.q
    ? kbApi.searchFacets({ q: params.q, kb: params.kb, chat: params.chat, person: params.person, top: 12, literal: params.literal }, signal)
    : Promise.resolve(null)), [params.q, params.kb, params.chat, params.person, params.literal], { debounce: 220 })
  const facets = params.q ? counts.data ?? null : null

  // query understanding for the chips
  const words = q.trim()
  const parse = useResource(signal => (words ? kbApi.searchParse(words, signal) : Promise.resolve(null)), [words], { debounce: 150 })
  const parsed = words ? parse.data ?? null : null

  // recent searches, kept in this browser once typing settles
  const settled = useDebounced(q.trim(), 1500)
  useEffect(() => {
    if (settled.length < 2) return
    setRecents(r => { const next = [settled, ...r.filter(x => x !== settled)].slice(0, 8); saveRecents(next); return next })
  }, [settled])

  const more = async () => {
    if (!nextCursor || paging.current) return
    const ctl = new AbortController()
    paging.current = ctl
    try {
      const r = await kbApi.search({ ...params, limit: PAGE, cursor: nextCursor, top: 0 }, ctl.signal)
      if (ctl.signal.aborted) return
      found.set(d => d && { ...d, hits: [...d.hits, ...r.hits], next_cursor: r.next_cursor })
    } catch (e) { if (!ctl.signal.aborted) setPageError(errorText(e)) } finally { if (paging.current === ctl) paging.current = null }
  }
  const total = page?.total ?? null
  const everywhere = groups.reduce((n, g) => n + g.count, total ?? 0)
  const corrected = page?.corrected ?? null
  const didYouMean = page?.did_you_mean ?? null
  const indexing = !!page?.indexing

  const open = (h: SearchHit) => {
    if (h.chat) nav.go({ section: 'chats', kb: h.kb, chat: h.chat.id, message: h.id })
    else nav.openRef(h.ref)
  }
  // Ask Celine about the results: the words and the filters as one plain sentence
  const askAll = () => {
    const where = [chat && `in ${chat.name}`, !chat && kb && `in ${kbs.find(k => k.id === kb)?.title ?? kb}`, person && `from ${person.name}`]
      .filter(Boolean).join(', ')
    askCeline(nav, `About "${q.trim()}"${where ? ` ${where}` : ''}: `)
  }
  const hitMenu = (h: SearchHit): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => open(h) },
    { label: 'Copy message link', icon: Copy, onSelect: () => void copyMessageLink(h.ref).then(ok => ok && toast({ text: 'Copied' })) },
    { label: `Ask ${name || 'your agent'}`, icon: BookHeart, onSelect: () => askCeline(nav, '', [h.ref]) },
  ]

  // a head over each place with its count; in Messages, Group: Chat puts a head over each run of results from one chat
  type Row = { key: string; head: string; icon: LucideIcon; count?: string; parent?: string | null }
    | { key: string; h: SearchHit; i: number } | { key: string; p: PlaceHit; place: Section; i: number }
  const rows = useMemo<Row[]>(() => {
    const out: Row[] = []
    let i = 0
    for (const g of groups) {
      out.push({ key: `in-${g.place}`, head: g.place === 'celine' ? name || 'Chats' : TITLE[g.place], icon: icon(g.place), count: results(g.count) })
      g.results.forEach((p, k) => out.push({ key: `${g.place}-${k}`, p, place: g.place, i: i++ }))
    }
    if (hits.length) out.push({ key: 'in-chats', head: TITLE.chats, icon: icon('chats'), count: total !== null ? results(total) : undefined })
    let at = ''
    hits.forEach((h, k) => {
      const key = `${h.kb}:${h.chat?.id}`
      if (group === 'Chat' && key !== at) { out.push({ key: `g-${key}-${k}`, head: h.chat?.name ?? h.ref, icon: MessagesSquare, parent: h.chat?.parent }); at = key }
      out.push({ key: `${h.ref}-${k}`, h, i: i++ })
    })
    return out
  }, [groups, hits, group, total, name])
  const targets = useMemo(() => rows.filter(r => 'i' in r), [rows])
  const go = (r: Row) => ('h' in r ? open(r.h) : 'p' in r ? nav.goPath(r.p.link) : undefined)

  // the box is a combobox over the results: arrows move, Enter opens, Esc clears
  useEffect(() => {
    const at = rows.findIndex(r => 'i' in r && r.i === cursor)
    if (at >= 0) list.current?.scrollToIndex(at)
  }, [cursor, rows])
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape' && q) { e.preventDefault(); setQ('') }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setCursor(c => Math.min(c + 1, targets.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setCursor(c => Math.max(c - 1, 0)) }
    else if (e.key === 'Enter' && targets[cursor]) { e.preventDefault(); go(targets[cursor]!) }
  }

  const sourceWord = kb ? kbs.find(k => k.id === kb)?.title ?? kb : undefined
  const typed = parsed?.has[0] as MediaType | undefined
  const typeWord = TYPES.find(([t]) => t === typed)?.[1]
  const dated = DATES.find(([d]) => q.includes(`during:${d}`))
  const count = (k: string) => facets?.kbs.find(f => f.kb === k)?.count
  const filters = [
    { label: 'Search in', icon: ListFilter, onClear: () => setLeft([]),
      picked: included.length === offered.length ? undefined : included.length === 1 ? placeWord(included[0]!) : `${included.length} places`,
      items: offered.map(p => {
        const on = included.includes(p)
        return { label: placeWord(p), icon: icon(p), current: on, disabled: on && included.length === 1,
                 onSelect: () => setLeft(on ? [...left, p] : left.filter(x => x !== p)) }
      }) },
    ...(!archive ? [] : [
      { label: 'Source', icon: MessagesSquare, picked: sourceWord, onClear: () => setKb(''),
        items: [{ label: 'All', current: !kb, detail: facets ? results(facets.total) : undefined, onSelect: () => setKb('') },
                ...chatKbs.map(k => ({ label: k.title, current: kb === k.id, detail: count(k.id) !== undefined ? results(count(k.id)!) : undefined,
                                       onSelect: () => { setKb(k.id); if (chat && !chat.ref.startsWith(`${k.id}:`)) setChat(null) } }))] },
      { label: 'Person', icon: User, picked: person?.name, onClear: () => setPerson(null),
        items: (facets?.people ?? []).filter(p => p.ref).map(p => ({ label: p.name, face: <Avatar name={p.name} size={28} />, detail: num(p.count),
                                                                     current: p.ref === person?.ref, onSelect: () => setPerson({ ref: p.ref!, name: p.name }) })) },
      { label: 'Chat', icon: MessagesSquare, picked: chat?.name, onClear: () => setChat(null),
        items: (facets?.chats ?? []).map(c => ({ label: c.name, face: <Avatar name={c.name} seed={c.ref} kind={c.kind} size={28} />, detail: c.parent ?? num(c.count),
                                                 current: c.ref === chat?.ref, onSelect: () => setChat({ ref: c.ref, name: c.name }) })) },
      { label: 'Type', icon: Shapes, picked: typeWord, onClear: () => setQ(x => withHas(x, null)),
        items: TYPES.map(([t, w]) => ({ label: w, current: t === typed, onSelect: () => setQ(x => withHas(x, t)) })) },
      { label: 'Date', icon: CalendarDays, picked: dated?.[1], onClear: () => setQ(x => withDate(x, null)),
        items: DATES.map(([d, w]) => ({ label: w, current: d === dated?.[0], onSelect: () => setQ(x => withDate(x, d)) })) },
    ]),
  ]
  const typedNothing = !q.trim()
  // a filter with nothing to pick is left out (Person and Chat wait for the counts); a picked one stays
  // before you type: only where you search (and what is picked), no Sort or Group
  const shown = filters.filter(f => (f.items.length || f.picked) && (!typedNothing || f.label === 'Search in' || f.label === 'Source' || f.picked))
  const ordered = !typedNothing && archive

  return (
    <div className="list-page">
      <PlaceHead level={1} title="Search" count={!typedNothing && (total !== null || groups.length) ? everywhere : undefined}
                 search={{ value: q, onChange: v => { setQ(v); setLiteral(false) }, placeholder: 'Search', label: 'Search',
                           loading, inputRef: input, onKeyDown: onKey }}
                 actions={targets.length > 0 && <IconButton icon={BookHeart} label={`Ask ${name || 'your agent'}`} onClick={askAll} />}
                 more={[{ label: 'Clear recent searches', icon: Eraser, disabled: !recents.length, onSelect: () => { setRecents([]); saveRecents([]) } }]} />
      <div className="list-page__top">
        <Toolbar filters={shown}
                 sort={!ordered ? undefined : { label: 'Sort', value: sort, onChange: v => setSort(v as Sort),
                                           options: (Object.keys(SORT_LABEL) as Sort[]).map(v => ({ value: v, label: SORT_LABEL[v] })) }}
                 group={!ordered ? undefined : { label: 'Group', value: group, onChange: setGroup, options: ['Chat', 'None'].map(v => ({ value: v, label: v })) }} />
        {(!!parsed?.chips.length || !!parsed?.suggestions.length) && (
          <div className="search-chips">
            {parsed?.chips.map((c, i) => c.ambiguous && c.candidates?.length ? (
              <MenuButton key={`${c.kind}-${i}`} icon={User} text={c.label} label={c.label} size="sm"
                          items={c.candidates.map(p => ({ label: p.name, face: <Avatar name={p.name} size={28} />, detail: p.kb,
                                                          onSelect: () => setQ(x => x.replace(c.raw, `${c.kind === 'from' ? 'from' : 'in'}:"${p.name}"`)) }))} />
            ) : <OperatorChip key={`${c.kind}-${i}`} chip={c} onRemove={() => setQ(x => removeFromQuery(x, c.raw))} />)}
            {parsed?.suggestions.map((s, i) => (
              <Button key={`s-${i}`} size="sm" variant="ghost" icon={Plus} onClick={() => setQ(x => `${x.trim()} ${s.value}`.trim())}>{s.label}</Button>
            ))}
          </div>
        )}
        {corrected && (
          <p className="search-line">
            <span>{`Results for "${corrected.query}"`}</span>
            <Button variant="ghost" size="sm" onClick={() => setLiteral(true)}>{`Search for "${corrected.original}"`}</Button>
          </p>
        )}
        {indexing && hits.length > 0 && <p className="search-line">Search is still indexing your messages, so some results may be missing.</p>}
        {didYouMean && (
          <p className="search-line"><Button variant="ghost" size="sm" icon={SearchIcon} onClick={() => setQ(didYouMean)}>{didYouMean}</Button></p>
        )}
      </div>
      {error && <div className="list-page__note"><InlineError message={error} onRetry={retry} /></div>}
      {typedNothing ? (
          recents.length ? (
            <div className="list-page__rows">
              <VirtualList items={recents} estimate={48} getKey={r => r} label="Recent" before={<ListSection label="Recent" />}
                           renderRow={r => {
                             const items: MenuItem[] = [{ label: 'Search', icon: SearchIcon, onSelect: () => setQ(r) }, 'sep',
                               { label: 'Remove', icon: Eraser, danger: true, onSelect: () => setRecents(x => { const n = x.filter(y => y !== r); saveRecents(n); return n }) }]
                             return <ListRow icon={History} title={r} onClick={() => setQ(r)} onContextMenu={e => ctx.open(e, items, r)}
                                             menu={items} />
                           }} />
            </div>
          ) : <div className="list-page__empty"><EmptyState icon={SearchIcon} title="No searches yet" /></div>
        )
        : !loading && !error && rows.length === 0 ? (
          <div className="list-page__empty">
            <EmptyState icon={SearchX} title="No results match" action={<Button onClick={() => setQ('')}>Clear search</Button>}>
              {indexing ? 'Search is still indexing your messages, so some results may be missing.' : undefined}
            </EmptyState>
          </div>
        ) : (
          <div className="list-page__rows">
            <VirtualList handleRef={list} items={rows} estimate={92} overscan={8} getKey={r => r.key}
                         onEndReached={() => void more()} label="Results" resetOn={`${q.trim()}|${others}|${archive}|${kb}|${chat?.ref}|${person?.ref}|${sort}|${group}`}
                         after={nextCursor ? <LoadingRow label="Loading more" /> : null}
                         renderRow={r => {
                           if ('head' in r) return <GroupHead icon={r.icon} title={r.head} count={r.count} detail={r.parent ?? undefined} />
                           if ('p' in r) return (
                             <ListRow density="lg" selected={r.i === cursor} icon={icon(r.place)} title={r.p.title} subtitle={<Found html={r.p.snippet} />}
                                      meta={listTime(r.p.time)} detail={r.p.detail ?? undefined} href={r.p.link}
                                      onClick={e => { e.preventDefault(); nav.goPath(r.p.link) }} />
                           )
                           const { h, i } = r
                           const who = h.from_me ? 'You' : h.sender?.name
                           return (
                             <ListRow density="lg" selected={i === cursor} title={h.chat?.name ?? h.ref} tooltip={h.chat?.name}
                                      leading={<Avatar name={h.chat?.name} seed={`${h.kb}:${h.chat?.id}`} size={40} kind={h.chat?.kind} />}
                                      subtitle={<Found html={h.snippet} />} meta={listTime(h.ts)}
                                      detail={[who, h.chat?.parent ?? kbs.find(k => k.id === h.kb)?.title, h.deleted ? 'Deleted' : h.edited ? 'Edited' : null]
                                        .filter(Boolean).join(', ')}
                                      href={h.chat ? format({ section: 'chats', kb: h.kb, chat: h.chat.id, message: h.id }) : undefined}
                                      onClick={e => { e.preventDefault(); open(h) }} onContextMenu={e => ctx.open(e, hitMenu(h), 'Result')}
                                      menu={hitMenu(h)} />
                           )
                         }} />
          </div>
        )}
      {ctx.menu}
    </div>
  )
}

/* an operator the box understood (from:, after:): its words, an x to take it out (a name
   that matched several is a menu of them, to pick one) */
function OperatorChip({ chip, onRemove }: { chip: SearchChip; onRemove: () => void }) {
  return <Chip tone={chip.error ? 'error' : 'neutral'} title={chip.error ?? chip.raw} onRemove={onRemove} removeLabel={`Remove ${chip.label}`}>{chip.label}</Chip>
}
