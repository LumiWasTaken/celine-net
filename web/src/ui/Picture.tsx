/* Pictures and sound: Picture (a picture in a message, a story, the Gallery: a click opens
   it big in the Viewer), Player (the mini player of a playing track) and Screen (its
   terminal's screen or its browser's, framed as a screen). */
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { Check, Heart, Pause, Play } from 'lucide-react'
import { Progress } from './Bits'
import { Button, IconButton } from './Button'
import { cx } from './cx'
import { CloseButton } from './Dialog'
import { ICON } from './icons'
import { useContextMenu, type MenuItem } from './Menu'
import type { PickProps } from './Select'
import { Viewer } from './Viewer'

type Shown = { src: string; name: string }

/* ── Picture: a click opens the Viewer (in a gallery it flips through `gallery`); its
   right-click opens `more`. In a grid: a check while selecting, the favourite mark. While
   it is being made (`making`), a calm breathing surface in its shape; a file that does
   not load, the same surface with Retry. ── */
export function Picture({ src, name, ratio = '1', more = [], gallery, at = 0, viewer, onOpen, current, pick, favourite, onFavourite, making, className }: {
  src?: string
  /** what it shows, never a file name: its alt, tip and the Viewer's title */
  name: string
  /** its shape ("3 / 4"); the picture is cut to it */
  ratio?: string
  /** its "..." items, also the right-click and the Viewer's menu */
  more?: MenuItem[]
  /** the pictures the Viewer flips through, with this one at `at` */
  gallery?: Shown[]
  at?: number
  /** the Viewer's own buttons and panel for this picture */
  viewer?: { actions?: ReactNode; panel?: ReactNode; caption?: ReactNode }
  /** a page of its own opens it (the Gallery's viewer route) in place of the Viewer here */
  onOpen?: () => void
  /** the one open now: the accent ring */
  current?: boolean
  pick?: PickProps
  /** marked as a favourite; with `onFavourite` the mark is a button */
  favourite?: boolean
  onFavourite?: (on: boolean) => void
  /** being made: the words on the breathing surface ("Creating image") */
  making?: ReactNode
  className?: string
}) {
  const ctx = useContextMenu()
  const [open, setOpen] = useState<number | null>(null)
  // its file did not load (gone, or the server away): a quiet surface with Retry
  const [failed, setFailed] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const picking = pick?.checked !== undefined
  const list = gallery ?? (src ? [{ src, name }] : [])
  const style = { '--pic-ratio': ratio } as CSSProperties
  if (making !== undefined || !src) {
    return <div className={cx('picture', 'picture--making', className)} style={style} role="img" aria-label={name}>
      {making != null && <span className="picture__making">{making}</span>}
    </div>
  }
  const shown = open !== null ? list[open] : null
  return (
    <div className={cx('picture', picking && 'picture--picking', (pick?.checked || (!picking && current)) && 'is-picked', className)} style={style}
         onContextMenu={more.length && !picking ? e => ctx.open(e, more, name) : undefined}
         {...(more.length && !picking ? ctx.press(() => more, name) : {})}>
      <button type="button" className="picture__open" title={name} aria-pressed={picking ? pick!.checked : undefined}
              onClick={e => { if (pick?.onPick(e)) return; if (onOpen) onOpen(); else setOpen(at) }}
              onPointerDown={pick?.onPointerDown} onPointerUp={pick?.onPointerUp}
              onPointerCancel={pick?.onPointerCancel} onPointerLeave={pick?.onPointerLeave}>
        <img key={attempt} src={src} alt={name} loading="lazy" draggable={false} onError={() => setFailed(src)} />
      </button>
      {failed === src && (
        <div className="picture__failed">
          <span>Couldn't load the picture.</span>
          <Button size="sm" variant="secondary" onClick={() => { setFailed(null); setAttempt(n => n + 1) }}>Retry</Button>
        </div>
      )}
      {picking && <span className={cx('list-row__pick', 'picture__pick', pick!.checked && 'is-on')} aria-hidden>{pick!.checked && <Check size={ICON.sm} />}</span>}
      {onFavourite && !picking
        ? <IconButton icon={Heart} fill={favourite} label={favourite ? 'Remove from Favourites' : 'Add to Favourites'} size="sm"
                      className={cx('picture__fav', favourite && 'is-on')} pressed={favourite} onClick={() => onFavourite(!favourite)} />
        : favourite && <span className="picture__fav is-on" aria-label="Favourite" role="img"><Heart size={ICON.sm} fill="currentColor" aria-hidden /></span>}
      {shown && (
        <Viewer src={shown.src} name={shown.name} more={more} actions={viewer?.actions} panel={viewer?.panel} caption={viewer?.caption}
                index={open!} total={list.length} onClose={() => setOpen(null)}
                onPrev={open! > 0 ? () => setOpen(open! - 1) : undefined}
                onNext={open! < list.length - 1 ? () => setOpen(open! + 1) : undefined} />
      )}
      {ctx.menu}
    </div>
  )
}

/* ── Player: the track that plays, kept in one small bar (play and pause, its name, how far,
   close) while you go on looking. ── */
export function Player({ src, title, detail, onClose, className }: {
  src: string
  title: string
  /** one muted line: who or what made it */
  detail?: ReactNode
  onClose: () => void
  className?: string
}) {
  const audio = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [time, setTime] = useState(0)
  const [length, setLength] = useState(0)
  useEffect(() => { void audio.current?.play().catch(() => setPlaying(false)) }, [src])
  const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`
  return (
    <div className={cx('player', className)} role="group" aria-label={title}>
      <audio ref={audio} src={src} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
             onTimeUpdate={e => setTime(e.currentTarget.currentTime)} onLoadedMetadata={e => setLength(e.currentTarget.duration || 0)} />
      <IconButton icon={playing ? Pause : Play} label={playing ? 'Pause' : 'Play'} variant="secondary"
                  onClick={() => (playing ? audio.current?.pause() : void audio.current?.play())} />
      <span className="player__text">
        <span className="player__title">{title}</span>
        {detail && <span className="player__detail">{detail}</span>}
      </span>
      <span className="player__time">{mmss(time)} of {mmss(length)}</span>
      <span className="player__bar"><Progress value={time} max={length || null} label={title} /></span>
      <CloseButton onClick={onClose} />
    </div>
  )
}

/* ── Screen: what its terminal or its browser shows, framed: a bar with its title (a
   program, an address), then the screen; `mono` for a terminal's text. ── */
export function Screen({ title, children, mono = false, current = false, className }: {
  title?: ReactNode
  children: ReactNode
  mono?: boolean
  /** the one shown now among several (its latest shot) */
  current?: boolean
  className?: string
}) {
  return (
    <figure className={cx('screen', mono && 'screen--mono', current && 'screen--current', className)}>
      {title != null && <figcaption className="screen__bar">{title}</figcaption>}
      <div className="screen__body">{children}</div>
    </figure>
  )
}
