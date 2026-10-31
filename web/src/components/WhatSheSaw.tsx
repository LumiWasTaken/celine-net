/* "What it saw" for one of its replies: its own block for the message it answers (On
   its mind: its mood, the time since), then each step's request as it went out (the
   system text, the tools, the conversation) and what the model's server answered, raw. GET .../messages/{id}/context,
   .../requests and .../raw per step; Copy all copies them with the conversation's id. A step
   with nothing kept is left out; with none kept, one line says so. */
import { useEffect, useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { conversationApi, type Message, type SentRequest } from '../lib/api'
import { contextApi, type OnHerMind } from '../lib/context'
import { useOwner } from '../lib/name'
import { errorText, num } from '../lib/format'
import { Button, InlineError, LoadingRow, Sheet, StatusLine, useCopied } from '../ui'
import './Transcript.css'

type Step = { requests: SentRequest[]; raw: unknown | null }

/** a request message's content as text: a string, or its parts' text */
const textOf = (c: unknown): string =>
  typeof c === 'string' ? c
    : Array.isArray(c) ? c.map(p => (p && typeof p === 'object' && 'text' in p ? String(p.text) : `[${String((p as { type?: string })?.type ?? 'part')}]`)).join('\n')
    : c == null ? '' : JSON.stringify(c, null, 2)

const ROLE: Record<string, string> = { user: 'User', assistant: 'Assistant', tool: 'Tool', system: 'System' }

export function WhatSheSaw({ conversationId, steps, title, onClose }: {
  conversationId: string
  /** the reply's steps, one model request each */
  steps: Message[]
  title: string
  onClose: () => void
}) {
  const [data, setData] = useState<Step[] | null>(null)
  const [mind, setMind] = useState<OnHerMind | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [copied, copyIt] = useCopied()
  const ids = steps.map(m => m.id).join(' ')
  useEffect(() => {
    let live = true
    Promise.all(ids.split(' ').map(async id => ({
      requests: (await conversationApi.requests(conversationId, id)).requests,
      raw: (await conversationApi.raw(conversationId, id)).raw,
    })))
      .then(r => { if (live) setData(r) }, e => { if (live) setError(errorText(e)) })
    // a reply with no context block has no On its mind
    contextApi.ofReply(conversationId, ids.split(' ')[0]!)
      .then(r => { if (live) setMind(r.report.mind ?? null) }, () => undefined)
    return () => { live = false }
  }, [conversationId, ids])

  const copy = () => {
    const text = JSON.stringify({ conversation: conversationId, steps: data }, null, 2)
    void copyIt(text)
  }
  const kept = data?.map((st, i) => ({ st, i })).filter(({ st }) => st.requests.length > 0 || st.raw !== null)

  return (
    <Sheet title={title} size="lg" className="saw" onClose={onClose}
           actions={<Button variant="secondary" icon={copied ? Check : Copy} disabled={!data}
                            onClick={copy}>{copied ? 'Copied' : 'Copy all'}</Button>}>
      {mind && <MindView mind={mind} />}
      {error ? <InlineError compact message={error} />
        : !kept ? <LoadingRow />
        : !kept.length ? <StatusLine>Not kept</StatusLine>
        : kept.map(({ st, i }) => <StepView key={steps[i]!.id} n={data!.length > 1 ? i + 1 : null} step={st} />)}
    </Sheet>
  )
}

/* its mood, the gap */
function MindView({ mind }: { mind: OnHerMind }) {
  const owner = useOwner()
  const lines = [
    // the server's line already starts with "mood:"
    ...(mind.mood ? [mind.mood.charAt(0).toUpperCase() + mind.mood.slice(1)] : []),
    ...(mind.gap ? [mind.gap] : []),
  ]
  if (!lines.length) return null
  return <Block label={`On ${owner} mind`}>{lines.join('\n')}</Block>
}

function StepView({ n, step }: { n: number | null; step: Step }) {
  const req = step.requests.at(-1)
  const msgs = req?.body.messages ?? []
  const system = msgs.filter(m => m.role === 'system')
  const talk = msgs.filter(m => m.role !== 'system')
  const tools = req?.body.tools ?? []
  const facts = req && [req.model, req.prompt_tokens != null && `${num(req.prompt_tokens)} in`,
    req.cache_read != null && `${num(req.cache_read)} cached`,
    req.completion_tokens != null && `${num(req.completion_tokens)} out`,
    req.first_token_ms != null && `first token ${(req.first_token_ms / 1000).toFixed(1)} s`,
    req.finish_reason && req.finish_reason !== 'stop' && req.finish_reason].filter(Boolean).join(', ')
  return (
    <section className="saw__step">
      {(n !== null || facts) && <p className="saw__head">{n !== null && <b>Step {n}</b>}{facts}</p>}
      {system.length > 0 && <Block label="System">{system.map(m => textOf(m.content)).join('\n\n')}</Block>}
      {tools.length > 0 && <Block label={tools.length === 1 ? '1 tool' : `${tools.length} tools`}>{JSON.stringify(tools, null, 2)}</Block>}
      {talk.length > 0 && <Block label="Conversation">{talk.map(m => `${ROLE[m.role] ?? m.role}${m.role === 'tool' && (m.name ?? m.tool_name) ? ` ${m.name ?? m.tool_name}` : ''}: ${textOf(m.content)}`).join('\n\n')}</Block>}
      {step.raw !== null && <Block label="Raw response">{JSON.stringify(step.raw, null, 2)}</Block>}
    </section>
  )
}

/* one part of a request: its label over its text, which scrolls in its own box */
function Block({ label, children }: { label: string; children: string }) {
  return <div className="saw__step"><span className="saw__label">{label}</span><pre className="code-block">{children}</pre></div>
}
