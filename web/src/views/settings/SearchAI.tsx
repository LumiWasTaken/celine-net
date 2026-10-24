/* Search index: how often the index catches up, and one line per source: how far its
   index is while it is not complete, else what the server says about its last build
   (the job's last outcome, kb.job), and what a refresh waits for. It maintains itself,
   so there is no per-source button. */
import type { Job, Kb } from '../../lib/kb'
import { stamp } from '../../lib/format'
import { Section } from '../../ui'
import { GeneralFields } from './General'
import { Page } from './parts'
import { useSchedules } from './SourcePage'
import './settings.css'

/** the line of a source: how far its index is, else its last build; null when the
    server has nothing to say (no build since the server started) */
function line(k: Kb, job: Job | undefined): string | null {
  const s = k.semantic
  const total = s?.passages ?? 0
  const done = Math.min(s?.embedded ?? 0, total)
  const waiting = job && job.state === 'unreachable' && !job.running ? job.error ?? null : null
  const wait = waiting ? ` ${waiting}` : ''
  if (total === 0) return `${k.title}: not indexed yet.${wait}`
  if (!s?.ready || done < total) return `${k.title}: ${Math.floor((done / total) * 100)}% indexed.${wait}`
  if (waiting) return `${k.title}: ${waiting}`
  if (job?.state === 'failed') return `${k.title}: the last build failed at ${stamp(job.finished_at)}. ${job.error ?? ''}`.trim()
  if (job?.state === 'done') return `${k.title}: last built at ${stamp(job.finished_at)}.`
  return null
}

export function SearchAI({ kbs }: { kbs: Kb[] }) {
  const indexed = kbs.filter(k => k.kind === 'chat' && k.semantic && (k.messages ?? 0) > 0)
  const jobs = useSchedules()
  const jobOf = (kb: string) => (jobs.data ?? []).find(x => x.kb === kb && x.kind === 'semantic')
  return (
    <Page title="Search index">
      <Section>
        {indexed.map(k => {
          const text = line(k, jobOf(k.id))
          return text && <p key={k.id} className="src-line">{text}</p>
        })}
        <GeneralFields only={['smart_search_refresh_minutes']} />
      </Section>
    </Page>
  )
}
