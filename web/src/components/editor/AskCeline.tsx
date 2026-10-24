/* Ask Celine, beside every large editor: a box to ask it, then its draft beside the text as it
   streams in (events `texts.draft`, the whole draft so far each time). The draft never touches
   the text until Copy, Insert or Replace is pressed. The agent's face and name are its own. */
import { useRef, useState } from 'react'
import { Check, Copy, RefreshCw } from 'lucide-react'
import { Button, Card, ComposerBox, Dots, IconButton, Notice, useCopied } from '../../ui'
import { useServerEvents } from '../../lib/bus'
import { textsApi, type AskDraft, type TextOf } from '../../lib/texts'
import { errorText } from '../../lib/format'
import { useName } from '../../lib/name'
import { Portrait } from '../Portrait'

export function AskCeline({ of, text, label, hint, onInsert, onReplace }: {
  /** what the draft is for; a new item without one still asks, for its kind */
  of: TextOf | undefined
  /** the text as it stands, which it reads */
  text: string
  /** the text the draft is for, when the editor has several */
  label?: string
  hint: string
  onInsert: (draft: string) => void
  onReplace: (draft: string) => void
}) {
  const name = useName()
  const [q, setQ] = useState('')
  const [asked, setAsked] = useState('')
  const [id, setId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, copyIt] = useCopied()

  // a draft's first events can come before its id does: the newest of each is kept
  const seen = useRef(new Map<string, AskDraft>())
  const show = (d: AskDraft) => {
    setDraft(d.text)
    if (d.error) setError(d.error)
    if (d.done || d.error) setBusy(false)
  }
  useServerEvents(ev => {
    if (ev.type !== 'texts.draft') return
    const d = ev as unknown as AskDraft
    if (d.id === id) show(d)
    else if (!id) seen.current.set(d.id, d)
  })

  const send = async (prompt: string) => {
    if (!prompt) return
    setAsked(prompt); setDraft(''); setError(null); setBusy(true); setQ('')
    setId(null); seen.current.clear()
    try {
      const { id: started } = await textsApi.ask({ of: of ?? 'new', prompt, text })
      setId(started)
      const early = seen.current.get(started)
      if (early) show(early)
      seen.current.clear()
    } catch (e) { setError(errorText(e)); setBusy(false) }
  }

  return (
    <aside className="led-celine" aria-label={`Ask ${name}`}>
      <ComposerBox value={q} onChange={setQ} onSend={() => void send(q.trim())} running={busy}
                   start={<Portrait size="sm" />} placeholder={hint} label={`Ask ${name}`} />
      {asked && (
        <Card className="led-draft" title={asked} subtitle={label}
              actions={<>
                <Button size="sm" icon={copied ? Check : Copy} disabled={busy || !draft}
                        onClick={() => void copyIt(draft)}>{copied ? 'Copied' : 'Copy'}</Button>
                <Button size="sm" disabled={busy || !draft} onClick={() => onInsert(draft)}>Insert</Button>
                <Button size="sm" disabled={busy || !draft} onClick={() => onReplace(draft)}>Replace</Button>
                <span className="led-grow" />
                <IconButton icon={RefreshCw} label="Regenerate" size="sm" disabled={busy} onClick={() => void send(asked)} />
              </>}>
          {error ? <Notice tone="error" action={{ label: 'Try again', onClick: () => void send(asked) }}>{error}</Notice>
            : <div className="led-draft__text">{draft}{busy && <Dots className="led-dots" />}</div>}
        </Card>
      )}
    </aside>
  )
}
