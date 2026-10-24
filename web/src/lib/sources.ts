/* Sources for Settings: one entry per chat source, with its state for the left list (the
   dot and "n to fix"), put together from the endpoints that know it: the connections'
   records (GET /api/connectors) and the task list. Only real problems count: a login
   to redo, a failed task to retry. A connection that does not answer is a state, not a problem. */
import { kbApi, type ConnectionRecord, type ConnectorSummary, type Kb, type Task } from './kb'
import { LINK, type Level } from './settingsApi'
import { useResource } from './useResource'
import { stamp } from './format'
import type { ServerEvent } from './bus'

export type Source = {
  id: string
  platform: string
  title: string
  kb: Kb
  level: Level
  sentence: string
  problems: number
}

type SourcesData = { kbs: Kb[]; sources: Source[] }

const EVENTS = new Set(['kb.changed', 'kb.task', 'kb.task.removed', 'kb.job', 'source.state', 'settings.changed'])

/** the sources whose login was lost and is not back (WhatsApp unlinked the phone,
 *  Discord refused the login): the rail's dot on Settings, and the line in a bridge
 *  conversation that cannot answer because of it */
export function useLostLogins(): ConnectorSummary[] {
  const r = useResource(() => kbApi.connectors().then(x => x.connectors), [],
                        { events: ev => ev.type === 'source.state' })
  return (r.data ?? []).filter(c => c.login_lost_at && c.connection.state === 'needs_login')
}

export function useSources() {
  return useResource(loadSources, [], { events: (ev: ServerEvent) => EVENTS.has(ev.type) })
}

async function loadSources(): Promise<SourcesData> {
  const [kbs, tasks, connectors] = await Promise.all([
    kbApi.list().then(r => r.kbs),
    kbApi.tasks().then(r => r.tasks).catch(() => [] as Task[]),
    kbApi.connectors().then(r => r.connectors).catch(() => [] as ConnectorSummary[]),
  ])
  const links = new Map(connectors.map(c => [c.kb, c]))
  const chat = kbs.filter(k => k.kind === 'chat')
  const sources = await Promise.all(chat.map(k => derive(k, tasks, links.get(k.id))))
  return { kbs, sources }
}

/** tasks of this source that failed and are still listed (automatic ones appear only then) */
export const failedTasks = (kb: string, tasks: Task[]) => tasks.filter(t => t.kb === kb && t.state === 'failed')

async function derive(k: Kb, tasks: Task[], link: ConnectorSummary | undefined): Promise<Source> {
  const base = { id: k.id, platform: k.platform ?? '', title: k.title, kb: k }
  let level: Level = 'off'
  let sentence = (k.messages ?? 0) > 0 ? `${LINK.down}.` : 'No messages yet.'
  let problems = failedTasks(k.id, tasks).length

  if (link) {
    const c = connectionState(k.platform ?? '', link.connection, link.account?.name ?? null, link.login_lost_at)
    level = c.level; sentence = c.sentence
    if (c.level === 'needs_you') problems += 1
  }
  if (level !== 'needs_you' && tasks.some(t => t.kb === k.id && t.state === 'running')) level = 'working'
  return { ...base, level, sentence, problems }
}

/* ── the one-sentence state of a connection (also used by the source pages) ── */

export function connectionState(platform: string, c: ConnectionRecord, account: string | null, lostAt?: number | null):
    { level: Level; sentence: string } {
  // never connected is a calm grey state; only a login that was there and got lost needs you
  if (c.state === 'needs_login' && !lostAt) return { level: 'off', sentence: c.error ?? `${LINK.down}.` }
  const wa = platform === 'whatsapp'
  switch (c.state) {
    case 'connected': {
      // WhatsApp's account name is the linked device's name
      return {
        level: 'ok',
        sentence: wa ? 'Connected to your phone.' : `Connected${account ? ` as ${account}` : ''}.`,
      }
    }
    case 'needs_login':
      return { level: 'needs_you', sentence: wa ? 'Not connected to your phone.' : 'Not logged in to Discord.' }
    default:
      // calm: the archive picks up where it stopped as soon as the connection answers again
      return { level: 'off', sentence: c.error ?? `${LINK.down}.` }
  }
}

/** "at 19:00", "at Fri 9 Oct, 19:00": when a login was lost */
export const lostWhen = (ms: number) => `at ${stamp(ms)}`

export const LEVEL_WORD: Record<Level, string> = {
  ok: LINK.up, working: 'Busy', needs_you: 'Waiting for you', off: LINK.down,
}
