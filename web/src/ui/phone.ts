/* Below 768 px the app shows one pane at a time. Parts that change
   their shape there (a Menu into a Sheet) ask usePhone. Below 1024 px (a tablet) the
   place sidebar is a Drawer too, so the page keeps its width: useNarrow. */
import { useCallback, useMemo, useRef, useSyncExternalStore, type PointerEvent } from 'react'

const PHONE = '(max-width: 767px)'
const NARROW = '(max-width: 1023px)'

function useMedia(query: string) {
  return useSyncExternalStore(f => {
    const m = matchMedia(query)
    m.addEventListener('change', f)
    return () => m.removeEventListener('change', f)
  }, () => matchMedia(query).matches, () => false)
}

/** a touch screen: nothing takes the focus by itself there (the keyboard would come up
    and cover the page before anyone asked for it) */
export const coarse = () => matchMedia('(pointer: coarse)').matches

/** a phone-width window, now */
export const isPhone = () => matchMedia(PHONE).matches

/** a tablet-width window or narrower, now */
export const isNarrow = () => matchMedia(NARROW).matches

/** a phone-width window, kept up to date */
export const usePhone = () => useMedia(PHONE)

/** a window too narrow for the place sidebar beside the page (a tablet or a phone) */
export const useNarrow = () => useMedia(NARROW)

/** how long a finger rests on something before it is a long press */
const LONG_PRESS_MS = 500

/** a long press by touch: `start(e, fn)` from onPointerDown runs `fn` once a finger has
    rested LONG_PRESS_MS (a mouse or a pen: nothing, false); `stop` from up, cancel and leave */
export function useLongPress() {
  const timer = useRef(0)
  const stop = useCallback(() => window.clearTimeout(timer.current), [])
  const start = useCallback((e: PointerEvent, fn: () => void) => {
    if (e.pointerType !== 'touch') return false
    stop()
    timer.current = window.setTimeout(fn, LONG_PRESS_MS)
    return true
  }, [stop])
  return useMemo(() => ({ start, stop }), [start, stop])
}
