/* Motion from JS: the tokens' values, reduced motion, the one way anything leaves, and
   the one way things in a list move. `useLeave` plays an exit on a copy of the element
   (`.ghost`, its own animations off) as React removes it, so Escape, the scrim, X, Cancel
   and a parent dropping it all leave the same way, and no caller waits for an animation.
   `useMove` glides the children of a box to their new places (FLIP). The exits move
   `transform`; the glides and the rise move the `translate` property, so neither fights
   a part's own transform (a virtual row's offset, a press, a centred toast). */
import { useLayoutEffect, useRef, type RefObject } from 'react'

const root = () => getComputedStyle(document.documentElement)

/** a duration token in ms: ms('--dur-fast') */
export function ms(name: string): number {
  const v = root().getPropertyValue(name).trim()
  const n = parseFloat(v)
  if (!Number.isFinite(n)) return 0
  return v.endsWith('ms') ? n : v.endsWith('s') ? n * 1000 : n
}

/** an easing token: curve('--ease') */
export const curve = (name: string) => root().getPropertyValue(name).trim()

/** the person asked for less motion: skip it */
export const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches

/** the exits */
export const OUT = {
  fade:   [{ opacity: 1 }, { opacity: 0 }],
  shrink: [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(0.98)' }],
  zoom:   [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(0.96)' }],
  drop:   [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(2px)' }],
  // one frame: it starts where the element is (a sheet let go mid-drag)
  down:   [{ transform: 'translateY(100%)' }],
} satisfies Record<string, Keyframe[]>

/** one exit on one element of the copy, on --ease-exit */
export function leave(el: Element, frames: Keyframe[], dur: '--dur-fast' | '--dur' = '--dur-fast') {
  return el.animate(frames, { duration: ms(dur), easing: curve('--ease-exit'), fill: 'forwards' })
}

export type Exit = (ghost: HTMLElement) => Animation | Animation[]

type Rect = { left: number; top: number; width: number; height: number }

/** A still copy of `el` for an exit, put into `into` before `before` (beside `el` by
    default). A fixed element's copy sits where it is; one in the flow is pinned at `at`,
    its box. `place()` once the copy is in the page and `el` is gone: a transformed parent
    (the centred toast host) places fixed children itself and moves once `el` is gone, so
    its offset is undone. */
export function copyOf(el: HTMLElement, at: Rect | null, into: Node = el.parentNode!, before: Node | null = el.nextSibling) {
  const ghost = el.cloneNode(true) as HTMLElement
  ghost.classList.add('ghost')
  ghost.inert = true
  ghost.setAttribute('aria-hidden', 'true')
  for (const n of [ghost, ...ghost.querySelectorAll('[id]')]) n.removeAttribute('id')
  if (at) Object.assign(ghost.style, { position: 'fixed', left: `${at.left}px`, top: `${at.top}px`, right: 'auto', bottom: 'auto',
                                       width: `${at.width}px`, height: `${at.height}px`, margin: '0', transform: 'none', translate: 'none' })
  into.insertBefore(ghost, before)
  const place = () => {
    if (!at) return
    const g = ghost.getBoundingClientRect()
    ghost.style.left = `${2 * at.left - g.left}px`
    ghost.style.top = `${2 * at.top - g.top}px`
  }
  return { ghost, place }
}

/** play `exit` on a copy, then drop the copy */
export function play(ghost: HTMLElement, exit: Exit) {
  const all = [exit(ghost)].flat()
  Promise.all(all.map(a => a.finished)).catch(() => {}).finally(() => ghost.remove())
}

/** When the component unmounts, a copy of `ref`'s element plays `exit` where the element
    was, then goes. Skipped with reduced motion and on StrictMode's pretend unmount. */
export function useLeave(ref: RefObject<HTMLElement | null>, exit: Exit) {
  const exitRef = useRef(exit)
  exitRef.current = exit
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    return () => {
      if (!el.parentNode || !el.isConnected || reduced()) return
      const { ghost, place } = copyOf(el, getComputedStyle(el).position !== 'fixed' ? el.getBoundingClientRect() : null)
      // React removes the element in this same commit; still there means it stays
      queueMicrotask(() => {
        if (el.isConnected) { ghost.remove(); return }
        place()
        play(ghost, exitRef.current)
      })
    }
  }, [ref])
}

/** `rise` (base.css) from JS, on the translate property: a row that arrives while you look */
export function rise(el: Element) {
  return el.animate([{ opacity: 0, translate: `0 ${root().getPropertyValue('--rise').trim()}` }, { opacity: 1, translate: '0 0' }],
                    { duration: ms('--dur'), easing: curve('--ease'), composite: 'add' })
}

/** `fade` (base.css) from JS: what replaces a skeleton */
export const fadeIn = (el: Element) => el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: ms('--dur'), easing: curve('--ease') })

type Box = { x: number; y: number; w: number; h: number }
const moving = new WeakMap<Element, Animation>()

/** FLIP for the children of `ref` (rows, cards, toasts). After a commit that changed `on`
    (every commit when `on` is undefined) a child that moved glides from where it was, on
    --dur --ease. `enter`: a child that was not there rises in. `leave`: a child that went
    fades out where it was, on --dur-fast. A change of `quiet` (a new filter) moves nothing
    that commit: it is another list, not this one changing. Children are measured against
    the box (its bottom edge for `bottom`, a stack that grows upward), so a scroll is no
    move; a hidden box (a place in the background) forgets them, so nothing glides when it
    shows again. Skipped with reduced motion. */
export function useMove(ref: RefObject<HTMLElement | null>, on?: unknown, { enter = false, leave: out = false, quiet = undefined as unknown, bottom = false } = {}) {
  const seen = useRef(new Map<Element, Box>())
  const was = useRef(on)
  const calm = useRef(quiet)
  useLayoutEffect(() => {
    const box = ref.current
    if (!box) return
    const changed = on === undefined || on !== was.current
    was.current = on
    const other = quiet !== calm.current
    calm.current = quiet
    const before = seen.current
    const kids = [...box.children].filter(el => !el.classList.contains('ghost'))
    if (changed) for (const el of kids) moving.get(el)?.cancel()
    const c = box.getBoundingClientRect()
    const now = new Map<Element, Box>()
    seen.current = now
    if (!c.width && !c.height) return
    for (const el of kids) {
      const r = el.getBoundingClientRect()
      now.set(el, { x: r.left - c.left, y: r.top - (bottom ? c.bottom : c.top), w: r.width, h: r.height })
    }
    if (!changed || other || !before.size || reduced()) return
    for (const [el, b] of now) {
      const a = before.get(el)
      if (!a) { if (enter) rise(el); continue }
      const dx = a.x - b.x, dy = a.y - b.y
      if (!dx && !dy) continue
      moving.set(el, el.animate([{ translate: `${dx}px ${dy}px` }, { translate: '0 0' }],
                                { duration: ms('--dur'), easing: curve('--ease'), composite: 'add' }))
    }
    if (!out) return
    for (const [el, a] of before) {
      if (now.has(el) || el.isConnected) continue
      const { ghost, place } = copyOf(el as HTMLElement, { left: c.left + a.x, top: (bottom ? c.bottom : c.top) + a.y, width: a.w, height: a.h }, box, null)
      place()
      play(ghost, g => leave(g, OUT.fade))
    }
  })
}
