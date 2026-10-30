/* Small pieces: Chip (always filled), Dot, EventLine, SourceGlyph (a source is a glyph, not a
   coloured pill), Avatar, Segmented, Filters, Tabs, Progress,
   DisclosureArrow. */
import { forwardRef, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import {
  Check, ChevronRight, Database, Gamepad2, Hash, LoaderCircle, Mail, MessageCircle, MessageSquareLock, NotebookText, Send, User, Users, X,
  type LucideIcon, type LucideProps,
} from 'lucide-react'
import { Sheet } from './Dialog'
import { Face } from './Face'
import { cx } from './cx'
import { ICON } from './icons'
import { useSlide } from './slide'
import { getJson, setJson } from '../lib/prefs'

/** one of the eight avatar tints (--av-1..8), the same for the same key */
function avatarHue(key: string | number | null | undefined): number {
  const s = String(key ?? '')
  let h = 2166136261
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) }
  return (Math.abs(h) % 8) + 1
}

/* ── Chip: a label (a file, an upload, a state), never a filter ── */
type ChipTone = 'neutral' | 'accent' | 'error' | 'warn' | 'success' | 'running'

export function Chip({ children, tone = 'neutral', icon: Icon, onRemove, removeLabel = 'Remove', title, className }: {
  children: ReactNode
  tone?: ChipTone
  icon?: LucideIcon
  /** a small x inside the chip */
  onRemove?: () => void
  removeLabel?: string
  title?: string
  className?: string
}) {
  return (
    <span className={cx('chip', tone !== 'neutral' && `chip--${tone}`, className)} title={title}>
      {Icon && <Icon size={ICON.xs} aria-hidden />}<span>{children}</span>
      {onRemove && (
        <button type="button" className="chip__remove" aria-label={removeLabel} title={removeLabel}
                onClick={e => { e.stopPropagation(); onRemove() }}>
          <X size={ICON.xs} aria-hidden />
        </button>
      )}
    </span>
  )
}

/* ── Dot: the one state mark. Beside a name, never beside a word that says the same. ── */
export type DotTone = 'ok' | 'error' | 'warn' | 'accent' | 'running' | 'muted' | 'you' | 'off'

export function Dot({ tone, label, pulse = false, text }: {
  tone: DotTone
  /** the state in words, for screen readers and the tooltip; leave it out when text beside says it */
  label?: string
  /** working right now */
  pulse?: boolean
  /** the server's word beside it ("Connected", "No API key"): a provider's or an app's health */
  text?: ReactNode
}) {
  const dot = <span className={cx('dot', `dot--${tone}`, pulse && 'dot--pulse')}
                    {...(label && !text ? { role: 'img', 'aria-label': label, title: label } : { 'aria-hidden': true })} />
  return text == null ? dot : <span className="dot-word">{dot}<span>{text}</span></span>
}

/* ── Step: the number of a step in a list of steps (a mission's sub-tasks, a setup in
   three steps); a check once done, the accent while it is the current one. `sm` in a
   dense list (its plan): never tapped, so it keeps its small size on a phone too. ── */
export function Step({ n, done = false, current = false, size = 'md' }: { n: number; done?: boolean; current?: boolean; size?: 'sm' | 'md' }) {
  return <span className={cx('ui-step', size === 'sm' && 'ui-step--sm', done && 'ui-step--done', current && 'ui-step--current')} aria-hidden>
    {done ? <Check size={ICON.xs} /> : n}
  </span>
}

/* ── Divider: a hairline across, with a few words in its middle (a break with its
   place and time, a day in a thread). Never a chip. ── */
export function Divider({ children, className }: { children?: ReactNode; className?: string }) {
  return <div className={cx('divider', className)} role="separator">{children != null && <span className="divider__words">{children}</span>}</div>
}

/* ── Quote: someone's own words shown in the app (what it asked the vault for, the
   message a reply answers): a rule on the left, its words, who said it under them. ── */
export function Quote({ children, who, onClick, className }: {
  children: ReactNode
  who?: ReactNode
  /** the quote is a way to the original ("Go to the message") */
  onClick?: () => void
  className?: string
}) {
  const body = <>
    <span className="quote__text">{children}</span>
    {who != null && <span className="quote__who">{who}</span>}
  </>
  return onClick
    ? <button type="button" className={cx('quote', 'quote--link', className)} onClick={onClick}>{body}</button>
    : <blockquote className={cx('quote', className)}>{body}</blockquote>
}

/* ── Snippet: a found passage with the words that were searched for marked; at most
   `lines` lines, then an ellipsis. ── */
export function Snippet({ text, words, lines = 2, className }: {
  text: string
  words: string[]
  lines?: number
  className?: string
}) {
  const clean = words.map(w => w.trim()).filter(Boolean).map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const parts = clean.length ? text.split(new RegExp(`(${clean.join('|')})`, 'gi')) : [text]
  return (
    <span className={cx('snippet', className)} style={{ '--lines': lines } as React.CSSProperties}>
      {parts.map((p, i) => (i % 2 ? <mark key={i}>{p}</mark> : p))}
    </span>
  )
}

/* ── Thoughts: its reasoning, on one quiet surface (the chat's Thoughts, a reply's
   thoughts). The text as it wrote it, lines kept. ── */
export function Thoughts({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx('ui-thoughts', className)}>{children}</div>
}

/* ── Fold: a block of its own that opens and closes (its thinking above a reply). The head
   is a button with the DisclosureArrow and says what is inside; `defaultOpen` is how it
   starts (the Appearance setting); the body is the content itself. With `remember` this
   device keeps the reader's click (lib/prefs). ── */
export function Fold({ title, icon: Icon, defaultOpen = false, remember, children, className }: {
  /** the head's words: what is inside ("The agent's thoughts") */
  title: ReactNode
  icon?: LucideIcon
  /** how it starts: open or closed, until it is clicked */
  defaultOpen?: boolean
  /** a key unique in the app: the click is kept on this device */
  remember?: string
  children: ReactNode
  className?: string
}) {
  const key = remember && `fold:${remember}`
  const [open, setOpen] = useState(() => (key ? getJson<boolean | null>(key, null) : null) ?? defaultOpen)
  const toggle = () => {
    setOpen(!open)
    if (key) setJson(key, !open === defaultOpen ? null : !open)
  }
  return (
    <div className={cx('ui-fold', className)}>
      <button type="button" className="ui-fold__head" aria-expanded={open} onClick={toggle}>
        {Icon && <Icon size={ICON.sm} aria-hidden />}
        <span className="ui-fold__title">{title}</span>
        <DisclosureArrow />
      </button>
      {open && <div className="ui-fold__body">{children}</div>}
    </div>
  )
}

/* ── Facts: what a thing is, as labels and values. In rows (a label beside its value,
   compact, inside a card or a section) or as a grid (each label over its value). ── */
export function Facts({ items, grid = false, className }: {
  items: { label: ReactNode; value: ReactNode }[]
  grid?: boolean
  className?: string
}) {
  return (
    <dl className={cx('facts', grid && 'facts--grid', className)}>
      {items.map((it, i) => <div key={i}><dt>{it.label}</dt><dd>{it.value}</dd></div>)}
    </dl>
  )
}

/* ── Dots: at work, nothing to say yet (the typing dots) ── */
export function Dots({ className }: { className?: string }) {
  return <span className={cx('dots', className)} aria-hidden><i /><i /><i /></span>
}

/* ── EventLine: something that happened, as one muted line. With `detail` the line is a
   button that opens it in a Sheet (a Dialog on a desktop); it never unfolds, nothing moves.
   `running`: the work is going on now (Thinking, a tool call): the words shimmer. ── */
export function EventLine({ icon: Icon, children, detail, detailTitle, meta, className, quiet = false, running = false }: {
  icon?: LucideIcon
  /** the one line */
  children: ReactNode
  /** what its Sheet shows */
  detail?: ReactNode
  /** the Sheet's title; the line's words when left out */
  detailTitle?: ReactNode
  /** right side: a time */
  meta?: ReactNode
  className?: string
  /** among messages: no surface of its own */
  quiet?: boolean
  running?: boolean
}) {
  const [open, setOpen] = useState(false)
  className = cx('event-line', quiet && 'event-line--quiet', running && 'event-line--running', className)
  const line = (
    <>
      {running ? <LoaderCircle size={ICON.sm} className="spin" aria-hidden /> : Icon && <Icon size={ICON.sm} aria-hidden />}
      <span className="event-line__text">{children}</span>
      {meta != null && <span className="event-line__meta">{meta}</span>}
    </>
  )
  if (!detail) return <div className={className}><div className="event-line__head">{line}</div></div>
  return (
    <div className={className}>
      <button type="button" className="event-line__head event-line__head--open" aria-haspopup="dialog" onClick={() => setOpen(true)}>
        {line}<ChevronRight size={ICON.sm} className="event-line__go" aria-hidden />
      </button>
      {open && <Sheet title={detailTitle ?? children} size="md" onClose={() => setOpen(false)}>{detail}</Sheet>}
    </div>
  )
}

/* ── SourceGlyph ── */
const SOURCE_LABEL: Record<string, string> = {
  whatsapp: 'WhatsApp', discord: 'Discord', documents: 'Documents',
  telegram: 'Telegram', signal: 'Signal', email: 'Email',
}
const SOURCE_ICON: Record<string, LucideIcon> = {
  whatsapp: MessageCircle, discord: Gamepad2, documents: NotebookText,
  telegram: Send, signal: MessageSquareLock, email: Mail,
}
export const sourceLabel = (source?: string | null) => (source ? SOURCE_LABEL[source] ?? source : '')
export const sourceIcon = (source?: string | null): LucideIcon | undefined => (source ? SOURCE_ICON[source] : undefined)

/* Which app a chat or hit comes from: a small monochrome glyph, the name in its
   tooltip and accessible name. Scales to 20 sources without 20 colours. */
export function SourceGlyph({ source, label, showLabel = false, className }: {
  source?: string | null
  /** overrides the default name ("WhatsApp"), e.g. the source's own title */
  label?: string
  showLabel?: boolean
  className?: string
}) {
  if (!source) return null
  const Icon = SOURCE_ICON[source] ?? Database
  const name = label ?? sourceLabel(source)
  return (
    <span className={cx('source-glyph', className)} title={name}
          role="img" aria-label={showLabel ? undefined : name}>
      <Icon size={ICON.sm} aria-hidden />
      {showLabel && <span>{name}</span>}
    </span>
  )
}

/* ── Avatar: a chat person's face (Face): the picture when there is one (Discord's
   server and DM icons), else one initial on a colour picked from who it is, so the same
   person always gets the same colour. `kind` picks the glyph for a name with no letter (a
   phone number): a person, a group, `#` for a channel. `size` in px picks the nearest of
   the three face sizes. ── */
export function Avatar({ name, src, seed, size = 40, kind = 'person' }: {
  name: string | null | undefined
  src?: string | null
  seed?: string | number
  size?: number
  kind?: 'person' | 'dm' | 'group' | 'channel'
}) {
  const n = avatarHue(seed ?? name)
  return <Face name={name ?? ''} src={src} bg={`var(--av-${n}-bg)`} fg={`var(--av-${n}-fg)`}
               size={size <= 32 ? 'sm' : size <= 64 ? 'md' : 'lg'}
               fallback={kind === 'channel' ? Hash : kind === 'group' ? Users : User} />
}

/* Arrow keys in a radio group (Segmented, Choice): choose the next option and put the
   focus on it, so a focus ring never marks an option that is not chosen. */
export function moveRadio<V extends string>(e: KeyboardEvent<HTMLElement>, values: V[], value: V, onChange: (v: V) => void) {
  const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0
  if (!step || !values.length) return
  e.preventDefault()
  const at = Math.max(0, values.indexOf(value))
  const next = (at + step + values.length) % values.length
  onChange(values[next]!)
  e.currentTarget.querySelectorAll<HTMLElement>('[role="radio"]')[next]?.focus()
}

export type Option<V extends string> = { value: V; label: ReactNode; icon?: LucideIcon; title?: string; disabled?: boolean }

/* ── Segmented: ONE of at most six short options, picked (a sort, a check-in rule, a
   kind). A track with a thumb at surface-4 that slides to the chosen one; one row while it
   fits, and it wraps onto a second row when it has less room (six in a 380 px drawer). Never
   navigation (Tabs), never a filter row (Filters). ── */
export function Segmented<V extends string>({ value, onChange, options, label, className, title, disabled = false }: {
  value: V
  onChange: (value: V) => void
  options: Option<V>[]
  label: string
  className?: string
  title?: string
  disabled?: boolean
}) {
  const box = useRef<HTMLDivElement>(null)
  useSlide(box, '[aria-checked="true"]', value)
  return (
    <div ref={box} className={cx('segmented', className)} role="radiogroup" aria-label={label} title={title}
         onKeyDown={e => { if (!disabled) moveRadio(e, options.map(o => o.value), value, onChange) }}>
      <Radios value={value} onChange={onChange} options={options} disabled={disabled} />
    </div>
  )
}

/* ── Filters: a row of words that narrows a list or picks a kind (Intercom's kinds,
   the thinking level, which commands show), in the look of Tabs: the chosen word in its
   colour over the sliding line. One row that never wraps: it scrolls sideways and its
   edges fade. ── */
export function Filters<V extends string>({ value, onChange, options, label, className, disabled = false }: {
  value: V
  onChange: (value: V) => void
  options: Option<V>[]
  label: string
  className?: string
  disabled?: boolean
}) {
  const box = useRef<HTMLDivElement>(null)
  useSlide(box, '[aria-checked="true"]', value)
  useEdgeFade(box, value)
  return (
    <div ref={box} className={cx('tabs', className)} role="radiogroup" aria-label={label}
         onKeyDown={e => { if (!disabled) moveRadio(e, options.map(o => o.value), value, onChange) }}>
      <Radios value={value} onChange={onChange} options={options} disabled={disabled} />
    </div>
  )
}

/* A row that scrolls sideways fades at the edge it can scroll to (data-fade start, end),
   and brings the chosen one into the middle. */
export function useEdgeFade(ref: React.RefObject<HTMLElement | null>, key: unknown) {
  useLayoutEffect(() => {
    const b = ref.current
    if (!b) return
    const on = b.querySelector<HTMLElement>('[aria-selected="true"], [aria-checked="true"]')
    if (on && b.scrollWidth > b.clientWidth) b.scrollLeft = on.offsetLeft - (b.clientWidth - on.offsetWidth) / 2
    const mark = () => {
      const start = b.scrollLeft > 1, end = b.scrollLeft + b.clientWidth < b.scrollWidth - 1
      const v = [start && 'start', end && 'end'].filter(Boolean).join(' ')
      if (v) b.dataset.fade = v; else delete b.dataset.fade
    }
    mark()
    b.addEventListener('scroll', mark, { passive: true })
    const ro = new ResizeObserver(mark)
    ro.observe(b)
    return () => { b.removeEventListener('scroll', mark); ro.disconnect() }
  }, [ref, key])
}

function Radios<V extends string>({ value, onChange, options, disabled }: {
  value: V; onChange: (value: V) => void; options: Option<V>[]; disabled: boolean
}) {
  // one tab stop, on the chosen option (a radio group): focus lands on what is chosen
  const at = Math.max(0, options.findIndex(o => o.value === value))
  return <>{options.map((o, i) => (
    <button key={o.value} type="button" role="radio" aria-checked={value === o.value} title={o.title}
            tabIndex={i === at ? 0 : -1} disabled={disabled || o.disabled} onClick={() => onChange(o.value)}>
      {o.icon && <o.icon size={ICON.sm} aria-hidden />}{o.label}
    </button>
  ))}</>
}

/* ── Tabs: navigation between the views of one place (the side panel,
   Intercom's Waiting and All, Chats' sources). Words on the head's hairline, an underline
   that slides to the open one; the arrow keys move between them. One row that never
   wraps: it scrolls sideways and fades at the edge there is more. ── */
export function Tabs<V extends string>({ value, onChange, options, label, className }: {
  value: V
  onChange: (value: V) => void
  options: Option<V>[]
  label: string
  className?: string
}) {
  const box = useRef<HTMLDivElement>(null)
  useSlide(box, '[aria-selected="true"]', value)
  useEdgeFade(box, value)
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = options.findIndex(o => o.value === value)
    const j = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : e.key === 'Home' ? 0
      : e.key === 'End' ? options.length - 1 : null
    if (j === null) return
    e.preventDefault()
    const at = (j + options.length) % options.length
    onChange(options[at]!.value)
    box.current?.querySelectorAll<HTMLElement>('[role="tab"]')[at]?.focus()
  }
  return (
    <div ref={box} className={cx('tabs', className)} role="tablist" aria-label={label} onKeyDown={onKey}>
      {options.map(o => (
        <button key={o.value} type="button" role="tab" aria-selected={value === o.value} title={o.title}
                tabIndex={value === o.value ? 0 : -1} disabled={o.disabled} onClick={() => onChange(o.value)}>
          {o.icon && <o.icon size={ICON.sm} aria-hidden />}{o.label}
        </button>
      ))}
    </div>
  )
}

/* ── Progress: determinate when `max` is known, a sliding bar otherwise ── */
export function Progress({ value, max, label }: { value?: number; max?: number | null; label?: string }) {
  const pct = max ? Math.min(100, ((value ?? 0) / max) * 100) : null
  return (
    <div className="progress" role="progressbar" aria-label={label}
         aria-valuemin={0} aria-valuemax={max ?? undefined} aria-valuenow={max ? value : undefined}>
      <div className={cx('progress__bar', pct === null && 'progress__bar--indeterminate')}
           style={pct !== null ? { width: `${pct}%` } : undefined} />
    </div>
  )
}

/* ── DisclosureArrow: the one "click to see more" arrow. It points right and turns down
   while its control is open (an [aria-expanded='true'] button or an open <details>).
   Before the label in trees, at the end of rows that are sentences. ── */
export const DisclosureArrow = forwardRef<SVGSVGElement, LucideProps>(function DisclosureArrow(
  { className, size = ICON.sm, ...rest }, ref,
) {
  return <ChevronRight ref={ref} size={size} className={cx('disclosure-arrow', className)} aria-hidden {...rest} />
})
