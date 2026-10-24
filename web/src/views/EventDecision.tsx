/* One decision of Bastet's on the Events page (GET /api/guard/decisions/{id}): her line,
   then the question the small role was asked, its score, the model, how long it took,
   the call's arguments, the quote and the piece. Allow once and Pass answer it as its
   banner's buttons do (POST /guard/banners/{id}/replies); Open goes where it happened. Its title is Details (its line wraps
   to four lines at 400 px), its line the first fact. Every
   word of the row is the server's. server/celine/guard/store.py one, api.py. */
import { Check, TicketCheck } from 'lucide-react'
import { Markdown } from '../components/Markdown'
import { errorText } from '../lib/format'
import { guardApi, type GuardAction } from '../lib/guard'
import { useResource } from '../lib/useResource'
import { Button, Dialog, Facts, InlineError, LoadingRow, Quote, toast } from '../ui'

export const ACTION_WORDS: Record<GuardAction, string> = { once: 'Allow once', pass: 'Pass' }
const LEVEL_WORDS: Record<string, string> = { L1: 'First look', L2: 'Second look' }
const VERDICT_WORDS: Record<string, string> = { Y: 'Yes', N: 'No', unreachable: 'Could not decide' }
export const ACTION_ICONS = { once: Check, pass: TicketCheck }

/** answer a decision from outside its thread; Bastet's reply is the toast */
export async function answer(id: number, action: GuardAction) {
  try {
    const { banner } = await guardApi.answer(id, { button: action })
    toast({ text: banner.thread.at(-1)?.text ?? ACTION_WORDS[action] })
  } catch (e) {
    toast({ text: errorText(e) })
  }
}

type Label = (id: string) => string

export function EventDecision({ id, open, rule, mode, onClose }: {
  id: number
  /** where it happened, the event's link */
  open: string | null
  rule: Label
  mode: Label
  onClose: () => void
}) {
  const res = useResource(() => guardApi.decision(id).then(r => r.decision), [id])
  const d = res.data
  const facts = d ? [
    { label: 'Decision', value: d.line },
    { label: 'Rule', value: rule(d.rule) },
    { label: 'Mode', value: mode(d.mode) },
    d.question && { label: 'Question', value: d.question },
    // a confirmed yes or a no is 1 or 0: only a probability is a score
    d.p != null && d.p > 0 && d.p < 1 && { label: 'Score', value: d.p.toFixed(2) },
    d.model && { label: 'Model', value: d.model },
    d.ms != null && { label: 'Time', value: `${d.ms} ms` },
    d.source && { label: 'Source', value: d.source },
    d.quote && { label: 'Quote', value: <Quote>{d.quote}</Quote> },
    d.piece && { label: 'Piece', value: <Quote>{d.piece}</Quote> },
    ...d.chain.filter(l => VERDICT_WORDS[l.verdict]).map(l => ({
      label: LEVEL_WORDS[l.level] ?? l.level,
      value: [VERDICT_WORDS[l.verdict], l.why, l.model].filter(Boolean)
        .map(s => String(s).replace(/[.!?]+$/, '')).join('. ') + '.',
    })),
    { label: 'Arguments', value: <div className="prose"><Markdown text={'```json\n' + JSON.stringify(d.arguments, null, 2) + '\n```'} /></div> },
  ].filter(x => !!x) as { label: string; value: React.ReactNode }[] : []
  const act = (a: GuardAction) => { void answer(id, a).then(res.reload) }
  return (
    <Dialog title="Details" onClose={onClose} size="lg"
            actions={<>
              {d?.actions.map(a => <Button key={a} icon={ACTION_ICONS[a]} onClick={() => act(a)}>{ACTION_WORDS[a]}</Button>)}
              {open && <Button variant="primary" href={open}>Open</Button>}
            </>}>
      {res.error ? <InlineError message={res.error} /> : !d ? <LoadingRow /> : <Facts items={facts} />}
    </Dialog>
  )
}
