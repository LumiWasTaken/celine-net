/* Status: where a reply will land before it is there, in Celine's chat and in every Talk
   (a mission's): the typing dots, with what holds it in words as their tooltip
   ("Waiting for the model: Self-review is running.", the model loading); the reply's first
   line takes the same place. Words come from the server. A conversation shows one, for
   its oldest queued turn; turns after it a muted "Queued".

   TurnEnd is the line where an answer did not come, in soft words: "Paused." with
   Continue, "The reply failed." with Retry, the server's sentence as their tooltip. A line
   that needs the user (it names a setting, or the failure carries a reason) says it
   whole, with Open Settings when it names one. A reply that failed because its provider is
   busy (reason_code `busy`) offers Switch provider (the model chooser; the pick becomes the
   conversation's model, then Regenerate) and Keep waiting (Continue with `wait`).

   `statusWords` is what both say, so the live region (Transcript) reads what the screen
   shows. */
import { useEffect, useState } from 'react'
import { RUNNING, conversationApi, type Live, type Turn } from '../lib/api'
import { errorText } from '../lib/format'
import { useNav } from '../lib/nav'
import { OFFLINE_TITLE, useOnline } from '../lib/socket'
import type { QueueWait } from '../lib/useConversation'
import { settingsApi } from '../lib/settingsApi'
import { useResource } from '../lib/useResource'
import { ArrowLeftRight, Hourglass, Play, RefreshCw, Settings } from 'lucide-react'
import { Button, Dots, InlineError, cx } from '../ui'
import { ModelChooser } from './ModelChooser'
import './Transcript.css'

/** what holds the reply, in words; null while the dots say it (or nothing waits) */
export function waitWords(wait: QueueWait | null, live: Live | null): string | null {
  if (wait) {
    if (wait.noModel != null) return `Queued until a chat model answers. ${wait.noModel}`.trim()
    if (wait.held) return wait.held
    if (wait.limited) return wait.limited
    return wait.holder ? `Waiting for the model: ${wait.holder} is running.` : 'Waiting for the model.'
  }
  if (live?.phase === 'loading') return `Loading ${live.model ?? 'the model'}.`
  return null
}

/** the reply's slot: the dots, what holds it as their tooltip */
export function Status({ words, queued = false }: { words: string | null; queued?: boolean }) {
  return (
    <div className={cx('status', queued && 'status--queued')}>
      {queued ? 'Queued' : <span className="status__dots" title={words ?? undefined} role="status" aria-label={words ?? undefined}><Dots /></span>}
    </div>
  )
}

/* One sentence for a reason the server says nothing about in words. A failed turn's
   reason is the server's sentence (turns.reason) and shows as it is. */
const REASON: Record<string, string> = {
  restart: 'The server restarted during the answer.',
  wall_clock: 'The answer took too long and was stopped.',
  time_up: 'The answer took too long and was stopped.',
  max_tool_iterations: 'Stopped at the step limit.',
  cut_length: 'The answer reached the length limit.',
}
const ENDED: Record<string, string> = {
  aborted: 'Stopped.', interrupted: 'Interrupted.', capped: 'Stopped at the step limit.', failed: 'The answer failed.',
  done: 'The answer reached the length limit.',
}

/** the line under an answer that did not come; null when it came */
export function endWords(turn: Turn | null, error: { message: string } | null, unanswered: boolean): string | null {
  const ended = !!turn && !RUNNING.includes(turn.state) && (turn.state !== 'done' || !!turn.cut)
  if (ended) {
    if (turn.state === 'failed' && turn.reason) return turn.reason
    return (turn.reason_code && REASON[turn.reason_code]) || ENDED[turn.state] || 'The answer did not finish.'
  }
  if (error) return error.message
  return unanswered ? 'No answer yet.' : null
}

/* "Settings, Models" in a sentence: the page it names */
const PAGE: Record<string, string> = {
  general: 'general', people: 'people', models: 'models', tools: 'tools', email: 'email', vault: 'vault',
  security: 'security', sources: 'sources', 'search index': 'search',
}
export function settingOf(sentence: string): { label: string; page: string } | null {
  const m = /Settings, ([A-Z][a-z]+(?: index)?)/.exec(sentence)
  const page = m ? PAGE[m[1]!.toLowerCase()] : undefined
  return m && page ? { label: `Open Settings, ${m[1]}`, page } : null
}

export function TurnEnd({ conversationId, turn, running, error, unanswered, onRetry }: {
  conversationId: string; turn: Turn | null; running: boolean
  error: { message: string; retryable: boolean } | null; unanswered: boolean; onRetry: () => Promise<void>
}) {
  const online = useOnline()
  const nav = useNav()
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const [choosing, setChoosing] = useState(false)
  const providers = useResource(() => settingsApi.providers().then(r => r.providers), [], { enabled: choosing })
  useEffect(() => { setFailed(null) }, [turn?.id, turn?.state])
  if (running) return null
  const sentence = endWords(turn, error, unanswered)
  if (!sentence) return null
  const ended = !!turn && !RUNNING.includes(turn.state)
  // the provider is busy: Switch provider or Keep waiting, in place of Continue and Retry
  const busyTurn = ended && turn.state === 'failed' && turn.reason_code === 'busy'
  const canContinue = !busyTurn && !!(ended && turn.continuable?.ok)
  // a stop you asked for needs no Retry; everything else that left no answer does
  const canRetry = !busyTurn && !canContinue && (turn?.state === 'failed' || turn?.state === 'interrupted' || unanswered || !!error?.retryable)
  const setting = settingOf(sentence)

  const act = async (fn: () => Promise<unknown>, el: HTMLElement) => {
    setBusy(true); setFailed(null)
    try {
      await fn()
      // on from here: the box, ready for what you say next
      let at: HTMLElement | null = el
      while (at && !at.querySelector('.composer textarea')) at = at.parentElement
      at?.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus({ preventScroll: true })
    } catch (e) { setFailed(errorText(e)) } finally { setBusy(false) }
  }
  // a failure with the server's reason, or one that names a setting, needs the user: said whole
  const broke = turn?.state === 'failed' || turn?.state === 'interrupted' || (!!error && !ended)
  const owner = !!setting || (turn?.state === 'failed' && !!turn.reason) || (!!error && !ended)
  const words = broke ? 'The reply failed.' : canContinue ? 'Paused.' : sentence
  return (
    <div className={cx('turn-end', owner && broke && 'turn-end--error')}>
      <span className="turn-end__text" title={words !== sentence && !owner ? sentence : undefined}>
        {owner && broke ? <><b>{words}</b> {sentence}</> : words}
      </span>
      {setting && (
        <Button size="sm" icon={Settings} onClick={() => nav.go({ section: 'settings', page: setting.page })}>{setting.label}</Button>
      )}
      {busyTurn && <>
        <Button size="sm" icon={ArrowLeftRight} disabled={!online || busy} title={online ? undefined : OFFLINE_TITLE}
                onClick={() => setChoosing(true)}>Switch provider</Button>
        <Button size="sm" icon={Hourglass} loading={busy} disabled={!online} title={online ? undefined : OFFLINE_TITLE}
                onClick={e => { const el = e.currentTarget; void act(() => conversationApi.continueTurn(conversationId, turn!.id, true), el) }}>
          Keep waiting
        </Button>
      </>}
      {choosing && (providers.data ? (
        <ModelChooser title="Switch provider" providers={providers.data} needs={['chat']} onClose={() => setChoosing(false)}
                      onPick={ref => { setChoosing(false); void act(async () => {
                        await settingsApi.patchConversationSettings(conversationId, { model: ref }); await onRetry()
                      }, document.body) }} />
      ) : providers.error && <InlineError compact message={providers.error} onRetry={providers.reload} />)}
      {(canContinue || canRetry) && (
        <Button size="sm" icon={canContinue ? Play : RefreshCw} loading={busy} disabled={!online} title={online ? undefined : OFFLINE_TITLE}
                onClick={e => { const el = e.currentTarget; void act(canContinue ? () => conversationApi.continueTurn(conversationId, turn!.id) : onRetry, el) }}>
          {canContinue ? 'Continue' : 'Retry'}
        </Button>
      )}
      {failed && <InlineError compact message={failed} />}
    </div>
  )
}
