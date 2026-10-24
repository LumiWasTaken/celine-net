/* The agent's plan in a conversation's head: one small button with where it is ("4 of 5",
   "Waiting for you", "Done"; a phone shows the count only), shown while the plan has
   steps. It opens the kit's Dialog "Plan": its words, then every step with its number,
   its state, where it asks you, and its time (done when, doing since). */
import { useState } from 'react'
import { ListChecks, MessageCircleQuestion } from 'lucide-react'
import type { Plan, PlanStep } from '../lib/api'
import { stamp } from '../lib/format'
import { Button, Dialog, ICON, Step, cx, usePhone } from '../ui'
import './Plan.css'

export function PlanButton({ plan }: { plan: Plan | null }) {
  const [open, setOpen] = useState(false)
  const phone = usePhone()
  if (!plan || plan.steps.length === 0) return null
  const done = plan.steps.filter(s => s.state === 'done').length
  const count = `${done} of ${plan.steps.length}`
  const label = plan.waiting ? 'Waiting for you' : done === plan.steps.length ? 'Done' : count
  return (
    <>
      <Button icon={ListChecks} variant="ghost" className="plan-button" title="Plan" onClick={() => setOpen(true)}>
        {phone ? count : label}
      </Button>
      {open && <PlanDialog plan={plan} onClose={() => setOpen(false)} />}
    </>
  )
}

function when(s: PlanStep): string {
  if (s.state === 'done' && s.done_at) return `done ${stamp(s.done_at)}`
  if (s.state === 'doing' && s.started_at) return `since ${stamp(s.started_at)}`
  return ''
}

function PlanDialog({ plan, onClose }: { plan: Plan; onClose: () => void }) {
  const at = plan.steps.findIndex(s => s.state === 'doing')
  return (
    <Dialog title="Plan" onClose={onClose} size="md">
      {plan.text.trim() && <p className="plan-dialog__text">{plan.text.trim()}</p>}
      <ol className="plan-dialog__steps">
        {plan.steps.map((s, i) => (
          <li key={i} className={cx('plan-dialog__step', s.state === 'done' && 'is-done', i === at && 'is-current')}>
            <Step n={i + 1} size="sm" done={s.state === 'done'} current={i === at} />
            <span className="plan-dialog__words">{s.title}</span>
            {s.checkpoint === 'ask' && <span className="plan-dialog__ask" title="Asks you"><MessageCircleQuestion size={ICON.xs} aria-label="Asks you" /></span>}
            <span className="plan-dialog__time">{when(s)}</span>
          </li>
        ))}
      </ol>
    </Dialog>
  )
}
