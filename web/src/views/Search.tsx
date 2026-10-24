/* Search: word search across every chat source, or its own conversations, on every
   keystroke. No model is involved. One page, no place sidebar. Before you
   type: the recent searches, and of the Toolbar only Source. Then the results, each its chat,
   who and the words found; Source, Person, Chat, Type and Date narrow them, Sort orders them.
   People and dates are words in the box (from:, after:), shown as chips under it; a rare
   word's swap is offered, never applied. A result opens in its chat with its message
   marked (the messages around it); its menu copies its link or asks Celine about it.

   Three requests, each debounced and aborted: the hits (60 ms), the counts per source,
   chat and person (220 ms, over the same filtered set) and query understanding for the
   chips (150 ms, text only). */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useName } from '../lib/name'
import {
  ArrowUpRight, BookHeart, CalendarDays, Copy, Eraser, History, MessagesSquare, MoreHorizontal, Plus, Search as SearchIcon, SearchX, Shapes, User,
} from 'lucide-react'
import {
  chatText, kbApi, type MediaType, type SearchChip, type SearchHit,
} from '../lib/kb'
import { api } from '../lib/api'
import { useDebounced } from '../lib/useDebounced'
import { useResource } from '../lib/useResource'
import { askCeline, copyMessageLink } from '../lib/ask'
import { format, useNav, type SearchFilters } from '../lib/nav'
import { errorText, listTime, num } from '../lib/format'
import { getJson, setJson } from '../lib/prefs'
import {
  Avatar, Button, Chip, EmptyState, GroupHead, IconButton, InlineError, ListRow, ListSection, Loaded, LoadingRow, MenuButton, PlaceHead,
  coarse, Snippet, toast, Toolbar, useContextMenu, VirtualList, type MenuItem, type VirtualListHandle,
} from '../ui'
import './Search.css'

const PAGE = 50
const RECENTS_KEY = 'search:recent'
type Sort = 'best' | 'recent' | 'oldest'
const SORT_LABEL: Record<Sort, string> = { best: 'Best matches', recent: 'Newest', oldest: 'Oldest' }
/** its own conversations, the one source that is not the archive */
const OWN = 'conversations'
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

export function Search({ active }: { active: boolean }) {
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
  const own = kb === OWN
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

  const params = useMemo(() => ({ q: own ? '' : q.trim(), kb: kb || undefined, chat: chat?.ref, person: person?.ref, sort, literal }),
                         [q, kb, chat, person, sort, literal, own])

  // hits, every keystroke; `paging`: the later page loading, if one is
  const found = useResource(signal => (params.q ? kbApi.search({ ...params, limit: PAGE, top: 0 }, signal) : Promise.resolve(null)), [params], { debounce: 60 })
  const page = params.q ? found.data : null
  // eslint-disable-next-line react-hooks/exhaustive-deps -- no page: a new empty list, nothing to keep
  const hits = page?.hits ?? []
  const nextCursor = page?.next_cursor ?? null
  const paging = useRef<AbortController | null>(null)
  const [pageError, setPageError] = useState<string | null>(null)
  useEffect(() => { setCursor(0); setPageError(null); paging.current?.abort(); paging.current = null }, [params])
  const loading = found.loading && !!params.q
  const error = (params.q ? found.error : null) ?? pageError
  const retry = () => { setPageError(null); found.reload() }

  // the counts per source, chat and person, over the same filtered set
  const counts = useResource(signal => (params.q
    ? kbApi.searchFacets({ q: params.q, kb: params.kb, chat: params.chat, person: params.person, top: 12, literal: params.literal }, signal)
    : Promise.resolve(null)), [params.q, params.kb, params.chat, params.person, params.literal], { debounce: 220 })
  const facets = params.q ? counts.data ?? null : null

  // query understanding for the chips
  const words = q.trim() && !own ? q.trim() : ''
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
    { label: `Ask ${name || 'Celine'}`, icon: BookHeart, onSelect: () => askCeline(nav, '', [h.ref]) },
  ]

  // Group: Chat puts a head over each run of results from one chat
  type Row = { key: string; head: string; parent?: string | null } | { key: string; h: SearchHit; i: number }
  const rows = useMemo<Row[]>(() => {
    const out: Row[] = []
    let at = ''
    hits.forEach((h, i) => {
      const key = `${h.kb}:${h.chat?.id}`
      if (group === 'Chat' && key !== at) { out.push({ key: `g-${key}-${i}`, head: h.chat?.name ?? h.ref, parent: h.chat?.parent }); at = key }
      out.push({ key: `${h.ref}-${i}`, h, i })
    })
    return out
  }, [hits, group])

  // the box is a combobox over the results: arrows move, Enter opens, Esc clears
  useEffect(() => {
    const at = rows.findIndex(r => 'h' in r && r.i === cursor)
    if (at >= 0) list.current?.scrollToIndex(at)
  }, [cursor, rows])
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape' && q) { e.preventDefault(); setQ('') }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setCursor(c => Math.min(c + 1, hits.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setCursor(c => Math.max(c - 1, 0)) }
    else if (e.key === 'Enter' && hits[cursor]) { e.preventDefault(); open(hits[cursor]!) }
  }

  const sourceWord = own ? (name ? `${name}'s conversations` : 'Conversations') : kb ? kbs.find(k => k.id === kb)?.title ?? kb : undefined
  const typed = parsed?.has[0] as MediaType | undefined
  const typeWord = TYPES.find(([t]) => t === typed)?.[1]
  const dated = DATES.find(([d]) => q.includes(`during:${d}`))
  const count = (k: string) => facets?.kbs.find(f => f.kb === k)?.count
  const filters = [
    { label: 'Source', icon: MessagesSquare, picked: sourceWord, onClear: () => setKb(''),
      items: [{ label: 'All', current: !kb, detail: facets ? `${num(facets.total)} results` : undefined, onSelect: () => setKb('') },
              ...chatKbs.map(k => ({ label: k.title, current: kb === k.id, detail: count(k.id) !== undefined ? `${num(count(k.id))} results` : undefined,
                                     onSelect: () => { setKb(k.id); if (chat && !chat.ref.startsWith(`${k.id}:`)) setChat(null) } })),
              { label: name ? `${name}'s conversations` : 'Conversations', current: own, onSelect: () => { setKb(OWN); setChat(null); setPerson(null) } }] },
    ...(own ? [] : [
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
  // before you type: only what you search in (and what is picked), no Sort or Group
  const offered = filters.filter(f => (f.items.length || f.picked) && (!typedNothing || f.label === 'Source' || f.picked))
  const ordered = !typedNothing && !own

  return (
    <div className="list-page">
      <PlaceHead level={1} title="Search" count={!typedNothing && total !== null && !own ? total : undefined}
                 search={{ value: q, onChange: v => { setQ(v); setLiteral(false) }, placeholder: 'Search', label: 'Search',
                           loading, inputRef: input, onKeyDown: onKey }}
                 actions={hits.length > 0 && <IconButton icon={BookHeart} label={`Ask ${name || 'Celine'}`} onClick={askAll} />}
                 more={[{ label: 'Clear recent searches', icon: Eraser, disabled: !recents.length, onSelect: () => { setRecents([]); saveRecents([]) } }]} />
      <div className="list-page__top">
        <Toolbar filters={offered}
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
        {indexing && hits.length > 0 && <p className="search-line">The word index is still being built</p>}
        {didYouMean && (
          <p className="search-line"><Button variant="ghost" size="sm" icon={SearchIcon} onClick={() => setQ(didYouMean)}>{didYouMean}</Button></p>
        )}
      </div>
      {error && <div className="list-page__note"><InlineError message={error} onRetry={retry} /></div>}
      {own ? <ConversationResults q={q.trim()} />
        : typedNothing ? (
          recents.length ? (
            <div className="list-page__rows">
              <VirtualList items={recents} estimate={48} getKey={r => r} label="Recent" before={<ListSection label="Recent" />}
                           renderRow={r => {
                             const items: MenuItem[] = [{ label: 'Search', icon: SearchIcon, onSelect: () => setQ(r) }, 'sep',
                               { label: 'Delete', icon: Eraser, danger: true, onSelect: () => setRecents(x => { const n = x.filter(y => y !== r); saveRecents(n); return n }) }]
                             return <ListRow icon={History} title={r} onClick={() => setQ(r)} onContextMenu={e => ctx.open(e, items, r)}
                                             trailing={<MenuButton icon={MoreHorizontal} label="More" items={items} size="sm" />} />
                           }} />
            </div>
          ) : <div className="list-page__empty"><EmptyState icon={SearchIcon} title="No searches yet" /></div>
        )
        : !loading && !error && hits.length === 0 ? (
          <div className="list-page__empty">
            <EmptyState icon={SearchX} title="No results match" action={<Button onClick={() => setQ('')}>Clear search</Button>}>
              {indexing ? 'The word index is still being built' : undefined}
            </EmptyState>
          </div>
        ) : (
          <div className="list-page__rows">
            <VirtualList handleRef={list} items={rows} estimate={92} overscan={8} getKey={r => r.key}
                         onEndReached={() => void more()} label="Results" resetOn={`${params.q}|${kb}|${chat?.ref}|${person?.ref}|${sort}|${group}`}
                         after={nextCursor ? <LoadingRow label="Loading more" /> : null}
                         renderRow={r => {
                           if ('head' in r) return <GroupHead icon={MessagesSquare} title={r.head} detail={r.parent ?? undefined} />
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
                                      trailing={<MenuButton icon={MoreHorizontal} label="More" items={hitMenu(h)} size="sm" />} />
                           )
                         }} />
          </div>
        )}
      {ctx.menu}
    </div>
  )
}

/* Words in its own conversations, chats and missions (GET /search, what the chat list's
   search finds); a result opens its conversation where it lives */
function ConversationResults({ q }: { q: string }) {
  const nav = useNav()
  const list = useResource(() => api.listConversations(), [])
  const hits = useResource(() => (q ? api.search(q) : Promise.resolve({ hits: [] })), [q], { debounce: 150 })
  const byId = useMemo(() => new Map((list.data ?? []).map(c => [c.id, c])), [list.data])
  if (!q) return <div className="list-page__empty"><EmptyState icon={SearchIcon} title="No searches yet" /></div>
  return (
    <Loaded page res={hits} icon={SearchX} label="Searching" empty={hits.data?.hits.length === 0 && { title: 'No results match' }}>{data => (
    <div className="list-page__rows">
      <VirtualList items={data.hits} estimate={68} label="Results" getKey={(h, i) => `${h.conversation_id}-${i}`}
                   renderRow={h => {
                     const c = byId.get(h.conversation_id)
                     return <ListRow density="lg" icon={MessagesSquare} title={c?.mission_name || h.title || 'Untitled'} subtitle={<Found html={h.snippet} />}
                                     meta={listTime(h.created_at)}
                                     onClick={() => nav.goPath(h.link)} />
                   }} />
    </div>
    )}</Loaded>
  )
}

/* an operator the box understood (from:, after:): its words, an x to take it out (a name
   that matched several is a menu of them, to pick one) */
function OperatorChip({ chip, onRemove }: { chip: SearchChip; onRemove: () => void }) {
  return <Chip tone={chip.error ? 'error' : 'neutral'} title={chip.error ?? chip.raw} onRemove={onRemove} removeLabel={`Remove ${chip.label}`}>{chip.label}</Chip>
}
