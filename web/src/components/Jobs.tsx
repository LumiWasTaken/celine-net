/* The agent's background jobs for one conversation, in its side panel (Model); nothing when it has
   none. One row each: the spinner while it runs, what it was asked (else the tool), how long it
   runs or when it ended (the state word when it did not end done), Stop while it runs. A
   row opens a sheet with what it was asked, what it says it is doing and what it answered,
   read from the list, so it follows it. The view fetches the list again on the server's
   job events and on a running job's progress events. */
import { useState } from 'react'
import { api, type Job } from '../lib/api'
import { fullDate, listTime } from '../lib/format'
import { useAction } from '../lib/useAction'
import { Button, InlineError, ListRow, ListSection, Sheet, StateWord } from '../ui'
import { useToolLabels } from './ToolList'
import { Elapsed, JOB_STATE_WORD } from './ToolRow'


export function Jobs({ jobs, error, reload }: {
  jobs: Job[]
  error: string | null
  reload: () => void
}) {
  const toolLabel = useToolLabels()
  const [openId, setOpenId] = useState<string | null>(null)
  const open = jobs.find(j => j.id === openId) ?? null
  const stop = useAction(async (id: string) => { await api.cancelJob(id); reload() })

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
          <ListRow key={j.id} density="sm" running={running} title={j.prompt || label} tooltip={label}
                   meta={running ? (j.started_at && <Elapsed since={j.started_at} />)
                     : j.state === 'done' ? <span title={fullDate(ended)}>{listTime(ended)}</span>
                     : <StateWord tone={j.state === 'cancelled' ? 'muted' : 'error'}>{JOB_STATE_WORD[j.state] ?? j.state}</StateWord>}
                   trailing={running && <Button size="sm" variant="ghost" loading={stop.busy} onClick={() => void stop.run(j.id)}>Stop</Button>}
                   trailingAlways onClick={() => setOpenId(j.id)} />
        )
      })}
      {open && (
        <Sheet title={open.prompt || toolLabel(open.tool_name)} size="md" onClose={() => setOpenId(null)}>
          {open.prompt && <p>{open.prompt}</p>}
          {open.state === 'running' && open.progress && <p>{open.progress}</p>}
          {open.state !== 'running' && open.result && <pre className="code-block">{open.result}</pre>}
        </Sheet>
      )}
    </div>
  )
}
