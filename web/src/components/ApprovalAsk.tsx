/* A request of hers that waits for you (server/celine/approvals.py: an edit of herself, a
   vault tap, a public share, a change of Settings), after the message that made the call,
   drawn as Bastet's ask is (GuardBanner): the kind's name and the server's line, Approve
   and Deny; a change of Settings lists each field before and after, and her why. Your answer
   is the server's to apply; the row goes by the event approval.closed, here and in every
   other tab. */
import { CircleHelp } from 'lucide-react'
import { Button, ICON, InlineError } from '../ui'
import { settingsApi, type ChatApproval } from '../lib/settingsApi'
import { useAction } from '../lib/useAction'
import './GuardBanner.css'

export function ApprovalAsk({ ask }: { ask: ChatApproval }) {
  const answer = useAction((ok: boolean) => settingsApi.answerApproval(ask.id, ok))
  return (
    <div className="guard-banner">
      <div className="guard-banner__row">
        <span className="guard-banner__icon guard-banner__icon--ask">
          <CircleHelp size={ICON.sm} aria-hidden />
        </span>
        <p className="guard-banner__line"><b>{ask.label}</b> {ask.line}</p>
        <span className="guard-banner__actions">
          <Button size="sm" loading={answer.busy} onClick={() => void answer.run(true)}>Approve</Button>
          <Button size="sm" variant="ghost" disabled={answer.busy} onClick={() => void answer.run(false)}>Deny</Button>
        </span>
      </div>
      {ask.changes && (
        <div className="guard-banner__thread">
          {ask.changes.map(c => (
            <p key={c.label} className="guard-banner__said"><b>{c.label}</b> {c.before !== undefined && <>{c.before} {'→'} </>}{c.after}</p>
          ))}
          {ask.why && <p className="guard-banner__said">{ask.why}</p>}
        </div>
      )}
      {answer.error && <InlineError compact message={answer.error} />}
    </div>
  )
}
