/* Viewer: a picture or a face, big. It grows from what was clicked and shrinks back on
   close (Escape, the X, a click beside the picture). It fits the window; the wheel or a
   pinch zooms around the pointer, a double click toggles 1:1, a drag pans a zoomed picture.
   In a gallery Left and Right flip, "2 of 5" in the caption. A right-click on the picture
   opens its menu: Copy picture, Save, Open original, and the caller's (`more`); the bar has
   Open original and the same menu behind "...". One that does not load says so with
   Retry. A click never opens a new tab. */
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { ChevronLeft, ChevronRight, Copy, Download, ExternalLink, MoreHorizontal } from 'lucide-react'
import { Button, IconButton } from './Button'
import { CloseButton, useModal } from './Dialog'
import { MenuButton, useContextMenu, type MenuItem } from './Menu'
import { leave, OUT, useLeave } from './motion'
import './Viewer.css'

type View = { s: number; x: number; y: number }
const FIT: View = { s: 1, x: 0, y: 0 }
const MAX = 8

async function copyPicture(src: string) {
  const blob = await (await fetch(src)).blob()
  const png = blob.type === 'image/png' ? blob : await new Promise<Blob>((ok, no) => {
    void createImageBitmap(blob).then(bmp => {
      const c = document.createElement('canvas')
      c.width = bmp.width; c.height = bmp.height
      c.getContext('2d')!.drawImage(bmp, 0, 0)
      c.toBlob(b => (b ? ok(b) : no(new Error('no png'))), 'image/png')
    }, no)
  })
  await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })])
}

function save(src: string, name: string) {
  const a = document.createElement('a')
  a.href = src; a.download = name
  document.body.append(a); a.click(); a.remove()
}

export function Viewer({ src, name, caption, more = [], actions, panel, onClose, onPrev, onNext, index, total }: {
  src: string
  name: string
  /** under the picture: what it was made from */
  caption?: ReactNode
  /** the caller's items in its menu */
  more?: MenuItem[]
  /** the caller's buttons in the bar (Regenerate, Variants, Use as portrait) */
  actions?: ReactNode
  /** a panel beside the picture (under it on a phone): its prompt and settings */
  panel?: ReactNode
  onClose: () => void
  onPrev?: () => void
  onNext?: () => void
  index?: number
  total?: number
}) {
  useModal()
  const layer = useRef<HTMLDivElement>(null)
  const img = useRef<HTMLImageElement>(null)
  const opener = useRef<Element | null>(document.activeElement)
  const [origin] = useState(() => {
    const r = opener.current instanceof HTMLElement && opener.current !== document.body ? opener.current.getBoundingClientRect() : null
    return r ? `${r.left + r.width / 2}px ${r.top + r.height / 2}px` : '50% 50%'
  })
  const [view, setView] = useState<View>(FIT)
  const [state, setState] = useState<'loading' | 'shown' | 'failed'>('loading')
  const [attempt, setAttempt] = useState(0)
  const ctx = useContextMenu()
  useLeave(layer, g => leave(g, OUT.zoom))

  useEffect(() => { setView(FIT); setState('loading') }, [src, attempt])
  useEffect(() => { layer.current?.focus({ preventScroll: true }) }, [])
  useEffect(() => () => {
    const o = opener.current
    if (o instanceof HTMLElement && o.isConnected) o.focus({ preventScroll: true })
  }, [])

  const close = () => onClose()

  // zoom by `k` around the screen point (px, py)
  const zoomAt = (k: number, px: number, py: number, v: View = view): View => {
    const el = img.current
    if (!el) return v
    const s = Math.min(MAX, Math.max(1, v.s * k))
    if (s === 1) return FIT
    const box = el.offsetParent ? el.getBoundingClientRect() : null
    if (!box) return v
    // the picture's own point under the pointer stays under it
    const left = box.left - v.x, top = box.top - v.y
    const qx = (px - left - v.x) / v.s, qy = (py - top - v.y) / v.s
    return { s, x: px - left - qx * s, y: py - top - qy * s }
  }

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close() }
    else if (e.key === 'ArrowLeft' && onPrev) { e.preventDefault(); onPrev() }
    else if (e.key === 'ArrowRight' && onNext) { e.preventDefault(); onNext() }
  }

  // drag to pan, two fingers to pinch
  const pointers = useRef(new Map<number, { x: number; y: number }>())
  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const map = pointers.current
    const was = map.get(e.pointerId)
    if (!was) return
    const now = { x: e.clientX, y: e.clientY }
    if (map.size === 2) {
      const other = [...map.entries()].find(([id]) => id !== e.pointerId)![1]
      const d0 = Math.hypot(was.x - other.x, was.y - other.y), d1 = Math.hypot(now.x - other.x, now.y - other.y)
      if (d0 > 0) setView(v => zoomAt(d1 / d0, (now.x + other.x) / 2, (now.y + other.y) / 2, v))
    } else if (view.s > 1) {
      setView(v => ({ ...v, x: v.x + now.x - was.x, y: v.y + now.y - was.y }))
    }
    map.set(e.pointerId, now)
  }
  const onPointerUp = (e: React.PointerEvent) => { pointers.current.delete(e.pointerId) }

  useLayoutEffect(() => {
    const el = img.current
    if (!el) return
    // a wheel zooms (passive listeners cannot keep the page from scrolling)
    const wheel = (e: WheelEvent) => { e.preventDefault(); setView(v => zoomAt(Math.exp(-e.deltaY * 0.002), e.clientX, e.clientY, v)) }
    el.addEventListener('wheel', wheel, { passive: false })
    return () => el.removeEventListener('wheel', wheel)
  })

  const items: MenuItem[] = [
    { label: 'Copy picture', icon: Copy, onSelect: () => void copyPicture(src).catch(() => undefined) },
    { label: 'Save', icon: Download, onSelect: () => save(src, name) },
    { label: 'Open original', icon: ExternalLink, onSelect: () => window.open(src, '_blank', 'noopener') },
    ...(more.length ? ['sep' as const, ...more] : []),
  ]
  const many = total !== undefined && total > 1

  return createPortal(
    <div ref={layer} className="viewer" role="dialog" aria-modal="true" aria-label={name} tabIndex={-1}
         style={{ transformOrigin: origin }} onKeyDown={onKey}
         onClick={e => { if (e.target === e.currentTarget || (e.target as HTMLElement).classList.contains('viewer__stage')) close() }}>
      <div className="viewer__bar">
        <span className="viewer__name">{name}</span>
        {many && index !== undefined && <span className="viewer__count">{index + 1} of {total}</span>}
        <span className="viewer__actions">
          {actions}
          <IconButton icon={ExternalLink} label="Open original" onClick={() => window.open(src, '_blank', 'noopener')} />
          <MenuButton icon={MoreHorizontal} label="More" items={items} />
        </span>
        <CloseButton onClick={close} />
      </div>
      <div className={panel ? 'viewer__main viewer__main--panel' : 'viewer__main'}>
      <div className="viewer__stage">
        {state === 'failed' ? (
          <div className="viewer__failed" role="alert">
            <span>Couldn't load the picture.</span>
            <Button size="sm" variant="secondary" onClick={() => setAttempt(n => n + 1)}>Retry</Button>
          </div>
        ) : (
          <img ref={img} key={`${src}#${attempt}`} src={attempt ? `${src}${src.includes('?') ? '&' : '?'}retry=${attempt}` : src}
               alt={name} draggable={false}
               className={state === 'loading' ? 'viewer__img is-loading' : 'viewer__img'}
               style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.s})`, cursor: view.s > 1 ? 'grab' : 'zoom-in' }}
               onLoad={() => setState('shown')} onError={() => setState('failed')}
               onDoubleClick={e => {
                 const el = img.current
                 if (!el) return
                 const one = el.naturalWidth / (el.getBoundingClientRect().width / view.s)
                 setView(view.s > 1 ? FIT : zoomAt(Math.max(1.5, one), e.clientX, e.clientY))
               }}
               onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
               onContextMenu={e => ctx.open(e, items, name)} />
        )}
        {many && <IconButton icon={ChevronLeft} label="Previous picture" className="viewer__nav viewer__nav--prev" variant="secondary"
                             disabled={!onPrev} onClick={() => onPrev?.()} />}
        {many && <IconButton icon={ChevronRight} label="Next picture" className="viewer__nav viewer__nav--next" variant="secondary"
                             disabled={!onNext} onClick={() => onNext?.()} />}
      </div>
      {panel && <aside className="viewer__panel">{panel}</aside>}
      </div>
      {caption && <div className="viewer__caption">{caption}</div>}
      {ctx.menu}
    </div>,
    document.body,
  )
}
