/* Pieces every Settings page uses: the page frame, the status dot, a task as one
   quiet line. The generic form is the kit's Form (ui/Form.tsx). */
import { useSyncExternalStore, type ReactNode } from 'react'
import { CircleCheck, RefreshCw, Square, TriangleAlert, X } from 'lucide-react'
import { kbApi, type Task } from '../../lib/kb'
import { count, num } from '../../lib/format'
import { format, useNav } from '../../lib/nav'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { useAction } from '../../lib/useAction'
import type { Level } from '../../lib/settingsApi'
import { LEVEL_WORD } from '../../lib/sources'
import { Button, Dot, ICON, IconButton, InlineError, PlaceHead, Progress, cx, usePhone, type Crumb, type MenuItem } from '../../ui'
import './settings.css'

/* ── the page frame: the pane head and a scrolling body with the page's column in the
   middle. Below 1024 px the list and the page take turns (settings.css), so the head
   leads back to Settings; a page under another page always leads back to it. ── */
const ONE_PANE = '(max-width: 1023px)'
const onePane = () => matchMedia(ONE_PANE).matches
const watchPane = (cb: () => void) => {
  const m = matchMedia(ONE_PANE)
  m.addEventListener('change', cb)
  return () => m.removeEventListener('change', cb)
}

export function Page({ title, detail, actions, more, children, wide = false, fill = false, up }: {
  title: ReactNode
  /** a muted state beside the title (a provider's health) */
  detail?: ReactNode
  /** the page's own action in its head (Check provider) */
  actions?: ReactNode
  /** the page's rare actions behind "..." (Remove this provider) */
  more?: MenuItem[]
  children: ReactNode
  /** lists that need the room (Discord servers, people) */
  wide?: boolean
  /** the content fills the height and scrolls itself (a virtual list) */
  fill?: boolean
  /** the page this one sits under (a provider under Models) */
  up?: { page: string; label: string }
}) {
  const nav = useNav()
  const phone = usePhone()
  const one = useSyncExternalStore(watchPane, onePane, () => false)
  const above = up ?? (one ? { page: '', label: 'Settings' } : null)
  // a phone has the head's back arrow; a wider screen names the page above as a crumb
  const back = phone && above ? { label: above.label, onClick: () => nav.go({ section: 'settings', page: above.page || undefined }) } : undefined
  const crumbs: Crumb[] | undefined = !phone && above
    ? [{ label: above.label, href: format({ section: 'settings', page: above.page || undefined }) }] : undefined
  return (
    <>
      <PlaceHead title={title} detail={detail} actions={actions} more={more} level={1} className="settings__head" back={back} crumbs={crumbs} />
      <div className={cx('page', 'settings__content', fill && 'page--fill')}>
        <div className={cx('page__col settings__col', wide && 'page__col--wide settings__col--wide', fill && 'page__col--fill')}>{children}</div>
      </div>
    </>
  )
}

/* ── the source dot: filled (OK), pulsing (busy), amber (needs you), hollow (imports only);
   `word`: the state's word beside it (a page's head) ── */
export function StatusDot({ level, word = false }: { level: Level; word?: boolean }) {
  const tone = ({ ok: 'ok', working: 'running', needs_you: 'warn', off: 'off' } as const)[level]
  return <Dot tone={tone} pulse={level === 'working'} label={LEVEL_WORD[level]} text={word ? LEVEL_WORD[level] : undefined} />
}

/* ── one task, as one quiet line where it was started ────────────────────────
   Running: what it does, a progress bar and Stop. Done: one sentence of result and an
   × to dismiss it. Failed: the reason, Retry and ×. Stopped: Continue and ×. Finished
   tasks also go away on their own (done after an hour, failed after a day). */
const WHAT: Record<string, string> = {
  sync: 'Syncing', import: 'Importing', feed: 'Syncing new messages',
  subscribe: 'Syncing past messages', channels: 'Syncing new channels',
  semantic: 'Updating topic search', captions: 'Writing photo descriptions',
}
/* server progress units in the glossary's words; the semantic topic search index shows no unit */
const UNIT: Record<string, string> = {
  'passages embedded': '', 'chats cut into passages': 'chats', files: 'files', chats: 'chats',
}

function taskWhat(t: Task): string {
  const kind = t.kind.split(':')[0]!
  return WHAT[t.kind] ?? WHAT[kind] ?? 'In progress'
}

export function TaskLine({ task: t, onChanged }: { task: Task; onChanged?: () => void }) {
  const online = useOnline()
  const stop = useAction(() => kbApi.cancelTask(t.id))
  const retry = useAction(async () => { await kbApi.retryTask(t.id); onChanged?.() })
  const dismiss = useAction(async () => { await kbApi.dismissTask(t.id); onChanged?.() })
  const x = t.state !== 'running' && (
    <IconButton icon={X} size="sm" label="Dismiss" loading={dismiss.busy} onClick={() => void dismiss.run()} />
  )
  return (
    <div className={`task task--${t.state}`}>
      {t.state === 'running' ? (
        <div className="task__line">
          <span className="task__text">{taskWhat(t)}</span>
          <span className="task__count">
            {t.progress?.done ? `${num(t.progress.done)}${t.progress.total ? ` of ${num(t.progress.total)}` : ''} ${UNIT[t.progress.unit] ?? t.progress.unit}`.trim() : ''}
          </span>
          <Button variant="ghost" size="sm" icon={Square} loading={stop.busy} onClick={() => void stop.run()}>Stop</Button>
        </div>
      ) : t.state === 'done' ? (
        <div className="task__line">
          <CircleCheck size={ICON.sm} className="task__icon" aria-hidden />
          <span className="task__text">{resultText(t)}</span>
          {x}
        </div>
      ) : t.state === 'failed' ? (
        <div className="task__line">
          <TriangleAlert size={ICON.sm} className="task__icon" aria-hidden />
          <span className="task__text">{failedText(t)}</span>
          <Button size="sm" icon={RefreshCw} loading={retry.busy} disabled={!online} title={online ? undefined : OFFLINE_TITLE}
                  onClick={() => void retry.run()}>Retry</Button>
          {x}
        </div>
      ) : t.state === 'unreachable' ? (
        <div className="task__line">
          <span className="task__text">{t.error}</span>
          <Button size="sm" icon={RefreshCw} loading={retry.busy} disabled={!online} title={online ? undefined : OFFLINE_TITLE}
                  onClick={() => void retry.run()}>Retry</Button>
          {x}
        </div>
      ) : (
        <div className="task__line">
          <span className="task__text">Stopped.</span>
          <Button size="sm" icon={RefreshCw} loading={retry.busy} disabled={!online} title={online ? undefined : OFFLINE_TITLE}
                  onClick={() => void retry.run()}>Continue</Button>
          {x}
        </div>
      )}
      {t.state === 'running' && <Progress value={t.progress?.done} max={t.progress?.total ?? null} label={taskWhat(t)} />}
      {(stop.error || retry.error || dismiss.error) && <InlineError compact message={stop.error ?? retry.error ?? dismiss.error} />}
    </div>
  )
}

/* A task error that is already a sentence is shown as it is. */
function failedText(t: Task): string {
  const e = t.error?.trim()
  if (e && /^[A-Z]/.test(e) && /[.!?]$/.test(e)) return e
  return `${taskWhat(t)} stopped: ${e || 'no reason given'}.`
}

/* One sentence, never the raw result fields. The server's own sentence wins. */
function resultText(t: Task): string {
  const r = t.result ?? {}
  if (typeof r.sentence === 'string' && r.sentence) return r.sentence
  const n = (k: string) => (typeof r[k] === 'number' ? r[k] as number : 0)
  if (typeof r.note === 'string') return `Saved ${t.label} to Documents.`
  const added = n('added'), existing = n('existing')
  let text = added
    ? `Added ${count(added, 'new message')}.${existing ? ` ${num(existing)} were already in the archive.` : ''}`
    : existing ? `Nothing new: all ${count(existing, 'message')} were already in the archive.` : 'Nothing new.'
  if (n('new_dms')) text += ` ${count(n('new_dms'), 'new direct message')}.`
  const skipped = Array.isArray(r.skipped) ? (r.skipped as unknown[]).map(String) : []
  if (skipped.length) text += ` ${count(skipped.length, 'chat')} had nothing readable.`
  return text
}

