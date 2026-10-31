/* Card: one thing in a grid of things with a picture or a face (people, folders,
   documents, presets), or one pick of a few (a preset, a picture).
   Its "..." and a right-click (or a long press) open the same menu. */
import { useRef, type ReactNode } from 'react'
import { Check, MoreHorizontal } from 'lucide-react'
import { cx } from './cx'
import { ICON } from './icons'
import type { PickProps } from './Select'
import { MenuButton, useContextMenu, type MenuItem } from './Menu'
import { useMove } from './motion'

export function Card({ media, cover, title, subtitle, meta, selected, onClick, href, menu, menuLabel, children, actions, pick, className, tooltip }: {
  /** a face or a glyph before the title */
  media?: ReactNode
  /** a picture across the card's whole top, to its edges (a cover, a portrait) */
  cover?: ReactNode
  title: ReactNode
  /** one muted line */
  subtitle?: ReactNode
  /** at the title's right: a time, a count */
  meta?: ReactNode
  /** a pick of one of a few: the accent ring */
  selected?: boolean
  onClick?: (e: React.MouseEvent<HTMLElement>) => void
  href?: string
  /** the "..." items, also its right-click */
  menu?: MenuItem[]
  menuLabel?: string
  /** more under the head (a few lines of text) */
  children?: ReactNode
  /** buttons at the card's foot (Continue, Open): their own presses, beside the card's */
  actions?: ReactNode
  /** the grid's Select (ui/Select usePicker): a check on the card while selecting */
  pick?: PickProps
  className?: string
  tooltip?: string
}) {
  const ctx = useContextMenu()
  const name = menuLabel ?? (typeof title === 'string' ? title : undefined)
  const picking = pick?.checked !== undefined
  if (pick) {
    const open = onClick
    onClick = e => { if (pick.onPick(e)) { e.preventDefault(); e.stopPropagation() } else open?.(e) }
  }
  const cls = cx('card', (selected || pick?.checked) && 'card--selected', (onClick || href) && 'card--press', cover != null && 'card--cover',
                 picking && 'card--picking', className)
  const body = (
    <>
      {cover != null && <span className="card__cover">{cover}</span>}
      {media && <span className="card__media">{media}</span>}
      <span className="card__head">
        <span className="card__title">{title}</span>
        {meta != null && <span className="card__meta">{meta}</span>}
      </span>
      {subtitle && <span className="card__sub">{subtitle}</span>}
      {children}
    </>
  )
  const right = menu && !picking && {
    onContextMenu: (e: React.MouseEvent) => ctx.open(e, menu, name),
    ...ctx.press(() => menu, name),
  }
  const press = pick && { onPointerDown: pick.onPointerDown, onPointerUp: pick.onPointerUp,
                          onPointerCancel: pick.onPointerCancel, onPointerLeave: pick.onPointerLeave }
  // a card with its own "..." cannot itself be a <button>: the press is a cover under it
  const main = href
    ? <a className="card__press" href={href} onClick={onClick} aria-current={selected || undefined} title={tooltip} {...press}>{body}</a>
    : onClick
      ? <button type="button" className="card__press" onClick={onClick} aria-pressed={picking ? pick!.checked : selected} title={tooltip} {...press}>{body}</button>
      : <div className="card__press" title={tooltip}>{body}</div>
  return (
    <div className={cls} {...right}>
      {main}
      {actions && !picking && <div className="card__foot">{actions}</div>}
      {picking && <span className={cx('list-row__pick', 'card__pick', pick!.checked && 'is-on')} aria-hidden>{pick!.checked && <Check size={ICON.sm} />}</span>}
      {menu && !picking && <MenuButton icon={MoreHorizontal} label={name ? `More for ${name}` : 'More'} items={menu} size="sm" className="card__more" />}
      {ctx.menu}
    </div>
  )
}

/** cards in as many columns as fit; a card that arrives rises, one that goes fades where
    it was, the rest glide to their places (a new sort, a card gone), whenever `on` (the
    list the cards are drawn from) changes */
export function CardGrid({ on, className, children }: { on: unknown; className?: string; children?: ReactNode }) {
  const box = useRef<HTMLDivElement>(null)
  useMove(box, on, { enter: true, leave: true })
  return <div ref={box} className={cx('card-grid', className)}>{children}</div>
}
