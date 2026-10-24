/* History of one long text: its own page inside the caller's place, newest first, with Search,
   for 400 versions and more (pages of 50, the next as the list nears its end). A row is the
   time, who changed it and the one-line change. Open one and it shows beside the current text,
   the sentences that differ in the change colour, with Previous version, Next version and
   Restore this version (the caller puts that text into its editor, nothing saved until Save,
   or saves it at once, as its page does). A desktop keeps the list beside them; a phone
   shows the two texts, and Close goes back to the list where it was. */
import { useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, History, RotateCcw, SearchX, X } from 'lucide-react'
import { Button, EmptyState, IconButton, ListRow, LoadingRow, Notice, PlaceHead, VirtualList, type Crumb, type VirtualListHandle } from '../../ui'
import { textsApi, type TextHistory as Page, type TextOf, type TextVersion, type TextVersionFull } from '../../lib/texts'
import { errorText, stamp } from '../../lib/format'
import './editor.css'

/** where the versions come from: the shared store (an `of`), or a place's own (the notebook) */
export type HistorySource = {
  list: (q: string, before?: number) => Promise<Page>
  get: (id: number) => Promise<TextVersionFull>
}
export const textHistory = (of: TextOf): HistorySource => ({
  list: (q, before) => textsApi.history(of, q, before),
  get: id => textsApi.version(id),
})

/** a paragraph's sentences, as server/celine/texts.py splits them for the change line */
const sentences = (t: string) => (t.match(/[^.!?\n]+[.!?]*/g) ?? []).map(s => s.trim()).filter(Boolean)
/** the text as paragraphs of sentences, those the other text lacks in the change colour */
function Marked({ text, other }: { text: string; other: string }) {
  const theirs = new Set(sentences(other))
  return <div className="led-cmp__text">{text.split(/\n+/).filter(p => p.trim()).map((p, k) => (
    <p key={k}>{sentences(p).map((s, i) => (
      <span key={i}>{i > 0 && ' '}{theirs.has(s) ? s : <span className="led-cmp__diff">{s}</span>}</span>
    ))}</p>
  ))}</div>
}

export function TextHistory({ crumbs, source, current, onRestore }: {
  crumbs: Crumb[]
  source: HistorySource
  /** the text as it is now */
  current: string
  /** Restore this version: the text into the caller's editor, or saved at once (a promise:
      its failure shows here) */
  onRestore: (text: string, id: number) => void | Promise<unknown>
}) {
  const [q, setQ] = useState('')
  const [rows, setRows] = useState<TextVersion[]>([])
  const [total, setTotal] = useState(0)
  const [next, setNext] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // the selected row stays selected after Close, so the list comes back where you were
  const [at, setAt] = useState<number | null>(null)
  const [shown, setShown] = useState(false)
  const [open, setOpen] = useState<TextVersionFull | null>(null)
  const handle = useRef<VirtualListHandle>(null)
  // a caller may build its source on every render (textHistory(of)): the latest is used
  const src = useRef(source)
  src.current = source

  // the newest version, the one that stands now (the first row of the list without a search)
  const [newest, setNewest] = useState<TextVersion | null>(null)
  const busy = useRef(false)
  /** the next page (or the first): its versions, [] while one loads or on a failure */
  const load = async (before?: number): Promise<TextVersion[]> => {
    if (before != null && busy.current) return []
    busy.current = true; setLoading(true)
    try {
      const p = await src.current.list(q, before)
      setRows(r => (before != null ? [...r, ...p.versions] : p.versions)); setTotal(p.total); setNext(p.next); setError(null)
      if (!q && before == null) setNewest(p.versions[0] ?? null)
      return p.versions
    } catch (e) { setError(errorText(e)); return [] } finally { busy.current = false; setLoading(false) }
  }
  useEffect(() => { void load() }, [q]) // eslint-disable-line react-hooks/exhaustive-deps -- a new search starts the list again
  // the open version stays in view as Previous and Next move it, and after Close
  useEffect(() => {
    const k = rows.findIndex(x => x.id === at)
    if (k >= 0) handle.current?.scrollToIndex(k, 'auto')
  }, [at, shown]) // eslint-disable-line react-hooks/exhaustive-deps -- only when the selection moves, not on each page
  /** Previous version: the row below, from the next page when it is the last loaded */
  const older = async () => {
    const below = rows[i + 1] ?? (next != null ? (await load(next))[0] : undefined)
    if (below) setAt(below.id)
  }
  useEffect(() => {
    if (at == null) { setOpen(null); return }
    let live = true
    src.current.get(at).then(v => { if (live) setOpen(v) }, e => { if (live) setError(errorText(e)) })
    return () => { live = false }
  }, [at])

  const i = rows.findIndex(x => x.id === at)
  const v = shown && open && open.id === at ? open : null
  const row = (x: TextVersion) => (
    <ListRow title={x.change} tooltip={x.change} subtitle={x.by} meta={stamp(x.at)} aside={x.id === newest?.id ? 'Current version' : undefined}
             selected={x.id === at} onClick={() => { setAt(x.id); setShown(true) }} />
  )
  const list = rows.length
    ? <VirtualList items={rows} estimate={70} getKey={x => x.id} renderRow={row} label="History" className="led-hist__rows" resetOn={q} handleRef={handle}
                   onEndReached={() => { if (next != null) void load(next) }} after={loading ? <LoadingRow /> : undefined} />
    : loading ? <LoadingRow />
    : q ? <div className="led-hist__empty"><EmptyState icon={SearchX} title="No changes match" action={<Button onClick={() => setQ('')}>Clear search</Button>} /></div>
    : <div className="led-hist__empty"><EmptyState icon={History} title="No changes yet" /></div>
  // nothing to search before the first change
  const none = !q && !loading && !error && !rows.length

  return (
    <div className="page page--fill led">
      <PlaceHead level={1} crumbs={crumbs} title="History" count={none ? undefined : total}
                 search={none ? undefined : { value: q, onChange: setQ, placeholder: 'Search changes' }} />
      {error && <Notice tone="error">{error}</Notice>}
      <div className={v ? 'led-hist led-hist--open' : 'led-hist'}>
        <div className="led-hist__list">{list}</div>
        {v && (
          <div className="led-cmp">
            <section className="led-cmp__col" aria-label={stamp(v.at)}>
              <div className="led-cmp__head">
                <div className="led-cmp__who"><b>{stamp(v.at)}</b><span>{v.by}</span></div>
                <IconButton icon={ChevronLeft} label="Previous version" disabled={i < 0 || (i >= rows.length - 1 && next == null)} onClick={() => void older()} />
                <IconButton icon={ChevronRight} label="Next version" disabled={i <= 0} onClick={() => setAt(rows[i - 1]!.id)} />
                <IconButton icon={X} label="Close" onClick={() => setShown(false)} />
              </div>
              <p className="led-cmp__change">{v.change}</p>
              <Marked text={v.text} other={current} />
              <div className="led-cmp__foot">
                <Button variant="primary" icon={RotateCcw} disabled={v.text === current} onClick={() => { void Promise.resolve(onRestore(v.text, v.id)).catch(e => setError(errorText(e))) }}>Restore this version</Button>
              </div>
            </section>
            <section className="led-cmp__col led-cmp__col--now" aria-label="Current version">
              <div className="led-cmp__head"><div className="led-cmp__who"><b>Current version</b>{newest && <span>{stamp(newest.at)}</span>}</div></div>
              <Marked text={current} other={v.text} />
            </section>
          </div>
        )}
      </div>
    </div>
  )
}
