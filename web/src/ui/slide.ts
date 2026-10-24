/* One moving mark for a row of choices: the rail's pill, a tab's underline, a segmented
   thumb. The container gets --slide-x, --slide-y, --slide-w and --slide-h, the box of its
   chosen child (`selector`) relative to itself; its CSS draws the mark with them and
   transitions them under [data-slide='live'] (set a frame after the first placing, so the
   mark never flies in from the corner). Measured again when the choice changes and when
   anything resizes. */
import { useLayoutEffect, type RefObject } from 'react'

export function useSlide(ref: RefObject<HTMLElement | null>, selector: string, key: unknown) {
  useLayoutEffect(() => {
    const box = ref.current
    if (!box) return
    const place = () => {
      const on = box.querySelector<HTMLElement>(selector)
      if (!on) { box.removeAttribute('data-slide'); return }
      // hidden (a section not on screen): measured again when it shows
      if (!box.offsetParent && getComputedStyle(box).position !== 'fixed') return
      if (!box.dataset.slide) {
        box.dataset.slide = 'still'
        requestAnimationFrame(() => { if (box.dataset.slide) box.dataset.slide = 'live' })
      }
      const a = box.getBoundingClientRect(), b = on.getBoundingClientRect()
      box.style.setProperty('--slide-x', `${b.left - a.left + box.scrollLeft}px`)
      box.style.setProperty('--slide-y', `${b.top - a.top + box.scrollTop}px`)
      box.style.setProperty('--slide-w', `${b.width}px`)
      box.style.setProperty('--slide-h', `${b.height}px`)
    }
    place()
    const ro = new ResizeObserver(place)
    ro.observe(box)
    for (const el of box.children) ro.observe(el)
    return () => ro.disconnect()
  }, [ref, selector, key])
}
