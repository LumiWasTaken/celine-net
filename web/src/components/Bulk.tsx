/* What a list does to many at once (POST /bulk-actions): the actions its Select bar has on each
   shelf, and "Archive older than" with the server's count before it runs. */
import { useState } from 'react'
import { Archive, ArchiveRestore, RotateCcw, Trash2 } from 'lucide-react'
import { bulkApi, type BulkWhat, type OlderKind } from '../lib/api'
import { useResource } from '../lib/useResource'
import {
  Button, Choice, Dialog, InlineError, type BulkAction, type BulkResult, type MenuItem, type Shelf,
} from '../ui'

/** a list's actions on what is picked, for the shelf it shows. `archive`: the list has
    an Archive; `purge`: its Trash deletes permanently; `run`: the place's own way to do
    one action to many (Intercom, a place with things of two kinds) */
export function bulkActions({ what, shelf, noun, archive = false, purge = true, run }: {
  what: BulkWhat
  shelf: Shelf
  noun: (n: number) => string
  archive?: boolean
  purge?: boolean
  run?: (action: 'archive' | 'unarchive' | 'trash' | 'restore' | 'purge', keys: string[]) => Promise<BulkResult>
}): BulkAction[] {
  const go = (action: 'archive' | 'unarchive' | 'trash' | 'restore' | 'purge') => (keys: string[]) =>
    run ? run(action, keys) : bulkApi.run(what, action, keys)
  const toTrash: BulkAction = {
    label: 'Move to Trash', icon: Trash2, danger: true, done: 'moved',
    ask: n => ({ title: `Move ${noun(n)} to Trash?`, confirm: 'Move to Trash', body: 'Restore brings them back.' }),
    run: go('trash'),
  }
  if (shelf === 'archive') {
    return [{ label: 'Restore', icon: ArchiveRestore, done: 'restored', run: go('unarchive') }, toTrash]
  }
  if (shelf === 'trash') {
    return [
      { label: 'Restore', icon: RotateCcw, done: 'restored', run: go('restore') },
      ...(purge ? [{
        label: 'Delete permanently', icon: Trash2, danger: true, done: 'deleted',
        ask: (n: number) => ({ title: `Delete ${noun(n)} permanently?`, confirm: 'Delete permanently' }),
        run: go('purge'),
      }] : []),
    ]
  }
  return [...(archive ? [{ label: 'Archive', icon: Archive, done: 'archived', run: go('archive') }] : []), toTrash]
}

/** "Archive older than" for a list head's More: its item, and its dialog to render */
export function useArchiveOlder(kind: OlderKind, noun: (n: number) => string) {
  const [asking, setAsking] = useState(false)
  const item: MenuItem = { label: 'Archive older than', icon: Archive, onSelect: () => setAsking(true) }
  return { item, dialog: asking && <ArchiveOlder kind={kind} noun={noun} onClose={() => setAsking(false)} /> }
}

const PERIODS = [
  { value: '7', label: '1 week' }, { value: '30', label: '1 month' }, { value: '90', label: '3 months' },
  { value: '180', label: '6 months' }, { value: '365', label: '1 year' },
]

/* "Archive older than": a period, the server's count for it, then Archive. Pinned ones
   and the ones at work stay (the server's rules). */
function ArchiveOlder({ kind, noun, onClose }: { kind: OlderKind; noun: (n: number) => string; onClose: () => void }) {
  const [days, setDays] = useState('90')
  const found = useResource(() => bulkApi.older(kind, Number(days)), [kind, days])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const n = found.data?.ids.length
  const period = PERIODS.find(p => p.value === days)!.label
  const run = async () => {
    setBusy(true); setError(null)
    try {
      const r = await bulkApi.archiveOlder(kind, Number(days))
      if (r.skipped.length) {
        setError(`${noun(r.skipped.length)} not archived: ${[...new Set(r.skipped.map(s => s.reason))].join(' ')}`)
        found.reload()
      } else onClose()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  return (
    <Dialog title="Archive older than" onClose={onClose} dismissable={!busy} closeButton={false}
            actions={<>
              <Button variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
              <Button variant="primary" icon={Archive} loading={busy} disabled={!n}
                      onClick={() => void run()}>{n ? `Archive ${noun(n)}` : 'Archive'}</Button>
            </>}>
      <div className="dialog__body">
        <Choice list label="Older than" value={days} onChange={setDays} options={PERIODS} />
        <p>{found.error ? null : n === undefined ? '' : n
          ? `Archive ${noun(n)} older than ${period}?` : `Nothing older than ${period}.`}</p>
      </div>
      {(error ?? found.error) && <InlineError compact message={(error ?? found.error)!} />}
    </Dialog>
  )
}
