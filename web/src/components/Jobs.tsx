/* The agent's background jobs for one conversation, in its side panel (Model); nothing when it has
   none. One row each: a dot for its state, what it was asked (else the tool), how long it
   runs or when it ended (the state word when it did not end done), Stop while it runs. A
   row opens a sheet with what it was asked, what it says it is doing and what it answered.
   The list is fetched by the view; while shown and something runs, again every 5 s. */
import { useEffect, useState } from 'react'
import { api, type Job } from '../lib/api'
import { fullDate, listTime } from '../lib/format'
import { useAction } from '../lib/useAction'
import { Button, InlineError, ListRow, ListSection, Sheet, type DotTone } from '../ui'
import { useToolLabels } from './ToolList'
import { Elapsed, JOB_STATE_WORD } from './ToolRow'

const TONE: Record<string, DotTone> = {
  running: 'running', done: 'ok', failed: 'error', timeout: 'error', interrupted: 'error', cancelled: 'muted',
}

export function Jobs({ jobs, error, reload, active = true }: {
  jobs: Job[]
  error: string | null
  reload: () => void
  active?: boolean
}) {
  const toolLabel = useToolLabels()
  const [open, setOpen] = useState<Job | null>(null)
  const anyRunning = jobs.some(j => j.state === 'running')
  const stop = useAction(async (id: string) => { await api.cancelJob(id); reload() })

  useEffect(() => {
    if (!active || !anyRunning) return
    const t = setInterval(() => { if (document.visibilityState === 'visible') reload() }, 5000)
    return () => clearInterval(t)
  }, [active, anyRunning, reload])

  if (jobs.length === 0 && !error) return null
  return (
    <div role="list" aria-label="Background jobs" id="jobs" tabIndex={-1}>
      <ListSection label="Background jobs" />
      {error && <InlineError message={error} compact onRetry={reload} />}
      {stop.error && <InlineError message={stop.error} compact />}
      {jobs.map(j => {
        const running = j.state === 'running'
        const ended = j.finished_at ?? j.created_at
        const label = toolLabel(j.tool_name)
        return (
          <ListRow key={j.id} density="sm" dot={TONE[j.state] ?? 'muted'} title={j.prompt || label} tooltip={label}
                   meta={running ? (j.started_at && <Elapsed since={j.started_at} />)
                     : j.state === 'done' ? <span title={fullDate(ended)}>{listTime(ended)}</span> : JOB_STATE_WORD[j.state] ?? j.state}
                   trailing={running && <Button size="sm" variant="ghost" loading={stop.busy} onClick={() => void stop.run(j.id)}>Stop</Button>}
                   trailingAlways onClick={() => setOpen(j)} />
        )
      })}
      {open && (
        <Sheet title={open.prompt || toolLabel(open.tool_name)} size="md" onClose={() => setOpen(null)}>
          {open.prompt && <p>{open.prompt}</p>}
          {open.state === 'running' && open.progress && <p>{open.progress}</p>}
          {open.state !== 'running' && open.result && <pre className="code-block">{open.result}</pre>}
        </Sheet>
      )}
    </div>
  )
}
