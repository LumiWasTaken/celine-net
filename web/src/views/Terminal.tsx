/* One terminal: its history, then its screen as the daemon streams it, the controls on
   its own, your own keys into yours, its commands, and the message box that sends to a
   conversation with a link to the terminal or a quoted command (docs/ARCHITECTURE.md,
   terminals: your terminal is a real terminal, raw keys go as binary frames). In the
   Terminals place, beside its sidebar. Loaded lazily: xterm is its own chunk.
   URL: /terminal/<id>, ?reply=<command id>. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useName } from '../lib/name'
import { CircleStop, CornerUpLeft, MessageCircle, Pause, SearchX, X } from 'lucide-react'
import { Terminal as XTerm } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { format, useNav } from '../lib/nav'
import { listTime } from '../lib/format'
import { getPref, setPref } from '../lib/prefs'
import {
  commandWords, isShellEvent, isYours, outcomeWords, spaced, stateDetail,
  terminalsApi, type Action, type CommandRow, type TerminalRow,
} from '../lib/terminals'
import { backoffMs, keepAlive } from '../lib/socket'
import { useAction } from '../lib/useAction'
import { useResource } from '../lib/useResource'
import {
  Button, EmptyState, IconButton, InlineError, ListRow, LoadingRow, MenuButton, PlaceHead, Screen, SearchField, Section, SendButton, StatusLine,
  Switch, TextArea, cx, isSendKey,
} from '../ui'
import { CloseTerminal, CommandDialog, TerminalsPlace } from './Terminals'
import './Terminals.css'
import { setPageName } from '../lib/title'

const COLS = 120
const ROWS = 40
const SCROLLBACK = 50_000       // lines of its history to scroll through, as many as its tmux keeps
const FONT_MAX = 18 // the font follows the room up to this
/** the floor: the box's font size, the kit's mono size --t-xs (Terminals.css), never a smaller px */
const fontMin = (el: HTMLElement) => parseFloat(getComputedStyle(el).fontSize) || 14
const DRAFT_KEY = (id: string) => `terminal:draft:${id}`
const LINE_MS = 1500            // after Enter in your terminal: the log gets the line
const RECENT = 3                // conversations offered beside its own


export function Terminal({ id, reply }: { id: string; reply?: string }) {
  return <TerminalsPlace open={id}><TerminalPage id={id} reply={reply} /></TerminalsPlace>
}

function TerminalPage({ id, reply }: { id: string; reply?: string }) {
  const her = useName()
  const nav = useNav()
  const t = useResource(() => terminalsApi.get(id), [id], { events: ev => ev.type === 'terminal.state' })
  const detail = t.data
  const name = detail?.name ?? ''
  useEffect(() => { if (name) setPageName(`Terminal ${name}`) }, [name])

  const [q, setQ] = useState('')
  const commands = useResource(() => terminalsApi.commands({ terminal: id, q: q.trim(), limit: 100 }), [id, q],
    { events: isShellEvent, debounce: q ? 250 : 0 })
  const lineTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => clearTimeout(lineTimer.current), [])

  const live = detail?.live ?? null
  const yours = detail ? isYours(detail) : false
  const ended = !!detail?.ended_at

  const control = useAction(async (action: Action) => {
    await terminalsApi.control(id, action)
    t.reload(); commands.reload()
  })
  const mayType = useAction(async (on: boolean) => {
    await terminalsApi.mayType(id, on)
    t.reload()
  })
  const [closing, setClosing] = useState(false)

  // the message box, its draft kept per terminal
  const [quote, setQuote] = useState<CommandRow | null>(null)
  const [to, setTo] = useState('')        // a conversation id, or '': its own (the terminal's, else a new one)
  const [draft, setDraftState] = useState(() => getPref(DRAFT_KEY(id)) ?? '')
  const setDraft = (v: string) => { setDraftState(v); setPref(DRAFT_KEY(id), v) }
  const textarea = useRef<HTMLTextAreaElement>(null)
  const targets = useResource(() => terminalsApi.targets(id), [id],
    { events: ev => ev.type === 'conversation.title' })
  const own = detail?.conversation ?? null
  // its own, then the few conversations used most lately
  const options = useMemo(() => [
    { value: '', label: her },
    ...(targets.data?.targets ?? []).filter(c => c.id !== own?.id).slice(0, RECENT).map(c => ({ value: c.id, label: c.name })),
  ], [targets.data, own?.id, her])
  const target = options.some(o => o.value === to) ? to : ''
  const doReply = (c: CommandRow) => {
    setQuote(c)
    setTo(c.from.conversation_id && c.from.conversation_id !== own?.id ? c.from.conversation_id : '')
    requestAnimationFrame(() => textarea.current?.focus())
  }
  // ?reply=<command id> from the Terminals page: quoted once, then the URL is plain again
  const taken = useRef<string | null>(null)
  useEffect(() => {
    if (!reply || taken.current === reply || !commands.data) return
    const c = commands.data.commands.find(x => x.id === reply)
    if (!c) return
    taken.current = reply
    doReply(c)
    nav.go({ section: 'terminals', terminal: id }, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reply, commands.data])
  const send = useAction(async () => {
    const text = draft.trim()
    if (!text && !quote) return
    await terminalsApi.message(id, { text, ...(target ? { conversation_id: target } : {}), ...(quote ? { command_id: quote.id } : {}) })
    setDraft(''); setQuote(null)
    targets.reload()
    commands.reload()
  })

  const [opened, setOpened] = useState<CommandRow | null>(null)
  const crumbs = [{ label: 'Terminals', href: format({ section: 'terminals' }) }]

  if (!detail) {
    return (
      <div className="term">
        <PlaceHead level={1} crumbs={crumbs} title="" />
        {t.error ? <div className="term-note"><InlineError message={t.error} onRetry={t.reload} /></div> : <LoadingRow />}
      </div>
    )
  }

  const paused = !!live?.paused
  const canPause = !!live && !ended && (paused || live.busy || live.fullscreen)
  const canStop = !!live && !ended && !paused && (live.busy || live.fullscreen || (!!live.program && live.waiting))
  const error = control.error ?? mayType.error
  const rows = commands.data?.commands ?? []

  return (
    <div className="term">
      <PlaceHead level={1} crumbs={crumbs} title={name} detail={stateDetail(detail) || undefined}
                 actions={<>
                   {!yours && !ended && <>
                     <Button icon={Pause} pressed={paused} loading={control.busy} disabled={!canPause}
                             onClick={() => void control.run(paused ? 'resume' : 'pause')}>Pause</Button>
                     <Button icon={CircleStop} disabled={!canStop || control.busy} onClick={() => void control.run('stop')}>Stop</Button>
                   </>}
                   {yours && !ended && (
                     <Switch checked={detail.may_type} label={`${her} may type`} disabled={mayType.busy} onChange={v => void mayType.run(v)} />
                   )}
                 </>}
                 more={[
                   ...(own && !yours ? [{ label: 'Open conversation', icon: MessageCircle, onSelect: () => nav.goPath(own.link) }] : []),
                   ...(!ended ? ['sep' as const, { label: 'Close terminal', icon: X, danger: true, onSelect: () => setClosing(true) }] : []),
                 ]} />
      {error && <StatusLine className="term-note" tone="error">{error}</StatusLine>}

      <div className="term-body">
        <Screen title={live?.program ?? name} className="term-screen">
          <XTermBox key={`${id}:${yours}`} id={id} yours={yours} detail={detail} onEnd={() => t.reload()}
                    onLine={() => { clearTimeout(lineTimer.current); lineTimer.current = window.setTimeout(() => commands.reload(), LINE_MS) }} />
        </Screen>

        <div className="term-side">
          <Section>
            <div className="term-send">
              {quote && (
                <ListRow dot={outcomeWords(quote).tone === 'error' ? 'error' : 'ok'} title={<span className="mono">{commandWords(quote)}</span>}
                         subtitle={spaced(outcomeWords(quote).words)} trailingAlways
                         trailing={<IconButton icon={X} label="Remove quoted command" size="sm" onClick={() => setQuote(null)} />} />
              )}
              {options.length > 1 && (
                <MenuButton label="Send to" className="term-to" text={<span className="term-to__name">{options.find(o => o.value === target)?.label}</span>}
                            items={options.map(o => ({ label: o.label, current: o.value === target, onSelect: () => setTo(o.value) }))} />
              )}
              <div className="term-box-line">
                <div className="term-box-line__text">
                  <TextArea ref={textarea} rows={1} max={6} value={draft} placeholder={`Message ${her} about ${name}`}
                            aria-label={`Message ${her} about ${name}`} onChange={e => setDraft(e.target.value)}
                            onKeyDown={e => { if (isSendKey(e)) { e.preventDefault(); void send.run() } }} />
                </div>
                <SendButton ready={!send.busy && (!!draft.trim() || !!quote)} onSend={() => void send.run()} />
              </div>
              {send.error && <InlineError compact message={send.error} />}
            </div>
          </Section>
          {(rows.length > 0 || q.trim() || !commands.data) && <Section title="Commands" count={commands.data?.total}>
            <SearchField value={q} onChange={setQ} placeholder="Search commands" loading={commands.loading} />
            {commands.error && <InlineError compact message={commands.error} onRetry={commands.reload} />}
            {!commands.data && !commands.error && <LoadingRow />}
            {commands.data && rows.length === 0 && q.trim() && <EmptyState compact icon={SearchX} title="No commands match" />}
            <div className="term-rows">
              {rows.map(c => {
                const out = outcomeWords(c)
                return (
                  <ListRow key={c.id} dot={out.tone === 'error' ? 'error' : out.tone === 'running' ? 'running' : 'ok'}
                           title={<span className="mono">{commandWords(c)}</span>} tooltip={commandWords(c)}
                           subtitle={c.from.kind === 'you' ? 'You' : c.from.title ?? undefined} meta={listTime(c.at)} onClick={() => setOpened(c)}
                           trailing={c.from.kind !== 'you' && <IconButton icon={CornerUpLeft} label="Reply" size="sm" onClick={e => { e.stopPropagation(); doReply(c) }} />} />
                )
              })}
            </div>
          </Section>}
        </div>
      </div>

      {opened && <CommandDialog c={opened} onClose={() => setOpened(null)} onReply={doReply} />}
      {closing && <CloseTerminal t={detail} onClose={() => setClosing(false)} onClosed={() => { t.reload(); commands.reload() }} />}
    </div>
  )
}

/* ── the screen: xterm, the socket, the font that fills the room ── */

/** the tokens as rgba(): a token's own text can be `oklch(from var(--accent) ...)`, which a
    browser computes to oklch() or oklab(), none of which xterm reads; a canvas pixel turns
    any colour into rgba. One probe per token: a reused one can answer with the colour of
    the token before (Chrome did, so the text took the background's colour) */
function theme() {
  const px = document.createElement('canvas').getContext('2d', { willReadFrequently: true })!
  const v = (n: string) => {
    const probe = document.body.appendChild(document.createElement('i'))
    probe.style.color = `var(${n})`
    const color = getComputedStyle(probe).color
    probe.remove()
    px.clearRect(0, 0, 1, 1)
    px.fillStyle = color
    px.fillRect(0, 0, 1, 1)
    const [r, g, b, a] = px.getImageData(0, 0, 1, 1).data
    return `rgba(${r}, ${g}, ${b}, ${Math.round(a! / 255 * 100) / 100})`
  }
  const t = {
    background: v('--surface-sunk'), foreground: v('--text-primary'),
    cursor: v('--accent'), cursorAccent: v('--surface-sunk'), selectionBackground: v('--accent-wash-strong'),
    black: v('--ansi-black'), red: v('--ansi-red'), green: v('--ansi-green'), yellow: v('--ansi-yellow'),
    blue: v('--ansi-blue'), magenta: v('--ansi-magenta'), cyan: v('--ansi-cyan'), white: v('--ansi-white'),
    brightBlack: v('--ansi-bright-black'), brightRed: v('--ansi-bright-red'), brightGreen: v('--ansi-bright-green'),
    brightYellow: v('--ansi-bright-yellow'), brightBlue: v('--ansi-bright-blue'), brightMagenta: v('--ansi-bright-magenta'),
    brightCyan: v('--ansi-bright-cyan'), brightWhite: v('--ansi-bright-white'),
  }
  return t
}

function XTermBox({ id, yours, detail, onEnd, onLine }: {
  id: string
  yours: boolean
  detail: TerminalRow
  /** the socket said end, or the live view was refused on a row that has ended */
  onEnd: () => void
  /** Enter went into your terminal */
  onLine: () => void
}) {
  const box = useRef<HTMLDivElement>(null)
  const term = useRef<XTerm | null>(null)
  const [ready, setReady] = useState(false)
  const [endedLive, setEndedLive] = useState(false)
  const [lost, setLost] = useState(false)
  const endRef = useRef(onEnd); endRef.current = onEnd
  const lineRef = useRef(onLine); lineRef.current = onLine
  const open = !detail.ended_at
  const ended = !open || endedLive
  const paused = !!detail.live?.paused

  // the terminal itself, once the mono font is in so the cells are measured right
  useEffect(() => {
    const el = box.current
    if (!el) return
    let gone = false
    let made: XTerm | null = null
    void document.fonts.load('12px "JetBrains Mono Variable"').catch(() => undefined).then(() => {
      if (gone) return
      made = new XTerm({
        cols: COLS, rows: ROWS, disableStdin: !yours, scrollback: SCROLLBACK,
        fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim(),
        fontSize: fontMin(el), lineHeight: 1, cursorBlink: true, cursorStyle: 'block', cursorInactiveStyle: 'block',
        theme: theme(),
      })
      made.open(el)
      term.current = made
      setReady(true)
      if (yours) made.focus()
    })
    return () => { gone = true; made?.dispose(); term.current = null; setReady(false) }
  }, [id, yours])

  // the font follows the room: it 120 by 40 as big as the box holds it. A guess from
  // the sizes, then half a pixel down while it is too big (xterm resizes at once)
  useEffect(() => {
    const xt = term.current, el = box.current, room = el?.parentElement
    if (!ready || !xt || !el || !room) return
    const ro = new ResizeObserver(entries => {
      const { width = 0, height = 0 } = entries[0]?.contentRect ?? {}
      if (!width || !el.offsetWidth) return
      // the cells do not scale evenly at small sizes: guess again from what the last guess measured
      const min = fontMin(el)
      let next = xt.options.fontSize ?? min
      for (let i = 0; i < 4; i++) {
        const scale = Math.min(width / el.offsetWidth, height / el.offsetHeight)
        const guess = Math.min(FONT_MAX, Math.max(min, Math.floor(next * scale * 2) / 2))
        if (guess === next) break
        xt.options.fontSize = next = guess
      }
      while (next > min && (el.offsetWidth > width || el.offsetHeight > height)) xt.options.fontSize = next -= 0.5
    })
    ro.observe(room)
    return () => ro.disconnect()
  }, [ready])

  // its history first, then, while the terminal is open, the live view on top: binary
  // frames are the screen, the text frame end is the end; your keys go back
  useEffect(() => {
    const xt = term.current
    if (!ready || !xt) return
    let closed = false, attempt = 0, opens = 0, done = false
    let ws: WebSocket | null = null
    let retry: number | undefined
    let history = new Uint8Array()
    const replay = () => { xt.reset(); xt.write(history) }
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const sock = new WebSocket(`${proto}://${location.host}/ws/terminals/${encodeURIComponent(id)}`)
      sock.binaryType = 'arraybuffer'
      ws = sock
      sock.onopen = () => {
        // tmux redraws the whole screen on a fresh attach: start again from the history
        if (opens++ > 0) replay()
      }
      sock.onmessage = e => {
        // the backoff starts again once the screen speaks, not on an open the box then closes
        attempt = 0
        if (e.data instanceof ArrayBuffer) { xt.write(new Uint8Array(e.data)); return }
        let ev: unknown
        try { ev = JSON.parse(String(e.data)) } catch { return }
        if (ev && typeof ev === 'object' && (ev as { type?: unknown }).type === 'end') {
          done = true
          setEndedLive(true)
          endRef.current()
        }
      }
      sock.onclose = () => {
        if (closed || ws !== sock || done) return
        // refused or broken: a row that has ended shows its history instead of a retry
        void terminalsApi.get(id).then(row => !!row.ended_at, () => false).then(over => {
          if (closed) return
          if (over) { endRef.current(); return }
          const n = attempt++
          retry = window.setTimeout(connect, backoffMs(n))
        })
      }
    }
    void terminalsApi.log(id).then(b => { history = b; setLost(false) }, () => setLost(true)).then(() => {
      if (closed) return
      replay()
      if (open) connect()
    })
    const keys = xt.onData(d => {
      if (!yours || ws?.readyState !== WebSocket.OPEN) return
      ws.send(new TextEncoder().encode(d))
      if (d.includes('\r')) lineRef.current()
    })
    const unping = keepAlive(() => ws)
    return () => {
      closed = true
      clearTimeout(retry); unping(); keys.dispose()
      const s = ws; ws = null
      if (s) { s.onclose = null; s.close() }
    }
  }, [ready, open, id, yours])

  return (
    <div className={cx('term-box', yours && 'term-box--yours')}>
      <div ref={box} className={cx('term-xterm', (ended || paused) && 'term-xterm--dim')}
           aria-label={yours ? 'Your terminal' : `Terminal ${detail.name}`} />
      {lost && <div className="term-lost" role="status">The history could not be loaded.</div>}
    </div>
  )
}
