/* Tip: the one tooltip layer, mounted once (App.tsx). It shows the `data-tip` of the
   control under the pointer or in keyboard focus, and adopts any `title` the same way (the
   title moves into data-tip, so the browser's own tooltip never shows), so every view gets
   the same tips without changes. After --tip-delay on hover, at once on keyboard focus and
   within --tip-skip of the last one; one at a time; pointer down, Escape and scrolling hide
   it. A touch screen has no hover: a long press on an icon button (or a head's button that
   lost its words) shows its tip, and the tap that ends the press does nothing else (a
   row's long press is its menu, so not in a row). A disabled kit button is aria-disabled, so the reason in its tip still shows.
   Text: starts with a verb, about 70 characters at most. */
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { leave, ms, OUT, useLeave } from './motion'
import { LONG_PRESS_MS } from './phone'

/** the text a control's tip shows; a title becomes its data-tip (and its name, when it had no other) */
function adopt(el: HTMLElement): string | null {
  const title = el.getAttribute('title')
  if (title) {
    el.removeAttribute('title')
    el.setAttribute('data-tip', title)
    if (!el.hasAttribute('aria-label') && !el.hasAttribute('aria-labelledby') && !el.textContent?.trim()) el.setAttribute('aria-label', title)
  }
  return el.getAttribute('data-tip') || null
}

// iframes and form parts keep their title: it is their name, not a tip
const SKIP = 'iframe, option, svg *'
const find = (t: EventTarget | null) => {
  const el = t instanceof Element ? t.closest<HTMLElement>('[title], [data-tip]') : null
  return el && !el.matches(SKIP) ? el : null
}

export function TipLayer() {
  const [tip, setTip] = useState<{ text: string; el: HTMLElement } | null>(null)
  const id = useId()
  useEffect(() => {
    let timer = 0
    let hiddenAt = -Infinity
    let at: HTMLElement | null = null
    let press = 0                          // a finger resting on an icon button
    let held: HTMLElement | null = null    // the one whose tip a long press showed
    const hide = () => {
      clearTimeout(timer)
      if (at) { at.removeAttribute('aria-describedby'); hiddenAt = performance.now() }
      at = null
      setTip(null)
    }
    const open = (el: HTMLElement, now: boolean) => {
      const text = adopt(el)
      if (!text) return
      clearTimeout(timer)
      at = el
      const quick = now || performance.now() - hiddenAt < ms('--tip-skip')
      timer = window.setTimeout(() => {
        // a menu or card it opened is showing: the tip would cover it
        if (at !== el || !el.isConnected || el.getAttribute('aria-expanded') === 'true') return
        if (el.getAttribute('aria-label') !== text) el.setAttribute('aria-describedby', id)
        setTip({ text, el })
      }, quick ? 0 : ms('--tip-delay'))
    }
    const over = (e: PointerEvent) => {
      if (e.pointerType === 'touch') return
      const el = find(e.target)
      if (el === at) return
      if (at) hide()
      if (el) open(el, false)
    }
    const focus = (e: FocusEvent) => {
      const el = find(e.target)
      if (el && el.matches(':focus-visible')) { if (at) hide(); open(el, true) }
    }
    const down = (e: PointerEvent) => {
      hide()
      clearTimeout(press)
      held = null
      if (e.pointerType !== 'touch') return
      const el = find(e.target)
      if (!el || !el.matches('.btn--icon, [data-tip-words]') || el.closest('.list-row')) return
      press = window.setTimeout(() => { held = el; open(el, true) }, LONG_PRESS_MS)
    }
    const lift = () => clearTimeout(press)
    // the tap that ends a long press only showed the tip
    const click = (e: MouseEvent) => {
      if (held && e.target instanceof Node && held.contains(e.target)) { e.preventDefault(); e.stopPropagation() }
      held = null
    }
    const blur = (e: FocusEvent) => { if (at && at === e.target) hide() }
    const key = (e: KeyboardEvent) => { held = null; if (e.key === 'Escape' && at) hide() }
    document.addEventListener('pointerover', over, true)
    document.addEventListener('pointerdown', down, true)
    document.addEventListener('pointerup', lift, true)
    document.addEventListener('pointercancel', lift, true)
    document.addEventListener('click', click, true)
    document.addEventListener('focusin', focus, true)
    document.addEventListener('focusout', blur, true)
    document.addEventListener('keydown', key, true)
    document.addEventListener('scroll', hide, true)
    window.addEventListener('blur', hide)
    return () => {
      clearTimeout(timer)
      clearTimeout(press)
      document.removeEventListener('pointerover', over, true)
      document.removeEventListener('pointerdown', down, true)
      document.removeEventListener('pointerup', lift, true)
      document.removeEventListener('pointercancel', lift, true)
      document.removeEventListener('click', click, true)
      document.removeEventListener('focusin', focus, true)
      document.removeEventListener('focusout', blur, true)
      document.removeEventListener('keydown', key, true)
      document.removeEventListener('scroll', hide, true)
      window.removeEventListener('blur', hide)
    }
  }, [id])
  return tip ? createPortal(<TipBox key={tip.text} id={id} text={tip.text} el={tip.el} />, document.body) : null
}

/* above the control, below it at the top edge, always inside the window */
function TipBox({ id, text, el }: { id: string; text: string; el: HTMLElement }) {
  const box = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  useLeave(box, g => leave(g, OUT.fade))
  useLayoutEffect(() => {
    const b = box.current
    if (!b) return
    const r = el.getBoundingClientRect(), w = b.offsetWidth, h = b.offsetHeight, gap = 6
    const top = r.top - h - gap < 8 ? r.bottom + gap : r.top - h - gap
    const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8)
    setPos({ left, top })
  }, [el, text])
  return (
    <div ref={box} id={id} role="tooltip" className="tip" style={pos ?? { left: -9999, top: -9999 }}>{text}</div>
  )
}
