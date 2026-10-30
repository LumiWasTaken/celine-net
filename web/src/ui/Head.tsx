/* Heads: PlaceHead (the head of every place and page), Toolbar (the state of a list under
   it) and GroupHead (above each group of a grouped list).

   PlaceHead is ONE row at every width. Its order: the title and count, Search, Select,
   Archive, Trash, the caller's own actions, More, then New at the far right. It measures
   itself: when the row does not fit, it steps down, one step at a time, until it does:
   the buttons lose their words, then Select, Archive and Trash move into More, then New
   becomes a round "+", then the title ends in an ellipsis, and last the caller's own
   actions take their own line under the row. In a narrow head (a list column, a phone)
   Search takes its own row under it, and the buttons have no words. A touch screen has no
   hover to show a tip, so there the words stay one step longer: Select, Archive and Trash
   move into More first, then the words go, at every width. On a shelf New keeps its place, unseen, so nothing
   the user just clicked moves. */
import { useLayoutEffect, useRef, useState, type ReactNode, type Ref } from 'react'
import {
  Archive, ArrowUpDown, ChevronDown, ChevronLeft, ChevronRight, Layers, ListChecks, MoreHorizontal, PanelLeft, Plus, Trash2, X, type LucideIcon,
} from 'lucide-react'
import { Segmented, useEdgeFade } from './Bits'
import { Button, IconButton } from './Button'
import { cx } from './cx'
import { SearchField } from './Form'
import { ICON } from './icons'
import { coarse } from './phone'
import { usePush } from './motion'
import { Menu, MenuButton, useContextMenu, type MenuItem } from './Menu'
import { usePlace } from './Place'
import type { Picker } from './Select'
import type { Shelf } from './Shelf'

/** a pick of one in the Toolbar: Sort or Group */
export type ToolChoice = {
  label: string
  value: string
  options: { value: string; label: string }[]
  onChange: (value: string) => void
}

export type PlaceSearch = {
  value: string
  onChange: (value: string) => void
  /** "Search", or what it searches: "Search conversations" */
  placeholder?: string
  label?: string
  loading?: boolean
  inputRef?: Ref<HTMLInputElement>
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void
}

type PlaceShelf = {
  /** which shelves the place has */
  archive?: boolean
  trash?: boolean
  /** the one shown */
  value: Shelf
  onChange: (shelf: Shelf) => void
}

const SHELF = { archive: { label: 'Archive', icon: Archive }, trash: { label: 'Trash', icon: Trash2 } } as const

/** the steps a head takes when it does not fit (see the top of this file) */
const STEPS = 5
/** below this width Search takes its own row */
const NARROW = 640

/** one step of a page's breadcrumb: the place or page above it */
export type Crumb = { label: string; href?: string; onClick?: (e: React.MouseEvent<HTMLElement>) => void }

export function PlaceHead({
  title, count, detail, titleAttr, level = 2, back, crumbs, lead, search, picker, shelf, more = [], actions,
  newLabel, onNew, newHref, newPressed, newDisabled, tools, className,
}: {
  title: ReactNode
  /** the server's number of things in the place (or on the open shelf) */
  count?: number
  /** a state beside the title, muted (a terminal's program, "Deleted") */
  detail?: ReactNode
  /** the title's tooltip (a path) */
  titleAttr?: string
  level?: 1 | 2
  /** the way back to the list, where list and page share one pane (a phone) */
  back?: { label: string; onClick: () => void }
  /** the pages above this one ("Real life > Noor"), before the title; on a phone only a
      back arrow to the last of them */
  crumbs?: Crumb[]
  /** before the title at every width: a link back from a page, a face */
  lead?: ReactNode
  search?: PlaceSearch
  /** Select: many at once */
  picker?: Picker
  /** Archive and Trash */
  shelf?: PlaceShelf
  /** rare actions ("Archive older than"); Select, Archive and Trash join them when there is no room */
  more?: MenuItem[]
  /** a page's own controls, before More; with no room they take their own line under the row */
  actions?: ReactNode
  /** the one primary: "New conversation" */
  newLabel?: string
  onNew?: () => void
  newHref?: string
  /** its page is open */
  newPressed?: boolean
  /** why it cannot act, as its tip */
  newDisabled?: string | boolean
  /** the one row under the head (Tabs, until a place drops them) */
  tools?: ReactNode
  className?: string
}) {
  const row = useRef<HTMLDivElement>(null)
  const [step, setStep] = useState(0)
  const [narrow, setNarrow] = useState(false)
  const [moreAt, setMoreAt] = useState<HTMLElement | null>(null)
  const width = useRef(0)
  const [touch] = useState(coarse)

  // a new width starts from the roomiest step; the effect below steps down until it fits
  useLayoutEffect(() => {
    const el = row.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => {
      const w = Math.round(e!.contentRect.width)
      if (w === width.current) return
      width.current = w
      setNarrow((el.parentElement?.clientWidth ?? w) < NARROW)
      setStep(0)
    })
    ro.observe(el.parentElement ?? el)
    return () => ro.disconnect()
  }, [])
  // what it holds changed: measure again from the top
  const name = typeof title === 'string' ? title : null
  useLayoutEffect(() => setStep(0), [name, count, shelf?.value, picker?.on, more.length])
  // eslint-disable-next-line react-hooks/exhaustive-deps -- measured after every render
  useLayoutEffect(() => {
    const el = row.current
    if (!el) return
    const acts = el.querySelector<HTMLElement>('.place-head__acts')
    const over = el.scrollWidth > el.clientWidth + 1
    const sticks = acts ? acts.getBoundingClientRect().right > el.getBoundingClientRect().right + 1 : false
    if (step < STEPS && (over || sticks)) setStep(s => s + 1)
  })

  const words = touch ? step < 2 : step < 1 && !narrow
  const shelvesInMore = step >= (touch ? 1 : 2)

  // a page's own buttons lose their words with the rest; the words become their tip
  useLayoutEffect(() => {
    const head = row.current?.parentElement
    if (!head) return
    for (const b of head.querySelectorAll<HTMLElement>(':is(.place-head__acts, .place-head__own) > .btn:not(.btn--icon, .place-head__new)')) {
      if (!words) { if (!b.dataset.tip) { b.dataset.tip = b.textContent ?? ''; b.dataset.tipWords = '' } }
      else if (b.dataset.tipWords !== undefined) { delete b.dataset.tip; delete b.dataset.tipWords }
    }
  })

  const open = shelf && shelf.value !== 'list' ? shelf.value : null
  const roundNew = step >= 3 || narrow
  const clip = step >= 4
  const actsBelow = step >= 5 && !!actions

  const toggle = (s: 'archive' | 'trash') => shelf!.onChange(shelf!.value === s ? 'list' : s)
  const selectItem: MenuItem | null = picker ? { label: picker.on ? 'Done' : 'Select', icon: ListChecks, onSelect: () => (picker.on ? picker.stop() : picker.start()) } : null
  const shelfItems: MenuItem[] = (['archive', 'trash'] as const).filter(s => shelf?.[s])
    .map(s => ({ label: SHELF[s].label, icon: SHELF[s].icon, current: shelf!.value === s, onSelect: () => toggle(s) }))
  const moreItems: MenuItem[] = shelvesInMore ? [selectItem, ...shelfItems].filter((x): x is MenuItem => !!x) : []
  const allMore = [...moreItems, ...(moreItems.length && more.length ? ['sep' as const] : []), ...more]
  // a More of one plain item with an icon is that item's own button: no menu for one choice
  // (one that destroys stays in the menu, its last item)
  const only = allMore.length === 1 ? allMore[0] : undefined
  const lone = only && only !== 'sep' && !('heading' in only) && only.icon && typeof only.label === 'string'
    && !only.danger && only.current === undefined ? only : null

  // a labelled button with room, an icon button without
  const act = (icon: LucideIcon, label: string, on: boolean, onClick: (e: React.MouseEvent<HTMLElement>) => void, extra?: object) => words
    ? <Button icon={icon} pressed={on} onClick={onClick} {...extra}>{label}</Button>
    : <IconButton icon={icon} label={label} pressed={on} onClick={onClick} {...extra} />

  const H = level === 1 ? 'h1' : 'h2'
  const place = usePlace()
  const up = crumbs?.at(-1)
  // a phone: a deeper page pushes in, back reverses it
  const self = useRef<HTMLElement>(null)
  usePush(self, (crumbs?.length ?? 0) + (back ? 1 : 0))
  const searchBox = search && (
    <SearchField value={search.value} onChange={search.onChange} placeholder={search.placeholder ?? 'Search'}
                 label={search.label} loading={search.loading} inputRef={search.inputRef} onKeyDown={search.onKeyDown}
                 className="place-head__search" />
  )
  return (
    <header ref={self} className={cx('place-head', narrow && 'place-head--narrow', !words && 'place-head--icons', className)}>
      <div ref={row} className="place-head__row">
        {place && !back && !up && <IconButton icon={PanelLeft} label={place.title} className="place-head__side" onClick={place.open} />}
        {back && <IconButton icon={ChevronLeft} label={back.label} className="place-head__back" iconSize="lg" onClick={back.onClick} />}
        {up && <IconButton icon={ChevronLeft} label={`Back to ${up.label}`} className="place-head__up" href={up.href} onClick={up.onClick} />}
        {crumbs && crumbs.length > 0 && (
          <nav className="place-head__crumbs" aria-label="Breadcrumb">
            {crumbs.map((c, i) => (
              <span key={i} className="place-head__crumb">
                {c.href || c.onClick ? <a href={c.href} onClick={c.onClick}>{c.label}</a> : <span>{c.label}</span>}
                <ChevronRight size={ICON.sm} aria-hidden />
              </span>
            ))}
          </nav>
        )}
        {lead}
        <H className={cx('place-head__title', clip && 'place-head__title--clip')} title={titleAttr}>
          {open && <IconButton icon={ChevronLeft} label="Back to the list" size="sm" iconSize="md" onClick={() => shelf!.onChange('list')} />}
          <span className="place-head__name">{open ? SHELF[open].label : title}</span>
          {count != null && <span className="place-head__count">{count}</span>}
          {detail && <span className="place-head__detail">{detail}</span>}
        </H>
        {!narrow && searchBox}
        <div className="place-head__acts">
          {!shelvesInMore && picker && act(ListChecks, 'Select', picker.on, () => (picker.on ? picker.stop() : picker.start()))}
          {!shelvesInMore && (['archive', 'trash'] as const).filter(s => shelf?.[s])
            .map(s => <span key={s} className="place-head__slot">{act(SHELF[s].icon, SHELF[s].label, shelf!.value === s, () => toggle(s))}</span>)}
          {!actsBelow && actions}
          {lone ? <IconButton icon={lone.icon!} label={lone.label as string} disabled={lone.disabled} title={lone.title} onClick={lone.onSelect} />
            : allMore.length > 0 && (
            <IconButton icon={MoreHorizontal} label="More" pressed={!!moreAt} aria-haspopup="menu" aria-expanded={!!moreAt}
                        onClick={e => setMoreAt(moreAt ? null : e.currentTarget)} />
          )}
          {newLabel && (onNew || newHref) && <span className={cx('place-head__slot', open && 'is-invisible')} inert={open ? true : undefined}>{roundNew
            ? <IconButton icon={Plus} label={newLabel} variant="primary" href={newHref} pressed={newPressed}
                          disabled={!!newDisabled} title={typeof newDisabled === 'string' ? newDisabled : undefined}
                          onClick={onNew && (() => onNew())} className="place-head__new" />
            : <Button icon={Plus} variant="primary" href={newHref} pressed={newPressed} disabled={!!newDisabled}
                      title={typeof newDisabled === 'string' ? newDisabled : undefined}
                      onClick={onNew && (() => onNew())} className="place-head__new">{newLabel}</Button>}</span>}
        </div>
      </div>
      {actsBelow && <div className="place-head__line place-head__own">{actions}</div>}
      {narrow && searchBox && <div className="place-head__line">{searchBox}</div>}
      {tools && <div className="place-head__line place-head__tools">{tools}</div>}
      {moreAt && <Menu anchor={moreAt} items={allMore} label="More" align="end" onClose={() => setMoreAt(null)} />}
    </header>
  )
}

/* ── Toolbar: under a PlaceHead, the state of the list (All, Playing, Ended with their
   counts; a state with none shows no 0), the filters as buttons that show what is picked
   (an x clears it), then Sort and Group at the right end ("Sort: Recent"). One row that
   never wraps: when it does not fit, Sort and Group, then the filters not picked, become
   icons with their words as the tip, then the state becomes one button with its menu; a picked filter shrinks to an ellipsis, and what
   still has no room scrolls sideways and fades at the edge with more; while selecting,
   the SelectBar takes its place. ── */
type ToolFilter = {
  /** the filter's name: "Folder" */
  label: string
  icon: LucideIcon
  /** what is picked ("Noor Haddad", "2 folders"); nothing picked shows the name */
  picked?: string
  /** the options, each `current` when picked */
  items: MenuItem[]
  onClear: () => void
}

/** a filter that is on or off (Favourites): a button that stays pressed while on */
type ToolToggle = { label: string; icon: LucideIcon; on: boolean; onChange: (on: boolean) => void }

export function Toolbar({ state, filters = [], toggles = [], sort, group, className }: {
  state?: { label: string; value: string; onChange: (value: string) => void; options: { value: string; label: string; count?: number }[] }
  filters?: ToolFilter[]
  /** after the filters */
  toggles?: ToolToggle[]
  sort?: ToolChoice
  group?: ToolChoice
  className?: string
}) {
  const [at, setAt] = useState<{ el: HTMLElement; label: string; items: MenuItem[]; align: 'start' | 'end' } | null>(null)
  const row = useRef<HTMLDivElement>(null)
  // 0 words, 1 Sort and Group as icons, 2 the filters too, 3 the state as one button with
  // its menu; a new width starts from 0
  const [tight, setTight] = useState(0)
  const width = useRef(0)
  useLayoutEffect(() => {
    const el = row.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => {
      const w = Math.round(e!.contentRect.width)
      if (w !== width.current) { width.current = w; setTight(0) }
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const picked = filters.map(f => f.picked ?? '').join('|')
  useLayoutEffect(() => setTight(0), [state?.value, picked, sort?.value, group?.value])
  // eslint-disable-next-line react-hooks/exhaustive-deps -- measured after every render
  useLayoutEffect(() => {
    const el = row.current
    if (el && tight < 3 && el.scrollWidth > el.clientWidth + 1) setTight(t => t + 1)
  })
  useEdgeFade(row, `${state?.value}${tight}`)
  // a menu opens under its button, lined up with its outer edge: the filters' left, Sort's and Group's right
  const opener = (label: string, items: MenuItem[], align: 'start' | 'end' = 'start') => (e: React.MouseEvent<HTMLElement>) => {
    const el = e.currentTarget
    setAt(a => (a?.el === el ? null : { el, label, items, align }))
  }
  const pick = (c: ToolChoice, icon: LucideIcon) => {
    const items: MenuItem[] = c.options.map(o => ({ label: o.label, current: o.value === c.value, onSelect: () => c.onChange(o.value) }))
    const words = `${c.label}: ${c.options.find(o => o.value === c.value)?.label}`
    if (tight >= 1) return (
      <IconButton icon={icon} label={words} size="sm" className="place-toolbar__tool" aria-haspopup="menu"
                  aria-expanded={at?.label === c.label} onClick={opener(c.label, items, 'end')} />
    )
    return (
      <Button size="sm" variant="ghost" icon={icon} iconEnd={ChevronDown} className="place-toolbar__tool" aria-haspopup="menu"
              aria-expanded={at?.label === c.label} onClick={opener(c.label, items, 'end')} title={`${c.label}: ${c.options.find(o => o.value === c.value)?.label}`}>
        {c.label}: <b>{c.options.find(o => o.value === c.value)?.label}</b>
      </Button>
    )
  }
  return (
    <div className={cx('place-toolbar', className)} role="toolbar" aria-label={state?.label ?? 'Filters'}>
      <div ref={row} className="place-toolbar__row">
      {state && tight >= 3 && (() => {
        const now = state.options.find(o => o.value === state.value)
        const items: MenuItem[] = state.options.map(o => ({ label: o.count ? `${o.label} ${o.count}` : o.label, current: o.value === state.value, onSelect: () => state.onChange(o.value) }))
        return (
          <Button size="sm" variant="ghost" iconEnd={ChevronDown} className="place-toolbar__tool" aria-haspopup="menu"
                  aria-expanded={at?.label === state.label} title={state.label} onClick={opener(state.label, items)}>
            {now?.label}{!!now?.count && <span className="place-toolbar__n">{now.count}</span>}
          </Button>
        )
      })()}
      {state && tight < 3 && (
        <Segmented label={state.label} value={state.value} onChange={state.onChange}
                   options={state.options.map(o => ({ value: o.value, label: <>{o.label}{!!o.count && <span className="place-toolbar__n">{o.count}</span>}</> }))} />
      )}
      {filters.map(f => f.picked
        ? (
          <span key={f.label} className="place-toolbar__chip">
            <button type="button" className="place-toolbar__pick" aria-haspopup="menu" aria-expanded={at?.label === f.label}
                    onClick={opener(f.label, f.items)}>
              <f.icon size={ICON.sm} aria-hidden /><span>{f.picked}</span>
            </button>
            <IconButton icon={X} label={`Clear ${f.label}`} size="sm" iconSize="xs" onClick={f.onClear} />
          </span>
        )
        : tight >= 2 ? (
          <IconButton key={f.label} icon={f.icon} label={f.label} size="sm" className="place-toolbar__tool" aria-haspopup="menu"
                      aria-expanded={at?.label === f.label} onClick={opener(f.label, f.items)} />
        ) : (
          <Button key={f.label} size="sm" variant="ghost" icon={f.icon} iconEnd={ChevronDown} className="place-toolbar__tool" aria-haspopup="menu"
                  aria-expanded={at?.label === f.label} onClick={opener(f.label, f.items)}>{f.label}</Button>
        ))}
      {toggles.map(t => (
        <Button key={t.label} size="sm" variant="ghost" icon={t.icon} pressed={t.on} className="place-toolbar__tool"
                onClick={() => t.onChange(!t.on)}>{t.label}</Button>
      ))}
      {(sort || group) && (
        <span className="place-toolbar__end">
          {sort && pick(sort, ArrowUpDown)}
          {group && pick(group, Layers)}
        </span>
      )}
      </div>
      {at && <Menu anchor={at.el} items={at.items} label={at.label} align={at.align} onClose={() => setAt(null)} />}
    </div>
  )
}

/* ── GroupHead: above each group of a grouped list, in plain words; the title opens the
   group's own page. It never opens or closes. ── */
export function GroupHead({ icon: Icon, title, detail, count, href, onClick, menu, onDrop, className }: {
  icon?: LucideIcon
  title: ReactNode
  /** what the group is, muted ("Part of Real life") */
  detail?: ReactNode
  /** with its noun: "22 conversations" */
  count?: string
  href?: string
  onClick?: (e: React.MouseEvent<HTMLElement>) => void
  /** the group's own "..." (a folder: Rename, New note here, Move to Trash), also its right-click */
  menu?: MenuItem[]
  /** a row dragged onto the head lands in the group (a note into a folder) */
  onDrop?: (e: React.DragEvent<HTMLElement>) => void
  className?: string
}) {
  const ctx = useContextMenu()
  const [over, setOver] = useState(false)
  const name = typeof title === 'string' ? title : undefined
  const inner = <>{Icon && <Icon size={ICON.md} aria-hidden />}<span>{title}</span></>
  const drop = onDrop && {
    onDragOver: (e: React.DragEvent) => { e.preventDefault(); setOver(true) },
    onDragLeave: () => setOver(false),
    onDrop: (e: React.DragEvent<HTMLElement>) => { e.preventDefault(); setOver(false); onDrop(e) },
  }
  return (
    <div className={cx('group-head', menu && 'group-head--menu', over && 'group-head--drop', className)} {...drop}
         onContextMenu={menu && (e => ctx.open(e, menu, name))}>
      {href ? <a className="group-head__title group-head__title--link" href={href} onClick={onClick}>{inner}</a>
        : onClick ? <button type="button" className="group-head__title group-head__title--link" onClick={onClick}>{inner}</button>
        : <span className="group-head__title">{inner}</span>}
      {detail && <span className="group-head__detail">{detail}</span>}
      {count && <span className="group-head__count">{count}</span>}
      {menu && <MenuButton icon={MoreHorizontal} label={name ? `More for ${name}` : 'More'} items={menu} size="sm" className="group-head__more" />}
      {ctx.menu}
    </div>
  )
}
