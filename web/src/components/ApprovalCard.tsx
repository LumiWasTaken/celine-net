/* A request of the agent's that waits for you (server/celine/approvals.py Ask.card), the same
   card wherever it shows: in its chat after the message that made the call, on the Vault
   page, in Waiting for you. Bastet's banner row (GuardBanner.css): the raised hand, the
   server's one-sentence title, its "?" (why it is asked), Approve, Deny and Details (the
   long text, whole, in a Sheet); the call's own data as facts under it, where long
   values wrap. `onOpen` (Waiting for you): the line that names where its work lives,
   and Open. Your answer is the server's to apply; the card goes by the events
   approval.closed and approvals.changed, here and in every other tab. */
import { useState } from 'react'
import { Hand } from 'lucide-react'
import { Button, Facts, Help, ICON, InlineError, Sheet, cx } from '../ui'
import { settingsApi, type Approval } from '../lib/settingsApi'
import { useAction } from '../lib/useAction'
import './GuardBanner.css'
import './ApprovalCard.css'

export function ApprovalCard({ ask, alone = false, onOpen }: {
  ask: Approval
  /** not in a chat: it starts at the edge, not past the agent's face */
  alone?: boolean
  onOpen?: (link: string) => void
}) {
  const answer = useAction((ok: boolean) => settingsApi.answerApproval(ask.id, ok))
  const [details, setDetails] = useState(false)
  const where = onOpen ? ask.where : null
  return (
    <div className={cx('guard-banner', 'approval', (alone || onOpen) && 'approval--alone')}>
      <div className="guard-banner__row">
        <span className="guard-banner__icon guard-banner__icon--ask">
          <Hand size={ICON.sm} aria-hidden />
        </span>
        <p className="guard-banner__line">{ask.title} <Help text={ask.help} label="this request" /></p>
        <span className="guard-banner__actions">
          <Button size="sm" loading={answer.busy} onClick={() => void answer.run(true)}>Approve</Button>
          <Button size="sm" variant="ghost" disabled={answer.busy} onClick={() => void answer.run(false)}>Deny</Button>
          {ask.detail && <Button size="sm" variant="ghost" onClick={() => setDetails(true)}>Details</Button>}
          {onOpen && ask.link && (
            <Button size="sm" variant="ghost" href={ask.link}
                    onClick={e => { e.preventDefault(); onOpen(ask.link!) }}>Open</Button>
          )}
        </span>
      </div>
      {(where || ask.facts.length > 0) && (
        <div className="guard-banner__thread">
          {where && <p className="guard-banner__said">{where}</p>}
          {ask.facts.length > 0 && <Facts items={ask.facts} className="approval__facts" />}
        </div>
      )}
      {details && ask.detail && (
        <Sheet title={ask.detail.title} size="lg" onClose={() => setDetails(false)}>
          <p className="approval__text">{ask.detail.text}</p>
        </Sheet>
      )}
      {answer.error && <InlineError compact message={answer.error} />}
    </div>
  )
}
