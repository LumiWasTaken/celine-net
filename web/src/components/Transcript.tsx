/* The transcript, the same everywhere (Celine's chat, a mission), in the one design: every message is the kit's Message with its action row under
   it, the last one too, shown on hover, focus or tap with its height reserved
   (MessageActions); times on hover only (the action row, the face's tooltip). Settled tool
   calls are not in the stream: a call at work is one shimmering line, its pictures and
   music are their blocks, and the reply's "..." has Activity. Wait words are the typing
   dots with the words as their tooltip; where an answer did not come, "Paused." with
   Continue or "The reply failed." with Retry (Status TurnEnd). The agent's thinking follows
   Appearance: hidden with Hide thinking (moved to Thoughts in "..."), or shown above
   its reply as a fold ("The agent's thoughts") whose starting state follows Collapse thinking by
   default. While it thinks and has no words yet, one shimmering "Thinking" line. The agent's
   settled tool calls sit under its words as their rows (hidden with Hide tool calls,
   leaving Activity in "..."). Bastet's banners sit after the reply whose call she
   decided on (GuardBanner), and so does a request of hers that waits for you
   (ApprovalAsk). Never hidden: Retry, Continue, Stop, reminders, intercom lines,
   Bastet's banners and her requests. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  conversationApi, type Attachment, type Compaction, type ImageNotice, type Live, type Message, type Queued, type Switched,
  type ToolCall, type Turn,
} from '../lib/api'
import { bytes, errorText, fullDate, stamp } from '../lib/format'
import { fileUrl } from '../lib/media'
import { format } from '../lib/nav'
import { getPref, setPref } from '../lib/prefs'
import { Bell, Brain, Download, FileText, ImageIcon, Mail, Megaphone, Repeat, Shrink, type LucideIcon } from 'lucide-react'
import { Button, EventLine, Fold, ICON, InlineError, Message as Said, Picture, TextArea, Thoughts, cx, curve, ms, reduced, isSendKey } from '../ui'
import { Markdown, StreamText, TextWithRefs } from './Markdown'
import { useMessageActions } from './MessageActions'
import { Portrait } from './Portrait'
import { Status, TurnEnd, endWords } from './Status'
import { capital, usePronouns } from '../lib/name'
import { useHer } from '../lib/her'
import { useAppearance } from '../lib/appearance'
import { titleOf } from '../lib/pictures'
import type { Forming } from '../lib/useConversation'
import { ArtifactCards, FileLine, FormingRow, ShareCards, ToolLines, artifactsOf, sharesOf } from './ToolRow'
import { GuardBanner } from './GuardBanner'
import { ApprovalAsk } from './ApprovalAsk'
import { useFrozen, type GuardBanner as Banner } from '../lib/guard'
import type { ChatApproval } from '../lib/settingsApi'
import './Transcript.css'

/** when it was written: the local time, the full date on hover */
function Stamp({ at }: { at: string }) {
  const t = Date.parse(at)
  if (Number.isNaN(t)) return null
  return <span title={fullDate(t)}>{stamp(t)}</span>
}
const fullOf = (at: string) => { const t = Date.parse(at); return Number.isNaN(t) ? undefined : fullDate(t) }

/* A message that reached the conversation other than as you typed it: one quiet line,
   its text behind the line. `Reminder` (one it set; one you set reads `Note to Celine`), `Brief from
   Sam` (in a sub-agent's conversation `Brief from Celine`), `Brief changed`, `Time
   limit`, a mission's note `Mission RSS feed needs you` (a link to the mission's
   conversation), `From scout`, `Report from scout` (`Report from scout, stopped` when
   its turn did not end done), `Announcement to everyone`, `Note to her` (one you
   scheduled). Null for your own message, a trigger's prompt and a job's result (the
   result shows in its tool row). */
const REPORT_STATE: Record<string, string> = {
  aborted: 'stopped', failed: 'failed', interrupted: 'interrupted', capped: 'stopped at its limit',
}

function eventOf(m: Message, user: string, assistant: string, her: string): { heading: ReactNode; body: string; icon?: LucideIcon } | null {
  const ic = m.intercom
  if (!ic || ic.kind === 'job' || ic.kind === 'trigger') return null
  const you = user || 'you'
  const sender = ic.from === 'user' ? you : ic.from.startsWith('agent:') ? (ic.from_name ?? assistant) : (ic.from_name ?? 'the app')
  if (ic.to === 'all') return { heading: ic.from === 'user' ? 'Announcement to everyone' : `Announcement from ${sender}`, body: m.content, icon: Megaphone }
  if (ic.kind === 'message' && ic.from === 'user' && !ic.due_at) return null
  if (ic.due_at) return { heading: ic.from === 'user' ? `Note to ${her}` : 'Reminder', body: m.content, icon: Bell }
  if (ic.kind === 'brief') {
    return { heading: ic.data.changed ? 'Instructions changed' : `Instructions from ${ic.from === 'user' ? you : assistant}`, body: m.content }
  }
  if (ic.kind === 'notice') return { heading: ic.data.time_limit ? 'Time limit' : `Notice from ${sender}`, body: m.content }
  if (ic.kind === 'report' && ic.data.mission) {
    const [first = '', ...rest] = m.content.trimEnd().split('\n')
    const said = /^(Mission .+?) (needs you|done): ?(.*)$/.exec(first)
    const state = said ? (said[2] === 'needs you' ? 'waiting for you' : 'done') : ''
    const title = said ? `${said[1]} ${state}` : first
    const body = [said ? said[3]! : '', ...rest].join('\n').trim()
    return { heading: <a href={format({ section: 'missions', conversation: ic.data.mission })}>{title}</a>, body }
  }
  if (ic.kind === 'report') {
    // a sub-agent's report says how its last turn ended when that was not done
    const state = typeof ic.data.state === 'string' && ic.data.state !== 'done' ? REPORT_STATE[ic.data.state] ?? ic.data.state : null
    return { heading: `Result from ${sender}${state ? `, ${state}` : ''}`, body: m.content }
  }
  return { heading: `From ${sender}`, body: m.content }
}

/* a message of yours or an event line; a reply: its messages in a row (each step of a turn
   is one), shown as one block with its face; the replies of a quiet turn, shown as what of
   their calls is at work */
type Segment =
  | { kind: 'msg'; m: Message; index: number }
  | { kind: 'reply'; msgs: Message[]; index: number }
  | { kind: 'quiet'; msgs: Message[]; index: number }

/** your message, shown the moment you send it, until the server's copy arrives */
export type Pending = { text: string; files: string[] }

type Props = {
  conversationId: string | null
  messages: Message[]
  toolCalls: Record<string, ToolCall[]>
  turn: Turn | null
  running: boolean
  error: { message: string; retryable: boolean } | null
  pending: Pending | null
  /** where older messages were summarised for Celine */
  compactions: Compaction[]
  /** its answer waits for the model: what for, in words (Status waitWords); the dots'
      tooltip where the reply lands */
  waiting: string | null
  /** the reply being written and its phase */
  live: Live | null
  /** the live turns in the order they run: the ones after the first show "Queued" */
  queue?: Queued[]
  /** replies another model wrote because the chosen one failed */
  switched: Record<string, Switched>
  /** the lines after a switch to a model that can't see images */
  imageNotices: ImageNotice[]
  /** Bastet's banners, after the messages that made their calls */
  banners?: Banner[]
  /** her requests that wait for you, after the messages that made their calls */
  approvals?: ChatApproval[]
  /** tool calls it is still writing, shown under their message */
  forming?: Forming[]
  /** what a slash command said, last in the transcript */
  note?: ReactNode
  /** the assistant's name, for the brief it gave a sub-agent */
  assistant: string
  /** your name, for the brief you gave it ('' until the status is in) */
  user: string
  /** answer the last message again (Retry) */
  onRetry: () => Promise<void>
  /** read-only (a mission in the Trash): nothing here writes to it */
  closed?: boolean
}

/** what every line of one transcript shares */
type T = {
  cid: string; running: boolean; turn: Turn | null; closed: boolean; lastId?: string
  editing: string | null; setEditing: (id: string | null) => void
}

export function Transcript(
  { conversationId, messages, toolCalls, turn, running, error, pending, compactions, waiting, live, queue = [], switched, imageNotices, banners = [],
    approvals = [], forming = [], note, assistant, user, onRetry, closed = false }: Props,
) {
  const streaming = running && turn?.state === 'streaming'
  const frozen = useFrozen()
  // a background job's result shows in the call that started the job, not again here
  const visible = messages.filter(m => m.role !== 'tool' && m.intercom?.kind !== 'job')
  const last = visible.at(-1)
  const [editing, setEditing] = useState<string | null>(null)
  const t: T = { cid: conversationId ?? '', running, turn, closed, lastId: last?.id, editing, setEditing }

  // The agent's replies that arrive while you watch rise in once (Web Animations, so a return to
  // this conversation or this section never replays them). A conversation that loads
  // shows at once: every id on screen when it first appears counts as seen.
  const seen = useRef<{ cid: string | null; ids: Set<string> }>({ cid: conversationId, ids: new Set(visible.map(m => m.id)) })
  if (seen.current.cid !== conversationId || (seen.current.ids.size === 0 && visible.length > 1)) {
    seen.current = { cid: conversationId, ids: new Set(visible.map(m => m.id)) }
  }
  const arrive = (m: Message) => (el: HTMLDivElement | null) => {
    if (!el || seen.current.ids.has(m.id)) return
    seen.current.ids.add(m.id)
    // your own message rose already, as the one shown at once (pending)
    if (m.role !== 'user' || m.author !== 'user') riseIn(el)
  }

  const segments: Segment[] = []
  visible.forEach((m, index) => {
    const prev = segments.at(-1)
    if (m.role !== 'assistant') { segments.push({ kind: 'msg', m, index }); return }
    const kind = m.meta.quiet ? 'quiet' : 'reply'
    if (prev?.kind === kind) { prev.msgs.push(m); prev.index = index }
    else segments.push({ kind, msgs: [m], index })
  })

  const words = usePronouns()
  const { hide_thinking, hide_tools, collapse_thinking } = useAppearance()
  const her = useHer()
  // by its name, as chosen ("What Celine saw")
  const sawTitle = `What ${her?.name || words.subject} saw`
  const thoughtsTitle = `${capital(words.possessive)} thoughts`
  // the message being written now: its row keeps its place, empty
  const liveId = running ? live?.message_id ?? (streaming ? last?.id : undefined) : undefined
  const thoughtsOf = (msgs: Message[]) => {
    const text = msgs.map(m => m.reasoning).filter(Boolean).join('\n\n')
    return text ? { title: thoughtsTitle, text } : null
  }
  const replyEdit = (m: Message) => editing === m.id && !!conversationId

  // the calls it is still writing under a message
  const formingOf = (id: string) => forming.filter(f => f.message_id === id)
    .map(f => <FormingRow key={f.index} name={f.name} chars={f.chars} preview={f.preview} words={f.words} />)

  // its face on a reply; the time on hover
  const faceOf = (speaking: boolean, at?: string) => (
    <span className="msg__face" title={at ? `${assistant}, ${fullOf(at)}` : undefined}><Portrait size="sm" speaking={speaking} /></span>
  )
  function renderSegment(seg: Segment): ReactNode {
    if (seg.kind === 'quiet') {
      // a quiet turn: its text stays on the server; what of its calls is at work, and its
      // pictures, show where its reply would be
      const calls = seg.msgs.flatMap(m => toolCalls[m.id] ?? [])
      const writing = seg.msgs.flatMap(m => formingOf(m.id))
      if (!calls.length && !writing.length) return null
      return <div key={seg.msgs[0]!.id} ref={arrive(seg.msgs[0]!)} className="msg-quiet">{writing}<ToolLines calls={calls} /></div>
    }
    if (seg.kind === 'reply') return renderReply(seg)
    const m = seg.m

    // a message from someone other than you as you typed: a reminder, a brief, a note
    const ev = m.role === 'user' ? eventOf(m, user, assistant, words.object) : null
    if (ev) {
      return (
        <div key={m.id} ref={arrive(m)} className="msg-event">
          <EventLine quiet icon={ev.icon ?? Mail} meta={<Stamp at={m.created_at} />}
                     detail={ev.body && <TextWithRefs text={ev.body} />}>{ev.heading}</EventLine>
        </div>
      )
    }
    const edit = editing === m.id && !!conversationId
    const before = (m.attachments ?? []).length > 0 && <Attached list={m.attachments!} />
    return (
      <div key={m.id} ref={arrive(m)} className="msg-yours">
        <Line t={t} m={m} mine sawTitle="" before={before || undefined} time={<Stamp at={m.created_at} />}>
          {edit ? <Editor conversationId={conversationId!} message={m} onClose={() => setEditing(null)} />
            : <div className="msg__text"><Markdown text={m.content} breaks /></div>}
        </Line>
        <Described attachments={m.attachments ?? []} />
      </div>
    )
  }

  function renderReply(seg: Extract<Segment, { kind: 'reply' }>) {
    const lastM = seg.msgs.at(-1)!
    const writingHere = seg.msgs.some(m => m.id === liveId)
    const calls = seg.msgs.flatMap(m => toolCalls[m.id] ?? [])
    // a step with words, calls or a call on its way draws; a step with none draws nothing
    const steps = seg.msgs.filter(m => m.content.trim() || switched[m.id] || m.id === liveId || forming.some(f => f.message_id === m.id)
                                       || (toolCalls[m.id] ?? []).length)
    // a reply stopped before it said anything: nothing to draw
    if (!steps.length) return null
    const reasoning = seg.msgs.map(m => m.reasoning).filter(Boolean).join('\n\n')
    // its thinking: the fold above its reply, open by the Appearance setting. While it
    // thinks with no words yet: one shimmering "Thinking".
    const thinkingNow = writingHere && live?.phase === 'thinking' && !lastM.content
    const at = seg.msgs[0]!.created_at
    const edit = replyEdit(lastM)
    return (
      <div key={seg.msgs[0]!.id} ref={arrive(seg.msgs[0]!)} className="msg-hers">
        <Line t={t} m={lastM} steps={seg.msgs} calls={calls} sawTitle={sawTitle}
              writing={writingHere} thoughts={hide_thinking ? thoughtsOf(seg.msgs) : null}
              face={faceOf(writingHere, at)} time={<Stamp at={at} />}>
          {edit ? <Editor conversationId={conversationId!} message={lastM} onClose={() => setEditing(null)} /> : (
            <div className="reply">
              {!hide_thinking && reasoning && (
                <Fold key={String(collapse_thinking)} title={thoughtsTitle} icon={Brain} defaultOpen={!collapse_thinking}
                      remember={`thoughts:${seg.msgs[0]!.id}`}><Thoughts>{reasoning}</Thoughts></Fold>
              )}
              {thinkingNow && <EventLine quiet running>Thinking</EventLine>}
              {steps.map(renderStep)}
              <ArtifactCards cards={artifactsOf(calls)} />
              <ShareCards cards={sharesOf(calls)} />
              <FileCards list={seg.msgs.flatMap(m => (m.attachments ?? []).filter(a => !a.batch_id && a.kind !== 'image' && a.kind !== 'audio'))} />
            </div>
          )}
        </Line>
      </div>
    )

    // one step of its reply: what it says (at a steady pace), or the dots where it will
    // land; then the calls on their way and at work, its pictures
    function renderStep(m: Message) {
      const swap = switched[m.id]
      const isLive = m.id === liveId
      return (
        <div key={m.id} className="step">
          {swap && <EventLine quiet icon={Repeat} detail={swap.why || undefined}>Answered by {swap.model} instead of {swap.instead_of}</EventLine>}
          {m.content ? <div className="msg__prose"><StreamText text={m.content} live={isLive && live?.phase !== 'done'} /></div>
            : isLive && !thinkingNow && <Status words={waiting} />}
          {formingOf(m.id)}
          <ToolLines calls={toolCalls[m.id] ?? []} inline={!hide_tools} />
          {/* the pictures and sounds it sent you, where it sent them; files are cards under the
              reply (FileCards); what a batch made is in its gallery */}
          <Attached list={(m.attachments ?? []).filter(a => !a.batch_id && (a.kind === 'image' || a.kind === 'audio'))} />
        </div>
      )
    }
  }

  // a divider after the last message each summary covers
  // (a summary that ends on a tool result sits after the step that called the tool)
  const shownAt = new Map<string, string>()
  let lastShown: string | null = null
  for (const m of messages) {
    if (m.role !== 'tool') lastShown = m.id
    if (lastShown) shownAt.set(m.id, lastShown)
  }
  const summaryAfter = new Map<string, Compaction[]>()
  for (const c of compactions) {
    const at = shownAt.get(c.through_message_id)
    if (at) summaryAfter.set(at, [...(summaryAfter.get(at) ?? []), c])
  }
  const divider = (ids: string[]) => ids.flatMap(id => summaryAfter.get(id) ?? []).map(c => (
    <div key={c.id} className="msg-event">
      <EventLine quiet icon={Shrink} detail={<p className="compaction__text">{c.text}</p>} detailTitle="Compacted">Compacted</EventLine>
    </div>
  ))
  const noticesAfter = (ids: string[]) => imageNotices.filter(n => ids.includes(n.after_message_id))
    .map((n, k) => <div key={`images-${n.after_message_id}-${k}`} className="msg-event"><EventLine quiet icon={ImageIcon}>{imageNoticeText(n)}</EventLine></div>)

  const bannersAfter = (ids: string[]) => banners.filter(b => b.message_id && ids.includes(b.message_id))
    .map(b => <GuardBanner key={`guard-${b.id}`} banner={b} user={user} />)
  const asksAfter = (ids: string[]) => approvals.filter(a => a.message_id && ids.includes(a.message_id))
    .map(a => <ApprovalAsk key={`ask-${a.id}`} ask={a} />)

  // Where the next reply lands before it exists (your message sent, a turn queued or
  // getting ready): its face and the dots.
  const shownLive = live && visible.some(m => m.id === live.message_id && !m.meta.quiet)
  const ghost = !!pending || (running && !shownLive)
  const thinkingFirst = running && live?.phase === 'thinking'
  // frozen (the server's): its queued turn waits, it is not writing
  const held = !!frozen && turn?.state === 'queued'
  const ghostRow = (queued: boolean, key: string) => (
    <div key={key} className="msg-hers">
      <Said face={<Portrait size="sm" speaking={!queued} away={queued} />}>
        {queued ? <Status words={null} queued /> : thinkingFirst ? <EventLine quiet running>Thinking</EventLine> : <Status words={waiting} />}
      </Said>
    </div>
  )
  // the turns waiting after the one running: "Queued", one line each
  const later = queue.filter(q => q.turn_id !== turn?.id)
  // a message that lands quietly (a notice with wake off) waits for no answer
  const unanswered = last?.role === 'user' && last.intercom?.wake !== false
  // what the live region says: what the screen shows
  const liveWords = held ? 'Queued' : pending || running ? (waiting ?? `${assistant} is writing.`) : endWords(turn, error, unanswered && !closed) ?? ''

  return (
    <div className="transcript__inner is-transcript">
      {segments.map(seg => {
        const ids = seg.kind === 'msg' ? [seg.m.id] : seg.msgs.map(m => m.id)
        return [renderSegment(seg), ...divider(ids), ...noticesAfter(ids), ...bannersAfter(ids), ...asksAfter(ids)]
      })}

      {pending && (
        <div className="msg-yours" ref={riseIn}>
          <Said mine before={pending.files.length > 0 ? pending.files.map(f => <FileLine key={f} name={f} />) : undefined}>
            <div className="msg__text"><Markdown text={pending.text} breaks /></div>
          </Said>
        </div>
      )}

      {ghost && ghostRow(held, 'next')}
      {running && later.map(q => ghostRow(true, `q-${q.turn_id}`))}

      <div className="sr-only" aria-live="polite">{liveWords}</div>

      {!pending && conversationId && !closed && (
        <TurnEnd conversationId={conversationId} turn={turn} running={running} error={error}
                 unanswered={unanswered} onRetry={onRetry} />
      )}
      {note}
    </div>
  )
}

/** one message, the kit's Message with its actions (MessageActions): the versions, the
    row's buttons, the menu and what they open */
function Line({ t, m, steps, calls, writing = false, thoughts = null, sawTitle, children, ...kit }: {
  t: T; m: Message; steps?: Message[]; calls?: ToolCall[]; writing?: boolean
  thoughts?: { title: string; text: string } | null
  sawTitle: string
  children?: ReactNode
} & Omit<React.ComponentProps<typeof Said>, 'versions' | 'actions' | 'menu' | 'children'>) {
  const editing = t.editing === m.id
  const a = useMessageActions({
    conversationId: t.cid, message: m, steps, calls, running: t.running, last: m.id === t.lastId, writing, turn: t.turn,
    onEdit: () => t.setEditing(m.id), sawTitle, closed: t.closed, thoughts,
  })
  return (
    <>
      <Said {...kit} versions={editing ? undefined : a.versions} actions={editing ? undefined : a.actions}
            menu={editing ? undefined : a.menu} time={editing || writing ? undefined : kit.time}
            className={cx(editing && 'is-editing', kit.className)}>
        {children}
      </Said>
      {a.overlays}
    </>
  )
}

/* The files on a message: pictures, tracks, and other files to download */
function Attached({ list }: { list: Attachment[] }) {
  if (!list.length) return null
  return <>
    {list.filter(a => a.kind === 'image').map(a => <YourPicture key={a.id} a={a} />)}
    {list.filter(a => a.kind === 'audio').map(a => (
      <div key={a.id} className="msg__audio">
        <span className="msg__audio-title">{a.filename}</span>
        <audio controls src={fileUrl(a)} preload="metadata" />
      </div>
    ))}
    {list.filter(a => a.kind !== 'image' && a.kind !== 'audio').map(a => <FileLine key={a.id} name={a.filename} a={a} />)}
  </>
}

/* A picture on a message: its shape before it loads; a click shows it big, titled by what it
   shows (its description) */
function YourPicture({ a }: { a: Attachment }) {
  return <Picture className="msg__picture" src={fileUrl(a)} name={titleOf(a.description)}
                  ratio={a.width && a.height ? `${a.width} / ${a.height}` : '4 / 3'} />
}

/* Under your message, when the model could not see its images: one muted line, the
   description it got instead behind it. */
function Described({ attachments }: { attachments: Attachment[] }) {
  const described = attachments.filter(a => a.kind === 'image' && a.description)
  if (!described.length) return null
  return (
    <EventLine quiet icon={ImageIcon} className="image-note"
               detail={described.map(a => <p key={a.id}>{described.length > 1 && <b>{a.filename}: </b>}{a.description}</p>)}>
      {described.length === 1 ? 'Image described in words' : `${described.length} images described in words`}
    </EventLine>
  )
}

/* the files it sent you in this reply, each a card to download, under the reply */
function FileCards({ list }: { list: Attachment[] }) {
  if (!list.length) return null
  return (
    <div className="artcards">
      {list.map(a => (
        <a key={a.id} className="artcard" href={fileUrl(a, true)} download title={`Download ${a.filename}`}>
          <FileText size={ICON.lg} className="artcard__icon" aria-hidden />
          <span className="artcard__main">
            <span className="artcard__title">{a.filename}</span>
            <span className="artcard__sub">{bytes(a.bytes)}</span>
          </span>
          <Download size={ICON.md} aria-hidden />
        </a>
      ))}
    </div>
  )
}

function imageNoticeText(n: ImageNotice): string {
  return n.describer ? `${n.model} gets the earlier images described in words` : `${n.model} can't see the earlier images`
}


/* Rise in once (--dur, --ease, --rise); nothing travels with reduced motion. */
function riseIn(el: HTMLElement | null) {
  if (!el || reduced()) return
  const rise = getComputedStyle(document.documentElement).getPropertyValue('--rise').trim()
  el.animate([{ opacity: 0, transform: `translateY(${rise})` }, { opacity: 1, transform: 'none' }],
             { duration: ms('--dur'), easing: curve('--ease') })
}

/* Your message or its reply, edited in place; saved, it is a new version (yours is
   answered again; its own is not). */
function Editor(
  { conversationId, message, onClose }:
  { conversationId: string; message: Message; onClose: () => void },
) {
  const key = `edit:${message.id}`
  const [text, setText] = useState(() => getPref(key) ?? message.content)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    const t = setTimeout(() => setPref(key, text !== message.content ? text : null), 200)
    return () => clearTimeout(t)
  }, [text, key, message.content])

  const save = async () => {
    if (!text.trim() || text === message.content) { onClose(); return }
    setSaving(true); setError(null)
    try {
      await conversationApi.edit(conversationId, message.id, text)
    } catch (e) { setError(errorText(e)); setSaving(false); return }
    setPref(key, null)
    onClose()
  }

  return (
    <div className="msg__edit">
      <TextArea rows={2} max={13} autoFocus value={text} aria-label="Edit the message"
                onFocus={e => e.currentTarget.setSelectionRange(text.length, text.length)}
                onChange={e => setText(e.target.value)}
                onKeyDown={e => {
                  if (isSendKey(e)) { e.preventDefault(); void save() }
                  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onClose() }
                }} />
      <div className="msg__edit-row">
        {error && <InlineError message={error} compact />}
        <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
        <Button size="sm" variant="primary" loading={saving} onClick={() => void save()}>{message.role === 'user' ? 'Save and resend' : 'Save'}</Button>
      </div>
    </div>
  )
}
