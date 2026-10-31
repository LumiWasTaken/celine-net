/* Dialog: named (aria-labelledby), Escape closes, Tab stays inside, focus goes in on
   open and back to where it was on close, the rest of the app is inert meanwhile.
   Rendered into <body> so no ancestor's overflow or stacking context clips it. */
import {
  useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { Trash2, X } from 'lucide-react'
import { Button, IconButton } from './Button'
import { cx } from './cx'
import { curve, leave, ms, OUT, reduced, useLeave } from './motion'
import { Checkbox } from './Form'
import { InlineError } from './Notice'
import { coarse, usePhone } from './phone'
import { isTyping } from '../lib/keys'

/** a release faster than this (px per ms, downwards) closes a sheet however short the drag */
const FLING = 0.5

// what Tab reaches: a radio group's other options (tabindex -1) are reached by the arrows
const FOCUSABLE = ['a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])', 'select:not([disabled])',
  'textarea:not([disabled])', '[tabindex]'].map(s => `${s}:not([tabindex="-1"])`).join(', ')

let openCount = 0
function setAppInert(on: boolean) {
  const root = document.getElementById('root')
  if (!root) return
  if (on) root.setAttribute('inert', '')
  else root.removeAttribute('inert')
}

/** the rest of the app is inert while this (a dialog, the viewer) is open */
export function useModal() {
  useLayoutEffect(() => {
    openCount += 1
    setAppInert(true)
    return () => {
      openCount -= 1
      if (openCount === 0) setAppInert(false)
    }
  }, [])
}

export function Dialog(props: DialogProps) {
  // on a phone every dialog is a sheet from the bottom edge 
  const phone = usePhone()
  if (props.inline) return <DialogBox {...props} />
  return phone && !props.className?.split(' ').includes('sheet') ? <SheetModal {...props} /> : <ModalDialog {...props} />
}

type DialogProps = {
  title: ReactNode
  onClose: () => void
  children?: ReactNode
  /** buttons row at the bottom, right-aligned (the primary action last) */
  actions?: ReactNode
  size?: 'sm' | 'md' | 'lg'
  className?: string
  /** false while something must finish first: Escape and the scrim do nothing */
  dismissable?: boolean
  /** false when the actions already hold the one way out (Cancel) */
  closeButton?: boolean
  /** a sheet's grabber: drag it down to close */
  grab?: ReactNode
  /** drawn in place, for /parts: no portal, no scrim, the app stays live */
  inline?: boolean
}

/* the dialog's own box: its head, its body, its actions */
function DialogBox({ title, onClose, children, actions, size = 'sm', className, dismissable = true, closeButton = true, grab,
                     inline, titleId, box, onKeyDown }: DialogProps & {
  titleId?: string; box?: React.RefObject<HTMLDivElement | null>; onKeyDown?: (e: React.KeyboardEvent) => void
}) {
  return (
    <div ref={box} className={cx('dialog', size !== 'sm' && `dialog--${size}`, inline && 'dialog--inline', className)}
         role="dialog" aria-modal={!!onKeyDown || undefined} aria-labelledby={titleId} aria-label={titleId ? undefined : typeof title === 'string' ? title : undefined}
         tabIndex={-1} onKeyDown={onKeyDown}>
      {grab}
      <div className={cx('dialog__head', closeButton && 'dialog__head--x')}>
        <h2 className="dialog__title" id={titleId}>{title}</h2>
        {closeButton && <CloseButton onClick={onClose} disabled={!dismissable} />}
      </div>
      {children}
      {actions && <div className="dialog__actions">{actions}</div>}
    </div>
  )
}

function ModalDialog(props: DialogProps) {
  const { onClose, dismissable = true } = props
  const titleId = useId()
  const box = useRef<HTMLDivElement>(null)
  const scrim = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  // it leaves as it came: the scrim fades, the dialog shrinks a little (a phone sheet drops
  // back over its edge)
  useLeave(scrim, g => {
    const panel = g.firstElementChild!
    const sheet = panel.classList.contains('sheet') && matchMedia('(max-width: 767px)').matches
    return [leave(g, OUT.fade, '--dur'), leave(panel, sheet ? OUT.down : OUT.shrink, '--dur')]
  })

  // app inert while any dialog is open; focus returns to the opener on close
  useModal()
  useLayoutEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    return () => { if (opener && document.contains(opener)) opener.focus({ preventScroll: true }) }
  }, [])

  // focus in: an autoFocus child already has it; otherwise the first field or button
  // in the body, then the actions (never the close X first). On a touch screen a field
  // would raise the keyboard before anyone tapped it: the box takes the focus instead
  useEffect(() => {
    const el = box.current
    if (!el || el.contains(document.activeElement)) return
    const first = [...el.querySelectorAll<HTMLElement>(FOCUSABLE)].find(n => !n.closest('.dialog__head'))
    ;(first && !(coarse() && isTyping(first)) ? first : el).focus({ preventScroll: true })
  }, [])

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.stopPropagation(); e.preventDefault()
      if (dismissable) closeRef.current()
      return
    }
    if (e.key !== 'Tab') return
    const el = box.current
    if (!el) return
    const items = [...el.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(n => n.offsetParent !== null)
    if (!items.length) { e.preventDefault(); return }
    const first = items[0]!, last = items[items.length - 1]!
    if (e.shiftKey && (document.activeElement === first || document.activeElement === el)) { e.preventDefault(); last.focus() }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
  }

  return createPortal(
    <div ref={scrim} className="dialog-scrim" onMouseDown={e => { if (e.target === e.currentTarget && dismissable) onClose() }}>
      <DialogBox {...props} titleId={titleId} box={box} onKeyDown={onKeyDown} />
    </div>,
    document.body,
  )
}

/* The one close: a 36 px ghost X, pinned --s-4 from the top right of whatever it closes
   (a dialog, a sheet, the side panel, the notebook's file). The surface is its
   positioning parent; the X never follows the title. */
export function CloseButton({ onClick, label = 'Close', disabled }: { onClick: () => void; label?: string; disabled?: boolean }) {
  return <IconButton icon={X} label={label} className="close-x" onClick={onClick} disabled={disabled} />
}

/* A sheet: what a Dialog or a Menu is on a desktop, for a phone: it rises from the bottom
   edge with a grabber; dragging it down, the back gesture, Escape, the scrim and its X
   close it. On a wide screen it sits near the top, as a dialog. */
export function Sheet(props: DialogProps) {
  return props.inline ? <Dialog {...props} className={cx('sheet', props.className)} /> : <SheetModal {...props} />
}

function SheetModal(props: DialogProps) {
  const { onClose, dismissable = true } = props
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  const canClose = useRef(dismissable)
  canClose.current = dismissable
  const [key] = useState(() => `sheet-${Math.random().toString(36).slice(2)}`)
  const alive = useRef(false)

  // the back gesture: the sheet is one step of history; back closes it, and closing it
  // any other way takes that step back out (unless a link inside already moved on)
  useEffect(() => {
    alive.current = true
    if (history.state?.sheet !== key) history.pushState({ ...history.state, sheet: key }, '')
    const onPop = () => {
      if (history.state?.sheet === key) return
      if (canClose.current) closeRef.current()
      else history.pushState({ ...history.state, sheet: key }, '')
    }
    window.addEventListener('popstate', onPop)
    return () => {
      alive.current = false
      window.removeEventListener('popstate', onPop)
      // StrictMode mounts twice: only a sheet that stays gone takes its step back
      queueMicrotask(() => { if (!alive.current && history.state?.sheet === key) history.back() })
    }
  }, [key])

  // drag the grabber down: the sheet follows the finger; far enough or flung and it
  // closes from where it is, else it springs back
  const drag = useRef<{ y: number; el: HTMLElement; lastY: number; lastT: number; v: number } | null>(null)
  const back = (el: HTMLElement) => {
    if (reduced() || !el.style.transform) { el.style.transform = ''; return }
    const a = el.animate([{ transform: 'none' }], { duration: ms('--dur'), easing: curve('--ease-spring') })
    a.finished.then(() => { el.style.transform = '' }, () => {})
  }
  const grab = (
    <div className="sheet__grab" aria-hidden
         onPointerDown={e => {
           const el = (e.currentTarget.parentElement as HTMLElement)
           el.getAnimations().forEach(a => a.cancel())
           drag.current = { y: e.clientY, el, lastY: e.clientY, lastT: e.timeStamp, v: 0 }
           e.currentTarget.setPointerCapture(e.pointerId)
         }}
         onPointerMove={e => {
           const d = drag.current
           if (!d) return
           if (e.timeStamp > d.lastT) d.v = (e.clientY - d.lastY) / (e.timeStamp - d.lastT)
           d.lastY = e.clientY; d.lastT = e.timeStamp
           d.el.style.transform = `translateY(${Math.max(0, e.clientY - d.y)}px)`
         }}
         onPointerUp={e => {
           const d = drag.current
           drag.current = null
           if (!d) return
           const moved = e.clientY - d.y
           // a finger that rested before letting go flung nothing
           const v = e.timeStamp - d.lastT < 100 ? d.v : 0
           if ((moved > d.el.offsetHeight / 4 || (moved > 0 && v > FLING)) && canClose.current) closeRef.current()
           else back(d.el)
         }}
         onPointerCancel={() => { if (drag.current) back(drag.current.el); drag.current = null }} />
  )
  return <Dialog {...props} grab={grab} className={cx('sheet', props.className)} />
}

/* "Delete X?" and friends. `onConfirm` returns an error message (or throws) to keep
   the dialog open and show it; with nothing returned it stays busy until the caller
   closes it. */
export function ConfirmDialog({ title, body, confirmLabel, cancelLabel = 'Cancel', danger = false, option, onConfirm, onClose, inline }: {
  title: ReactNode
  body?: ReactNode
  confirmLabel: string
  /** the way out, when "Cancel" would read as the same thing ("Keep it") */
  cancelLabel?: string
  danger?: boolean
  /** one Checkbox under the body, off by default ("Also delete its sub-agents") */
  option?: string
  /** gets the option's state */
  onConfirm: (option: boolean) => Promise<string | void | undefined> | string | void | undefined
  onClose: () => void
  /** drawn in place, for /parts */
  inline?: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [checked, setChecked] = useState(false)
  const run = async () => {
    setBusy(true); setError(null)
    try {
      const err = await onConfirm(checked)
      if (err) { setError(err); setBusy(false) }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e)); setBusy(false)
    }
  }
  return (
    <Dialog title={title} onClose={onClose} dismissable={!busy} closeButton={false} inline={inline}
            actions={<>
              <Button variant="secondary" onClick={onClose} disabled={busy} autoFocus={!inline && danger}>{cancelLabel}</Button>
              <Button variant={danger ? 'danger' : 'primary'} loading={busy} autoFocus={!inline && !danger}
                      icon={danger && confirmLabel.startsWith('Delete') ? Trash2 : undefined}
                      onClick={() => void run()}>{confirmLabel}</Button>
            </>}>
      {body && <div className="dialog__body">{body}</div>}
      {option && <Checkbox checked={checked} onChange={setChecked} label={option} />}
      {error && <InlineError message={error} compact />}
    </Dialog>
  )
}
