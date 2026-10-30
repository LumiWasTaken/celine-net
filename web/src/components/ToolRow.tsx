/* What Celine did with a tool. In the
   stream only what is at work shows, as one shimmering line (ToolLines), with the pictures
   and music it makes as their blocks; everything else is in its reply's Activity: a list with one row per call (ToolGroup), the icon, the words, a
   bare duration or count, the row's colour its state; a click opens everything about the
   call in a Sheet, the call by its name, the arguments, where it looked and the complete
   result, nothing cut. A shell command names its terminal (Open terminal), a browser step
   its tab (Open Browser; opened, the shots the vision model saw), a message it sent can
   be cancelled while it waits. A page it wrote is a card under its reply (ArtifactCards),
   one per page whatever the number of versions it wrote in that reply; so is a share of
   its box (ShareCards), with the server's dot. */
import { useContext, useState, type ReactNode } from 'react'
import { TextWithRefs } from './Markdown'
import {
  AlertTriangle, AppWindow, Bell, BookOpen, Bot, CircleSlash, Download, ExternalLink, FileText, Flag, Globe, ImagePlus, LockKeyhole, Mail, MessagesSquare,
  Music, NotebookPen, PanelRight, Paperclip, ScreenShare, Search, Square, Terminal, UserRound, Wrench, type LucideIcon,
} from 'lucide-react'
import { api, type ToolCall } from '../lib/api'
import { AgentsContext, agentsApi } from '../lib/agents'
import { useServerEvents } from '../lib/bus'
import { artifactOf, kindWord, ShowArtifact, type ArtifactCardData } from '../lib/artifacts'
import { SHARE_DOT, shareEvent, shareOf, sharesApi, type ShareCardData } from '../lib/shares'
import { useResource } from '../lib/useResource'
import { bytes, count, duration, errorText, stamp } from '../lib/format'
import { titleOf } from '../lib/pictures'
import { TILE, asBatch, fileUrl, tileUrl, type MediaFile } from '../lib/media'
import { format, refHref } from '../lib/nav'
import { intercomApi } from '../lib/intercom'
import { ConversationReminders } from '../lib/reminders'
import { useAction } from '../lib/useAction'
import { usePronouns } from '../lib/name'
import { useNow } from '../lib/useNow'
import { Button, Dot, EventLine, ICON, IconButton, InlineError, ListRow, Picture, Sheet, cx, sourceLabel } from '../ui'
import { BatchGallery, GalleryContext, StopBatch, doneWords, type Made } from './Batch'
import './ToolRow.css'


const ICONS: Record<string, typeof Wrench> = {
  chat_archive: MessagesSquare, user_documents: BookOpen, notebook: NotebookPen, shell: Terminal,
  web: Globe, past_conversations: Search, sub_agents: Bot, image_generator: ImagePlus,
  music_generator: Music, artifacts: AppWindow, email: Mail, browser: Globe, appearance: UserRound, me: UserRound,
  vault: LockKeyhole,
}

type Args = Record<string, unknown>

function argsOf(call: ToolCall): Args | null {
  try {
    const v = JSON.parse(call.arguments || '{}')
    return v && typeof v === 'object' && !Array.isArray(v) ? v as Args : null
  } catch { return null }
}

const str = (v: unknown) => (typeof v === 'string' && v.trim() ? v.trim() : null)
const list = (v: unknown) => (Array.isArray(v) ? v.map(String).filter(Boolean) : [])
const firstLine = (s: string, max = 80) => {
  const l = s.split('\n').find(x => x.trim())?.trim() ?? ''
  return l.length > max ? `${l.slice(0, max - 1)}…` : l
}

type ScopeChat = { ref: string; name?: string; kind?: string; platform?: string; missing?: boolean }
/** the chats a search was kept to (`display.scope_chats`) */
const scopeChats = (d: Record<string, unknown> | null): ScopeChat[] => (Array.isArray(d?.scope_chats) ? d.scope_chats as ScopeChat[] : [])
/* a call that did not run: the turn's time was up (not a failure), or gated (its
   skill down, not loaded or off) */
const notRun = (call: ToolCall) => call.state === 'gated' || (call.state === 'error' &&
  call.display?.time_up === true)

/** the row's words, by its state, as the server says them (skills/words.py) */
const rowWords = (call: ToolCall): string => (busy(call) ? call.words.running : call.words.settled)

/* a sub-agent's turn, as the colour of its row: at work, failed, or ended otherwise */
const AGENT_TONE: Record<string, Tone> = {
  queued: 'running', preparing: 'running', streaming: 'running', tools: 'running',
  done: null, aborted: 'muted', failed: 'error', interrupted: 'muted', capped: 'muted',
}

/** the sub-agent's own conversation, once it has one */
function agentConversation(call: ToolCall): string | null {
  const id = call.display?.conversation_id
  return typeof id === 'string' ? id : null
}

/** where the sub-agent's conversation opens, the server's (homes.py) */
function agentLink(call: ToolCall): string | null {
  const link = call.display?.link
  return typeof link === 'string' ? link : null
}

/** the latest state of a sub-agent's turn, from `turn.state` on the feed; null
 *  until one arrives after the row is shown */
function useAgentState(conversation: string | null): string | null {
  const [state, setState] = useState<string | null>(null)
  useServerEvents(ev => {
    if (conversation && ev.type === 'turn.state' && ev.conversation === conversation && typeof ev.state === 'string') setState(ev.state)
  })
  return state
}

function asImage(v: unknown): Made | null {
  const img = v as Made | undefined
  return img && typeof img.sha256 === 'string' ? img : null
}

/** a job's state as a word in a sentence */
export const JOB_STATE_WORD: Record<string, string> = {
  running: 'running', done: 'done', failed: 'failed', cancelled: 'stopped', timeout: 'timed out',
  interrupted: 'interrupted',
}

/** how long a call has been at work (ticking) or took */
export function Elapsed({ since, until }: { since: string; until?: string | null }) {
  const now = useNow(!until)
  const start = Date.parse(since)
  if (Number.isNaN(start)) return null
  const end = until ? Date.parse(until) : now
  return <>{duration(end - start)}</>
}

/* ── one call as one row of a list (Activity, a run's calls): the icon, the words, a bare
   duration or count; a call at work has the running dot, a failed one says "Could not", one
   not run or off is muted. A click opens everything about it in a Sheet: the call by its
   name, the arguments, where it looked and the complete result, nothing cut. Its own
   buttons (Stop, Open, Cancel) sit at its end. Nothing folds. ── */
type Tone = 'running' | 'error' | 'muted' | null

const busy = (call: ToolCall) => call.state === 'pending' || call.state === 'running' || call.state === 'detached'

function toneOf(call: ToolCall): Tone {
  if (busy(call)) return 'running'
  if (call.state === 'error') return 'error'
  return call.state === 'ok' ? null : 'muted'
}

/** the bare time a call took, or ticking while it works */
function took(call: ToolCall): ReactNode {
  if (!call.started_at) return null
  if (busy(call)) return <Elapsed since={call.started_at} />
  const ms = call.finished_at ? Date.parse(call.finished_at) - Date.parse(call.started_at) : NaN
  // every settled row has its time; under a second reads as one
  return Number.isNaN(ms) ? null : duration(Math.max(ms, 1000))
}

/** next to the sentence only what the sentence does not say: an ambiguous name, a
   failed exit. Counts and sizes stay in the opened call. */
function outcome(call: ToolCall): string | null {
  const d = call.display
  if (!d) return null
  if (d.ambiguous === true) return 'the name matches more than one'
  if (typeof d.exit === 'number' && d.exit !== 0) return `exit ${d.exit}`
  return null
}

/** what a row says and does: its own words, colour, buttons and what its Sheet shows */
type View = {
  icon: LucideIcon; words: string; tone: Tone; meta?: ReactNode
  /** buttons at its end (Stop, Open, Cancel) */
  actions?: ReactNode
  /** under it, always: a sub-agent's report, files from its shellbox, an error */
  under?: ReactNode
  /** what its Sheet shows besides the call */
  detail?: ReactNode
}

const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** a message it sent (intercom__send): `Message to user`, or with a time `Reminder 17:21`
    (to itself) and `Message to user 17:21`, and its text */
function reminderWords(call: ToolCall) {
  const d = call.display
  const a = argsOf(call)
  const due = str(d?.due_at)
  const time = due ? stamp(due) : ''
  const to = str(d?.to) ?? str(a?.to)
  const label = to === 'me' ? `Reminder ${time}`.trim()
    : `Message to ${to ?? ''} ${time}`.trim()
  return { label, time, text: str(d?.text) ?? str(a?.text) ?? '' }
}

const plainWords = (s: string) => s.replace(/\[\[|\]\]/g, '')

/** one call's row: by its tool, the words and buttons it needs */
function useView(call: ToolCall): View {
  const [skill = '', tool] = call.name.split('__')
  const a = argsOf(call)
  const d = call.display
  const reminders = useContext(ConversationReminders)
  const cancel = useAction((id: number) => intercomApi.cancel(id))
  // a sub-agent: its turn's state, as it changes; its step and report from agent.state
  const conversation = skill === 'sub_agents' ? agentConversation(call) : null
  const link = skill === 'sub_agents' ? agentLink(call) : null
  const agentState = useAgentState(conversation)
  const agent = useContext(AgentsContext).find(x => x.id === conversation)
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState<string | null>(null)

  if (call.name.startsWith('shell__')) {
    const name = str(d?.terminal) ?? str(a?.terminal)
    const id = str(d?.terminal_id)
    const ran = (call.name === 'shell__run' || call.name === 'shell__wait') && call.state === 'ok'
    const exit = ran ? num(d?.exit) : null
    const seconds = ran ? num(d?.seconds) : null
    const log = str(d?.log)
    return {
      icon: Terminal, tone: exit ? 'error' : toneOf(call), words: rowWords(call),
      meta: exit ? `exit ${exit}` : seconds !== null ? duration(seconds * 1000) : took(call),
      under: <Files call={call} />,
      detail: <>
        {log && <Part label="Log"><code className="tool__log">{log}</code></Part>}
        {id && <span><Button size="sm" icon={ExternalLink} href={format({ section: 'terminals', terminal: id })}
                             target="_blank" title={`Open terminal ${name} in a new tab`}>Open terminal</Button></span>}
      </>,
    }
  }
  if (call.name.startsWith('browser__')) {
    const tab = d?.restart ? null : str(d?.tab) ?? str(a?.tab)
    return {
      icon: Globe, tone: toneOf(call), meta: took(call), words: rowWords(call),
      detail: <>
        <BrowserShots display={d} />
        {tab && <span><Button size="sm" icon={ExternalLink} href={format({ section: 'browser' })} target="_blank"
                              title="Open the browser in a new tab">Open Browser</Button></span>}
      </>,
    }
  }
  if (call.name === 'intercom__send') {
    const id = typeof d?.id === 'number' ? d.id : null
    const { label, time, text } = reminderWords(call)
    const state = id !== null ? reminders.list?.find(r => r.id === id)?.state : undefined
    const waiting = state === 'pending'
    return {
      icon: Bell, words: `${label} ${text}`.trim(), meta: null,
      tone: call.state !== 'ok' ? toneOf(call) : waiting ? 'running' : state === 'cancelled' ? 'muted' : null,
      actions: waiting && (
        <Button size="sm" variant="ghost" loading={cancel.busy} aria-label={`Cancel the message ${time}`}
                onClick={() => void cancel.run(id!).then(reminders.reload)}>Cancel</Button>
      ),
      under: cancel.error && <InlineError message={cancel.error} compact />,
    }
  }
  if (call.name === 'intercom__cancel') {
    return { icon: Bell, tone: toneOf(call), words: rowWords(call) }
  }
  if (call.name === 'missions__update') {
    return { icon: Flag, tone: toneOf(call), meta: took(call), words: rowWords(call) }
  }
  if ((skill === 'image_generator' || skill === 'music_generator') && (asBatch(d) || tool === 'generate' || tool === 'read' || tool === 'stop')) {
    const b = asBatch(d)
    const failed = b && b.state !== 'running' && b.items.every(i => i.state !== 'done')
    return {
      icon: skill === 'music_generator' ? Music : ImagePlus, words: rowWords(call),
      tone: !b ? toneOf(call) : b.state === 'running' ? 'running' : failed ? 'error' : null,
      meta: b ? doneWords(b) : took(call), actions: b && <StopBatch batch={b} />,
      detail: b && <Part label="Batch"><BatchGallery batch={b} /></Part>,
    }
  }
  // every other call: what it did, a sub-agent's state, a job's Stop
  const refused = notRun(call)
  const working = call.name === 'sub_agents__start' && !!agent?.working
  const report = call.name === 'sub_agents__start' && !working ? firstLine(agent?.report?.text ?? '', 160) : ''
  // a step handed to a job carries the job id until the job settles it; Stop ends the job
  const jobId = call.state !== 'detached' ? null : typeof d?.job_id === 'string' ? d.job_id : call.job_id ?? null
  const stop = async () => {
    if (!jobId || stopping) return
    setStopping(true); setStopError(null)
    try {
      const r = await api.cancelJob(jobId)
      if (!r.cancelled) setStopping(false)      // already ending: its tool.done settles the row
    } catch (e) { setStopping(false); setStopError(errorText(e)) }
  }
  return {
    icon: refused ? CircleSlash : call.state === 'error' ? AlertTriangle : ICONS[skill] ?? Wrench,
    words: rowWords(call),
    tone: working ? 'running' : agentState ? AGENT_TONE[agentState] ?? null : refused ? 'muted' : toneOf(call),
    meta: working ? agent!.step_words ?? 'Running' : outcome(call) ?? took(call),
    actions: (working || link || jobId) && <>
      {working && (
        <Button size="sm" variant="ghost" aria-label={`Stop the sub-agent ${agent!.name}`}
                onClick={() => void agentsApi.stop(agent!.id).catch(e => setStopError(errorText(e)))}>Stop</Button>
      )}
      {link && (
        <Button size="sm" variant="ghost" href={link}
                aria-label={`Open the conversation of ${String(d?.name ?? 'the sub-agent')}`}>Open</Button>
      )}
      {jobId && (
        <Button size="sm" variant="ghost" loading={stopping} onClick={() => void stop()}
                aria-label={`Stop the background job: ${call.words.running}`}>Stop</Button>
      )}
    </>,
    under: <>
      {report && <div className="tool__report">{report}</div>}
      <Files call={call} />
      {stopError && <InlineError message={stopError} compact />}
    </>,
  }
}

/* one call in a list: a click opens it whole */
function CallRow({ call }: { call: ToolCall }) {
  const v = useView(call)
  const [open, setOpen] = useState(false)
  const words = plainWords(v.words)
  return (
    <div className="tool">
      {/* a failed row keeps its icon (its sentence says "Could not"), so every row's text
          starts at one edge; only a call at work trades it for the running dot */}
      <ListRow density="sm" icon={v.tone === 'running' ? undefined : v.icon}
               dot={v.tone === 'running' ? 'running' : undefined}
               title={<TextWithRefs text={v.words} />} tooltip={words}
               meta={v.meta} muted={v.tone === 'muted'} onClick={() => setOpen(true)}
               trailing={v.actions} trailingAlways={!!v.actions} />
      {v.under}
      {open && (
        <Sheet title={words} size="md" onClose={() => setOpen(false)}>
          <div className="tool__body">
            <ToolDetail call={call} />
            {v.detail}
          </div>
        </Sheet>
      )}
    </div>
  )
}

/** Every call of a reply as a list (the Activity of a reply, a run's calls): one row each,
    in the order it made them; loading a skill's guide is plumbing and leaves the list
    when there is anything else. */
export function ToolGroup({ calls }: { calls: ToolCall[] }) {
  const shown = calls.some(c => !PLUMBING.has(c.name)) ? calls.filter(c => !PLUMBING.has(c.name)) : calls
  if (shown.length === 0) return null
  return <div className="tool-list" role="list" aria-label="Activity">{shown.map(c => <CallRow key={c.id} call={c} />)}</div>
}

/* loading, unloading, reading a guide: plumbing, named only when it is all there is */
const PLUMBING = new Set(['skills__load', 'skills__read_guide'])

/** In the stream, under its words: a call at work as one shimmering line (with its Stop or
    Open), a message it set for later while it waits (with Cancel), and the pictures and
    music it makes, each one block from start to end.
    `inline` (the chat): the calls that settled stay under its words as
    their rows, the same rows the Activity list has, each opening its result. */
export function ToolLines({ calls, inline = false }: { calls: ToolCall[]; inline?: boolean }) {
  // a settled batch or page has its block under its words already (the gallery, the card):
  // no row for it too. read and stop name a batch again to show it: they get their row.
  const rows = calls.filter(c => !busy(c) && !PLUMBING.has(c.name)
    && !(/__(read|stop)$/.test(c.name) ? null : asBatch(c.display) || artifactOf(c.display)))
  return <>
    {calls.map(c => <ToolLine key={c.id} call={c} />)}
    {inline && rows.length > 0 && (
      <div className="tool-list">{rows.map(c => <CallRow key={c.id} call={c} />)}</div>
    )}
  </>
}

function ToolLine({ call }: { call: ToolCall }) {
  const v = useView(call)
  // what a batch call made, whatever the call's name: its display is the batch (read and stop show it again)
  const b = /__(read|stop)$/.test(call.name) ? null : asBatch(call.display)
  if (b) return <div className="tool__batch"><BatchGallery batch={b} /></div>
  const reminder = call.name === 'intercom__send' && v.tone === 'running'
  if (!reminder && v.tone !== 'running') return null
  if (PLUMBING.has(call.name) && !reminder) return null
  return (
    <>
      <EventLine quiet running={!reminder} icon={v.icon}
                 meta={<span className="tool__meta">{v.meta}{v.actions}</span>}>
        <TextWithRefs text={v.words} />
      </EventLine>
      {v.under}
    </>
  )
}

type CallFile = MediaFile & { kind: string }

/* the files kept with a call alone, whatever the tool (the server's `files` block):
   pictures as tiles that open in the Viewer, other files to download. What shell__send
   sent is on its message. */
function Files({ call }: { call: ToolCall }) {
  const blocks = Array.isArray(call.display?.blocks) ? call.display.blocks as { type?: string; files?: CallFile[] }[] : []
  const files = blocks.find(b => b.type === 'files')?.files ?? []
  if (!files.length) return null
  return (
    <div className="tool__files">
      {files.filter(f => f.kind === 'image').map(f => (
        <Picture key={f.id} className="tool__picture" src={tileUrl(f, TILE)} full={fileUrl(f)} name={titleOf(null)}
                 ratio={f.width && f.height ? `${f.width} / ${f.height}` : '4 / 3'} />
      ))}
      {files.filter(f => f.kind !== 'image').map(f => <FileLine key={f.id} name={f.filename} a={f} />)}
    </div>
  )
}

/* a file to download: its name, size and Download on one line (only its name while
   your message is on its way) */
export function FileLine({ name, a }: { name: string; a?: { id: string; bytes: number } }) {
  return (
    <div className="msg__file">
      <Paperclip size={ICON.sm} aria-hidden />
      <span className="msg__file-name">{name}</span>
      {a && <span className="msg__file-size">{bytes(a.bytes)}</span>}
      {a && <IconButton icon={Download} label="Download" size="sm" href={fileUrl(a, true)} download />}
    </div>
  )
}

/* Everything about one call. */
function ToolDetail({ call }: { call: ToolCall }) {
  const args = argsOf(call)
  const d = call.display
  const where = call.words.where
  const chats = scopeChats(d)
  const kbs = new Set([...list(d?.scope_kbs), ...chats.map(c => c.platform ?? '').filter(Boolean), 'documents'])
  return (
    <>
      {/* an empty section is left out; the tool's own name is the Result's tooltip */}
      {args && Object.keys(args).length > 0 ? (
        <Part label="Arguments">
          <dl className="tool__args">
            {Object.entries(args).map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                {/* text as it was written (a command keeps its lines), anything else as JSON */}
                <dd><pre>{typeof v === 'string' ? v : JSON.stringify(v, null, 2)}</pre></dd>
              </div>
            ))}
          </dl>
        </Part>
      ) : !args && call.arguments ? <Part label="Arguments"><pre>{call.arguments}</pre></Part> : null}
      {where && (
        <Part label="Where">
          <div>{where}</div>
          {chats.length > 2 && (
            <ul className="tool__chats">
              {chats.map(c => <li key={c.ref}>{c.name ?? c.ref}{c.platform ? ` (${sourceLabel(c.platform)})` : ''}</li>)}
            </ul>
          )}
        </Part>
      )}
      <Part label="Result" tip={call.name}>
        {call.result == null || call.result === ''
          ? <div className="tool__none">{busy(call) ? 'No result yet.' : 'The tool returned nothing.'}</div>
          : <pre>{withRefs(call.result, kbs)}</pre>}
      </Part>
    </>
  )
}

function Part({ label, tip, children }: { label: string; tip?: string; children: ReactNode }) {
  return (
    <div className="tool__part">
      <div className="tool__part-label" title={tip}>{label}</div>
      {children}
    </div>
  )
}

/* message references in a result (whatsapp:48213) become links to the message */
function withRefs(text: string, kbs: Set<string>): ReactNode[] {
  const out: ReactNode[] = []
  const re = /\b([a-z][a-z0-9_-]*):(\d+)\b/g
  let last = 0
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (!kbs.has(m[1]!)) continue
    const href = refHref(m[0])
    if (!href) continue
    out.push(text.slice(last, m.index), <a key={m.index} href={href}>{m[0]}</a>)
    last = m.index + m[0].length
  }
  out.push(text.slice(last))
  return out
}

type Step = { step?: string; effect?: string; shot?: unknown }
const SHOT = 240      // a shot's width in the strip (.tool__step)
/** an effect that means the step did nothing */
const IDLE = /^(nothing changed on the page|the view did not move)|is not on this page|did not appear within/

/** A browser call's shots as a strip: one per step that ran (the click point drawn in
    it by the box), each with the step in its words and what it did, a step that did
    nothing muted; a look's screen shot with the point it answered ringed. Full size on
    click. The agent's Activity and the Browser page's console show the same. */
export function BrowserShots({ display }: { display: Record<string, unknown> | null | undefined }) {
  const gallery = useContext(GalleryContext)
  const steps = Array.isArray(display?.steps) ? (display.steps as Step[]) : []
  const point = Array.isArray(display?.point) ? (display.point as number[]) : null
  const items: { img: Made | null; step?: string; effect?: string; point?: number[] | null }[] = [
    ...steps.map(s => ({ img: asImage(s.shot), step: s.step, effect: s.effect })),
    ...(asImage(display?.shot) ? [{ img: asImage(display?.shot), point }] : []),
  ]
  if (items.length === 0) return null
  return (
    <div className="tool__shots">
      {items.map((it, i) => (
        <figure key={i} className={cx('tool__step', it.effect && IDLE.test(it.effect) && 'is-idle')}>
          {it.img && (
            <div className="tool__shot">
              <Picture src={tileUrl(it.img, SHOT)} full={fileUrl(it.img)} name={it.step ?? 'Browser'} ratio={`${it.img.width || 16} / ${it.img.height || 10}`}
                       onOpen={gallery ? () => gallery.openImage(it.img!.id || it.img!.sha256, it.img!) : undefined} />
              {it.point && it.img.width && it.img.height && (
                <span className="tool__point" aria-hidden
                      style={{ left: `${(it.point[0]! / it.img.width) * 100}%`, top: `${(it.point[1]! / it.img.height) * 100}%` }} />
              )}
            </div>
          )}
          {(it.step || it.effect) && (
            <figcaption><code>{it.step}</code>{it.effect && <span>{it.effect}</span>}</figcaption>
          )}
        </figure>
      ))}
    </div>
  )
}

/* A call it is still writing, under its message: what it is about to do, in the present
   ("Reading the note ml/muon.md", "Writing Dana Holm"), as one shimmering line; once it is
   long enough to be worth watching, how much it has written (its lines, or its size while it
   is one line) and the text so far behind it (never for a draft, whose text becomes a card). */
const PREVIEW_FROM = 200

export function FormingRow({ name, chars, lines, preview, words }: { name: string; chars: number; lines: number; preview: string; words: string }) {
  const Icon = ICONS[name.split('__')[0] ?? ''] ?? Wrench
  const long = chars >= PREVIEW_FROM
  return (
    <EventLine quiet running icon={Icon} meta={long ? (lines > 1 ? count(lines, 'line') : bytes(chars)) : undefined}
               detail={long && preview ? <pre className="tool__forming">{preview}</pre> : undefined}>
      {words}
    </EventLine>
  )
}

/* The pages its calls wrote, one card each with its newest version: a fixed-size row with
   the title and the version. In the Celine view it shows the page beside the chat;
   elsewhere, and with a middle click, it opens the full page in a new tab. */
export function artifactsOf(calls: ToolCall[]): ArtifactCardData[] {
  const newest = new Map<string, ArtifactCardData>()
  for (const c of calls) {
    const a = artifactOf(c.display)
    if (a && (newest.get(a.id)?.n ?? 0) < a.n) newest.set(a.id, a)
  }
  return [...newest.values()]
}

export function ArtifactCards({ cards }: { cards: ArtifactCardData[] }) {
  const show = useContext(ShowArtifact)
  if (cards.length === 0) return null
  const Open = show ? PanelRight : ExternalLink
  return (
    <div className="artcards">
      {cards.map(a => {
        const Kind = a.kind === 'markdown' ? FileText : AppWindow
        const word = kindWord(a.kind)
        return (
          <a key={a.id} className="artcard" href={format({ section: 'artifact', id: a.id })} target="_blank" rel="noopener"
             onClick={show ? e => { e.preventDefault(); show(a.id) } : undefined}
             title={show ? `Show ${a.title} beside the chat` : `Open ${a.title} in a new tab`}>
            <Kind size={ICON.lg} className="artcard__icon" aria-hidden />
            <span className="artcard__main">
              <span className="artcard__title">{a.title}</span>
              <span className="artcard__sub">{word[0]!.toUpperCase() + word.slice(1)}, version {a.n}</span>
            </span>
            <Open size={ICON.sm} className="artcard__open" aria-hidden />
          </a>
        )
      })}
    </div>
  )
}

/* What its calls shared from its box, one card each: the title, the server's dot and the
   time left. The card opens the share in a new tab through its open route (which lets you
   in to a private one); Stop sharing beside it ends it for it too. The Pages tab lists the
   running ones with the same cards. */
export function sharesOf(calls: ToolCall[]): ShareCardData[] {
  const seen = new Map<string, ShareCardData>()
  for (const c of calls) {
    const s = shareOf(c.display)
    if (s) seen.set(s.token, s)
  }
  return [...seen.values()]
}

export function ShareCards({ cards }: { cards: ShareCardData[] }) {
  if (cards.length === 0) return null
  return <div className="artcards">{cards.map(s => <ShareCard key={s.token} card={s} />)}</div>
}

function ShareCard({ card }: { card: ShareCardData }) {
  const { possessive } = usePronouns()
  const res = useResource(signal => sharesApi.get(card.token, signal), [card.token], { events: shareEvent(card.token) })
  const s = res.data
  const now = useNow(!!s && s.state !== 'ended')
  const left = s ? Date.parse(s.ends_at) - now : 0
  // its end time is passive, no event marks it: the card ends itself at ends_at
  const ended = res.missing || (!!s && (s.state === 'ended' || left <= 0))
  const dot = ended ? SHARE_DOT.ended : s ? SHARE_DOT[s.state] : null
  const stop = useAction(() => sharesApi.stop(card.token))
  return (
    <div className="sharecard">
      <a className="artcard" href={ended ? undefined : s?.url} target="_blank" rel="noopener"
         title={ended ? undefined : `Open ${card.title} in a new tab`}>
        <ScreenShare size={ICON.lg} className="artcard__icon" aria-hidden />
        <span className="artcard__main">
          <span className="artcard__title">{card.title}</span>
          <span className="artcard__sub">
            {dot ? <Dot tone={dot.tone} text={ended ? dot.label : `${dot.label}, ${duration(left)} left`} /> : `Shared from ${possessive} box`}
          </span>
        </span>
        {s?.public && <span className="artcard__open" title="Public" role="img" aria-label="Public"><Globe size={ICON.sm} aria-hidden /></span>}
        {!ended && <ExternalLink size={ICON.sm} className="artcard__open" aria-hidden />}
      </a>
      {s && !ended && <IconButton icon={Square} label="Stop sharing" loading={stop.busy} onClick={() => void stop.run()} />}
      {stop.error && <InlineError compact message={stop.error} />}
    </div>
  )
}
