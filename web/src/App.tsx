import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { DoorOpen, Ellipsis, Rat, Settings as SettingsIcon, Snowflake } from 'lucide-react'
import './components/Shell.css'
import './components/App.css'
import { api, authApi, type ConfigStatus } from './lib/api'
import { securityApi } from './lib/passkey'
import { useServerEvents } from './lib/bus'
import { fullDate } from './lib/format'
import { useFocusHandback, useGlobalKeys, useWindowTitle } from './lib/shell'
import { useNotices } from './lib/notify'
import { knowBuild } from './lib/health'
import { HerContext } from './lib/her'
import { NameContext, PronounsContext } from './lib/name'
import { AppearanceContext, useAppearanceSettings } from './lib/appearance'
import { SpeechProvider } from './components/Speech'
import { NavProvider, format, useNav, type Section } from './lib/nav'
import { ConnectionProvider, changesApi } from './lib/socket'
import { useLostLogins } from './lib/sources'
import { useRoutinesAttention } from './lib/routines'
import { useMissionsWaiting } from './lib/missions'
import type { RowState, Work } from './components/ConversationList'
import { WaitingCount, isIntercomEvent } from './lib/intercom'
import { useResource } from './lib/useResource'
import { useSlide } from './ui/slide'
import { SectionBoundary } from './components/SectionBoundary'
import { ServerDown } from './components/ServerDown'
import { Portrait } from './components/Portrait'
import { VaultBanner } from './components/VaultBanner'
import { Shortcuts, Switcher } from './components/Switcher'
import { Button, ConfirmDialog, ICON, isNarrow, isPhone, ListRow, LoadingRow, Sheet, TipLayer, ToastHost, cx } from './ui'
import { guardApi, useFrozen } from './lib/guard'
import { settingsApi, type Home } from './lib/settingsApi'
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

const WORK_WORD: Record<RowState, string> = { running: 'Running', failed: 'Failed', needs_you: 'Waiting for you' }
/** the rail entry of each place a conversation lives in (server/celine/homes.py) */
const HOME_SECTION: Record<Home, Section> = { her: 'celine', missions: 'missions', routines: 'routines' }

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

  // the config status is an extra: when it fails, the app still works, so a failure
  // leaves it out instead of showing an error
  const loadStatus = useCallback(async () => {
    const s = await api.configStatus()
    knowBuild(s.build.commit)
    setStatus(s)
  }, [])
  useEffect(() => { void loadStatus().catch(() => undefined).finally(() => setStatusIn(true)) }, [loadStatus])
  // a settings file fixed or broken on disk shows on General at once
  useServerEvents(ev => { if (ev.type === 'settings.changed' || ev.type === 'her.changed') void loadStatus().catch(() => undefined) })
  // the onboarding, until it was finished once: every other URL lands there
  useEffect(() => {
    if (status?.setup === false && section !== 'welcome') nav.go({ section: 'welcome' }, { replace: true })
  }, [status?.setup, section, nav])
  // the places of the features that are on (the server's list); none yet: all of them
  const places = shownPlaces(status?.catalog, status?.features)
  const bar = BAR.filter(id => places.some(p => p.id === id))

  // the door out shows only while the login is on (never on a local run without one)
  const [login, setLogin] = useState(false)
  const [leaving, setLeaving] = useState(false)
  useEffect(() => { void authApi.status().then(s => setLogin(s.login), () => undefined) }, [])
  const logout = () => {
    setLeaving(true)
    securityApi.logout().catch(() => location.replace('/'))
  }

  // the rail's count of what waits for you, the server's: asked again on intercom.state,
  // taken from `changes` after a reconnect
  const waiting = useResource(() => changesApi.get(null).then(c => c.unread), [], { events: isIntercomEvent })
  useServerEvents(ev => { if (ev.type === 'changes') waiting.set(ev.unread as number) })
  const unread = waiting.data ?? 0
  // the approvals that wait, the server's: a dot on the place each one's conversation lives
  // in (unless it is the page on screen), which opens it, and the count in the window's title
  const asks = useResource(() => settingsApi.approvals(), [], { events: ev => ev.type === 'approvals.changed' })
  const asked = asks.data?.count ?? 0
  const onScreen = format(nav.route)
  const askedIn = (id: Section) =>
    asks.data?.approvals.find(a => a.home && a.link && HOME_SECTION[a.home] === id && a.link !== onScreen)

  // the window's title, on every page: the waiting count first (not in Setup, which has no
  // rail), then the page's own name (a terminal, an artifact: lib/title; Celine's place: its
  // name, views/Celine) or the section's
  const pageName = usePageName()
  const own = terminal || artifact || section === 'celine' ? pageName : ''
  useWindowTitle(own || (section === 'celine' ? name : `${TITLE[section]}${name ? ` - ${name}` : ''}`), section === 'welcome' ? 0 : unread + asked)

  // a source that lost its login: a dot on Settings
  const lost = useLostLogins()
  // a run at work or one that failed in the last two nights: a dot on Routines (the server's)
  const routines = useRoutinesAttention()
  const missionsWaiting = useMissionsWaiting()
  // what each place has going on, from its list: a dot on the other place's rail item
  const [work, setWork] = useState<Work>({})
  const onWork = useCallback((w: Work) => setWork(was => (JSON.stringify(was) === JSON.stringify(w) ? was : w)), [])

  // its place's list beside the chat; its toggle is in the list's head. A phone lands
  // on the list when no conversation is open; a narrow window starts with it closed.
  const [list, setList] = useState(() =>
    isPhone() ? !(nav.route.section === 'celine' && nav.route.conversation) : !isNarrow())

  // Bastet froze her (or you did): one state in the rail, on every place, with Unfreeze
  const frozen = useFrozen()

  // the keys of the whole app: Ctrl+K, Ctrl+Shift+O, ?
  const [sheet, setSheet] = useState<'switch' | 'keys' | 'more' | 'frozen' | null>(null)
  useGlobalKeys(nav, { goTo: () => setSheet(s => (s === 'switch' ? null : 'switch')), keys: () => setSheet('keys') })
  useEffect(() => { setSheet(s => (s === 'more' ? null : s)) }, [section])

  useFocusHandback()
  // system notifications while the tab is hidden (lib/notify.ts)
  useNotices(nav.goPath, status?.owner.portrait?.url)
  // the heads' Go to
  useServerEvents(ev => { if (ev.type === 'ui.goto') setSheet('switch') })

  const portrait = status?.owner.portrait
  const rail = useRef<HTMLElement>(null)
  useSlide(rail, '.rail__item[aria-current="page"]', section)

  // the brand mark says which build this is (hover, and the accessible name)
  const built = status?.build
  const build = built ? `${name}\n${built.commit}${built.built ? `, built ${fullDate(built.built)}` : ''}` : name
  const labelOf = (id: Section) => (id === 'celine' ? name : TITLE[id])
  const go = (id: Section) => (e: React.MouseEvent) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return
    e.preventDefault()
    const at = askedIn(id)?.link
    if (at) nav.goPath(at)
    else nav.goSection(id)
  }
  const badge = (id: Section) => {
    if (id === 'intercom' && unread > 0) return <span className="rail__count" aria-hidden>{unread > 99 ? '99+' : unread}</span>
    if (askedIn(id)) return <span className="rail__dot rail__dot--needs_you" aria-label={WORK_WORD.needs_you} />
    if (id === 'settings' && lost.length > 0) return <span className="rail__dot" aria-label={`${lost.length} to fix`} />
    if (id === 'routines' && routines && id !== section) return <span className={`rail__dot rail__dot--${routines}`} aria-label={routines === 'running' ? 'Running' : 'Failed'} />
    const w = id === section ? undefined : id === 'celine' ? work.celine : id === 'missions' && missionsWaiting ? 'needs_you' : undefined
    return w ? <span className={`rail__dot rail__dot--${w}`} aria-label={WORK_WORD[w]} /> : null
  }
  const railItem = (s: RailEntry, cls?: string) => (
    <a key={s.id} className={cx('rail__item', cls)} href={sectionHref(s.id)}
       aria-label={s.id === 'intercom' && unread > 0 ? `${labelOf(s.id)}, ${unread} waiting` : undefined}
       aria-current={section === s.id ? 'page' : undefined} onClick={go(s.id)}>
      <s.icon size={ICON.lg} aria-hidden />
      <span className="rail__label">{labelOf(s.id) || (!statusIn && <span className="rail__wait" />)}</span>
      {badge(s.id)}
    </a>
  )
  const moreHere = !bar.includes(section) && section !== 'artifact'
  const shown = {
    status, name, terminal: shownTerminal.current, reply, artifact,
    celine: { sidebarOpen: list, onSidebar: setList, onWork },
    onStatus: () => void loadStatus().catch(() => undefined),
  }

  return (
    <HerContext.Provider value={status?.owner ?? null}>
    <NameContext.Provider value={name}>
    <PronounsContext.Provider value={status?.owner.pronouns ?? 'she'}>
    <AppearanceContext.Provider value={appearance}>
    <WaitingCount.Provider value={waiting.data ?? null}>
    <SpeechProvider features={status?.features}>
    <div className="app">
      {section === 'welcome' ? (
        <Suspense fallback={<LoadingRow />}><Welcome status={status} onSaved={shown.onStatus} /></Suspense>
      ) : (<>
      <VaultBanner locked={!!status?.vault_locked} />
      <div className="frame">
        <nav ref={rail} className="rail" aria-label="Sections">
          {/* its face when it has one, the app's mark until then */}
          <div className="rail__brand" title={portrait ? undefined : build}>
            {portrait ? <Portrait className="rail__face" /> : <Rat size={ICON.lg} aria-hidden />}
          </div>
          {places.map(p => railItem(p, bar.includes(p.id) ? undefined : 'rail__item--more'))}
          {frozen && (
            <Button variant="ghost" className="rail__item rail__frozen" title={frozen.why} aria-haspopup="dialog"
                    onClick={() => setSheet('frozen')}>
              <Snowflake size={ICON.lg} aria-hidden />
              <span className="rail__label">Frozen</span>
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
            {places.some(p => !bar.includes(p.id) && askedIn(p.id))
              ? <span className="rail__dot rail__dot--needs_you" aria-label={WORK_WORD.needs_you} />
              : lost.length > 0 ? <span className="rail__dot" aria-label={`${lost.length} to fix`} />
              : routines && section !== 'routines' ? <span className={`rail__dot rail__dot--${routines}`} aria-label={routines === 'running' ? 'Running' : 'Failed'} /> : null}
          </Button>
        </nav>

        <div className="sections">
          {VIEWS.map(v => {
            const el = visited.has(v.id) && v.view({ ...shown, active: section === v.id })
            return el && (
              <section key={v.id} className="view" hidden={section !== v.id} aria-label={labelOf(v.id)}>
                <SectionBoundary><Suspense fallback={<LoadingRow />}>{el}</Suspense></SectionBoundary>
              </section>
            )
          })}
        </div>
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
            {[...places.filter(p => !bar.includes(p.id)), { id: 'settings' as const, icon: SettingsIcon }].map(p => (
              <ListRow key={p.id} href={sectionHref(p.id)} selected={section === p.id}
                       leading={<p.icon size={ICON.lg} aria-hidden />} title={labelOf(p.id)}
                       meta={badge(p.id)}
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

      {/* the one tooltip layer: every title and data-tip in the app */}
      <TipLayer />
      <ToastHost />
      {/* the server is gone: one block over everything, until it is back */}
      <ServerDown />
    </div>
    </SpeechProvider>
    </WaitingCount.Provider>
    </AppearanceContext.Provider>
    </PronounsContext.Provider>
    </NameContext.Provider>
    </HerContext.Provider>
  )
}
