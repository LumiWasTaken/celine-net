/* Settings: a list on the left, one page on the right (like system settings). No tabs.
   /settings/<page>[/<sub>]; on a phone the list is the first screen and a row opens
   its page (with a back arrow). Setup (the onboarding, run again) comes first, then General,
   then its page (named after it); Advanced (the time limits and sizes) is last.
   Sources are rows
   of their own, so 20 sources are 20 calm rows: glyph, name, a dot (amber when there
   is something to fix). */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Bell, Bot, Gauge, Heart, Images, ListChecks, LockKeyhole, Mail, Palette, Puzzle, ShieldAlert, ShieldCheck, SlidersHorizontal, Sparkles, ToggleRight, Users, Wrench,
  type LucideIcon,
} from 'lucide-react'
import { authApi, type AuthStatus, type ConfigStatus } from '../../lib/api'
import { useResource } from '../../lib/useResource'
import { format, useNav, type Route } from '../../lib/nav'
import { useSources, type Source } from '../../lib/sources'
import { LINK } from '../../lib/settingsApi'
import { Dot, EmptyState, ICON, InlineError, PlaceHead, ListRow, ListSection, SearchField, SourceGlyph, VirtualList } from '../../ui'
import { Page, StatusDot } from './parts'
import { Advanced, General } from './General'
import { Features } from './Features'
import { HerPage, HerText } from './Her'
import { Appearance } from './Appearance'
import { Notifications } from './Notifications'
import { SourcePage } from './SourcePage'
import { People } from './People'
import { SearchAI } from './SearchAI'
import { Models } from './Models'
import { Media } from './Media'
import { Tools } from './Tools'
import { BastetText, Guard } from './Guard'
import { Email } from './Email'
import { Vault } from './Vault'
import { Security } from './Security'
import { FormPage } from './FormPage'
import './settings.css'
import { nothingMatches } from '../../lib/format'

type PageId = 'general' | 'agent' | 'features' | 'guard' | 'appearance' | 'notifications' | 'sources' | 'people' | 'search' | 'models' | 'media' | 'tools' | 'email' | 'vault' | 'security' | 'advanced'

const PAGES: { id: PageId; label: string; icon: LucideIcon }[] = [
  { id: 'general', label: 'General', icon: SlidersHorizontal },
  { id: 'agent', label: '', icon: Heart },
  { id: 'features', label: 'Features', icon: ToggleRight },
  { id: 'guard', label: 'Guard', icon: ShieldAlert },
  { id: 'appearance', label: 'Appearance', icon: Palette },
  { id: 'notifications', label: 'Notifications', icon: Bell },
  { id: 'people', label: 'People', icon: Users },
  { id: 'search', label: 'Search index', icon: Sparkles },
  { id: 'models', label: 'Models', icon: Bot },
  { id: 'media', label: 'Images & Music', icon: Images },
  { id: 'tools', label: 'Tools', icon: Wrench },
  { id: 'email', label: 'Email', icon: Mail },
  { id: 'vault', label: 'Vault', icon: LockKeyhole },
  { id: 'security', label: 'Security', icon: ShieldCheck },
  { id: 'advanced', label: 'Advanced', icon: Gauge },
]
const KNOWN = new Set<string>(['sources', ...PAGES.map(p => p.id)])
/** the Settings pages whose every owner is off (`catalog[].pages`, GET /status `features`):
    a page leaves the list (Images & Music stays while Images or Music is on), a source as
    "source:<platform>" */
const offPages = (status: ConfigStatus | null) => {
  const catalog = status?.catalog ?? []
  const on = new Set(catalog.filter(f => status?.features?.[f.id] !== false).flatMap(f => f.pages ?? []))
  return new Set(catalog.flatMap(f => f.pages ?? []).filter(page => !on.has(page)))
}
const WITH_SUB = new Set<string>(['sources', 'models'])
/** the pages of the features with a form (a plugin's settings.toml, /api/forms/<id>), named after them */
const formPages = (status: ConfigStatus | null) =>
  (status?.catalog ?? []).filter(f => f.form).map(f => ({ id: f.form, label: f.label }))
const FIRST: PageId = 'general'

type Item =
  | { type: 'page'; id: PageId | string; label: string; icon: LucideIcon }
  /** the onboarding, run again (/welcome/her) */
  | { type: 'setup' }
  | { type: 'section'; label: string }
  | { type: 'source'; source: Source }
  | { type: 'filter' }
  | { type: 'none'; query: string }

// the source filter appears only when the list would get long
const FILTER_FROM = 9

export function Settings({ active, status }: { active: boolean; status: ConfigStatus | null }) {
  const nav = useNav()
  // the page the URL names; while another place is on screen, the one it showed last
  const last = useRef<Extract<Route, { section: 'settings' }>>({ section: 'settings' })
  if (nav.route.section === 'settings') last.current = nav.route
  const route = last.current
  const page = route.page
  const sources = useSources()
  const auth = useResource(() => authApi.status(), []).data ?? null
  const [filter, setFilter] = useState('')

  // coming back to Settings shows fresh states (a login or a sync may have changed meanwhile)
  useEffect(() => { if (active) sources.reload() }, [active]) // eslint-disable-line react-hooks/exhaustive-deps

  // a dead path (an unknown page, a sub-page where there are none, Security while the
  // login is off) goes to the Settings start, replacing the URL
  const forms = useMemo(() => formPages(status), [status])
  const known = (id: string) => KNOWN.has(id) || forms.some(f => f.id === id)
  useEffect(() => {
    if (!active || !page || !status) return
    if (!known(page) || (route.sub && !WITH_SUB.has(page)) || (page === 'security' && auth && !auth.login)) {
      nav.go({ section: 'settings' }, { replace: true })
    }
  }, [active, page, route.sub, auth, nav, status]) // eslint-disable-line react-hooks/exhaustive-deps

  const off = useMemo(() => offPages(status), [status])
  const list = (sources.data?.sources ?? []).filter(s => !off.has(`source:${s.platform}`))
  const people = list.some(s => s.level !== 'off' || (s.kb.messages ?? 0) > 0)
  const showFilter = list.length >= FILTER_FROM
  const items = useMemo<Item[]>(() => {
    const q = filter.trim().toLowerCase()
    const shown = q ? list.filter(s => s.title.toLowerCase().includes(q)) : list
    const out: Item[] = [{ type: 'setup' }, { type: 'page', ...PAGES[0]! }, { type: 'page', ...PAGES[1]!, label: status?.owner.name ?? '' }, { type: 'page', ...PAGES[2]! }]
    // the Sources heading only when there is something under it
    if (list.length > 0) out.push({ type: 'section', label: 'Sources' })
    if (showFilter) out.push({ type: 'filter' })
    for (const s of shown) out.push({ type: 'source', source: s })
    if (q && shown.length === 0) out.push({ type: 'none', query: filter })
    out.push({ type: 'section', label: 'App' })
    for (const p of PAGES.slice(3)) {
      if ((p.id === 'security' && !auth?.login) || off.has(p.id)) continue
      if (p.id === 'advanced') for (const f of forms) if (!off.has(f.id)) out.push({ type: 'page', ...f, icon: Puzzle })
      out.push({ type: 'page', ...p })
    }
    return out
  }, [list, filter, showFilter, auth, status, off, forms])

  // the page on screen: none chosen means General on a wide screen, the list on a phone
  const current = page && known(page) ? page : undefined
  const openSource = route.sub ?? list[0]?.id
  const selected = (it: Item) =>
    it.type === 'page' ? (current ?? FIRST) === it.id
      : it.type === 'source' ? current === 'sources' && openSource === it.source.id : false

  return (
    <div className="settings" data-pane={current ? 'page' : 'list'}>
      <aside className="settings__nav" aria-label="Settings">
        <PlaceHead title="Settings" />
        {sources.error && !sources.data && (
          <div className="settings__nav-error"><InlineError compact message={sources.error} onRetry={sources.reload} /></div>
        )}
        <VirtualList items={items} label="Settings pages" inset={8} className="settings__list"
                     getKey={(it, i) => it.type === 'source' ? `s:${it.source.id}` : it.type === 'page' ? it.id : `${it.type}:${i}`}
                     estimate={i => { const it = items[i]; return it?.type === 'section' ? 36 : it?.type === 'filter' ? 48 : 44 }}
                     renderRow={it => {
                       if (it.type === 'section') return <ListSection label={it.label} />
                       if (it.type === 'filter') {
                         return <div className="settings__filter"><SearchField size="sm" value={filter} onChange={setFilter} placeholder="Search sources" /></div>
                       }
                       if (it.type === 'none') return <EmptyState compact>{nothingMatches(it.query)}</EmptyState>
                       if (it.type === 'setup') {
                         return <ListRow href={format({ section: 'welcome', step: 'her' })} title="Setup"
                                         leading={<span className="settings__icon"><ListChecks size={ICON.md} aria-hidden /></span>} />
                       }
                       if (it.type === 'source') {
                         const s = it.source
                         return (
                           <ListRow href={format({ section: 'settings', page: 'sources', sub: s.id })}
                                    selected={selected(it)} tooltip={s.sentence || undefined}
                                    leading={<span className="settings__icon"><SourceGlyph source={s.platform} label={s.title} /></span>}
                                    title={s.title}
                                    meta={s.problems > 0
                                      ? <Dot tone="warn" label={`${s.problems} to fix`} />
                                      : s.level === 'off' ? <span className="settings__off">{LINK.down}</span>
                                      : <StatusDot level={s.level} />} />
                         )
                       }
                       return (
                         <ListRow href={format({ section: 'settings', page: it.id })} selected={selected(it)}
                                  leading={<span className="settings__icon"><it.icon size={ICON.md} aria-hidden /></span>}
                                  title={it.label}
                                  meta={it.id === 'people' && sources.data && !people
                                    ? <span className="settings__off">{LINK.down}</span> : undefined} />
                       )
                     }} />
      </aside>

      <main className="settings__page">
        <PageBody page={current ?? FIRST} sub={route.sub} edit={route.edit} history={!!route.history} status={status} sources={sources}
                  auth={auth} active={active} forms={forms} />
      </main>
    </div>
  )
}

function PageBody({ page, sub, edit, history, status, sources, auth, active, forms }: {
  page: string
  sub?: string
  /** its page and Guard: one of the long texts open in the large editor, or its History */
  edit?: string
  history: boolean
  status: ConfigStatus | null
  sources: ReturnType<typeof useSources>
  auth: AuthStatus | null
  active: boolean
  forms: { id: string; label: string }[]
}) {
  const form = forms.find(f => f.id === page)
  if (form) return <FormPage key={form.id} id={form.id} label={form.label} />
  switch (page) {
    case 'sources': {
      // /settings/sources alone shows the first source; the list on the left has them all
      const src = sub ? sources.data?.sources.find(s => s.id === sub) : sources.data?.sources[0]
      if (!src) {
        if (!sources.data) return <Page title="Sources"><div /></Page>
        return (
          <Page title="Sources">
            <EmptyState title={`No source called "${sub}".`} />
          </Page>
        )
      }
      return <SourcePage key={src.id} source={src} active={active} />
    }
    default: return <General status={status} />
    case 'agent': return edit ? <HerText key={`${edit}:${history}`} edit={edit} history={history} /> : <HerPage />
    case 'features': return <Features />
    case 'guard': return edit ? <BastetText key={`${edit}:${history}`} edit={edit} history={history} /> : <Guard />
    case 'appearance': return <Appearance />
    case 'notifications': return <Notifications />
    case 'people': return <People kbs={sources.data?.kbs.filter(k => k.kind === 'chat') ?? []} />
    case 'search': return <SearchAI kbs={sources.data?.kbs ?? []} />
    case 'models': return <Models providerId={sub} />
    case 'media': return <Media />
    case 'tools': return <Tools />
    case 'email': return <Email />
    case 'vault': return <Vault />
    case 'security': return auth?.login ? <Security /> : <Page title="Security"><div /></Page>
    case 'advanced': return <Advanced />
  }
}
