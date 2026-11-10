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

export const Celine = lazy(() => import('./views/Celine').then(m => ({ default: m.Celine })))
export const Missions = lazy(() => import('./views/Missions').then(m => ({ default: m.Missions })))
export const Routines = lazy(() => import('./views/Routines').then(m => ({ default: m.Routines })))
export const Notebook = lazy(() => import('./views/Notebook').then(m => ({ default: m.Notebook })))
export const Terminals = lazy(() => import('./views/Terminals').then(m => ({ default: m.Terminals })))
export const Chats = lazy(() => import('./views/Chats').then(m => ({ default: m.Chats })))
export const Intercom = lazy(() => import('./views/Intercom').then(m => ({ default: m.Intercom })))
export const Waiting = lazy(() => import('./views/Waiting').then(m => ({ default: m.Waiting })))
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
/** the places on now: the server's `places_off` (GET /status) leave the rail, the bar and More; no status yet: all */
export function shownPlaces(off: string[] | undefined): RailEntry[] {
  return PLACES.filter(p => !off?.includes(p.id))
}
/** the phone's bottom bar, before More */
export const BAR: Section[] = ['celine', 'missions', 'notebook', 'intercom']

export const TITLE: Record<Section, string> = {
  celine: '', missions: 'Missions', routines: 'Routines', notebook: 'Notebook', terminals: 'Terminals',
  browser: 'Browser', artifact: 'Page', intercom: 'Intercom', waiting: 'Waiting for you', events: 'Events', chats: 'Messages', search: 'Search', documents: 'Documents', settings: 'Settings', welcome: 'Setup',
}

export function sectionHref(s: Section): string {
  return s === 'celine' ? '/new_chat' : `/${s}`
}

/** what the shell hands every view */
export type ViewContext = {
  active: boolean
  status: ConfigStatus | null
  name: string
  /** its place's own: the list beside the chat, what each place has going on */
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
  { id: 'waiting', view: c => <Waiting active={c.active} /> },
  { id: 'events', view: c => <Events active={c.active} /> },
  { id: 'chats', view: c => <Chats active={c.active} /> },
  { id: 'search', view: c => <SearchPage active={c.active} /> },
  { id: 'documents', view: c => <Documents active={c.active} /> },
  { id: 'settings', view: c => <Settings active={c.active} status={c.status} /> },
]
