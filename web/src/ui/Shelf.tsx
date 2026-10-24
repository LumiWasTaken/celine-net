/* Shelves: Archive and Trash. Each is a button in a list's head (PlaceHead `shelf`, or
   ShelfButton in a Section) that shows its own things in place of the list, in the same
   place; the head's title then says where you are, with the way back before it. One row
   for a thing on a shelf: Restore, and Move to Trash (archived) or Delete permanently (in
   the Trash, where the place allows it, asked once). Right-click and the ContextMenu key open the same, with Select. */
import { useState, type ReactNode } from 'react'
import { Archive, ListChecks, RotateCcw, Trash2 } from 'lucide-react'
import { Button, IconButton } from './Button'
import { ConfirmDialog } from './Dialog'
import { cx } from './cx'
import { ListRow } from './List'
import { useContextMenu, type MenuItem } from './Menu'
import type { PickProps } from './Select'

export type Shelf = 'list' | 'archive' | 'trash'
const NAME = { archive: 'Archive', trash: 'Trash' } as const
const ICONS = { archive: Archive, trash: Trash2 } as const

/** a shelf's button: pressed while its things show; pressed again, back to the list */
export function ShelfButton({ shelf, on, onChange }: { shelf: 'archive' | 'trash'; on: boolean; onChange: (on: boolean) => void }) {
  return <IconButton icon={ICONS[shelf]} label={NAME[shelf]} title={on ? 'Back to the list' : NAME[shelf]} pressed={on}
                     onClick={() => onChange(!on)} />
}

export function ShelfRow({ shelf = 'trash', title, subtitle, leading, density, className, onClick, onRestore, onTrash, onDelete,
                           deleteOption, onSelect, pick, disabled, busy }: {
  shelf?: 'archive' | 'trash'
  title: ReactNode
  subtitle?: ReactNode
  leading?: ReactNode
  density?: 'sm' | 'md' | 'lg'
  className?: string
  /** opens it, where one on a shelf still opens */
  onClick?: () => void
  onRestore: () => void
  /** archived: to the Trash */
  onTrash?: () => void
  /** in the Trash: for good; left out where nothing is ever destroyed. Gets the state of
      `deleteOption` */
  onDelete?: (option: boolean) => Promise<unknown> | void
  /** one Checkbox in the Delete permanently dialog, off by default ("Also delete its
      sub-agents") */
  deleteOption?: string
  /** the row menu's Select */
  onSelect?: () => void
  pick?: PickProps
  disabled?: boolean
  busy?: boolean
}) {
  const ctx = useContextMenu()
  const [asking, setAsking] = useState(false)
  const name = typeof title === 'string' ? title : 'this'
  const items: MenuItem[] = [
    { label: 'Restore', icon: RotateCcw, disabled, onSelect: onRestore },
    ...(onSelect ? [{ label: 'Select', icon: ListChecks, onSelect }] : []),
    ...(shelf === 'archive' && onTrash ? ['sep' as const, { label: 'Move to Trash', icon: Trash2, danger: true, disabled, onSelect: onTrash }] : []),
    ...(shelf === 'trash' && onDelete ? ['sep' as const, { label: 'Delete permanently', icon: Trash2, danger: true, disabled, onSelect: () => setAsking(true) }] : []),
  ]
  return (
    <>
      <ListRow title={title} subtitle={subtitle} leading={leading} density={density} onClick={onClick} trailingAlways={!pick?.checked}
               className={cx(shelf === 'trash' && 'list-row--gone', className)} pick={pick}
               onContextMenu={e => ctx.open(e, items, name)} press={ctx.press(() => items, name)}
               trailing={pick?.checked === undefined && <Button size="sm" variant="ghost" icon={RotateCcw} disabled={disabled} loading={busy}
                                 onClick={onRestore}>Restore</Button>} />
      {ctx.menu}
      {asking && onDelete && (
        <ConfirmDialog title={`Delete ${name} permanently?`} danger confirmLabel="Delete permanently"
                       onClose={() => setAsking(false)}
                       option={deleteOption}
                       onConfirm={async option => { await onDelete(option); setAsking(false) }} />
      )}
    </>
  )
}
