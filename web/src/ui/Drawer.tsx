/* Drawer: a side panel beside the content (a run, a panel of settings). On a desktop it
   takes its column at once and the panel slides in by transform, so the content beside it
   changes width once, never on every frame; on a phone it lies over the content as a sheet
   from the bottom edge, over a scrim that closes it. `side="left"` (the place sidebar)
   comes from the left edge from 768 to 1023 px. Escape and its X close it; closed, it is inert. `fill`: its body is one
   box for a list that fills it (a VirtualList), with no padding and no scroll of its own.
   `enter`: made on its first open, it is drawn closed for a frame and then slides in. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { CloseButton } from './Dialog'
import { cx } from './cx'

export function Drawer({ open: wanted, title, onClose, children, actions, className, label, side = 'right', fill = false, inline = false, enter = false }: {
  open: boolean
  title: ReactNode
  onClose: () => void
  children?: ReactNode
  /** head actions before the X */
  actions?: ReactNode
  className?: string
  /** its name for screen readers, when the title is not words */
  label?: string
  /** the edge it comes from; left only for the place sidebar */
  side?: 'right' | 'left'
  fill?: boolean
  /** drawn in place, for /parts specimen */
  inline?: boolean
  /** mounted open, it still slides in */
  enter?: boolean
}) {
  const [entered, setEntered] = useState(!enter || !wanted)
  useEffect(() => {
    if (entered) return
    let id = requestAnimationFrame(() => { id = requestAnimationFrame(() => setEntered(true)) })
    return () => cancelAnimationFrame(id)
  }, [entered])
  const open = wanted && entered
  const box = useRef<HTMLElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  const panel = useRef<HTMLDivElement>(null)
  const was = useRef(open)
  useEffect(() => {
    // opened by the user: focus moves into it, so Escape closes it; closed, focus goes back
    if (open === was.current) return
    was.current = open
    if (open) {
      const from = document.activeElement as HTMLElement | null
      panel.current?.focus({ preventScroll: true })
      // eslint-disable-next-line react-hooks/exhaustive-deps -- the box as it is when it closes
      return () => { if (box.current?.contains(document.activeElement)) from?.focus({ preventScroll: true }) }
    }
  }, [open])
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && box.current?.contains(document.activeElement)) { e.preventDefault(); closeRef.current() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])
  return (
    <aside ref={box} className={cx('ui-drawer', side === 'left' && 'ui-drawer--left', fill && 'ui-drawer--fill', inline && 'ui-drawer--inline', className)}
           data-open={open || undefined} aria-label={label} aria-hidden={!open} inert={!open || undefined}>
      <div className="ui-drawer__scrim" onClick={onClose} aria-hidden />
      <div ref={panel} className="ui-drawer__panel" tabIndex={-1}>
        <div className="ui-drawer__head">
          <h2 className="ui-drawer__title">{title}</h2>
          {actions}
          <CloseButton onClick={onClose} />
        </div>
        <div className="ui-drawer__body">{children}</div>
      </div>
    </aside>
  )
}
