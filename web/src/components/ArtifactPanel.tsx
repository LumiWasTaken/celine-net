/* The side panel's Pages tab: its pages and documents, this conversation's or all of them
   (a page whose conversation was deleted is in All); Trash shows the trashed ones instead,
   each with Restore and Delete permanently. One picked shows beside the chat (ArtifactView);
   Ask Celine about this adds to the open conversation's draft. Below its pages, under
   Shared from its box, what it shares from its box that runs (lib/shares), as the cards
   under its replies: each opens in a new tab. ConversationPanel imports it with
   these props. */
import { useState } from 'react'
import { AppWindow, ExternalLink, FileText, MoreHorizontal, SquareArrowOutUpRight, Trash2 } from 'lucide-react'
import { draftToCeline } from '../lib/ask'
import { artifactEvent, artifactsApi, type ArtifactRow } from '../lib/artifacts'
import { shareEvent, sharesApi } from '../lib/shares'
import { stamp } from '../lib/format'
import { usePronouns } from '../lib/name'
import { format } from '../lib/nav'
import { useAction } from '../lib/useAction'
import { useResource } from '../lib/useResource'
import { Choice, EmptyState, IconButton, InlineError, ListRow, ListSection, LoadingRow, MenuButton, ShelfButton, ShelfRow, useContextMenu, type MenuItem } from '../ui'
import { ArtifactView } from './ArtifactView'
import { ShareCards } from './ToolRow'
import './Artifact.css'

type Scope = 'here' | 'all'

export function ArtifactPanel({ conversationId, artifact, onPick, assistant, active }: {
  conversationId: string | null
  /** the one on screen, or null for the list */
  artifact: string | null
  onPick: (id: string | null) => void
  assistant: string
  /** the panel is open on this tab: fetch and follow changes */
  active: boolean
}) {
  if (artifact) {
    return (
      <ArtifactView id={artifact} assistant={assistant} onAsk={draftToCeline} onBack={() => onPick(null)}
                    extra={(
                      <IconButton icon={ExternalLink} label="Open in a new tab" href={format({ section: 'artifact', id: artifact })}
                                  target="_blank" rel="noopener" />
                    )} />
    )
  }
  return <ArtifactList conversationId={conversationId} onPick={onPick} active={active} />
}

function ArtifactList({ conversationId, onPick, active }: {
  conversationId: string | null; onPick: (id: string) => void; active: boolean
}) {
  const [chosen, setScope] = useState<Scope>('here')
  const { possessive } = usePronouns()
  const scope = !conversationId && chosen === 'here' ? 'all' : chosen
  const [trashed, setTrashed] = useState(false)
  const ctx = useContextMenu()
  const conversation = scope === 'here' ? conversationId ?? undefined : undefined
  const list = useResource(signal => artifactsApi.list({ conversation, trashed }, signal), [conversation, trashed],
                           { enabled: active, events: artifactEvent() })
  const rows = list.data
  const shared = useResource(signal => sharesApi.list(conversation, signal), [conversation],
                             { enabled: active && !trashed, events: shareEvent() })
  const shares = trashed ? [] : shared.data ?? []
  const trash = useAction(async (id: string, on: boolean) => { await artifactsApi.setTrashed(id, on); list.reload() })
  const purge = useAction(async (id: string) => { await artifactsApi.remove(id); list.reload() })
  const menu = (r: ArtifactRow): MenuItem[] => [
    { label: 'Open', icon: SquareArrowOutUpRight, onSelect: () => onPick(r.id) },
    'sep',
    { label: 'Move to Trash', icon: Trash2, danger: true, onSelect: () => void trash.run(r.id, true) },
  ]
  return (
    <div className="artlist">
      <div className="artlist__bar">
        <Choice label="Which pages" value={scope} onChange={setScope}
                options={[
                  ...(conversationId ? [{ value: 'here' as const, label: 'This conversation' }] : []),
                  { value: 'all', label: 'All' },
                ]} />
        <ShelfButton shelf="trash" on={trashed} onChange={setTrashed} />
      </div>
      {list.error && <InlineError message={list.error} onRetry={list.reload} />}
      {(trash.error || purge.error) && <InlineError compact message={trash.error ?? purge.error!} />}
      {!rows ? (!list.error && <LoadingRow />) : rows.length === 0 && shares.length === 0 ? (
        <div className="artlist__empty">{trashed ? <EmptyState icon={Trash2} title="Trash is empty" /> : <EmptyState icon={AppWindow} title="No pages yet" />}</div>
      ) : (
        <div className="artlist__rows">
          {rows.map(r => {
            const icon = r.kind === 'html' ? AppWindow : FileText
            return r.trashed_at ? (
              <ShelfRow key={r.id} onClick={() => onPick(r.id)} leading={<FileText size={16} aria-hidden />} title={r.title}
                        subtitle={stamp(r.updated_at)} onRestore={() => void trash.run(r.id, false)}
                        onDelete={() => purge.run(r.id)} />
            ) : (
              <ListRow key={r.id} onClick={() => onPick(r.id)} icon={icon} title={r.title} tooltip={r.title} subtitle={stamp(r.updated_at)}
                       onContextMenu={e => ctx.open(e, menu(r), r.title)} press={ctx.press(() => menu(r), r.title)}
                       trailing={<MenuButton icon={MoreHorizontal} label="More" items={menu(r)} size="sm" />} />
            )
          })}
          {shares.length > 0 && <ListSection label={`Shared from ${possessive} box`} />}
          <ShareCards cards={shares} />
        </div>
      )}
      {ctx.menu}
    </div>
  )
}
