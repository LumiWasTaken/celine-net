/* A conversation inside another place (a mission's) on screen: its transcript and the chat's
   composer under it, the same box as its place's. What you send shows at once until the
   server's copy lands; the view stays at the bottom while lines arrive, and "New messages"
   brings you back down after you scrolled up. `HerFace` and `MoodText` are the chat head's
   parts (its face with its mood on its card, its mood beside its name) for any head that
   shows it. */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowDown, Settings2, X } from 'lucide-react'
import { conversationApi } from '../lib/api'
import { errorText } from '../lib/format'
import { useOnline } from '../lib/socket'
import type { useConversation } from '../lib/useConversation'
import { useStickToBottom } from '../lib/useStickToBottom'
import { Composer, type ComposerHandle, type ComposerTag, type Note } from './Composer'
import { Transcript, type Pending } from './Transcript'
import { waitWords } from './Status'
import { colorOf, type Her } from '../lib/her'
import type { Mood } from '../lib/chat'
import { useNav } from '../lib/nav'
import { Button, Face, IconButton, InlineError, StatusLine } from '../ui'

/** its mood as it set it: clipped in a narrow head, whole in its tooltip and on its face card */
export function MoodText({ mood }: { mood: Mood | null }) {
  if (!mood) return null
  return <span className="chat-mood" title={mood.text} aria-label={`Mood ${mood.text}`}>{mood.text}</span>
}

/** its face; its card's line is its mood */
export function HerFace({ who, size, speaking = false, mood }: { who: Her | null; size: 'sm' | 'lg'; speaking?: boolean; mood: Mood | null }) {
  const nav = useNav()
  const her = colorOf(who)
  return <Face name={who?.name ?? ''} src={who?.portrait?.url} size={size} speaking={speaking}
               bg={`color-mix(in oklch, ${her} 22%, var(--surface-2))`} fg={her}
               info={{ line: mood?.text ?? who?.name ?? '', actions: [{ label: 'Open Settings', icon: Settings2,
                 onSelect: () => nav.go({ section: 'settings', page: 'agent' }) }] }} />
}

type Convo = ReturnType<typeof useConversation>

export function Talk({ cid, convo, active, name, user, send, placeholder, noToolsText, closed, notices, tag, onTag, handle }: {
  cid: string
  convo: Convo
  /** its place is on screen */
  active: boolean
  /** who answers here, for the box's words */
  name: string
  /** your name, for a brief or a note to it */
  user: string
  /** your line goes out; the intercom's answer says which message will carry it */
  send: (text: string, attachments: string[]) => Promise<{ intercom_id?: number | null; turn_id?: string | null } | null>
  placeholder?: string
  noToolsText?: (model: string) => string
  /** nothing new is written here: the sentence in place of the box */
  closed: string | null
  notices?: ReactNode
  /** a chip above the box that goes out with your next message */
  tag?: ComposerTag | null
  onTag?: (tag: null) => void
  /** the box, for the page (focus it) */
  handle?: React.RefObject<ComposerHandle | null>
}) {
  const transcript = useRef<HTMLDivElement>(null)
  const own = useRef<ComposerHandle>(null)
  const composer = handle ?? own
  const [note, setNote] = useState<Note | null>(null)
  const [pending, setPending] = useState<(Pending & { intercomId?: number; turnId?: string | null }) | null>(null)
  const online = useOnline()
  const offline = convo.reconnecting || !online

  const lastMessage = convo.messages.at(-1)
  const { onScroll, unseen, jump } = useStickToBottom(
    transcript, [lastMessage?.content, lastMessage?.reasoning, lastMessage?.id, convo.toolCalls, note, pending],
    [cid], [cid, convo.messages.length > 0])
  useEffect(() => {
    setPending(null)
    setNote(null)
  }, [cid])

  const post = useCallback(async (text: string, ready: string[], names: string[]) => {
    setNote(null)
    setPending({ text, files: names })
    jump({ smooth: false })
    try {
      const r = await send(text, ready)
      if (r?.intercom_id == null) setPending(null)
      else setPending(p => p && { ...p, intercomId: r.intercom_id!, turnId: r.turn_id })
    } catch (e) {
      setPending(null)
      setNote({ tone: 'error', text: errorText(e) })
      throw e
    }
  }, [send]) // eslint-disable-line react-hooks/exhaustive-deps
  // the server's copy of your line (with its answer under way when one started) takes the
  // place of the one shown at once
  useEffect(() => {
    if (pending?.intercomId !== undefined && (!pending.turnId || convo.turn?.id === pending.turnId)
        && convo.messages.some(m => m.intercom?.id === pending.intercomId)) setPending(null)
  }, [pending, convo.messages, convo.turn?.id])

  const { markStopping } = convo
  const stop = useCallback(async () => {
    markStopping(true)
    try {
      const r = await conversationApi.abort(cid)
      if (!r.turn_id) markStopping(false)
    } catch (e) { markStopping(false); setNote({ tone: 'error', text: errorText(e) }) }
  }, [cid, markStopping])
  const attempt = useCallback(async (fn: () => Promise<unknown>) => {
    try { await fn() } catch (e) { setNote({ tone: 'error', text: errorText(e) }) }
  }, [])

  const noteLine = note && (
    <div className="chat-note">
      {note.tone === 'error' ? <InlineError message={note.text} compact /> : <StatusLine tone="success">{note.text}</StatusLine>}
      <IconButton icon={X} size="sm" label="Dismiss" onClick={() => setNote(null)} />
    </div>
  )

  return (
    <>
      <div className="chat-scroll" ref={transcript} onScroll={onScroll}>
        <Transcript conversationId={cid} messages={convo.messages} toolCalls={convo.toolCalls} turn={convo.turn}
                    running={convo.running} error={convo.error} pending={pending} compactions={convo.compactions}
                    onRetry={() => conversationApi.regenerate(cid).then(() => undefined)}
                    live={convo.live} switched={convo.switched} imageNotices={convo.imageNotices} banners={convo.banners} forming={convo.forming}
                    waiting={waitWords(convo.waiting, convo.live)} queue={convo.queue}
                    note={null} assistant={name} user={user} closed={!!closed} />
      </div>
      <Composer handle={composer} conversationId={cid} exists active={active} name={name}
                running={convo.running} offline={offline} sending={!!pending} pending={!!pending}
                empty={false} hidden={false} closed={closed} settings={convo.settings}
                onSend={post} onStop={() => void stop()} setNote={setNote} attempt={attempt}
                placeholder={placeholder} noToolsText={noToolsText} takesPrefill={false}
                notices={<>{noteLine}{notices}</>} tag={tag} onTag={onTag}
                jump={unseen && <Button size="sm" icon={ArrowDown} onClick={jump}>New messages</Button>} />
    </>
  )
}
