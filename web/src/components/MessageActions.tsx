/* A message's actions, the same on every line of a chat (Transcript gives them to the
   kit's Message, which draws the row of reserved height under it, shown on hover, focus or
   tap, and opens the same menu on a right-click or a long press). In the row: Regenerate
   (its reply), Edit, Copy, the versions' arrows, and "..." with everything: Regenerate,
   Edit, Copy, Previous version, Next version, Continue (when its turn can be carried on),
   Branch from here (a new conversation with every message up to this one), More like this
   (its voice keeps it as a sample), Activity (the reply's tool calls, in a Drawer; a Sheet
   on a phone), Thoughts (when they are not shown above it) and What it saw (the
   requests and the raw answer of each step), and Delete last, asked first. While it
   writes, what would change the conversation under it waits. */
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  Activity, Brain, Check, ChevronLeft, ChevronRight, Copy, Eye, GitBranch, Heart, Pencil, Play, RefreshCw, Trash2,
} from 'lucide-react'
import { conversationApi, type Message, type Siblings, type ToolCall, type Turn } from '../lib/api'
import { chatApi } from '../lib/chat'
import { errorText } from '../lib/format'
import { useNav } from '../lib/nav'
import { ConfirmDialog, Drawer, IconButton, InlineError, Thoughts, useCopied, type MenuItem } from '../ui'
import { ToolGroup } from './ToolRow'
import { WhatSheSaw } from './WhatSheSaw'
import './Transcript.css'
import { useReplySpeech } from './Speech'

type Props = {
  conversationId: string
  message: Message
  /** its reply's steps, one model request each ("What it saw" shows them all) */
  steps?: Message[]
  /** the reply's tool calls (Activity) */
  calls?: ToolCall[]
  running: boolean
  /** the last message on screen: its versions are asked again once its turn ends */
  last: boolean
  /** the reply is being written: no actions yet, the row keeps its place */
  writing?: boolean
  /** the conversation's latest turn (Continue when it can be carried on) */
  turn: Turn | null
  onEdit: () => void
  /** "What it saw", in its pronouns */
  sawTitle: string
  /** read-only: no Regenerate, Edit, Continue or Branch */
  closed?: boolean
  /** its thinking, where it is not shown above the reply ("The agent's thoughts") */
  thoughts?: { title: string; text: string } | null
}

/** what the kit's Message takes: its versions, its row's buttons, its menu, and what they open */
export type Actions = {
  versions?: { at: number; total: number; onPrev: () => void; onNext: () => void }
  actions?: ReactNode
  menu?: MenuItem[]
  /** dialogs and drawers the menu opens, and an error it met */
  overlays: ReactNode
}

export function useMessageActions(
  { conversationId, message, steps, calls = [], running, last, writing = false, turn, onEdit, sawTitle,
    closed = false, thoughts }: Props,
): Actions {
  const nav = useNav()
  const [sib, setSib] = useState<Siblings | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [copied, copyIt] = useCopied()
  // Activity or Thoughts: with a SideHost one at a time across the page, else this message's own
  const host = useContext(SideHost)
  const [own, setOwn] = useState<Panel | null>(null)
  const panel: Panel | null = !host ? own
    : host.active === `${message.id}:thoughts` ? 'thoughts' : host.active === `${message.id}:activity` ? 'activity' : null
  const setPanel = (p: Panel | null) => (host ? host.setActive(p && `${message.id}:${p}`) : setOwn(p))
  const [saw, setSaw] = useState(false)
  // a drawer is made on its first open (it slides in: Drawer `enter`) and kept
  const [made, setMade] = useState<Set<Panel>>(new Set())
  const show = (p: Panel) => { setMade(m => (m.has(p) ? m : new Set(m).add(p))); setPanel(p) }
  const [refused, setRefused] = useState<string | null>(null)
  const hers = message.role === 'assistant'
  const speechAction = useReplySpeech(message.id, conversationId, hers ? message.content : '',
    running, hers && turn?.state === 'done', last && hers)
  // More like this: the server says whether it is a sample now
  const [sample, setSample] = useState(!!message.sample)
  useEffect(() => setSample(!!message.sample), [message.sample])
  const tryIt = (fn: () => Promise<unknown>) => { setRefused(null); void fn().catch(e => setRefused(errorText(e))) }

  // the versions; its last reply asks again once its turn ends, so Regenerate's new
  // version counts without a reload (the new message arrives as the only one it knows)
  const settled = last && hers && !running
  useEffect(() => {
    if (!conversationId || (message.siblings < 2 && !settled)) { setSib(null); return }
    let live = true
    // an extra: without it the message still reads fine, so a failed lookup leaves the arrows out
    void conversationApi.siblings(conversationId, message.id)
      .then(s => { if (live) setSib(s.count > 1 ? s : null) })
      .catch(() => {})
    return () => { live = false }
  }, [conversationId, message.id, message.siblings, settled])

  const go = (dir: -1 | 1) => {
    const next = sib?.siblings[sib.index + dir]
    if (next && !running) tryIt(() => conversationApi.version(conversationId, next.id))
  }
  const copy = () => copyIt(message.content)
  const branch = () => tryIt(async () => {
    const c = await chatApi.branch(conversationId, message.id)
    nav.go({ section: 'celine', conversation: c.id })
  })

  if (writing || !conversationId) return { overlays: null }
  // a background job's result is the app's message, not yours to edit
  const editable = !closed && !running && ((hers && !!message.content.trim()) || (message.role === 'user' && message.intercom?.kind !== 'job'))
  // the server makes a new version beside a message under another; its first message (a
  // chat it started) has none to answer
  const regen = hers && !closed && !running && message.parent_id !== null
  const regenerate = () => tryIt(() => conversationApi.retry(conversationId, message.id))
  // a turn that stopped or was cut off picks up where it ended
  const canContinue = hers && !closed && last && !running && !!turn?.continuable?.ok
  const lovable = hers && !!message.content.trim()
  const work = calls.filter(c => !c.name.startsWith('skills__')).length > 0 ? calls : []

  const all: MenuItem[] = [
    ...(regen ? [{ label: 'Regenerate', icon: RefreshCw, onSelect: regenerate }] : []),
    ...(editable ? [{ label: 'Edit', icon: Pencil, onSelect: onEdit }] : []),
    { label: 'Copy', icon: Copy, onSelect: () => void copy() },
    ...(sib && !running && sib.index > 0 ? [{ label: 'Previous version', icon: ChevronLeft, onSelect: () => go(-1) }] : []),
    ...(sib && !running && sib.index < sib.count - 1 ? [{ label: 'Next version', icon: ChevronRight, onSelect: () => go(1) }] : []),
    'sep',
    ...(canContinue ? [{ label: 'Continue', icon: Play, onSelect: () => tryIt(() => conversationApi.continueTurn(conversationId, turn!.id)) }] : []),
    ...(!closed && !running && message.content.trim() ? [{ label: 'Branch from here', icon: GitBranch, onSelect: branch }] : []),
    ...(lovable ? [{ label: 'More like this', icon: Heart, current: sample, title: 'Keep it as a sample, so more replies sound like this',
                     onSelect: () => tryIt(async () => setSample((await conversationApi.sample(conversationId, message.id, !sample)).sample)) }] : []),
    ...(hers && work.length ? [{ label: 'Activity', icon: Activity, onSelect: () => show('activity') }] : []),
    ...(thoughts ? [{ label: thoughts.title, icon: Brain, onSelect: () => show('thoughts') }] : []),
    ...(steps ? [{ label: sawTitle, icon: Eye, onSelect: () => setSaw(true) }] : []),
    ...(!running && !closed ? ['sep' as const, { label: 'Delete', icon: Trash2, danger: true, onSelect: () => setConfirming(true) }] : []),
  ]
  const menu = all.filter((it, i) => it !== 'sep' || (i > 0 && all[i - 1] !== 'sep' && i < all.length - 1))

  return {
    versions: sib ? { at: sib.index + 1, total: sib.count, onPrev: () => go(-1), onNext: () => go(1) } : undefined,
    actions: <>
      {hers && !running && !!message.content.trim() && speechAction}
      {regen && <IconButton icon={RefreshCw} label="Regenerate" size="sm" onClick={regenerate} />}
      {editable && <IconButton icon={Pencil} label="Edit" size="sm" onClick={onEdit} />}
      <IconButton icon={copied ? Check : Copy} label={copied ? 'Copied' : 'Copy'} size="sm" onClick={() => void copy()} />
    </>,
    menu,
    overlays: <>
      {refused && (
        <div className="transcript-refused">
          <InlineError compact message={refused} />
        </div>
      )}
      {saw && steps && <WhatSheSaw conversationId={conversationId} steps={steps} title={sawTitle} onClose={() => setSaw(false)} />}
      {thoughts && made.has('thoughts') && <Side open={panel === 'thoughts'} title={thoughts.title} onClose={() => setPanel(null)}>
        <Thoughts className="thoughts">{thoughts.text}</Thoughts>
      </Side>}
      {work.length > 0 && made.has('activity') && <Side open={panel === 'activity'} title="Activity" onClose={() => setPanel(null)}>
        <ToolGroup calls={work} />
      </Side>}
      {confirming && (
        <ConfirmDialog title="Delete the message?" danger confirmLabel="Delete"
                       body="It goes for good, with every message after it."
                       onClose={() => setConfirming(false)}
                       onConfirm={async () => {
                         await conversationApi.remove(conversationId, message.id)
                         setConfirming(false)
                       }} />
      )}
    </>,
  }
}

type Panel = 'thoughts' | 'activity'

/** where a page has room beside its conversation (the chat's row, Celine.tsx): Activity
    and Thoughts open there as the kit's Drawer and the conversation narrows beside it;
    `active` is the one open on the page ("<message id>:<panel>") */
export const SideHost = createContext<{ host: HTMLElement | null; active: string | null; setActive: (key: string | null) => void } | null>(null)

/** Activity or Thoughts: in the page's SideHost, else a Drawer at the window's right
    edge over the conversation (a Sheet from the bottom on a phone) */
function Side({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  const host = useContext(SideHost)?.host
  const drawer = <Drawer enter open={open} title={title} onClose={onClose}>{children}</Drawer>
  return host ? createPortal(drawer, host) : createPortal(
    <div className="transcript-side" data-open={open || undefined}>{drawer}</div>,
    document.body,
  )
}
