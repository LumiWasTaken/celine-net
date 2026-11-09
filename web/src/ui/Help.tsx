/* Help: the "?" after a name (a setting, a role, a feature) that opens what it does, in one
   or two sentences from the server. The one place an explanation lives: never inline text,
   never a tooltip. A native popover (top layer over a Sheet too, light dismiss, Escape, one
   open at a time), placed under the "?" like the tip (placeNear), above it near the bottom
   edge. Tab reaches it, Enter or a tap opens it. Neither its click nor a click in its text
   reaches the row it sits in (a Switch row does not flip). */
import { useEffect, useId, useRef } from 'react'
import { CircleHelp } from 'lucide-react'
import { ICON } from './icons'
import { placeNear } from './Tip'

export function Help({ text, label }: { text: string; label: string }) {
  const id = useId()
  const btn = useRef<HTMLButtonElement>(null)
  const pop = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const p = pop.current
    if (!p) return
    const placed = (e: Event) => {
      if ((e as ToggleEvent).newState !== 'open' || !btn.current) return
      const at = placeNear(btn.current.getBoundingClientRect(), p.offsetWidth, p.offsetHeight, 'below')
      p.style.left = `${at.left}px`
      p.style.top = `${at.top}px`
    }
    p.addEventListener('toggle', placed)
    return () => { p.removeEventListener('toggle', placed) }
  }, [])
  return (
    <>
      <button ref={btn} type="button" className="help" aria-label={`About ${label}`} popoverTarget={id}
              onClick={e => { e.stopPropagation() }}>
        <CircleHelp size={ICON.sm} aria-hidden />
      </button>
      <div ref={pop} id={id} popover="auto" role="note" className="help__pop"
           onClick={e => { e.stopPropagation() }}>{text}</div>
    </>
  )
}
