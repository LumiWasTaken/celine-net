/* Lists: ListRow (the one row), ListSection (a label above a group), VirtualList
   (owns its scroll box and fills its parent, so no page computes 100vh - Npx). */
import {
  useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type Key, type ReactNode, type Ref,
} from 'react'
import { measureElement, useVirtualizer } from '@tanstack/react-virtual'
import { Check, type LucideIcon } from 'lucide-react'
import { Dot, Progress, type DotTone } from './Bits'
import { cx } from './cx'
import { ICON } from './icons'
import { useMove } from './motion'
import type { PickProps } from './Select'
import type { useContextMenu } from './Menu'

/* ── ListRow ── */
export function ListRow({
  leading, dot, icon: Icon, iconAccent = false, title, subtitle, detail, body, meta, aside, column, cell, depth = 0, trailing, trailingAlways = false, selected = false, muted = false,
  unread = false, checked, onCheck, progress, density = 'md', href, onClick, onContextMenu, press: hold, tooltip, className, style, pick,
}: {
  leading?: ReactNode
  /** a state dot before the name (instead of `leading`) */
  dot?: DotTone
  /** a glyph on a tile before the name (instead of `leading`): a folder */
  icon?: LucideIcon
  /** the tile in the accent wash: the one that is home (Real life) */
  iconAccent?: boolean
  /** the name */
  title: ReactNode
  /** ONE muted detail: what it is doing, or when it ended */
  subtitle?: ReactNode
  /** a third line, smaller: where it came from */
  detail?: ReactNode
  /** text that wraps under the title (a lore entry, a found passage), at most three lines */
  body?: ReactNode
  /** new to you: the title strong, an accent dot at the end of its line */
  unread?: boolean
  /** a check that is always there (a channel to include, a model to add): the row toggles it */
  checked?: boolean
  onCheck?: (checked: boolean) => void
  /** a file on its way (0 to 1, null while unknown): a bar under the text */
  progress?: number | null
  /** on the title's line, flush right, small and muted: a time, a count */
  meta?: ReactNode
  /** on the subtitle's line, flush right, small and muted: a kind, a due time */
  aside?: ReactNode
  /** a column beside the text on a wide screen: a terminal's conversation, a folder's faces. With it
      (or `cell`) the row is a wide row: `meta` takes its own column at the right end */
  column?: ReactNode
  /** a short cell before the time: "Ended", "20 documents"; on a phone a line under the text */
  cell?: ReactNode
  /** a branch's steps under the row it came from, drawn as a line to it */
  depth?: number
  /** actions, laid over the right end on hover and focus (in the row, always shown, on touch) */
  trailing?: ReactNode
  trailingAlways?: boolean
  selected?: boolean
  /** ended or settled: the title quiet, its summary as the second line */
  muted?: boolean
  density?: 'sm' | 'md' | 'lg'
  /** a real link (middle-click opens a tab); in-app paths are routed by lib/nav */
  href?: string
  onClick?: (e: React.MouseEvent) => void
  /** the right-click menu (ui/Menu useContextMenu); it makes the row focusable, for the ContextMenu key */
  onContextMenu?: (e: React.MouseEvent) => void
  /** a long press opens the menu (useContextMenu's `press`), where `pick` does not start Select */
  press?: ReturnType<ReturnType<typeof useContextMenu>['press']>
  tooltip?: string
  className?: string
  style?: CSSProperties
  /** the list's Select (ui/Select usePicker): a check before the row while selecting */
  pick?: PickProps
}) {
  if (onCheck && checked !== undefined && !pick) {
    const flip = onCheck, on = checked
    onClick = () => flip(!on)
  }
  const picking = pick?.checked !== undefined
  const box = picking ? pick!.checked : onCheck ? checked : undefined
  const wide = column !== undefined || cell !== undefined
  const cls = cx('list-row', density !== 'md' && `list-row--${density}`, wide && 'list-row--wide', picking && 'list-row--picking',
                 muted && 'list-row--muted', unread && 'list-row--unread', body != null && 'list-row--body', className)
  const hasMeta = meta != null && meta !== false
  const current = selected && !picking ? 'true' as const : undefined
  if (pick) {
    const open = onClick, menu = onContextMenu
    onClick = e => { if (pick.onPick(e)) { e.preventDefault(); e.stopPropagation() } else open?.(e) }
    onContextMenu = e => { if (pick.swallow()) e.preventDefault(); else menu?.(e) }
  }
  const press = pick ? { onPointerDown: pick.onPointerDown, onPointerUp: pick.onPointerUp,
                         onPointerCancel: pick.onPointerCancel, onPointerLeave: pick.onPointerLeave } : hold
  const aria = { 'aria-current': current, 'aria-selected': picking ? pick!.checked : undefined,
                 ...(onCheck && !picking ? { role: 'checkbox', 'aria-checked': !!checked } : {}) }
  const inner = (
    <>
      {box !== undefined && <span className={cx('list-row__pick', box && 'is-on')} aria-hidden>{box && <Check size={ICON.sm} />}</span>}
      {depth > 0 && <span className="list-row__tree" style={{ '--d': depth } as CSSProperties} aria-hidden />}
      {dot ? <Dot tone={dot} />
        : Icon ? <span className={cx('list-row__icon', iconAccent && 'list-row__icon--accent')} aria-hidden><Icon size={ICON.md} /></span>
        : leading}
      <span className="list-row__main">
        <span className="list-row__line">
          <span className="list-row__title">{title}</span>
          {unread && <Dot tone="accent" label="Unread" />}
          {hasMeta && <span className={cx('list-row__meta', wide && 'list-row__meta--line')}>{meta}</span>}
        </span>
        {(subtitle || aside) && (
          <span className="list-row__line">
            <span className="list-row__subtitle">{subtitle}</span>
            {aside && <span className="list-row__meta">{aside}</span>}
          </span>
        )}
        {detail && <span className="list-row__line"><span className="list-row__subtitle list-row__detail">{detail}</span></span>}
        {body != null && <span className="list-row__body">{body}</span>}
        {progress !== undefined && <Progress value={progress ?? undefined} max={progress == null ? null : 1} />}
        {cell != null && <span className="list-row__line list-row__cell list-row__cell--under">{cell}</span>}
      </span>
      {column != null && <span className="list-row__column">{column}</span>}
      {wide && <span className="list-row__cell list-row__cell--side">{cell}</span>}
      {wide && <span className="list-row__meta list-row__time">{hasMeta && meta}</span>}
      {(trailing || wide) && (
        <span className={cx('list-row__trailing', trailingAlways && 'list-row__trailing--always')}
              onClick={e => { e.stopPropagation(); e.preventDefault() }}>{trailing}</span>
      )}
    </>
  )
  if (href) {
    return <a href={href} className={cls} style={style} title={tooltip} {...aria} onClick={onClick}
              onContextMenu={onContextMenu} {...press}>{inner}</a>
  }
  if (onClick && !trailing) {
    return <button type="button" className={cls} style={style} title={tooltip} {...aria}
                   onClick={onClick} onContextMenu={onContextMenu} {...press}>{inner}</button>
  }
  // a row with its own buttons cannot itself be a <button>
  return (
    <div className={cls} style={style} title={tooltip} {...aria} onContextMenu={onContextMenu} {...press}
         role={onCheck && !picking ? 'checkbox' : onClick ? 'button' : undefined} tabIndex={onClick || onContextMenu ? 0 : undefined} onClick={onClick}
         onKeyDown={onClick ? e => {
           if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) { e.preventDefault(); e.currentTarget.click() }
         } : undefined}>
      {inner}
    </div>
  )
}

/* ── ListSection ── */
export function ListSection({ label, className }: {
  label: ReactNode
  className?: string
}) {
  return (
    <div className={cx('list-section', className)}>
      <span>{label}</span>
    </div>
  )
}

/* ── VirtualList ── */
export type VirtualListHandle = {
  scrollToIndex: (index: number, align?: 'auto' | 'start' | 'center' | 'end') => void
  element: HTMLDivElement | null
}

export function VirtualList<T>({
  items, estimate, getKey, renderRow, overscan = 10, onEndReached, endThreshold = 20,
  before, after, className, handleRef, inset = 0, style, label, resetOn, page = false,
}: {
  items: T[]
  /** a row's height in px (or per index) until it is measured */
  estimate: number | ((index: number) => number)
  getKey: (item: T, index: number) => Key
  renderRow: (item: T, index: number) => ReactNode
  overscan?: number
  /** called when the last rows come into view (paging) */
  onEndReached?: () => void
  endThreshold?: number
  /** content above/below the rows inside the same scroll box (an error, "loading more") */
  before?: ReactNode
  after?: ReactNode
  className?: string
  rowClassName?: string
  handleRef?: Ref<VirtualListHandle>
  /** left/right inset of rows in px, for rows with their own rounded hover */
  inset?: number
  style?: CSSProperties
  label?: string
  /** back to the top when this changes (the filter of a filtered list) */
  resetOn?: unknown
  /** the list is part of a page that scrolls (a Section of a long page): it has no scroll
      box of its own and follows the page's */
  page?: boolean
}) {
  const scroller = useRef<HTMLDivElement>(null)
  // rows glide when the items change (a conversation to the top), rise when they arrive and
  // fade where they were when they go; a scroll changes no items and a new filter is another list, so neither moves anything
  const spacer = useRef<HTMLDivElement>(null)
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the keys change with the items, getKey is a new function each render
  const keys = useMemo(() => new Set(items.map((it, i) => String(getKey(it, i)))), [items])
  useMove(spacer, items, { enter: true, leave: true, quiet: resetOn, keys })
  const firstReset = useRef(true)
  const [host, setHost] = useState<HTMLElement | null>(null)
  const [margin, setMargin] = useState(0)
  // in a page: the nearest box that scrolls, and where the list starts inside it
  useLayoutEffect(() => {
    const el = scroller.current
    if (!page || !el) return
    let p = el.parentElement
    while (p && !/(auto|scroll)/.test(getComputedStyle(p).overflowY)) p = p.parentElement
    const box = p ?? (document.scrollingElement as HTMLElement)
    const place = () => setMargin(Math.round(el.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop))
    setHost(box)
    place()
    const ro = new ResizeObserver(place)
    ro.observe(box)
    return () => ro.disconnect()
  }, [page])
  useEffect(() => {
    if (firstReset.current) { firstReset.current = false; return }
    if (!page) scroller.current?.scrollTo({ top: 0 })
  }, [resetOn, page])
  const virt = useVirtualizer({
    count: items.length,
    getScrollElement: () => (page ? host : scroller.current),
    scrollMargin: page ? margin : 0,
    estimateSize: typeof estimate === 'number' ? () => estimate : estimate,
    overscan,
    getItemKey: i => getKey(items[i]!, i),
    // a place stays mounted under [hidden] (App.tsx), where every row measures 0: a row keeps
    // its size there, or the place shows again with its sizes re-measured as rows above the
    // fold, the offset pushed down by their sum and the first rows blank until a reload
    measureElement: (el, entry, v) => (el as HTMLElement).offsetParent ? measureElement(el, entry, v)
      : v.measurementsCache[v.indexFromElement(el)]?.size ?? measureElement(el, entry, v),
  })
  useImperativeHandle(handleRef, () => ({
    scrollToIndex: (i, align = 'auto') => virt.scrollToIndex(i, { align }),
    get element() { return scroller.current },
  }), [virt])

  const rows = virt.getVirtualItems()
  const last = rows.at(-1)?.index ?? -1
  const endRef = useRef(onEndReached)
  endRef.current = onEndReached
  useEffect(() => {
    if (items.length && last >= items.length - endThreshold) endRef.current?.()
  }, [last, items.length, endThreshold])

  return (
    <div ref={scroller} className={cx('vlist', page && 'vlist--page', className)} style={style} role={label ? 'list' : undefined} aria-label={label}>
      {before}
      <div ref={spacer} className="vlist__spacer" style={{ height: virt.getTotalSize() }}>
        {rows.map(v => (
          <div key={v.key} data-index={v.index} data-key={String(v.key)} ref={virt.measureElement}
               className="vlist__row" role={label ? 'listitem' : undefined}
               style={{ transform: `translateY(${v.start - (page ? margin : 0)}px)`,
                        left: inset || undefined, right: inset || undefined }}>
            {renderRow(items[v.index]!, v.index)}
          </div>
        ))}
      </div>
      {after}
    </div>
  )
}
