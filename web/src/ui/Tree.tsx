/* Tree: a real hierarchy of folders (Notebook, Documents), the one place where things open
   and close. A folder's arrow (or Right and Left) opens and closes it; a click picks a
   row; a row can be dragged onto a folder; right-click opens its menu. Up and Down move
   between the rows you can see. */
import { useState, type ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { DisclosureArrow } from './Bits'
import { cx } from './cx'
import { ICON } from './icons'
import { useContextMenu, type MenuItem } from './Menu'

export type TreeNode = { id: string; label: string; icon?: LucideIcon; count?: number; children?: TreeNode[] }

export function Tree({ nodes, open, onOpen, selected, onSelect, menu, onMove, label, className }: {
  nodes: TreeNode[]
  /** the folders that are open */
  open: Set<string>
  onOpen: (id: string, open: boolean) => void
  selected?: string
  onSelect: (id: string) => void
  /** a row's right-click menu */
  menu?: (node: TreeNode) => MenuItem[]
  /** a row dropped onto a folder */
  onMove?: (id: string, into: string) => void
  label: string
  className?: string
}) {
  const ctx = useContextMenu()
  const [over, setOver] = useState<string | null>(null)
  const keys = (e: React.KeyboardEvent<HTMLElement>, n: TreeNode) => {
    const rows = [...(e.currentTarget.closest('.tree')?.querySelectorAll<HTMLElement>('.tree__row') ?? [])]
    const i = rows.indexOf(e.currentTarget)
    const folder = !!n.children?.length
    if (e.key === 'ArrowDown') rows[i + 1]?.focus()
    else if (e.key === 'ArrowUp') rows[i - 1]?.focus()
    else if (e.key === 'ArrowRight' && folder) onOpen(n.id, true)
    else if (e.key === 'ArrowLeft' && folder) onOpen(n.id, false)
    else if (e.key === 'Enter' || e.key === ' ') onSelect(n.id)
    else return
    e.preventDefault()
  }
  const draw = (list: TreeNode[], depth: number): ReactNode => list.map(n => {
    const folder = !!n.children?.length
    const isOpen = folder && open.has(n.id)
    const items = menu?.(n)
    return (
      <div key={n.id} role="none">
        <div role="treeitem" tabIndex={n.id === selected || (!selected && depth === 0 && n === nodes[0]) ? 0 : -1}
             aria-expanded={folder ? isOpen : undefined} aria-selected={n.id === selected}
             className={cx('tree__row', over === n.id && 'tree__row--drop')} style={{ '--depth': depth } as React.CSSProperties}
             draggable={!!onMove} onClick={() => onSelect(n.id)} onKeyDown={e => keys(e, n)}
             onContextMenu={items ? e => ctx.open(e, items, n.label) : undefined}
             onDragStart={onMove ? e => e.dataTransfer.setData('text/x-tree', n.id) : undefined}
             onDragOver={onMove && folder ? e => { e.preventDefault(); setOver(n.id) } : undefined}
             onDragLeave={onMove && folder ? () => setOver(null) : undefined}
             onDrop={onMove && folder ? e => { e.preventDefault(); setOver(null); const id = e.dataTransfer.getData('text/x-tree'); if (id && id !== n.id) onMove(id, n.id) } : undefined}>
          {folder
            ? <span className="tree__arrow" onClick={e => { e.stopPropagation(); onOpen(n.id, !isOpen) }}><DisclosureArrow /></span>
            : <span className="tree__arrow" />}
          {n.icon && <n.icon size={ICON.md} aria-hidden className="tree__icon" />}
          <span className="tree__label">{n.label}</span>
          {n.count != null && <span className="tree__count">{n.count}</span>}
        </div>
        {isOpen && <div role="group">{draw(n.children!, depth + 1)}</div>}
      </div>
    )
  })
  return (
    <div className={cx('tree', className)} role="tree" aria-label={label}>
      {draw(nodes, 0)}
      {ctx.menu}
    </div>
  )
}
