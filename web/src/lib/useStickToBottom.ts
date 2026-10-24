import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

const BOTTOM_THRESHOLD = 64

/** A transcript pinned to its bottom while it grows. Only you unpin it: a scroll up with
    the wheel, a finger, the keys or the scrollbar; a scroll the page itself makes (a reply
    growing, a burst, a smooth jump) never does. Unpinned, `unseen` says new text came.
    `follow`: what grows it. `reset`: another conversation, pinned again. `observe`: when
    to watch what grows without a new line (a font, an image, a fold). */
export function useStickToBottom(ref: RefObject<HTMLElement | null>, follow: unknown[],
                                 reset: unknown[], observe: unknown[]) {
  const atBottom = useRef(true)
  const escapedFromLock = useRef(false)
  const [unseen, setUnseen] = useState(false)
  const jumping = useRef(false)
  const programmatic = useRef(false)
  const lastScrollTop = useRef(0)
  const rafId = useRef<number | null>(null)

  const isNearBottom = (el: HTMLElement) =>
    el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_THRESHOLD

  const targetScrollTop = (el: HTMLElement) =>
    Math.max(0, el.scrollHeight - el.clientHeight)

  const stopAnimation = () => {
    if (rafId.current != null) {
      cancelAnimationFrame(rafId.current)
      rafId.current = null
    }
  }

  const smoothScrollToBottom = useCallback((instant = false) => {
    const el = ref.current
    if (!el) return
    stopAnimation()

    const target = targetScrollTop(el)
    const distance = target - el.scrollTop

    if (distance <= 0) return

    const reducedMotion = typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches
    if (instant || reducedMotion || distance > 1000) {
      programmatic.current = true
      el.scrollTop = target
      lastScrollTop.current = el.scrollTop
      return
    }

    const animate = () => {
      const currentEl = ref.current
      if (!currentEl || escapedFromLock.current || !atBottom.current) {
        stopAnimation()
        return
      }
      const currentTarget = targetScrollTop(currentEl)
      const diff = currentTarget - currentEl.scrollTop

      if (diff <= 1) {
        programmatic.current = true
        currentEl.scrollTop = currentTarget
        lastScrollTop.current = currentEl.scrollTop
        rafId.current = null
        return
      }

      // Smooth ease-out glide: moves smoothly to the moving target
      const step = Math.max(2, Math.ceil(diff * 0.25))
      programmatic.current = true
      currentEl.scrollTop += step
      lastScrollTop.current = currentEl.scrollTop
      rafId.current = requestAnimationFrame(animate)
    }

    rafId.current = requestAnimationFrame(animate)
  }, [ref])

  const unpin = useCallback(() => {
    stopAnimation()
    escapedFromLock.current = true
    atBottom.current = false
    jumping.current = false
  }, [])

  const onScroll = useCallback(() => {
    const el = ref.current
    if (!el) return

    const currentScrollTop = el.scrollTop
    const isProg = programmatic.current
    programmatic.current = false

    const previousScrollTop = lastScrollTop.current
    lastScrollTop.current = currentScrollTop

    if (isProg) return

    const near = isNearBottom(el)

    // User scrolled UP
    if (currentScrollTop < previousScrollTop - 1) {
      unpin()
      return
    }

    // User scrolled DOWN and reached near bottom
    if (near) {
      jumping.current = false
      escapedFromLock.current = false
      atBottom.current = true
      setUnseen(false)
    } else if (!jumping.current) {
      atBottom.current = false
    }
  }, [ref, unpin])

  // Wheel, touch, and key listeners for immediate unpinning on user upward interaction
  useEffect(() => {
    const el = ref.current
    if (!el) return

    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) {
        unpin()
      }
    }

    let touchY = 0
    const onTouchStart = (e: TouchEvent) => {
      touchY = e.touches[0]?.clientY ?? 0
    }
    const onTouchMove = (e: TouchEvent) => {
      const currentY = e.touches[0]?.clientY ?? 0
      // Swiping finger downward means scrolling content up
      if (currentY > touchY + 5) {
        unpin()
      }
    }

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'PageUp' || e.key === 'ArrowUp' || e.key === 'Home') {
        unpin()
      }
    }

    el.addEventListener('wheel', onWheel, { passive: true })
    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchmove', onTouchMove, { passive: true })
    el.addEventListener('keydown', onKeyDown, { passive: true })

    return () => {
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('keydown', onKeyDown)
      stopAnimation()
    }
  }, [ref, unpin])

  // When content grows (follow)
  useEffect(() => {
    const el = ref.current
    if (!el) return

    if (atBottom.current && !escapedFromLock.current) {
      smoothScrollToBottom()
      atBottom.current = true
    } else {
      setUnseen(true)
    }
  }, follow) // eslint-disable-line react-hooks/exhaustive-deps

  // When conversation resets
  useEffect(() => {
    stopAnimation()
    escapedFromLock.current = false
    atBottom.current = true
    setUnseen(false)
    const el = ref.current
    if (el) {
      programmatic.current = true
      el.scrollTop = targetScrollTop(el)
      lastScrollTop.current = el.scrollTop
    }
  }, reset) // eslint-disable-line react-hooks/exhaustive-deps

  // Watch size changes (images loading, folds toggling, etc.)
  useEffect(() => {
    const el = ref.current
    const inner = el?.firstElementChild
    if (!el || !inner) return

    const ro = new ResizeObserver(() => {
      if (atBottom.current && !escapedFromLock.current) {
        smoothScrollToBottom()
      }
    })
    ro.observe(inner)
    return () => {
      ro.disconnect()
      stopAnimation()
    }
  }, observe) // eslint-disable-line react-hooks/exhaustive-deps

  const jump = useCallback((opts?: { smooth?: boolean } | unknown) => {
    const el = ref.current
    if (!el) return
    stopAnimation()
    escapedFromLock.current = false
    atBottom.current = true
    setUnseen(false)

    const smooth = !(opts && typeof opts === 'object' && 'smooth' in opts && (opts as { smooth?: boolean }).smooth === false)
    if (smooth) {
      jumping.current = true
      smoothScrollToBottom()
    } else {
      jumping.current = false
      programmatic.current = true
      el.scrollTop = targetScrollTop(el)
      lastScrollTop.current = el.scrollTop
    }
  }, [ref, smoothScrollToBottom])

  return { onScroll, unseen, jump }
}
