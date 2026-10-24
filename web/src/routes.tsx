/* The places: each section's view (its own chunk, loaded on the first visit: one terminal's
   page brings xterm with it, the browser's noVNC), its rail icon, its title and its link. The
   lib/nav.tsx says what a URL means; App.tsx draws the rail
   and mounts the views from here. On a phone the BAR places and More are the bottom bar, the
   rest is in the More sheet. */
import { lazy, type ComponentProps, type ReactNode } from 'react'
import {
  Activity, CalendarClock, FileText, Flag, Globe, Heart, Mail, MessagesSquare, NotebookPen, Search, Terminal,
  type LucideIcon,
} from 'lucide-react'
import type { ConfigStatus } from './lib/api'
import type { Section } from './lib/nav'
import type { Feature } from './lib/settingsApi'

export const Celine = lazy(() => import('./views/Celine').then(m => ({ default: m.Celine })))
export const Missions = lazy(() => import('./views/Missions').then(m => ({ default: m.Missions })))
export const Routines = lazy(() => import('./views/Routines').then(m => ({ default: m.Routines })))
export const Notebook = lazy(() => import('./views/Notebook').then(m => ({ default: m.Notebook })))
export const Terminals = lazy(() => import('./views/Terminals').then(m => ({ default: m.Terminals })))
export const Chats = lazy(() => import('./views/Chats').then(m => ({ default: m.Chats })))
export const Intercom = lazy(() => import('./views/Intercom').then(m => ({ default: m.Intercom })))
export const SearchPage = lazy(() => import('./views/Search').then(m => ({ default: m.Search })))
export const Documents = lazy(() => import('./views/Documents').then(m => ({ default: m.Documents })))
export const Settings = lazy(() => import('./views/settings/Settings').then(m => ({ default: m.Settings })))
export const TerminalPage = lazy(() => import('./views/Terminal').then(m => ({ default: m.Terminal })))
export const BrowserPage = lazy(() => import('./views/Browser').then(m => ({ default: m.Browser })))
export const Events = lazy(() => import('./views/Events').then(m => ({ default: m.Events })))
export const Welcome = lazy(() => import('./views/Welcome').then(m => ({ default: m.Welcome })))
export const ArtifactPage = lazy(() => import('./views/Artifact').then(m => ({ default: m.ArtifactPage })))

export type RailEntry = { id: Section; icon: LucideIcon }

/** the rail, in this order; its place carries its name */
export const PLACES: RailEntry[] = [
  { id: 'celine', icon: Heart }, { id: 'missions', icon: Flag },
  { id: 'routines', icon: CalendarClock },
  { id: 'notebook', icon: NotebookPen }, { id: 'documents', icon: FileText }, { id: 'chats', icon: MessagesSquare },
  { id: 'intercom', icon: Mail }, { id: 'terminals', icon: Terminal }, { id: 'browser', icon: Globe }, { id: 'events', icon: Activity },
  { id: 'search', icon: Search },
]
/** the places on now: a place a feature owns (`catalog[].places`) leaves the rail, the bar
    and More while that feature is off (GET /status `features`); no catalog yet: all */
export function shownPlaces(catalog: Feature[] | null | undefined, features: Record<string, boolean> | undefined): RailEntry[] {
  const off = new Set((catalog ?? []).filter(f => features?.[f.id] === false).flatMap(f => f.places ?? []))
  return PLACES.filter(p => !off.has(p.id))
}
/** the phone's bottom bar, before More */
export const BAR: Section[] = ['celine', 'missions', 'notebook', 'intercom']

export const TITLE: Record<Section, string> = {
  celine: '', missions: 'Missions', routines: 'Routines', notebook: 'Notebook', terminals: 'Terminals',
  browser: 'Browser', artifact: 'Artifact', intercom: 'Intercom', events: 'Events', chats: 'Messages', search: 'Search', documents: 'Documents', settings: 'Settings', welcome: 'Setup',
}

export function sectionHref(s: Section): string {
  return s === 'celine' ? '/new_chat' : `/${s}`
}

/** what the shell hands every view */
export type ViewContext = {
  active: boolean
  status: ConfigStatus | null
  name: string
  /** its place's own: the list beside the chat, the lost logins, what each place has going on */
  celine: Omit<ComponentProps<typeof Celine>, 'active' | 'status'>
  /** the terminal the Terminals section shows (none: the list), and the text Reply quotes */
  terminal: string | undefined
  reply: string | undefined
  /** the artifact the Artifact section shows (none: the section is not drawn) */
  artifact: string | undefined
  /** the server's status changed (Setup saved) */
  onStatus: () => void
}

/** each section's view, mounted on its first visit and kept */
export const VIEWS: { id: Section; view: (c: ViewContext) => ReactNode }[] = [
  { id: 'celine', view: c => <Celine active={c.active} status={c.status} {...c.celine} /> },
  { id: 'missions', view: c => <Missions active={c.active} /> },
  { id: 'routines', view: c => <Routines active={c.active} /> },
  { id: 'notebook', view: c => <Notebook active={c.active} /> },
  { id: 'terminals', view: c => (c.terminal ? <TerminalPage key={c.terminal} id={c.terminal} reply={c.reply} /> : <Terminals />) },
  { id: 'browser', view: c => <BrowserPage active={c.active} /> },
  { id: 'artifact', view: c => (c.artifact ? <ArtifactPage id={c.artifact} assistant={c.name} /> : null) },
  { id: 'intercom', view: c => <Intercom active={c.active} /> },
  { id: 'events', view: c => <Events active={c.active} /> },
  { id: 'chats', view: c => <Chats active={c.active} /> },
  { id: 'search', view: c => <SearchPage active={c.active} /> },
  { id: 'documents', view: c => <Documents active={c.active} /> },
  { id: 'settings', view: c => <Settings active={c.active} status={c.status} /> },
]
