import { Fragment, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { DoorOpen, Ellipsis, Flag, Hand, Mail, PauseCircle, Plug, Rat, Settings as SettingsIcon, Snowflake } from 'lucide-react'
import './components/Shell.css'
import './components/App.css'
import { api, authApi, type ConfigStatus } from './lib/api'
import { securityApi } from './lib/passkey'
import { useServerEvents } from './lib/bus'
import { fullDate } from './lib/format'
import { useFocusHandback, useGlobalKeys, useWindowTitle } from './lib/shell'
import { useNotices } from './lib/notify'
import { knowBuild } from './lib/health'
import { AgentContext } from './lib/agent'
import { NameContext } from './lib/name'
import { AppearanceContext, useAppearanceSettings } from './lib/appearance'
import { SpeechProvider } from './components/Speech'
import { NavProvider, format, useNav, type Section } from './lib/nav'
import { ConnectionProvider } from './lib/socket'
import { useWaits } from './lib/waits'
import { ApprovalCard } from './components/ApprovalCard'
import { WaitingCount } from './lib/intercom'
import { useSlide } from './ui/slide'
import { SectionBoundary } from './components/SectionBoundary'
import { ServerDown } from './components/ServerDown'
import { VaultBanner } from './components/VaultBanner'
import { Shortcuts, Switcher } from './components/Switcher'
import { isTyping } from './lib/keys'
import { Button, ConfirmDialog, ICON, coarse, isNarrow, isPhone, ListRow, ListSection, LoadingRow, Sheet, TipLayer, ToastHost, cx, ms, reduced } from './ui'
import { guardApi, useFrozen } from './lib/guard'
import { usePageName } from './lib/title'
import { BAR, TITLE, VIEWS, Welcome, sectionHref, shownPlaces, type RailEntry } from './routes'

export function App() {
  return (
    <NavProvider>
      {/* one socket for everything; views listen through lib/bus */}
      <ConnectionProvider>
        <Shell />
      </ConnectionProvider>
    </NavProvider>
  )
}

/** a row of Waiting for you, by what waits */
const WAIT_ICON = { mission: Flag, login: Plug, thread: Mail } as const

function Shell() {
  const nav = useNav()
  const section = nav.route.section
  // /terminal/<id>: the terminal page in place of the Terminals list; `shownTerminal`:
  // while another place is on screen, the one the section showed last
  const terminal = nav.route.section === 'terminals' ? nav.route.terminal : undefined
  const shownTerminal = useRef<string | undefined>(undefined)
  if (nav.route.section === 'terminals') shownTerminal.current = terminal
  const reply = nav.route.section === 'terminals' ? nav.route.reply : undefined
  const artifact = nav.route.section === 'artifact' ? nav.route.id : undefined
  const [status, setStatus] = useState<ConfigStatus | null>(null)
  // the status answered or failed: its rail label stops waiting
  const [statusIn, setStatusIn] = useState(false)
  const appearance = useAppearanceSettings()
  // its name is the app's first place and every page outside a conversation
  const name = status?.owner.name ?? ''
  // sections mount on first visit and then stay mounted, so their state survives
  const [visited, setVisited] = useState<Set<Section>>(() => new Set([section]))
  useEffect(() => { setVisited(v => v.has(section) ? v : new Set(v).add(section)) }, [section])
  // a move to another section (not the first load): its title takes the focus, so Tab goes
  // on from there, and the place is announced. Never from a field being typed in, never on
  // a touch screen (a focus there can raise the keyboard); a view that took the focus
  // itself (the composer) keeps it
  const [announced, setAnnounced] = useState('')
  const firstSection = useRef(true)
  useEffect(() => {
    if (firstSection.current) { firstSection.current = false; return }
    // a view that loads on its first visit draws its title a moment later
    const view = document.querySelector<HTMLElement>(`.sections > .view[data-section="${section}"]`)
    setAnnounced(view?.getAttribute('aria-label') ?? '')
    const place = () => {
      const title = view?.querySelector<HTMLElement>('h1')
      if (!view || !title) return false
      if (!coarse() && !isTyping(document.activeElement) && !view.contains(document.activeElement)
          && !document.querySelector('[aria-modal="true"]')) {
        title.tabIndex = -1
        title.focus({ preventScroll: true })
      }
      return true
    }
    if (!view || place()) return
    const watch = new MutationObserver(() => { if (place()) watch.disconnect() })
    watch.observe(view, { childList: true, subtree: true })
    return () => watch.disconnect()
  }, [section])

  // the config status is an extra: when it fails, the app still works, so a failure
  // leaves it out instead of showing an error
  const loadStatus = useCallback(async () => {
    const s = await api.configStatus()
    knowBuild(s.build.commit)
    setStatus(s)
  }, [])
  useEffect(() => { void loadStatus().catch(() => undefined).finally(() => setStatusIn(true)) }, [loadStatus])
  // a settings file fixed or broken on disk shows on General at once
  useServerEvents(ev => { if (ev.type === 'settings.changed' || ev.type === 'agent.changed' || ev.type === 'agent.hold') void loadStatus().catch(() => undefined) })
  // the onboarding, until it was finished once: every other URL lands there
  useEffect(() => {
    if (status?.setup === false && section !== 'welcome') nav.go({ section: 'welcome' }, { replace: true })
  }, [status?.setup, section, nav])
  // the places of the features that are on (the server's list); none yet: all of them
  const places = shownPlaces(status?.places_off)
  const bar = BAR.filter(id => places.some(p => p.id === id))

  // the door out shows only while the login is on (never on a local run without one)
  const [login, setLogin] = useState(false)
  const [leaving, setLeaving] = useState(false)
  useEffect(() => { void authApi.status().then(s => setLogin(s.login), () => undefined) }, [])
  const logout = () => {
    setLeaving(true)
    securityApi.logout().catch(() => location.replace('/'))
  }

  // everything that waits for you, the server's (lib/waits.ts): the rail's Waiting, its
  // sheet, the window's title; the Intercom's Waiting for you counts its threads
  const waits = useWaits()
  const waiting = waits.data?.count ?? 0
  const threads = waits.data ? waits.data.items.filter(i => i.kind === 'thread').length : null

  // the window's title, on every page: the waiting count first (not in Setup, which has no
  // rail), then the page's own name (a terminal, an artifact: lib/title; Celine's place: its
  // name, views/Celine) or the section's
  const pageName = usePageName()
  const own = terminal || artifact || section === 'celine' ? pageName : ''
  useWindowTitle(own || (section === 'celine' ? name : `${TITLE[section]}${name ? ` - ${name}` : ''}`), section === 'welcome' ? 0 : waiting)

  // its place's list beside the chat; its toggle is in the list's head. A phone lands
  // on the list when no conversation is open; a narrow window starts with it closed.
  const [list, setList] = useState(() =>
    isPhone() ? !(nav.route.section === 'celine' && nav.route.conversation) : !isNarrow())

  // Bastet froze the agent (or you did): one state in the rail, on every place, with Unfreeze;
  // paused (no model of the agent's answers): one more, which opens Settings, Models and its words
  const frozen = useFrozen()
  const paused = !frozen && status?.hold === 'paused'

  // the keys of the whole app: Ctrl+K, Ctrl+Shift+O, ?
  const [sheet, setSheet] = useState<'switch' | 'keys' | 'more' | 'frozen' | 'waiting' | null>(null)
  useGlobalKeys(nav, { goTo: () => setSheet(s => (s === 'switch' ? null : 'switch')), keys: () => setSheet('keys') })
  useEffect(() => { setSheet(s => (s === 'more' ? null : s)) }, [section])
  // Open in Waiting for you: the page changing closes the sheet (closing it first would take
  // its history step back after the new page's step)
  const at = format(nav.route)
  useEffect(() => { setSheet(s => (s === 'waiting' ? null : s)) }, [at])
  // the last item answered: nothing waits, the sheet closes
  const none = waiting === 0
  useEffect(() => { if (none) setSheet(s => (s === 'waiting' ? null : s)) }, [none])

  useFocusHandback()
  // system notifications while the tab is hidden (lib/notify.ts)
  useNotices(nav.goPath, status?.owner.portrait?.url)
  // the heads' Go to
  useServerEvents(ev => { if (ev.type === 'ui.goto') setSheet('switch') })

  const rail = useRef<HTMLElement>(null)
  useSlide(rail, '.rail__item[aria-current="page"]', section)

  // switching place: the place left stays painted under the one fading in until the fade
  // is done, so the canvas never shows between them
  const [under, setUnder] = useState<Section | null>(null)
  const lastSection = useRef(section)
  useLayoutEffect(() => {
    const from = lastSection.current
    lastSection.current = section
    if (from === section || reduced()) return
    setUnder(from)
    const t = window.setTimeout(() => setUnder(null), ms('--dur'))
    return () => window.clearTimeout(t)
  }, [section])

  // the brand mark says which build this is (hover, and the accessible name)
  const built = status?.build
  const build = built ? `${name}\n${built.commit}${built.built ? `, built ${fullDate(built.built)}` : ''}` : name
  const labelOf = (id: Section) => (id === 'celine' ? name : TITLE[id])
  const go = (id: Section) => (e: React.MouseEvent) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return
    e.preventDefault()
    nav.goSection(id)
  }
  const count = (n: number) => <span className="rail__count" aria-hidden>{n > 99 ? '99+' : n}</span>
  const openWait = (link: string) => { if (link === at) setSheet(null); else nav.goPath(link) }
  const railItem = (s: RailEntry, cls?: string) => (
    <a key={s.id} className={cx('rail__item', cls)} href={sectionHref(s.id)}
       aria-current={section === s.id ? 'page' : undefined} onClick={go(s.id)}>
      <s.icon size={ICON.lg} aria-hidden />
      <span className="rail__label">{labelOf(s.id) || (!statusIn && <span className="rail__wait" />)}</span>
    </a>
  )
  const moreHere = !bar.includes(section) && section !== 'artifact'
  const shown = {
    status, name, terminal: shownTerminal.current, reply, artifact,
    celine: { sidebarOpen: list, onSidebar: setList },
    onStatus: () => void loadStatus().catch(() => undefined),
  }

  return (
    <AgentContext.Provider value={status?.owner ?? null}>
    <NameContext.Provider value={name}>
    <AppearanceContext.Provider value={appearance}>
    <WaitingCount.Provider value={threads}>
    <SpeechProvider features={status?.features}>
    <div className="app">
      {section === 'welcome' ? (
        <Suspense fallback={<LoadingRow />}><Welcome status={status} onSaved={shown.onStatus} /></Suspense>
      ) : (<>
      <VaultBanner locked={!!status?.vault_locked} />
      <div className="frame">
        <nav ref={rail} className="rail" aria-label="Sections">
          {/* its face when it has one, the app's mark until then */}
          <div className="rail__brand" title={build}>
            <Rat size={ICON.lg} aria-hidden />
          </div>
          {places.map(p => railItem(p, bar.includes(p.id) ? undefined : 'rail__item--more'))}
          {frozen && (
            <Button variant="ghost" className="rail__item rail__frozen" title={frozen.why} aria-haspopup="dialog"
                    onClick={() => setSheet('frozen')}>
              <Snowflake size={ICON.lg} aria-hidden />
              <span className="rail__label">Frozen</span>
            </Button>
          )}
          {paused && (
            <Button variant="ghost" className="rail__item" href="/settings/models"
                    onClick={e => { e.preventDefault(); nav.goPath('/settings/models') }}>
              <PauseCircle size={ICON.lg} aria-hidden />
              <span className="rail__label">Paused</span>
            </Button>
          )}
          {waiting > 0 && (
            <Button variant="ghost" className="rail__item rail__item--more" title="Waiting for you"
                    aria-label={`Waiting, ${waiting}`} aria-haspopup="dialog" onClick={() => setSheet('waiting')}>
              <Hand size={ICON.lg} aria-hidden />
              <span className="rail__label">Waiting</span>
              {count(waiting)}
            </Button>
          )}
          <div className="rail__spacer" />
          {railItem({ id: 'settings', icon: SettingsIcon }, 'rail__item--more')}
          {login && (
            <Button variant="ghost" className="rail__item rail__door rail__item--more" onClick={logout} disabled={leaving}>
              <DoorOpen size={ICON.lg} aria-hidden />
              <span className="rail__label">Log out</span>
            </Button>
          )}
          {/* the phone's fifth slot: everything else */}
          <Button variant="ghost" className="rail__item rail__more" aria-current={moreHere ? 'page' : undefined}
                  aria-haspopup="dialog" onClick={() => setSheet('more')}>
            <Ellipsis size={ICON.lg} aria-hidden />
            <span className="rail__label">More</span>
            {waiting > 0 && count(waiting)}
          </Button>
        </nav>

        <div className="sections">
          {VIEWS.map(v => {
            const el = visited.has(v.id) && v.view({ ...shown, active: section === v.id })
            return el && (
              <section key={v.id} className={cx('view', under === v.id && section !== v.id && 'view--under')} data-section={v.id}
                       hidden={section !== v.id && under !== v.id} inert={section !== v.id || undefined} aria-label={labelOf(v.id)}>
                <SectionBoundary><Suspense fallback={<LoadingRow />}>{el}</Suspense></SectionBoundary>
              </section>
            )
          })}
        </div>
        <p className="sr-only" aria-live="polite">{announced}</p>
      </div>
      </>)}

      {sheet === 'switch' && <Switcher onClose={() => setSheet(null)} />}
      {sheet === 'keys' && <Shortcuts onClose={() => setSheet(null)} />}
      {sheet === 'frozen' && frozen && (
        <ConfirmDialog title="Frozen" body={frozen.why} confirmLabel="Unfreeze"
                       onConfirm={async () => { await guardApi.unfreeze(); setSheet(null) }} onClose={() => setSheet(null)} />
      )}
      {sheet === 'more' && (
        <Sheet title="More" onClose={() => setSheet(null)}>
          <nav className="more" aria-label="More sections">
            {waiting > 0 && (
              <ListRow leading={<Hand size={ICON.lg} aria-hidden />} title="Waiting" meta={count(waiting)}
                       onClick={() => setSheet('waiting')} />
            )}
            {[...places.filter(p => !bar.includes(p.id)), { id: 'settings' as const, icon: SettingsIcon }].map(p => (
              <ListRow key={p.id} href={sectionHref(p.id)} selected={section === p.id}
                       leading={<p.icon size={ICON.lg} aria-hidden />} title={labelOf(p.id)}
                       // the place changing closes the sheet (the effect above): closing it first
                       // would take its history step back after the new place's step
                       onClick={e => { if (p.id !== section) go(p.id)(e); else { e.preventDefault(); setSheet(null) } }} />
            ))}
            {login && (
              <ListRow leading={<DoorOpen size={ICON.lg} aria-hidden />} title="Log out" onClick={logout} />
            )}
          </nav>
        </Sheet>
      )}

      {sheet === 'waiting' && (
        <Sheet title="Waiting for you" size="lg" onClose={() => setSheet(null)}>
          <div>
            {(waits.data?.items ?? []).map((w, i, all) => (
              <Fragment key={w.kind === 'approval' ? `a:${w.approval.id}` : `${w.kind}:${w.id}`}>
                {w.group !== all[i - 1]?.group && <ListSection label={w.group} />}
                {w.kind === 'approval'
                  ? <ApprovalCard ask={w.approval} onOpen={openWait} />
                  : <ListRow density="lg" icon={WAIT_ICON[w.kind]} title={w.title} subtitle={w.line || undefined}
                             href={w.link ?? undefined}
                             onClick={w.link ? e => { e.preventDefault(); openWait(w.link!) } : undefined} />}
              </Fragment>
            ))}
          </div>
        </Sheet>
      )}
      {/* the one tooltip layer: every title and data-tip in the app */}
      <TipLayer />
      <ToastHost />
      {/* the server is gone: one block over everything, until it is back */}
      <ServerDown />
    </div>
    </SpeechProvider>
    </WaitingCount.Provider>
    </AppearanceContext.Provider>
    </NameContext.Provider>
    </AgentContext.Provider>
  )
}
