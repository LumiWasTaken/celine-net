/* Motion from JS: the tokens' values, reduced motion, and the one
   way anything leaves. `useLeave` plays an exit on a copy of the element (`.ghost`, its
   own animations off) as React removes it, so Escape, the scrim, X, Cancel and a parent
   dropping it all leave the same way, and no caller waits for an animation. The exits
   move `transform`; a part placed by a shift uses the `translate` property for it (a
   centred toast), so the two never fight. */
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

/** When the component unmounts, a copy of `ref`'s element plays `exit` where the element
    was, then goes. A fixed element's copy sits beside it; one in the page flow is pinned
    at its box. Skipped with reduced motion and on StrictMode's pretend unmount. */
export function useLeave(ref: RefObject<HTMLElement | null>, exit: Exit) {
  const play = useRef(exit)
  play.current = exit
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    return () => {
      const parent = el.parentNode
      if (!parent || !el.isConnected || reduced()) return
      const ghost = el.cloneNode(true) as HTMLElement
      ghost.classList.add('ghost')
      ghost.inert = true
      ghost.setAttribute('aria-hidden', 'true')
      for (const n of [ghost, ...ghost.querySelectorAll('[id]')]) n.removeAttribute('id')
      const pin = getComputedStyle(el).position !== 'fixed' ? el.getBoundingClientRect() : null
      if (pin) Object.assign(ghost.style, { position: 'fixed', left: `${pin.left}px`, top: `${pin.top}px`,
                                            width: `${pin.width}px`, height: `${pin.height}px`, margin: '0' })
      parent.insertBefore(ghost, el.nextSibling)
      // React removes the element in this same commit; still there means it stays
      queueMicrotask(() => {
        if (el.isConnected) { ghost.remove(); return }
        // a transformed parent (the centred toast host) places fixed children itself, and
        // moves once the element is gone: undo its offset
        if (pin) {
          const g = ghost.getBoundingClientRect()
          ghost.style.left = `${2 * pin.left - g.left}px`
          ghost.style.top = `${2 * pin.top - g.top}px`
        }
        const all = [play.current(ghost)].flat()
        Promise.all(all.map(a => a.finished)).catch(() => {}).finally(() => ghost.remove())
      })
    }
  }, [ref])
}
