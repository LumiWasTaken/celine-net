/* Markdown with references. `[[kb:id]]` becomes a message card that opens the chat
   at that message, `[[documents:id]]` a quoted piece of a document, `[[terminal:id]]`
   a chip that opens one of its terminals in a new tab, `[[artifact:id]]` a chip with
   the page's title that opens it in a new tab, and `[[Some file]]` links a document by
   its name. Fenced and inline code are left alone. Used by Celine's
   replies and by Markdown documents. */
import { Children, isValidElement, memo, useContext, type ReactElement, type ReactNode } from 'react'
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { AppWindow, BookOpen, Link2Off, MessageSquareQuote, RefreshCw, Terminal, TriangleAlert } from 'lucide-react'
import { artifactRef, artifactsApi, ShowArtifact } from '../lib/artifacts'
import { isDocumentCard, parseRef, type DocumentCard } from '../lib/kb'
import { fullDate, listTime, stamp } from '../lib/format'
import { format, refHref, useNav } from '../lib/nav'
import { retryRef, useRefEntry } from '../lib/refs'
import { useResource } from '../lib/useResource'
import { blocks, closeOpen, useReveal } from '../lib/useReveal'
import { Avatar, ICON, IconButton } from '../ui'
import './Markdown.css'

const REF = 'cn-ref:'
const DOC = 'cn-doc:'
// A ref the citation verifier could not stand behind: `[[whatsapp:123|invalid]]`. Not a
// working link: it shows the id and "Invalid", never hides the failure. Any dest: a tool
// name written as a citation is marked invalid by the server too.
const INVALID = 'cn-invalid:'
const TERM = 'cn-term:'
const ART = 'cn-art:'

/* Rewrite [[...]] outside code into links with private schemes. */
function linkRefs(src: string): string {
  const out: string[] = []
  // split on fenced blocks, then on inline code spans
  const fences = src.split(/(```[\s\S]*?(?:```|$))/g)
  for (const part of fences) {
    if (part.startsWith('```')) { out.push(part); continue }
    const spans = part.split(/(`[^`\n]*`)/g)
    for (const s of spans) {
      if (s.startsWith('`')) { out.push(s); continue }
      out.push(s.replace(/\[\[([^\]\n]+?)\]\]/g, (_, inner: string) => {
        const target = inner.trim()
        const [dest, label] = target.split('|').map(x => x.trim())
        const term = terminalRef(dest ?? '')
        if (term) return `[Terminal](${TERM}${term})`
        const art = artifactRef(dest ?? '')
        if (art) return `[Page](${ART}${art})`
        if (dest && label?.toLowerCase() === 'invalid') {
          return `[${dest}](${INVALID}${encodeURIComponent(dest)})`
        }
        if (parseRef(dest!)) return `[${dest}](${REF}${encodeURIComponent(dest!)})`
        return `[${label || dest}](${DOC}${encodeURIComponent(dest!)})`
      }))
    }
  }
  return out.join('')
}

const transform = (url: string) =>
  [REF, DOC, INVALID, TERM, ART].some(p => url.startsWith(p)) ? url : defaultUrlTransform(url)

/* While a reply streams, the words of its last run of text are spans of their own, so
   each word that arrives mounts and fades in (.w, --dur) and the ones before it stay
   as they were (a word's span keeps its place as text is appended). Code, tables and
   reference chips never fade. */
type HNode = { type: string; tagName?: string; value?: string; children?: HNode[]; properties?: Record<string, unknown> }
function fadeTail() {
  return (tree: HNode) => {
    let last: { parent: HNode; node: HNode } | null = null
    const walk = (n: HNode) => {
      if (n.tagName === 'pre' || n.tagName === 'code' || n.tagName === 'table' || n.tagName === 'a') return
      for (const c of n.children ?? []) {
        if (c.type === 'text' && c.value?.trim()) last = { parent: n, node: c }
        else if (c.children) walk(c)
      }
    }
    walk(tree)
    if (!last) return
    const { parent, node } = last as { parent: HNode; node: HNode }
    const kids = (node.value ?? '').split(/(\s+)/).filter(Boolean).map((w): HNode => (/^\s+$/.test(w)
      ? { type: 'text', value: w }
      : { type: 'element', tagName: 'span', properties: { className: ['w'] }, children: [{ type: 'text', value: w }] }))
    parent.children!.splice(parent.children!.indexOf(node), 1, ...kids)
  }
}

/** `breaks`: a single newline is a line break, as typed (your own message) */
export const Markdown = memo(function Markdown({ text, streaming = false, breaks = false }: { text: string; streaming?: boolean; breaks?: boolean }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={streaming ? [fadeTail] : []} urlTransform={transform}
                   components={{ a: Link, p: Para, li: Item }}>
      {linkRefs(breaks ? text.replace(/([^\n])\n(?!\n)/g, '$1  \n') : text)}
    </ReactMarkdown>
  )
})

/* A reply, as it is written or as it was (lib/useReveal): on screen at a steady pace in
   whole words, parsed per block (blank lines outside code) with each finished block never
   parsed again; while it is written the last block has its open markers closed and its
   new words fade. `live`: tokens may still come. */
export function StreamText({ text, live }: { text: string; live: boolean }) {
  const shown = useReveal(text, live)
  const writing = live || shown.length < text.length
  const parts = blocks(shown)
  return <>{parts.map((b, i) => {
    const last = writing && i === parts.length - 1
    return <Block key={i} text={last ? closeOpen(b) : b} fade={last} />
  })}</>
}

const Block = memo(function Block({ text, fade }: { text: string; fade: boolean }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={fade ? [fadeTail] : []} urlTransform={transform}
                   components={{ a: Link, p: Para, li: Item }}>
      {linkRefs(text)}
    </ReactMarkdown>
  )
})

/* ── where a reference sits decides how it looks ──
   A paragraph that is nothing but references shows full cards. A reference inside a
   sentence is a small chip (date and sender) that previews on hover or focus, so the
   sentence keeps reading as one; neighbouring references share one chip group. */

type RefEl = ReactElement<{ href: string }>
const isRef = (c: unknown): c is RefEl =>
  isValidElement(c) && typeof (c.props as { href?: unknown }).href === 'string' &&
  (c.props as { href: string }).href.startsWith(REF)
const refOf = (c: RefEl) => decodeURIComponent(c.props.href.slice(REF.length))
const isGap = (c: unknown) => typeof c === 'string' && /^[\s,;&]*$/.test(c)

/* Punctuation right after a run of chips belongs to the sentence: it is glued to the
   last chip (no space before it, never alone at the start of a line). */
const TAIL = /^\s*([.,;:!?)\]]+)/

function arrange(children: ReactNode): { onlyRefs: string[] | null; inline: ReactNode[] } {
  const list = Children.toArray(children)
  const refs = list.filter(isRef)
  if (refs.length && list.every(c => isRef(c) || isGap(c))) return { onlyRefs: refs.map(refOf), inline: [] }
  const out: ReactNode[] = []
  let run: string[] = []
  let gap: ReactNode[] = []
  const flush = (tail = '') => {
    if (run.length) out.push(<RefChips key={`chips-${out.length}`} refs={run} tail={tail} />)
    if (!tail) out.push(...gap)
    run = []; gap = []
  }
  for (const c of list) {
    if (isRef(c)) { gap = []; run.push(refOf(c)) }
    else if (run.length && isGap(c)) gap.push(c)
    else if (run.length && typeof c === 'string') {
      const pending = gap.join('') + c
      const m = TAIL.exec(pending)
      if (m) { flush(m[1]); out.push(pending.slice(m[0].length)) }
      else { flush(); out.push(c) }
    }
    else { flush(); out.push(c) }
  }
  flush()
  return { onlyRefs: null, inline: out }
}

function Para({ children }: { children?: ReactNode }) {
  const { onlyRefs, inline } = arrange(children)
  if (onlyRefs) return <div className="refblock">{onlyRefs.map(r => <RefCard key={r} refId={r} />)}</div>
  return <p className="md-p">{inline}</p>
}

function Item({ children, className }: { children?: ReactNode; className?: string }) {
  const { onlyRefs, inline } = arrange(children)
  if (onlyRefs) return <li className={className}>{onlyRefs.map(r => <RefCard key={r} refId={r} />)}</li>
  return <li className={className}>{inline}</li>
}

/* Plain text (your own message) with its message references as chips. */
export function TextWithRefs({ text }: { text: string }) {
  const parts = text.split(/\[\[([^\]\n]+?)\]\]/g)
  return <>{parts.map((p, i) => {
    if (i % 2 === 0) return p
    const ref = p.trim()
    const term = terminalRef(ref)
    if (term) return <TerminalChip key={i} id={term} />
    const art = artifactRef(ref)
    if (art) return <ArtifactChip key={i} id={art} />
    return parseRef(ref) ? <RefChip key={i} refId={ref} /> : `[[${p}]]`
  })}</>
}

/* `[[terminal:<id>]]`: one of its terminals, by the six characters of its address. Not a
   message reference (parseRef stays as it is) and no lookup: the chip is the link, in a
   new tab like the terminal name in a tool row. Takes the text inside the brackets or
   the whole reference. */
const TERMINAL_REF = /^(?:\[\[)?terminal:([a-z0-9]{6})(?:\]\])?$/
const terminalRef = (text: string): string | null => TERMINAL_REF.exec(text.trim())?.[1] ?? null

function TerminalChip({ id }: { id: string }) {
  return (
    <a className="refchip" href={format({ section: 'terminals', terminal: id })} target="_blank" rel="noopener"
       aria-label="Open the terminal in a new tab">
      <Terminal size={ICON.sm} aria-hidden />
      <span className="refchip__who">Terminal</span>
    </a>
  )
}

/* `[[artifact:<id>]]`: one of its pages, by its title once known. In the Celine view the
   chip shows it in the side panel; elsewhere, and with a middle click, it opens the full
   page in a new tab. */
export function ArtifactChip({ id }: { id: string }) {
  const show = useContext(ShowArtifact)
  const meta = useResource(signal => artifactsApi.get(id, signal), [id])
  const title = meta.data?.title ?? (meta.error ? id : 'Page')
  return (
    <a className="refchip" href={format({ section: 'artifact', id })} target="_blank" rel="noopener"
       onClick={show ? e => { e.preventDefault(); show(id) } : undefined}
       aria-label={show ? `Show ${title} beside the chat` : `Open ${title} in a new tab`} title={meta.error ?? undefined}>
      <AppWindow size={ICON.sm} aria-hidden />
      <span className="refchip__who">{title}</span>
    </a>
  )
}

function RefChips({ refs, tail }: { refs: string[]; tail: string }) {
  return (
    <span className="refchips">
      {refs.map(r => <RefChip key={r} refId={r} />)}
      {tail}
    </span>
  )
}

export function RefChip({ refId }: { refId: string }) {
  const entry = useRefEntry(refId)
  if (entry.state !== 'ok') {
    return (
      <span className="refchip refchip--quiet" title={entry.state === 'error' ? entry.error : undefined}>
        {entry.state === 'missing' ? 'a message not in the archive' : 'a message'}
        {entry.state === 'error' && (
          <IconButton className="refcard__retry" size="sm" icon={RefreshCw} label="Retry" onClick={() => retryRef(refId)} />
        )}
      </span>
    )
  }
  if (isDocumentCard(entry.card)) return <DocumentChip refId={refId} card={entry.card} />
  const m = entry.card
  const who = m.from_me ? 'You' : m.sender?.name ?? m.chat.name
  const short = who.length > 18 ? `${who.slice(0, 17)}…` : who
  return (
    <a className="refchip" href={refHref(refId) ?? undefined}
       aria-label={`${who}, ${stamp(m.ts)}: open in ${m.chat.name}`}>
      <MessageSquareQuote size={ICON.sm} aria-hidden />
      <span className="refchip__date">{stamp(m.ts)}</span>
      <span className="refchip__who">{short}</span>
      <span className="refchip__pop" aria-hidden><RefCard refId={refId} preview /></span>
    </a>
  )
}

function Link({ href, children }: { href?: string; children?: ReactNode }) {
  const nav = useNav()
  if (href?.startsWith(REF)) return <RefCard refId={decodeURIComponent(href.slice(REF.length))} />
  if (href?.startsWith(TERM)) return <TerminalChip id={href.slice(TERM.length)} />
  if (href?.startsWith(ART)) return <ArtifactChip id={href.slice(ART.length)} />
  if (href?.startsWith(INVALID)) {
    const refId = decodeURIComponent(href.slice(INVALID.length))
    return (
      <span className="refchip refchip--invalid"
            title="This citation failed the check: the message was never shown in the conversation, or it does not say what the reply says.">
        <Link2Off size={ICON.sm} aria-hidden />
        <span>{refId}</span>
        <span>Invalid</span>
      </span>
    )
  }
  if (href?.startsWith(DOC)) {
    const target = decodeURIComponent(href.slice(DOC.length))
    return (
      <a href={format({ section: 'documents' })} className="doclink"
         onClick={e => { if (e.button === 0 && !e.metaKey && !e.ctrlKey) { e.preventDefault(); nav.openDocument(target) } }}>
        {children}
      </a>
    )
  }
  // links into the app itself ('/chats/…') stay in this tab; lib/nav routes them
  const inApp = href && href.startsWith('/') && !href.startsWith('//') ? href : null
  if (inApp) return <a href={inApp}>{children}</a>
  return <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>
}

/* Rendered with spans so it is valid inside a paragraph. `preview`: the card inside a
   chip's hover preview, not a link of its own (the chip is the link). */
function RefCard({ refId, preview = false }: { refId: string; preview?: boolean }) {
  const entry = useRefEntry(refId)

  if (entry.state === 'loading') {
    return <span className="refcard refcard--loading"><MessageSquareQuote size={ICON.sm} aria-hidden /> {refId}</span>
  }
  if (entry.state === 'missing') {
    return <span className="refcard refcard--missing" title="Not in the archive">{refId}</span>
  }
  if (entry.state === 'error') {
    return (
      <span className="refcard refcard--missing">
        <TriangleAlert size={ICON.sm} aria-hidden /> {refId}: {entry.error}
        <IconButton className="refcard__retry" size="sm" icon={RefreshCw} label="Retry" onClick={() => retryRef(refId)} />
      </span>
    )
  }
  if (isDocumentCard(entry.card)) return <DocumentRefCard card={entry.card} preview={preview} />
  const m = entry.card
  const who = m.from_me ? 'You' : m.sender?.name ?? ''
  const body = m.text || (m.media ? `[${m.media.type}]` : '')
  const inner = (
    <>
      <Avatar name={who} seed={m.sender?.id ?? who} size={22} />
      <span className="refcard__main">
        <span className="refcard__head">
          <b>{who}</b>
          {m.chat.name !== who && <span className="refcard__chat">{m.chat.name}</span>}
          <span className="refcard__time" title={fullDate(m.ts)}>
            {listTime(m.ts)}
          </span>
        </span>
        <span className="refcard__text">{body}</span>
      </span>
    </>
  )
  if (preview) return <span className="refcard">{inner}</span>
  return (
    <a className="refcard" href={refHref(refId) ?? undefined} title={`Open in ${m.chat.name}`}>
      {inner}
    </a>
  )
}

/* A quoted piece of a document: its title, where in it, and the text. */
function where(c: DocumentCard) {
  const s = c.segment
  return s.page ? `page ${s.page}` : s.speaker ?? null
}

function DocumentChip({ refId, card }: { refId: string; card: DocumentCard }) {
  const title = card.item.title.length > 22 ? `${card.item.title.slice(0, 21)}…` : card.item.title
  const w = where(card)
  return (
    <a className="refchip" href={format({ section: 'documents', item: card.item.id })}
       aria-label={`${card.item.title}${w ? `, ${w}` : ''}: open in Documents`}>
      <BookOpen size={ICON.sm} aria-hidden />
      <span className="refchip__who">{title}</span>
      {w && <span className="refchip__date">{w}</span>}
      <span className="refchip__pop" aria-hidden><RefCard refId={refId} preview /></span>
    </a>
  )
}

function DocumentRefCard({ card, preview }: { card: DocumentCard; preview: boolean }) {
  const w = where(card)
  const inner = (
    <>
      <BookOpen size={ICON.lg} aria-hidden className="refcard__icon" />
      <span className="refcard__main">
        <span className="refcard__head">
          <b>{card.item.title}</b>
          {w && <span className="refcard__chat">{w}</span>}
        </span>
        <span className="refcard__text">{card.text}</span>
      </span>
    </>
  )
  if (preview) return <span className="refcard">{inner}</span>
  return (
    <a className="refcard" href={format({ section: 'documents', item: card.item.id })} title="Open in Documents">
      {inner}
    </a>
  )
}
