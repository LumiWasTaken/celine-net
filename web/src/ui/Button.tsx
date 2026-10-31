import { forwardRef, type AnchorHTMLAttributes, type ButtonHTMLAttributes, type MouseEvent, type ReactNode } from 'react'
import { LoaderCircle, Send, Square, type LucideIcon } from 'lucide-react'
import { cx } from './cx'
import { ICON, type IconSize } from './icons'

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
/** one height scale: md (--h-md) everywhere, sm (--h-sm) only for in-row actions and the Toolbar */
export type ButtonSize = 'sm' | 'md'

/** a button that is a link: an in-app path (route it with the caller's onClick) or a file */
type LinkProps = Pick<AnchorHTMLAttributes<HTMLAnchorElement>, 'href' | 'target' | 'rel' | 'download'>

type ButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onClick'> & LinkProps & {
  variant?: ButtonVariant
  size?: ButtonSize
  /** leading icon; replaced by a spinner while `loading` */
  icon?: LucideIcon
  iconEnd?: LucideIcon
  /** busy: disabled, spinner in place, width unchanged */
  loading?: boolean
  /** full width, content left-aligned (sidebar actions) */
  wide?: boolean
  /** a toggle that is on (Archive or Trash open): aria-pressed and the accent wash */
  pressed?: boolean
  onClick?: (e: MouseEvent<HTMLElement>) => void
}

const iconFor = (size: ButtonSize) => (size === 'sm' ? ICON.sm : ICON.md)

/* A button that cannot act is aria-disabled, not disabled: it stays in reach of the
   pointer and the keyboard, so its tip (title) can say what is missing; a click does nothing. */
function off(blocked: boolean, onClick?: (e: MouseEvent<HTMLElement>) => void) {
  return {
    'aria-disabled': blocked || undefined,
    onClick: (e: MouseEvent<HTMLElement>) => { if (blocked) { e.preventDefault(); return } onClick?.(e) },
  }
}

/* the one element: a <button>, or an <a> when it has an href */
function Press({ href, target, rel, download, type, inner, forward, ...rest }: LinkProps & {
  type: ButtonHTMLAttributes<HTMLButtonElement>['type']; inner: ReactNode; forward: unknown
} & Record<string, unknown>) {
  if (href) {
    return <a ref={forward as React.Ref<HTMLAnchorElement>} href={href} target={target} rel={rel} download={download}
              {...(rest as AnchorHTMLAttributes<HTMLAnchorElement>)}>{inner}</a>
  }
  return <button ref={forward as React.Ref<HTMLButtonElement>} type={type}
                 {...(rest as ButtonHTMLAttributes<HTMLButtonElement>)}>{inner}</button>
}

/* A text button. Variants: primary (the one main action of a page or dialog), secondary
   (everything else), ghost (quiet in-row actions), danger (tonal red, only in a dialog
   that destroys and a menu's last item). */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', icon: Icon, iconEnd: End, loading = false, wide = false, pressed,
    className, children, disabled, type = 'button', onClick, ...rest },
  ref,
) {
  const s = iconFor(size)
  const overlay = loading && !Icon
  return (
    <Press forward={ref} type={type} aria-busy={loading || undefined} aria-pressed={pressed}
           className={cx('btn', `btn--${variant}`, size === 'sm' && 'btn--sm', wide && 'btn--wide',
                         overlay && 'btn--loading', className)}
           {...rest} {...off(!!disabled || loading, onClick)}
           inner={<>
             {loading && Icon ? <LoaderCircle size={s} className="spin" aria-hidden />
               : Icon ? <Icon size={s} aria-hidden /> : null}
             {children != null && children !== false && <span className="btn__label">{children}</span>}
             {End && <End size={s} aria-hidden />}
             {overlay && <span className="btn__overlay"><LoaderCircle size={s} className="spin" aria-hidden /></span>}
           </>} />
  )
})

type IconButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children' | 'aria-label' | 'onClick'> & LinkProps & {
  icon: LucideIcon
  /** the accessible name AND the tooltip; required, an icon alone says nothing */
  label: string
  size?: ButtonSize
  iconSize?: IconSize
  variant?: ButtonVariant
  /** a toggle: sets aria-pressed and the pressed look */
  pressed?: boolean
  loading?: boolean
  /** a filled square icon (Stop) */
  fill?: boolean
  onClick?: (e: MouseEvent<HTMLElement>) => void
}

/* An icon-only button. The label is its aria-label and its tip (Tip), so every icon
   button has a name for screen readers and a tip for the mouse; `title` gives the tip other
   words (why it cannot act). */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { icon: Icon, label, size = 'md', iconSize, variant = 'ghost', pressed, loading = false, fill = false,
    className, disabled, type = 'button', title, onClick, ...rest },
  ref,
) {
  const s = ICON[iconSize ?? (size === 'sm' ? 'sm' : 'md')]
  return (
    <Press forward={ref} type={type} aria-label={label} data-tip={title ?? label}
           aria-pressed={pressed} aria-busy={loading || undefined}
           className={cx('btn', 'btn--icon', `btn--${variant}`, size === 'sm' && 'btn--sm', className)}
           {...rest} {...off(!!disabled || loading, onClick)}
           inner={loading ? <LoaderCircle size={s} className="spin" aria-hidden />
             : <Icon size={s} aria-hidden fill={fill ? 'currentColor' : 'none'} />} />
  )
})

/* The one send button, in every composer: a round icon button in its colour while there
   is something to send, Stop (a filled square, the error colour) while an answer runs. */
export function SendButton({ ready, running = false, onSend, onStop, title, label = 'Send' }: {
  /** something to send and nothing in the way */
  ready: boolean
  running?: boolean
  onSend: () => void
  onStop?: () => void
  /** why it cannot send (offline, an image in the way) */
  title?: string
  label?: string
}) {
  const stop = running && !!onStop
  return (
    <IconButton icon={stop ? Square : Send} fill={stop} label={stop ? 'Stop' : label} iconSize={stop ? 'sm' : undefined}
                variant={stop ? 'danger' : ready ? 'primary' : 'secondary'} className="send-btn"
                data-keep-focus
                title={stop ? 'Stop the reply' : title} disabled={!stop && !ready}
                onClick={() => (stop ? onStop!() : onSend())} />
  )
}
