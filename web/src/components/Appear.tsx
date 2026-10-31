/* A line of the transcript that comes and goes while you watch it (a call at work, a call
   being written, Bastet's banner, a request of hers): one fade in, one fade out (useLeave).
   A conversation that loads shows its lines at once and another one replaces them at once:
   the transcript says `data-watching` from its first drawn frame on, and `data-cid`. */
import { useLayoutEffect, useRef, type ReactNode, type RefObject } from 'react'
import { curve, leave, ms, OUT, reduced, useLeave } from '../ui'

const box = (el: Element) => el.closest<HTMLElement>('.is-transcript')

/** `ref`'s element fades in when it mounts in a watched transcript and out when it goes
    from the same conversation */
export function useAppear(ref: RefObject<HTMLElement | null>) {
  const cid = useRef<string | undefined>(undefined)
  useLayoutEffect(() => {
    const el = ref.current
    const b = el && box(el)
    if (!el || !b) return
    cid.current = b.dataset.cid
    if (b.hasAttribute('data-watching') && !reduced()) {
      el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: ms('--dur'), easing: curve('--ease') })
    }
  }, [ref])
  useLeave(ref, g => {
    const b = g.isConnected ? box(g) : null
    return b && b.dataset.cid === cid.current ? leave(g, OUT.fade, '--dur') : []
  })
}

export function Appear({ children, className }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null)
  useAppear(ref)
  return <div ref={ref} className={className}>{children}</div>
}
