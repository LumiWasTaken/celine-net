/* States: Notice (the one error/info widget), InlineError, StatusLine (fixed height),
   EmptyState, LoadingRow, and Loaded, which picks one of them for a loaded resource. Principle: an error sits where it happened, with the
   action that fixes it, never in a toast. */
import type { ReactNode } from 'react'
import { CircleCheck, Info, LoaderCircle, RefreshCw, TriangleAlert, X, type LucideIcon } from 'lucide-react'
import { Button, IconButton } from './Button'
import { cx } from './cx'
import { ICON } from './icons'

type Tone = 'info' | 'warn' | 'error' | 'success'
const TONE_ICON: Record<Tone, LucideIcon> = { info: Info, warn: TriangleAlert, error: TriangleAlert, success: CircleCheck }

export type NoticeAction = { label: string; onClick: () => void; icon?: LucideIcon; busy?: boolean }

export function Notice({ tone = 'info', title, children, action, compact = false, className, icon, onDismiss, dismissLabel = 'Dismiss' }: {
  tone?: Tone
  title?: ReactNode
  children?: ReactNode
  /** the fix, next to the problem */
  action?: NoticeAction
  /** one line of coloured text with an icon, no box: under a form field, inside a row */
  compact?: boolean
  className?: string
  icon?: LucideIcon
  /** an X at the end that puts it away */
  onDismiss?: () => void
  dismissLabel?: string
}) {
  const Icon = icon ?? TONE_ICON[tone]
  return (
    <div role={tone === 'error' ? 'alert' : 'status'}
         className={cx('notice', `notice--${tone}`, compact && 'notice--compact', className)}>
      <Icon size={compact ? ICON.sm : ICON.md} className="notice__icon" aria-hidden />
      <div className="notice__body">
        {title && <span className="notice__title">{title}</span>}
        {children}
      </div>
      {action && !compact && (
        <Button size="sm" variant="secondary" className="notice__action" icon={action.icon}
                loading={action.busy} onClick={action.onClick}>{action.label}</Button>
      )}
      {action && compact && (
        <Button variant="ghost" size="sm" onClick={action.onClick} disabled={action.busy}>{action.label}</Button>
      )}
      {onDismiss && <IconButton size="sm" icon={X} label={dismissLabel} className="notice__action" onClick={onDismiss} />}
    </div>
  )
}

/* The error form of Notice, with Retry as the usual fix. */
export function InlineError({ message, onRetry, compact = false, className }: {
  message: ReactNode
  onRetry?: () => void
  compact?: boolean
  className?: string
}) {
  return (
    <Notice tone="error" compact={compact} className={className}
            action={onRetry ? { label: 'Retry', onClick: onRetry, icon: compact ? undefined : RefreshCw } : undefined}>
      {message}
    </Notice>
  )
}

/* A one-line status that always takes its height, text or not, so nothing below
   or beside it moves when it appears ("Reconnecting", "Saved", "3 selected"). */
export function StatusLine({ children, tone, spinner = false, className }: {
  children?: ReactNode
  tone?: 'running' | 'error' | 'warn' | 'success'
  spinner?: boolean
  className?: string
}) {
  return (
    <div className={cx('status-line', tone && `status-line--${tone}`, className)}
         role="status" aria-live="polite">
      {children ? <>
        {spinner && <LoaderCircle size={ICON.sm} className="spin" aria-hidden />}
        <span>{children}</span>
      </> : null}
    </div>
  )
}

/* Loading in place of content: skeleton rows in the surface colour, the height of what
   comes, so nothing jumps and no spinner turns mid-page. `inline`: one short bar. */
export function LoadingRow({ label = 'Loading', inline = false, className }: { label?: string; inline?: boolean; className?: string }) {
  return (
    <div className={cx('skeleton', inline && 'skeleton--inline', className)} role="status" aria-label={label}>
      <i /> {!inline && <><i /><i /></>}
    </div>
  )
}

/* Nothing here: the one EmptyState. An icon on a round surface, one line (t-lg, 580), at
   most one short second line and one action, centred in the pane, once per page.
   `compact`: the same words, left and smaller, inside a list. */
export function EmptyState({ icon: Icon, title, children, action, compact = false, className }: {
  icon?: LucideIcon
  title?: ReactNode
  /** the one line */
  children?: ReactNode
  action?: ReactNode
  /** left-aligned, for inside a list */
  compact?: boolean
  className?: string
}) {
  return (
    <div className={cx('empty', compact && 'empty--compact', className)}>
      {Icon && !compact && <span className="empty__icon"><Icon size={ICON.lg} aria-hidden /></span>}
      {title && <div className="empty__head">{title}</div>}
      {children && <div className="empty__line">{children}</div>}
      {action && <div className="empty__action">{action}</div>}
    </div>
  )
}

type Empty = { icon?: LucideIcon; title: ReactNode; action?: ReactNode; children?: ReactNode }

/* A loaded resource (useResource's result) drawn the same way everywhere: Not found (a 404,
   nothing to retry), the skeleton while it loads, its error with Retry while nothing came,
   the empty state the caller names, then the ready render. A reload that fails keeps what
   came and puts its error above it. `page`: on a list page, the error and the empty state
   are centred EmptyStates (list-page__empty); else an InlineError and a compact EmptyState.
   With no children it draws only the states before the data (`if (!d) return <Loaded res={r} />`). */
export function Loaded<T>({ res, page = false, icon, label, inline, compact, empty, children }: {
  res: { data: T | undefined; error: string | null; missing?: boolean; reload: () => void }
  page?: boolean
  /** the icon of Not found, the error and the empty state */
  icon?: LucideIcon
  /** the skeleton's words for screen readers */
  label?: string
  /** a one-line skeleton */
  inline?: boolean
  /** a compact InlineError */
  compact?: boolean
  /** shown in place of the ready render, when the caller says the data is empty */
  empty?: Empty | null | false
  children?: (data: T) => ReactNode
}) {
  const state = (title: ReactNode, action?: ReactNode, Icon = icon, line?: ReactNode) => (
    page ? <div className="list-page__empty"><EmptyState icon={Icon} title={title} action={action}>{line}</EmptyState></div>
      : <EmptyState compact icon={Icon} title={title} action={action}>{line}</EmptyState>
  )
  if (res.missing) return state('Not found')
  if (res.data === undefined) {
    if (!res.error) return <LoadingRow label={label} inline={inline} />
    return page ? state(res.error, <Button onClick={res.reload}>Retry</Button>)
      : <InlineError compact={compact} message={res.error} onRetry={res.reload} />
  }
  const failed = res.error && <InlineError compact message={res.error} onRetry={res.reload} />
  return <>
    {failed && (page ? <div className="list-page__note">{failed}</div> : failed)}
    {empty ? state(empty.title, empty.action, empty.icon ?? icon, empty.children) : children?.(res.data)}
  </>
}
