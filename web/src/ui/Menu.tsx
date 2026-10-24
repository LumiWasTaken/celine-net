/* Menu: a list of actions that grows from its button, or from the pointer for a
   right-click (the same menu both ways). Focus moves into it, on the first item that can act;
   outside click, Escape and Tab close it; arrows move; focus goes back to the button, or
   for a right-click to where it was before. Rendered into <body>.

   One list per thing: its "..." button (MenuButton), a right-click, a long press and the
   ContextMenu key (useContextMenu) all show the same items. A list with nothing that can
   act opens no menu. An item can lead with a face (a person, a folder). A menu
   of more than twelve items has a search field at its top: typing narrows it, arrows and
   Enter work from the field, Escape clears it and then closes. Every menu stops at about
   60 % of the window and scrolls inside. On a phone a menu of more than five items is a
   Sheet from the bottom edge with the same rows and the same search. An item that cannot
   act stays only where leaving it out would confuse, and then says why (its `title`) as a
   muted second line. At most three groups, the destructive one last. */
import {
  useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown, Circle, type LucideIcon } from 'lucide-react'
import { Button, IconButton, type ButtonSize } from './Button'
import { cx } from './cx'
import { ICON } from './icons'
import { leave, OUT, useLeave } from './motion'
import { Sheet } from './Dialog'
import { SearchField } from './Form'
import { useLongPress, usePhone } from './phone'

export type MenuItem =
  | {
      label: ReactNode; onSelect: () => void; danger?: boolean; disabled?: boolean; icon?: LucideIcon; title?: string
      /** one muted line under the label (a version's change note) */
      detail?: ReactNode
      /** the one that is shown now: marked with a check */
      current?: boolean
      /** leads the row in place of the icon: a small face (Portrait, Avatar, Face size sm) */
      face?: ReactNode
    }
  | 'sep'
  /** a muted label over the items after it (Sort, Group in the View menu) */
  | { heading: string }

const isAct = (it: MenuItem): it is Extract<MenuItem, { onSelect: () => void }> => it !== 'sep' && !('heading' in it)

type MenuAnchor = HTMLElement | { x: number; y: number }

/** a menu with more items than this has a search field */
const LONG = 12

/** something in the list can act */
const actionable = (items: MenuItem[]) => items.some(it => isAct(it) && !it.disabled)

export function Menu({ anchor, items, onClose, align = 'start', label, inline = false, search }: {
  /** where it opens; left out only with `inline` */
  anchor?: MenuAnchor
  items: MenuItem[]
  onClose: () => void
  /** for an element anchor: line the menu up with its left (start) or right (end) edge */
  align?: 'start' | 'end'
  label?: string
  /** drawn in place, open, for /parts: no portal, no placing, no focus */
  inline?: boolean
  /** the placeholder of the search field a long menu has ("Search people") */
  search?: string
}) {
  const phone = usePhone()
  const [q, setQ] = useState('')
  const acts = items.filter(isAct)
  // more than twelve: a search field at the top that narrows the items by their words
  const find = acts.length > LONG ? search ?? 'Search' : undefined
  const needle = q.trim().toLowerCase()
  const shown = needle ? acts.filter(it => typeof it.label === 'string' && it.label.toLowerCase().includes(needle)) : items
  // a phone: a menu of more than five is a Sheet from the bottom edge
  if (phone && !inline && acts.length > 5) {
    return <MenuSheet items={shown} label={label} onClose={onClose} search={find} q={q} setQ={setQ} />
  }
  return <MenuBox anchor={anchor} items={shown} onClose={onClose} align={align} label={label} inline={inline} search={find} q={q} setQ={setQ} />
}

/** Enter in the search field picks the first item that can act */
const pickFirst = (e: React.KeyboardEvent<HTMLInputElement>, menu: HTMLElement | null) => {
  if (e.key !== 'Enter') return
  e.preventDefault()
  menu?.querySelector<HTMLElement>('.menu__item:not(:disabled)')?.click()
}

/* the items as a Sheet's rows: the same words, icons and order */
function MenuSheet({ items, label, onClose, search, q, setQ }: {
  items: MenuItem[]; label?: string; onClose: () => void; search?: string; q: string; setQ: (q: string) => void
}) {
  const list = useRef<HTMLDivElement>(null)
  return (
    <Sheet title={label ?? 'More'} onClose={onClose}>
      {search && (
        <div className="menu-sheet__search">
          <SearchField value={q} onChange={setQ} placeholder={search} onKeyDown={e => pickFirst(e, list.current)} />
        </div>
      )}
      <div ref={list} className="menu menu--sheet" role="menu" aria-label={label}>
        <Items items={items} close={onClose} />
      </div>
    </Sheet>
  )
}

function Items({ items, close }: { items: MenuItem[]; close: (refocus: boolean) => void }) {
  return <>{items.map((it, i) => it === 'sep'
    ? <div key={`sep-${i}`} className="menu__sep" role="separator" />
    : !isAct(it) ? <div key={`h-${i}`} className="menu__heading" role="presentation">{it.heading}</div>
    : (
      <button key={i} type="button" role={it.current === undefined ? 'menuitem' : 'menuitemradio'}
              aria-checked={it.current} className={cx('menu__item', it.danger && 'menu__item--danger')}
              disabled={it.disabled} title={it.disabled ? undefined : it.title}
              onClick={() => { close(true); it.onSelect() }}>
        {it.face ? <span className="menu__face">{it.face}</span>
          : it.current !== undefined && !it.icon
          ? (it.current ? <Check size={ICON.md} className="menu__check" aria-hidden /> : <Circle size={ICON.md} aria-hidden />)
          : it.icon && <it.icon size={ICON.md} aria-hidden />}
        <span className="menu__text">
          <span>{it.label}</span>
          {(it.detail || (it.disabled && it.title)) && <span className="menu__detail">{it.detail ?? it.title}</span>}
        </span>
        {it.current && (it.icon || it.face) && <Check size={ICON.sm} className="menu__check" aria-hidden />}
      </button>
    ))}</>
}

function MenuBox({ anchor, items, onClose, align, label, inline, search, q, setQ }: {
  anchor?: MenuAnchor; items: MenuItem[]; onClose: () => void; align: 'start' | 'end'; label?: string; inline: boolean
  search?: string; q: string; setQ: (q: string) => void
}) {
  const box = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number; maxHeight?: number; width?: number; transformOrigin?: string } | null>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  const before = useRef(document.activeElement as HTMLElement | null)
  useLeave(box, g => leave(g, OUT.shrink))

  // place it, then keep it on screen; a menu taller than the room scrolls, under its button
  useLayoutEffect(() => {
    const el = box.current
    if (!el || !anchor) return
    // offset sizes: the box grows in, and its rect is the scaled one while it does
    const width = el.offsetWidth, height = el.offsetHeight
    let left: number, top: number, maxHeight: number | undefined, up = false
    if (anchor instanceof HTMLElement) {
      const r = anchor.getBoundingClientRect()
      left = align === 'end' ? r.right - width : r.left
      top = r.bottom + 4
      const below = window.innerHeight - 8 - top
      if (height > below) {
        if (height <= r.top - 12) { top = r.top - height - 4; up = true }
        else maxHeight = below
      }
    } else {
      left = anchor.x; top = anchor.y
      if (top + height > window.innerHeight - 8) top = Math.max(8, anchor.y - height)
    }
    left = Math.min(Math.max(8, left), window.innerWidth - width - 8)
    // it grows from where it was asked for: the pointer, or the button
    const from = anchor instanceof HTMLElement ? anchor.getBoundingClientRect() : { left: anchor.x, top: anchor.y, width: 0 }
    const ox = Math.min(Math.max(0, from.left + from.width / 2 - left), width)
    const oy = from.top > top ? (maxHeight ?? height) : 0
    // above its button the menu hangs from its bottom edge, so a search that narrows it
    // keeps it on the button; with a search its width stays while you type
    setPos({ left, ...up ? { bottom: window.innerHeight - top - height } : { top }, maxHeight,
             width: search ? width : undefined, transformOrigin: up ? `${ox}px 100%` : `${ox}px ${oy}px` })
  }, [anchor, align]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (inline) return
    if (!search) box.current?.querySelector<HTMLElement>('.menu__item:not(:disabled)')?.focus({ preventScroll: true })
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (box.current?.contains(t)) return
      if (anchor instanceof HTMLElement && anchor.contains(t)) return
      closeRef.current()
    }
    const onBlur = () => closeRef.current()
    document.addEventListener('mousedown', onDown, true)
    window.addEventListener('blur', onBlur)
    window.addEventListener('resize', onBlur)
    return () => {
      document.removeEventListener('mousedown', onDown, true)
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('resize', onBlur)
    }
  }, [anchor, inline]) // eslint-disable-line react-hooks/exhaustive-deps

  const close = (refocus: boolean) => {
    onClose()
    if (!refocus) return
    const back = anchor instanceof HTMLElement ? anchor : before.current
    if (back && document.contains(back)) back.focus({ preventScroll: true })
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    const list = [...(box.current?.querySelectorAll<HTMLElement>('.menu__item:not(:disabled)') ?? [])]
    const i = list.indexOf(document.activeElement as HTMLElement)
    const field = box.current?.querySelector<HTMLInputElement>('.menu__search input')
    const inField = e.target === field
    // the field takes the focus back: from the first item up, or any letter typed on an item
    const toField = () => { field?.focus({ preventScroll: true }); box.current?.scrollTo({ top: 0 }) }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true) }
    else if (e.key === 'Tab') { close(false) }
    else if (e.key === 'ArrowDown') { e.preventDefault(); list[(i + 1) % list.length]?.focus() }
    else if (e.key === 'ArrowUp') {
      e.preventDefault()
      if (field && i === 0) toField()
      else list[i < 0 ? list.length - 1 : (i - 1 + list.length) % list.length]?.focus()
    }
    else if (inField) return
    else if (e.key === 'Home') { e.preventDefault(); list[0]?.focus() }
    else if (e.key === 'End') { e.preventDefault(); list.at(-1)?.focus() }
    else if (field && e.key.length === 1 && e.key !== ' ' && !e.ctrlKey && !e.metaKey && !e.altKey) toField()
  }

  const menu = (
    <div ref={box} className={cx('menu', inline && 'menu--inline')} role="menu" aria-label={label} onKeyDown={onKeyDown}
         style={inline ? undefined : pos ?? { left: -9999, top: -9999 }} onContextMenu={e => e.preventDefault()}>
      {search && (
        <div className="menu__search">
          <SearchField value={q} onChange={setQ} placeholder={search} size="sm" autoFocus={!inline}
                       onKeyDown={e => pickFirst(e, box.current)} />
        </div>
      )}
      <Items items={items} close={close} />
    </div>
  )
  return inline ? menu : createPortal(menu, document.body)
}

/** a menu opens toward the wider side of the window: from a button's right edge on the
    right half, from its left edge on the left half, so it stays over what the button is in */
const side = (el: HTMLElement): 'start' | 'end' => {
  const r = el.getBoundingClientRect()
  return r.left + r.width / 2 < window.innerWidth / 2 ? 'start' : 'end'
}

/* A button that opens a Menu under itself: an icon button, or with `text` a quiet text
   button with a chevron (the version menu: text "v4, 18:04", one item per version). */
export function MenuButton({ icon, text, label, items, size = 'md', className }: {
  icon?: LucideIcon
  /** shown on the button instead of an icon */
  text?: ReactNode
  /** accessible name and tooltip */
  label: string
  items: MenuItem[]
  size?: ButtonSize
  className?: string
}) {
  const btn = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  // nothing in it can act: no menu
  const props = { ref: btn, size, className, 'aria-haspopup': 'menu' as const, 'aria-expanded': open,
                  disabled: !actionable(items), onClick: () => setOpen(o => !o) }
  return (
    <>
      {text != null || !icon
        ? <Button {...props} variant="ghost" iconEnd={ChevronDown} title={label}>{text ?? label}</Button>
        : <IconButton {...props} icon={icon} label={label} />}
      {open && btn.current && <Menu anchor={btn.current} items={items} align={side(btn.current)} label={label} onClose={() => setOpen(false)} />}
    </>
  )
}

/* Right-click: `open(e, items)` from an onContextMenu handler shows the menu at the
   pointer; render `menu` once somewhere in the component. An empty list, or text you
   selected, leaves the browser's own menu alone (so Copy of a selection still works).
   The ContextMenu key and Shift+F10 send the same event: the menu opens at the row.
   iOS sends no contextmenu for a long press: `press(items, label)` spread on a target
   opens the same menu when a finger rests on it half a second, and swallows the tap that
   ends the press. */
export function useContextMenu() {
  const [at, setAt] = useState<{ x: number; y: number; items: MenuItem[]; label?: string } | null>(null)
  const held = useRef(false)
  const long = useLongPress()
  const press = useCallback((items: () => MenuItem[], label?: string) => {
    const { stop } = long
    return {
      onPointerDown: (e: React.PointerEvent) => {
        const { clientX: x, clientY: y } = e
        if (long.start(e, () => {
          const list = items()
          if (actionable(list)) { held.current = true; setAt({ x, y, items: list, label }) }
        })) held.current = false
      },
      onPointerUp: stop, onPointerCancel: stop, onPointerLeave: stop,
      onClickCapture: (e: React.MouseEvent) => {
        if (held.current) { held.current = false; e.preventDefault(); e.stopPropagation() }
      },
    }
  }, [long])
  const open = useCallback((e: React.MouseEvent, items: MenuItem[], label?: string) => {
    if (!actionable(items) || e.defaultPrevented) return
    const sel = window.getSelection()
    if (sel && !sel.isCollapsed && sel.toString().trim()) return
    e.preventDefault()
    // from the keyboard the event has no pointer: at the top of the focused row
    const r = e.clientX === 0 && e.clientY === 0 ? (e.currentTarget as HTMLElement).getBoundingClientRect() : null
    setAt(r ? { x: r.left + 8, y: Math.min(r.bottom, r.top + 32), items, label } : { x: e.clientX, y: e.clientY, items, label })
  }, [])
  const menu = at
    ? <Menu anchor={at} items={at.items} label={at.label} onClose={() => setAt(null)} />
    : null
  return { open, press, menu }
}
