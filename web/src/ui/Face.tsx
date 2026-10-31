/* Face: one look for every face in the app (its Portrait, a chat person's
   Avatar, the worker). The agent's picture when there is one and it loads, else ONE initial in the
   serif on its colour (the colour comes from who it is, never from the letter); a 2 px ring
   in its colour that pulses while it speaks; half see-through while it is away. A face is
   a picture only: it does nothing on a click. */
import { useEffect, useState, type CSSProperties } from 'react'
import type { LucideIcon } from 'lucide-react'
import { cx } from './cx'
import { ICON } from './icons'
import './Face.css'

export type FaceSize = 'sm' | 'md' | 'lg'

/** the first character of the name (a letter, an emoji: one grapheme), upper case */
const initial = (name: string) => {
  const clean = name.replace(/[^\p{L}\p{N}\p{Extended_Pictographic}]/gu, '')
  const seg = typeof Intl !== 'undefined' && 'Segmenter' in Intl ? new Intl.Segmenter() : null
  const first = seg ? seg.segment(clean)[Symbol.iterator]().next().value?.segment : [...clean][0]
  return /\p{N}/u.test(first ?? '') ? '' : (first ?? '').toUpperCase()
}

export function Face({ name, src, bg, fg, size = 'md', speaking = false, away = false, fallback: Fallback, className, style }: {
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
  className?: string
  style?: CSSProperties
}) {
  const [broken, setBroken] = useState(false)
  useEffect(() => setBroken(false), [src])
  const letter = initial(name)
  const pic = src && !broken ? src : null
  const look = { ...style, '--face-bg': bg, '--face-fg': fg } as CSSProperties
  const body = pic ? <img src={pic} alt="" loading="lazy" draggable={false} onError={() => setBroken(true)} />
    : letter ? <span className="face__initial" aria-hidden>{letter}</span>
    : Fallback ? <Fallback size={size === 'sm' ? ICON.sm : ICON.lg} aria-hidden /> : null
  return (
    <span className={cx('face', `face--${size}`, speaking && 'is-speaking', away && 'is-away', className)} style={look}
          role="img" aria-label={name || undefined}>{body}</span>
  )
}
