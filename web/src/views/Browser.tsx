/* The agent's browser: its tabs and the history of its calls; a tab with Brave's window on the
   browser box's screen in noVNC (view only by default), Take control and Return control
   (its calls are refused while you have it; its next result says when you had it),
   Restart, the shots of its latest call's steps, and Tools: the console that runs one of
   its calls exactly as it runs it (POST /api/browser/calls) and shows exactly what it
   gets, its text beside the shots. The place's sidebar holds Tabs and History with their
   counts and its tabs as Recent. Loaded lazily: noVNC is its own chunk. URL: /browser. */
import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, Copy, Globe, History, MessageCircle, Monitor, MoreHorizontal, MousePointerClick, Play, RefreshCw, SearchX, X } from 'lucide-react'
import RFB from '@novnc/novnc'
import { BrowserShots } from '../components/ToolRow'
import { browserApi, browserHistory, isBrowserEvent, type Box, type BrowserCall, type Tab } from '../lib/browser'
import { clock, count, dayLabel } from '../lib/format'
import { useNav } from '../lib/nav'
import { usePronouns } from '../lib/name'
import { LINK } from '../lib/settingsApi'
import { backoffMs, keepAlive } from '../lib/socket'
import { useAction } from '../lib/useAction'
import { useResource } from '../lib/useResource'
import {
  Button, ConfirmDialog, Dialog, Dot, GroupHead, InlineError, ListRow, ListSection, Loaded, MenuButton, Notice, Place,
  PlaceHead, Screen, Section, Segmented, TextArea, Toolbar, VirtualList, useContextMenu, type MenuItem, copied
} from '../ui'
import './Browser.css'

const XK_CONTROL_L = 0xffe3, XK_V = 0x76     // X's names for the keys of a paste

/* what the place says while there is no screen to show */
function notUp(b: Box): string | null {
  if (b.state === 'starting') return 'Starting'
  if (b.state === 'down') return 'Brave could not start'
  if (b.state === 'unreachable') return LINK.down
  return null
}
const hostOf = (url: string) => { try { return new URL(url).host } catch { return '' } }
const tabTitle = (t: Tab) => t.title || hostOf(t.url) || 'New tab'

type Page = { kind: 'tabs' } | { kind: 'history' } | { kind: 'tab'; name: string }

export function Browser({ active }: { active: boolean }) {
  const box = useResource(() => browserApi.get(), [], { events: isBrowserEvent })
  const calls = useResource(() => browserHistory({ limit: 1 }), [], { events: ev => ev.type === 'tool.done' && typeof ev.name === 'string' && ev.name.startsWith('browser__') })
  const [page, setPage] = useState<Page>({ kind: 'tabs' })
  const [restarting, setRestarting] = useState(false)
  const b = box.data
  const tabs = b?.tabs ?? []
  const yours = b?.state === 'up' && !!b.taken
  const control = useAction(async (action: 'take' | 'give') => { await browserApi.control(action); box.reload() })
  const onTab = useAction(async (action: 'front' | 'close', tab: string) => { await browserApi.tab(action, tab); box.reload() })
  const openTab = (t: Tab) => { setPage({ kind: 'tab', name: t.name }); if (t.name !== b?.front) void onTab.run('front', t.name) }
  const take = b && b.state !== 'off' && (yours
    ? <Button variant="primary" icon={MousePointerClick} loading={control.busy} title={b.taken ? `Yours since ${clock(b.taken * 1000)}` : undefined}
              onClick={() => void control.run('give')}>Return control</Button>
    : <Button icon={MousePointerClick} loading={control.busy} disabled={b.state !== 'up'} onClick={() => void control.run('take')}>Take control</Button>)
  const restart: MenuItem = { label: 'Restart', icon: RefreshCw, disabled: !b || b.state === 'off', onSelect: () => setRestarting(true) }
  const tab = page.kind === 'tab' ? tabs.find(t => t.name === page.name) : undefined
  const error = control.error ?? onTab.error

  return (
    <Place title="Browser"
           side={<>
             <ListRow density="sm" icon={Globe} title="Tabs" meta={b && b.state !== 'off' ? tabs.length : undefined} selected={page.kind === 'tabs'}
                      onClick={() => setPage({ kind: 'tabs' })} />
             <ListRow density="sm" icon={History} title="History" meta={calls.data?.total} selected={page.kind === 'history'} onClick={() => setPage({ kind: 'history' })} />
             {tabs.length > 0 && <ListSection label="Recent" />}
             {tabs.map(t => (
               <ListRow key={t.name} density="sm" leading={t.busy ? <Dot tone="running" pulse /> : <Globe size={16} aria-hidden />} title={tabTitle(t)}
                        tooltip={t.url} selected={page.kind === 'tab' && page.name === t.name} onClick={() => openTab(t)} />
             ))}
           </>}>
      {error && <div className="list-page__note"><InlineError compact message={error} /></div>}
      {page.kind === 'history' ? <HistoryList />
        : page.kind === 'tab' && b && tab ? (
          <TabPage b={b} tab={tab} last={calls.data?.calls[0]} active={active} take={take} yours={yours} onBack={() => setPage({ kind: 'tabs' })}
                   more={[{ label: 'Copy', icon: Copy, detail: tab.url, onSelect: () => copied(tab.url) }, restart,
                          'sep', { label: 'Close tab', icon: X, danger: true, onSelect: () => { void onTab.run('close', tab.name); setPage({ kind: 'tabs' }) } }]}
                   onConnect={box.reload} />
        ) : <TabsList box={box} take={take} restart={restart} onOpen={openTab} onClose={t => void onTab.run('close', t.name)} />}
      {restarting && (
        <ConfirmDialog title="Restart Brave?" confirmLabel="Restart" onClose={() => setRestarting(false)}
                       body={'The tabs reopen at their last addresses. Anything typed into pages is lost.' + (yours ? ' You also return control.' : '')}
                       onConfirm={async () => { await browserApi.control('restart'); setRestarting(false); box.reload() }} />
      )}
    </Place>
  )
}

/* ── its tabs; off and starting states ── */
function TabsList({ box, take, restart, onOpen, onClose }: {
  box: ReturnType<typeof useResource<Box>>
  take: React.ReactNode
  restart: MenuItem
  onOpen: (t: Tab) => void
  onClose: (t: Tab) => void
}) {
  const nav = useNav()
  const [q, setQ] = useState('')
  const ctx = useContextMenu()
  const b = box.data
  const off = b?.state === 'off'
  const tabs = (b?.tabs ?? []).filter(t => `${t.title} ${t.url}`.toLowerCase().includes(q.trim().toLowerCase()))
  const menu = (t: Tab): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => onOpen(t) },
    { label: 'Copy', icon: Copy, detail: t.url, onSelect: () => copied(t.url) },
    ...(t.conversation ? [{ label: 'Open conversation', icon: MessageCircle, onSelect: () => nav.goPath(t.conversation!.link) }] : []),
    'sep', { label: 'Close tab', icon: X, danger: true, onSelect: () => onClose(t) },
  ]
  const waiting = b && notUp(b)
  return (
    <div className="list-page">
      <PlaceHead level={1} title="Tabs" count={b && !off ? b.tabs.length : undefined}
                 search={off || !b?.tabs.length ? undefined : { value: q, onChange: setQ, placeholder: 'Search tabs' }}
                 actions={!off && take} more={off ? [] : [restart]} />
      {waiting && <Notice tone={b.state === 'starting' ? 'info' : 'warn'} className="brw-notice">{waiting}</Notice>}
      <Loaded page res={box} icon={Monitor}
              empty={off ? { title: b?.reason ?? LINK.unset, action: <Button variant="primary" onClick={() => nav.go({ section: 'welcome', step: 'browser' })}>Open Setup</Button> }
                : !b?.tabs.length ? { icon: Globe, title: 'No tabs yet' }
                : !tabs.length && { icon: SearchX, title: 'No tabs match', action: <Button onClick={() => setQ('')}>Clear search</Button> }}>
        {b => (
          <div className="list-page__rows">
            <VirtualList items={tabs} estimate={68} getKey={t => t.name} label="Tabs" resetOn={q}
                         renderRow={t => (
                           <ListRow icon={Globe} iconAccent={t.name === b.front} title={tabTitle(t)} subtitle={t.url} tooltip={t.url} onClick={() => onOpen(t)}
                                    column={t.conversation ? <span>{t.conversation.title}</span> : ''}
                                    cell={t.busy ? <Dot tone="running" pulse text="Running" /> : t.name === b.front ? 'On the screen' : ''}
                                    onContextMenu={e => ctx.open(e, menu(t), tabTitle(t))}
                                    trailing={<MenuButton icon={MoreHorizontal} label="More" items={menu(t)} size="sm" />} />
                         )} />
          </div>
        )}
      </Loaded>
      {ctx.menu}
    </div>
  )
}

/* ── its calls, newest first, day by day ── */
function HistoryList() {
  const nav = useNav()
  const [q, setQ] = useState('')
  const [failed, setFailed] = useState(false)
  const [pages, setPages] = useState(1)
  const [opened, setOpened] = useState<BrowserCall | null>(null)
  const ctx = useContextMenu()
  useEffect(() => setPages(1), [q, failed])
  const list = useResource(async () => {
    const out = await browserHistory({ q: q.trim(), failed })
    for (let n = 1; n < pages && out.next; n++) {
      const r = await browserHistory({ q: q.trim(), failed, before: out.next })
      out.calls.push(...r.calls)
      out.next = r.next
    }
    return out
  }, [q, failed, pages], { debounce: q ? 250 : 0, events: ev => ev.type === 'tool.done' && typeof ev.name === 'string' && ev.name.startsWith('browser__') })
  const calls = list.data?.calls ?? []
  const dayOf = (c: BrowserCall) => dayLabel(Date.parse(c.at))
  const perDay = new Map<string, number>()
  for (const c of calls) perDay.set(dayOf(c), (perDay.get(dayOf(c)) ?? 0) + 1)
  type Row = { key: string; day: string } | { key: string; c: BrowserCall }
  const rows: Row[] = calls.flatMap((c, i) => [
    ...(i === 0 || dayOf(calls[i - 1]!) !== dayOf(c) ? [{ key: `d:${dayOf(c)}`, day: dayOf(c) }] : []),
    { key: c.id, c },
  ])
  const menu = (c: BrowserCall): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => setOpened(c) },
    { label: 'Open conversation', icon: MessageCircle, onSelect: () => nav.goPath(c.conversation.link) },
  ]
  const tone = (c: BrowserCall) => (c.state === 'failed' ? 'error' : c.state === 'running' ? 'running' : c.state === 'cancelled' ? 'muted' : 'ok') as 'error' | 'running' | 'muted' | 'ok'
  const draw = (r: Row) => 'day' in r ? <GroupHead title={r.day} count={count(perDay.get(r.day) ?? 0, 'call')} /> : (
    <ListRow dot={tone(r.c)} title={r.c.line} tooltip={r.c.line} subtitle={r.c.conversation.title} column={<span className="mono">{r.c.tool}</span>}
             cell={r.c.shots ? count(r.c.shots, 'screenshot') : ''} meta={clock(Date.parse(r.c.at))} onClick={() => setOpened(r.c)}
             onContextMenu={e => ctx.open(e, menu(r.c), r.c.line)} trailing={<MenuButton icon={MoreHorizontal} label="More" items={menu(r.c)} size="sm" />} />
  )
  const none = !!list.data && list.data.total === 0
  return (
    <div className="list-page">
      <PlaceHead level={1} title="History" count={list.data?.total} search={none ? undefined : { value: q, onChange: setQ, placeholder: 'Search history', loading: list.loading && !!q }} />
      {!none && (
        <div className="list-page__top">
          <Toolbar state={{ label: 'State', value: failed ? 'failed' : 'all', onChange: v => setFailed(v === 'failed'),
                            options: [{ value: 'all', label: 'All', count: list.data?.total }, { value: 'failed', label: 'Failed', count: list.data?.failed }] }} />
        </div>
      )}
      <Loaded page res={list} icon={History}
              empty={none ? { title: 'No calls yet' }
                : !calls.length && { icon: SearchX, title: 'No calls match', action: <Button onClick={() => { setQ(''); setFailed(false) }}>Clear search</Button> }}>
        {data => (
          <div className="list-page__rows">
            <VirtualList items={rows} estimate={i => ('day' in rows[i]! ? 56 : 68)} getKey={r => r.key} renderRow={draw} label="History" resetOn={`${q}|${failed}`}
                         onEndReached={() => { if (data.next && !list.loading) setPages(n => n + 1) }} />
          </div>
        )}
      </Loaded>
      {ctx.menu}
      {opened && (
        <Dialog title={opened.line} size="lg" onClose={() => setOpened(null)}
                actions={<Button href={opened.conversation.link}>Open conversation</Button>}>
          <BrowserShots display={opened.display} />
          {!opened.shots && <p className="brw-quiet">{opened.conversation.title}</p>}
        </Dialog>
      )}
    </div>
  )
}

/* ── a tab: the live screen, the shots of its latest call, a call to run ── */
function TabPage({ b, tab, last, active, take, yours, more, onBack, onConnect }: {
  b: Box
  tab: Tab
  last: BrowserCall | undefined
  active: boolean
  take: React.ReactNode
  yours: boolean
  more: MenuItem[]
  onBack: () => void
  onConnect: () => void
}) {
  const waiting = notUp(b)
  const fit = b.screen ? { '--ar': b.screen.w / b.screen.h } as React.CSSProperties : undefined
  return (
    <div className="brw-page">
      <PlaceHead level={1} crumbs={[{ label: 'Tabs', onClick: onBack }]} title={tabTitle(tab)} titleAttr={tab.url}
                 detail={yours ? 'You have control' : tab.busy ? 'Running' : undefined} actions={take} more={more} />
      <div className="page">
        <div className="page__col page__col--wide brw-col">
          <Screen title={tab.url || 'about:blank'} current={yours} className="brw-screen">
            <div className="brw-frame" data-fit={fit ? '' : undefined} style={fit}>
              <Live on={active && b.state === 'up'} yours={yours} onConnect={onConnect} />
              {waiting && <div className="brw-off">{waiting}</div>}
            </div>
          </Screen>
          <Console last={last} />
        </div>
      </div>
    </div>
  )
}

const TOOLS = [['open', 'Open'], ['act', 'Act'], ['scroll', 'Scroll'], ['look', 'Read'], ['close', 'Close']].map(([value, label]) => ({ value: value!, label: label! }))
/** what the args field starts with for each of its tools */
const EXAMPLE: Record<string, string> = {
  open: '{"url": "https://"}',
  act: '{"steps": [{"do": "click", "target": "e1"}]}',
  scroll: '{"direction": "down"}',
  look: '{"what": "screen"}',
  close: '{}',
}

/* its latest call's step shots (its newest call, which the place fetches again as each of
   its calls is done: the feed's copy of tool.done carries no display), and Tools: one of
   its calls, run as it runs it */
function Console({ last }: { last: BrowserCall | undefined }) {
  const { possessive } = usePronouns()
  const latest = last?.display
  const [tool, setTool] = useState('act')
  const [args, setArgs] = useState(EXAMPLE.act!)
  const [out, setOut] = useState<Awaited<ReturnType<typeof browserApi.call>> | null>(null)
  const run = useAction(async () => {
    let parsed: unknown
    try { parsed = JSON.parse(args || '{}') } catch { throw new Error('The arguments are not JSON.') }
    setOut(await browserApi.call(tool, parsed as Record<string, unknown>))
  })
  const steps = Array.isArray(latest?.steps) ? latest.steps.length : 0
  return (
    <>
      {(steps > 0 || !!latest?.shot) && <Section title="Steps" count={steps || undefined}><BrowserShots display={latest} /></Section>}
      <Section title="Tools">
        <form className="brw-console" aria-label={`Try ${possessive} calls`} onSubmit={e => { e.preventDefault(); void run.run() }}>
          <Segmented label="Tool" value={tool} options={TOOLS} onChange={t => { setTool(t); setArgs(EXAMPLE[t] ?? '{}') }} />
          <TextArea mono rows={3} max={16} aria-label="Arguments" spellCheck={false} value={args} onChange={e => setArgs(e.target.value)} />
          <div className="brw-console__foot"><Button type="submit" variant="primary" icon={Play} loading={run.busy}>Run</Button></div>
        </form>
        {run.error && <InlineError compact message={run.error} />}
        {out && !run.error && (
          <div className="brw-answer">
            <pre className={out.is_error ? 'brw-text brw-text--error' : 'brw-text'}>{out.content}</pre>
            <BrowserShots display={out.display} />
          </div>
        )}
      </Section>
    </>
  )
}

/* ── the screen: noVNC on the app's relay, while the page is shown and the box is up
   (the status on the feed says when it is); after a close it connects again with the
   backoff of the other sockets ── */

function Live({ on, yours, onConnect }: {
  on: boolean
  yours: boolean
  /** connected, maybe to a box that just restarted: its state is worth a new look */
  onConnect: () => void
}) {
  const el = useRef<HTMLDivElement>(null)
  const rfb = useRef<RFB | null>(null)
  const yoursRef = useRef(yours); yoursRef.current = yours
  const connectRef = useRef(onConnect); connectRef.current = onConnect

  useEffect(() => {
    const target = el.current
    if (!on || !target) return
    let closed = false, attempt = 0
    let retry: number | undefined
    let sock: WebSocket | null = null
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      sock = new WebSocket(`${proto}://${location.host}/ws/browser`)
      const r = new RFB(target, sock)
      r.scaleViewport = true
      r.background = 'transparent'
      r.viewOnly = !yoursRef.current
      r.addEventListener('connect', () => { attempt = 0; connectRef.current() })
      r.addEventListener('disconnect', () => {
        if (rfb.current === r) rfb.current = null
        if (closed) return
        const n = attempt++
        retry = window.setTimeout(connect, backoffMs(n))
      })
      rfb.current = r
    }
    connect()
    const unping = keepAlive(() => sock)
    // your paste: noVNC would send Ctrl+V as two bare keys, so this browser keeps it and
    // fires paste with your clipboard's text; the text goes to Xvnc's clipboard, then
    // Ctrl+V to its browser. While you watch, noVNC sends neither
    const keepPaste = (e: KeyboardEvent) => { if (e.ctrlKey && e.code === 'KeyV') e.stopPropagation() }
    const paste = (e: ClipboardEvent) => {
      const text = e.clipboardData?.getData('text/plain')
      const r = rfb.current
      if (!text || !r) return
      e.preventDefault()
      r.clipboardPasteFrom(text)
      r.sendKey(XK_CONTROL_L, 'ControlLeft', true)
      r.sendKey(XK_V, 'KeyV')
      r.sendKey(XK_CONTROL_L, 'ControlLeft', false)
    }
    target.addEventListener('keydown', keepPaste, true)
    target.addEventListener('paste', paste)
    return () => {
      closed = true
      clearTimeout(retry); unping()
      target.removeEventListener('keydown', keepPaste, true)
      target.removeEventListener('paste', paste)
      rfb.current?.disconnect()
    }
  }, [on])

  // your keys and mouse go to its browser only while it is yours
  useEffect(() => {
    if (!rfb.current) return
    rfb.current.viewOnly = !yours
    if (yours) rfb.current.focus()
  }, [yours])

  return <div ref={el} className="brw-live" aria-label="The browser's screen" />
}
