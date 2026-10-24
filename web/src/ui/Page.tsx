/* A page's blocks: Section (one block on its own surface) and PageIndex (a long page as one
   scroll, with a side index that jumps to each section and follows the scroll; a top bar
   that scrolls sideways on a phone). Every section stays open: no tabs, no folds. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { cx } from './cx'
import { SearchField } from './Form'
import type { PlaceSearch } from './Head'
import { MenuButton, type MenuItem } from './Menu'
import { MoreHorizontal } from 'lucide-react'
import { reduced } from './motion'
import { useEdgeFade } from './Bits'

/* ── Section ── */
export function Section({ id, title, count, search, action, more, children, className }: {
  /** the PageIndex target */
  id?: string
  title?: ReactNode
  count?: number
  /** a search in this section, right aligned */
  search?: PlaceSearch
  /** one Button ("New entry"), at the right end of the head */
  action?: ReactNode
  /** the section's rare actions behind "..." at the head's end (Move to Trash) */
  more?: MenuItem[]
  children?: ReactNode
  className?: string
}) {
  const head = title != null || count != null || search || action || more
  return (
    <section id={id} className={cx('ui-section', className)}>
      {head && (
        <div className="ui-section__head">
          {title != null && <h2 className="ui-section__title">{title}</h2>}
          {count != null && <span className="ui-section__count">{count}</span>}
          {search && (
            <SearchField value={search.value} onChange={search.onChange} placeholder={search.placeholder ?? 'Search'}
                         label={search.label} loading={search.loading} inputRef={search.inputRef} className="ui-section__search" />
          )}
          {action && <div className="ui-section__action">{action}</div>}
          {more && <MenuButton icon={MoreHorizontal} label={typeof title === 'string' ? `More for ${title}` : 'More'} items={more} className="ui-section__more" />}
        </div>
      )}
      {children}
    </section>
  )
}

/* ── SubHead: a small heading inside a Section, over one group of its rows (a role's
   models, a schedule's runs); its count and one action beside it ── */
export function SubHead({ children, count, action }: { children: ReactNode; count?: number; action?: ReactNode }) {
  return (
    <div className="ui-subhead">
      <h3 className="ui-subhead__title">{children}</h3>
      {count != null && <span className="ui-section__count">{count}</span>}
      {action && <div className="ui-section__action">{action}</div>}
    </div>
  )
}

/* ── PageIndex: the index beside the page's sections, and the page itself ── */
export function PageIndex({ sections, children, label = 'On this page', className }: {
  /** in the page's order; `id` is the Section's */
  sections: { id: string; label: string; count?: number }[]
  /** the Sections */
  children: ReactNode
  label?: string
  className?: string
}) {
  const [on, setOn] = useState(sections[0]?.id ?? '')
  const nav = useRef<HTMLElement>(null)
  // the phone's top bar fades at the edge that has more
  useEdgeFade(nav, on)
  // a jump holds the mark on its target until the scroll it started ends
  const jumping = useRef(false)
  const ids = sections.map(s => s.id).join(' ')

  // the current one: the last section whose top has passed the top fifth of the box that
  // scrolls; at the very end, the last section. Sections drawn later (a text still loading)
  // count once they are there: the page changing size picks again
  useEffect(() => {
    const root = nav.current?.parentElement
    if (!root) return
    let box: HTMLElement | null = root.parentElement
    while (box && !/(auto|scroll)/.test(getComputedStyle(box).overflowY)) box = box.parentElement
    const scroller: HTMLElement | Window = box ?? window
    let frame = 0
    const pick = () => {
      frame = 0
      if (jumping.current) return
      const els = ids.split(' ').map(id => document.getElementById(id)).filter((e): e is HTMLElement => !!e)
      if (!els.length) return
      const top = box ? box.getBoundingClientRect().top : 0
      const height = box ? box.clientHeight : window.innerHeight
      // scrolled to the very end; a page that fits on screen never scrolled, so it starts at the first
      const atEnd = box ? box.scrollTop > 0 && box.scrollTop + box.clientHeight >= box.scrollHeight - 2
        : window.scrollY > 0 && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2
      let cur = els[0]!
      for (const el of els) if (el.getBoundingClientRect().top - top <= height / 5) cur = el
      setOn(atEnd && els.at(-1)!.getBoundingClientRect().top - top < height ? els.at(-1)!.id : cur.id)
    }
    const onScroll = () => { if (!frame) frame = requestAnimationFrame(pick) }
    const onEnd = () => { jumping.current = false }
    scroller.addEventListener('scroll', onScroll, { passive: true })
    scroller.addEventListener('scrollend', onEnd)
    const ro = new ResizeObserver(onScroll)
    ro.observe(root)
    pick()
    return () => {
      scroller.removeEventListener('scroll', onScroll); scroller.removeEventListener('scrollend', onEnd)
      ro.disconnect(); cancelAnimationFrame(frame)
    }
  }, [ids])

  // the phone's top bar keeps the current one in view
  useEffect(() => {
    const a = nav.current?.querySelector<HTMLElement>('[aria-current="true"]')
    const bar = nav.current
    if (a && bar && bar.scrollWidth > bar.clientWidth) bar.scrollTo({ left: a.offsetLeft - (bar.clientWidth - a.offsetWidth) / 2 })
  }, [on])

  const jump = (id: string) => (e: React.MouseEvent) => {
    e.preventDefault()
    const el = document.getElementById(id)
    if (!el) return
    setOn(id)
    jumping.current = true
    // a scroll that ends nowhere new (already there) sends no scrollend
    window.setTimeout(() => { jumping.current = false }, 1500)
    el.scrollIntoView({ behavior: reduced() ? 'auto' : 'smooth', block: 'start' })
  }

  return (
    <div className={cx('page-index', className)}>
      <nav ref={nav} className="page-index__nav" aria-label={label}>
        {sections.map(s => (
          <a key={s.id} href={`#${s.id}`} aria-current={s.id === on || undefined} onClick={jump(s.id)}>
            <span>{s.label}</span>
            {s.count != null && <span className="page-index__n">{s.count}</span>}
          </a>
        ))}
      </nav>
      <div className="page-index__body">{children}</div>
    </div>
  )
}
