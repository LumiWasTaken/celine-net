/* Face: one look for every face in the app (its Portrait, a chat person's
   Avatar, the worker). The agent's picture when there is one and it loads, else ONE initial in the
   serif on its colour (the colour comes from who it is, never from the letter); a 2 px ring
   in its colour that pulses while it speaks; half see-through while it is away. A face
   that is not part of a bigger button opens its FaceCard on a click: the face big, the
   name, one muted line and the caller's actions; the big face opens the picture. */
import {
  useEffect, useLayoutEffect, useRef, useState, type CSSProperties,
} from 'react'
import { createPortal } from 'react-dom'
import type { LucideIcon } from 'lucide-react'
import { Button } from './Button'
import { cx } from './cx'
import { ICON } from './icons'
import { leave, OUT, useLeave } from './motion'
import { Viewer } from './Viewer'
import './Face.css'

export type FaceSize = 'sm' | 'md' | 'lg'
export type FaceAction = { label: string; icon?: LucideIcon; onSelect: () => void }
/** what the card says: one muted line, the actions that exist for this one */
export type FaceInfo = { line?: string; actions?: FaceAction[] }

/** the first character of the name (a letter, an emoji: one grapheme), upper case */
const initial = (name: string) => {
  const clean = name.replace(/[^\p{L}\p{N}\p{Extended_Pictographic}]/gu, '')
  const seg = typeof Intl !== 'undefined' && 'Segmenter' in Intl ? new Intl.Segmenter() : null
  const first = seg ? seg.segment(clean)[Symbol.iterator]().next().value?.segment : [...clean][0]
  return /\p{N}/u.test(first ?? '') ? '' : (first ?? '').toUpperCase()
}

export function Face({ name, src, bg, fg, size = 'md', speaking = false, away = false, fallback: Fallback, info, still = false, className, style }: {
  name: string
  src?: string | null
  /** its colours: the circle and the initial (and the ring) */
  bg: string
  fg: string
  size?: FaceSize
  speaking?: boolean
  away?: boolean
  /** a glyph for a name with no letter (a phone number) */
  fallback?: LucideIcon
  /** the card's line and actions; left out: the card shows the name */
  info?: FaceInfo
  /** never opens a card (the big face in one) */
  still?: boolean
  className?: string
  style?: CSSProperties
}) {
  const [broken, setBroken] = useState(false)
  useEffect(() => setBroken(false), [src])
  const ref = useRef<HTMLSpanElement>(null)
  // a face inside a bigger button or link is part of it; on its own it opens its card
  const [own, setOwn] = useState(false)
  useLayoutEffect(() => {
    setOwn(!still && !ref.current?.parentElement?.closest('a, button, [role="button"], [role="radio"], label'))
  }, [still])
  const [card, setCard] = useState(false)
  const letter = initial(name)
  const pic = src && !broken ? src : null
  const look = { ...style, '--face-bg': bg, '--face-fg': fg } as CSSProperties
  const body = pic ? <img src={pic} alt="" loading="lazy" draggable={false} onError={() => setBroken(true)} />
    : letter ? <span className="face__initial" aria-hidden>{letter}</span>
    : Fallback ? <Fallback size={size === 'sm' ? ICON.sm : ICON.lg} aria-hidden /> : null
  const cls = cx('face', `face--${size}`, speaking && 'is-speaking', away && 'is-away', className)
  return (
    <>
      {own ? (
        <span ref={ref} className={cx(cls, 'face--open')} style={look} role="button" tabIndex={0}
              aria-label={name} aria-haspopup="dialog" aria-expanded={card} data-tip={name ? `Show ${name}` : undefined}
              onClick={e => { e.stopPropagation(); setCard(true) }}
              onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setCard(true) } }}>
          {body}
        </span>
      ) : (
        <span ref={ref} className={cls} style={look} role="img" aria-label={name || undefined}>{body}</span>
      )}
      {card && ref.current && (
        <FaceCard anchor={ref.current} name={name} src={pic} bg={bg} fg={fg} info={info} onClose={() => setCard(false)} />
      )}
    </>
  )
}

/* The card a face opens: it grows from the face, the face big, the name, one muted line,
   the actions. Outside click, Escape and scrolling close it; focus goes back to the face.
   While its picture is shown big the card is out of the way, back when the picture closes. */
function FaceCard({ anchor, name, src, bg, fg, info, onClose }: {
  anchor: HTMLElement; name: string; src: string | null; bg: string; fg: string; info?: FaceInfo; onClose: () => void
}) {
  const box = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<CSSProperties | null>(null)
  const [viewing, setViewing] = useState(false)
  useLeave(box, g => leave(g, OUT.shrink))
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    const r = anchor.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight
    const below = r.bottom + 8 + h < window.innerHeight
    const top = below ? r.bottom + 8 : Math.max(8, r.top - 8 - h)
    const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8)
    setPos({ left, top, transformOrigin: `${r.left + r.width / 2 - left}px ${below ? 0 : h}px` })
    el.focus({ preventScroll: true })
  }, [anchor])
  useEffect(() => {
    if (viewing) return
    box.current?.focus({ preventScroll: true })     // back from the viewer: Escape closes it again
    const down = (e: PointerEvent) => { if (!box.current?.contains(e.target as Node)) closeRef.current() }
    const away = () => closeRef.current()
    document.addEventListener('pointerdown', down, true)
    document.addEventListener('scroll', away, true)
    window.addEventListener('resize', away)
    return () => {
      document.removeEventListener('pointerdown', down, true)
      document.removeEventListener('scroll', away, true)
      window.removeEventListener('resize', away)
    }
  }, [viewing])
  const close = () => { onClose(); if (anchor.isConnected) anchor.focus({ preventScroll: true }) }
  return createPortal(
    <>
      <div ref={box} className="face-card" role="dialog" aria-label={name} tabIndex={-1} style={{ ...pos ?? { left: -9999, top: -9999 }, ...viewing && { visibility: 'hidden' } }}
           onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); close() } }}>
        {src ? (
          <button type="button" className="face-card__face" data-tip="Show the picture" onClick={() => setViewing(true)}>
            <Face name={name} src={src} bg={bg} fg={fg} size="lg" />
          </button>
        ) : <Face name={name} bg={bg} fg={fg} size="lg" still />}
        <div className="face-card__name">{name}</div>
        {info?.line && <div className="face-card__line">{info.line}</div>}
        {!!info?.actions?.length && (
          <div className="face-card__actions">
            {info.actions.map(a => (
              <Button key={a.label} size="sm" variant="secondary" icon={a.icon} onClick={() => { close(); a.onSelect() }}>{a.label}</Button>
            ))}
          </div>
        )}
      </div>
      {viewing && src && <Viewer src={src} name={name} onClose={() => setViewing(false)} />}
    </>,
    document.body,
  )
}
