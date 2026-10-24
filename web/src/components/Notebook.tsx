/* The notebook: its notes, every one shown to you. The place's sidebar holds Notes
   and Guides with their counts, the folder tree (a folder shows its notes) and Recent; the
   page is the list of notes (Group: Folder or None, Sort: Recent or Name, Trash, Select), a
   note as it reads it, a note in the large editor (components/editor), or its History over
   the notebook's own versions. An open editor stays mounted until Save or Cancel, so an
   unsaved note survives a visit elsewhere. The agent's guides (tools/) and protected notes are
   read-only. */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowUpRight, BookOpen, Copy, Download, FileText, Folder, FolderInput, FolderPlus, History, ListChecks, Lock, MoreHorizontal, NotebookPen,
  Eye, Pencil, Plus, RotateCcw, SearchX, Trash2,
} from 'lucide-react'
import { bulkApi, del, notebookApi, query, request, type NotebookNode } from '../lib/api'
import { count, errorText, listTime, stamp } from '../lib/format'
import { format, useNav } from '../lib/nav'
import { useName } from '../lib/name'
import { useResource, type Resource } from '../lib/useResource'
import type { TextHistory as HistoryPage, TextVersion, TextVersionFull } from '../lib/texts'
import {
  Button, ConfirmDialog, Dialog, EmptyState, Field, GroupHead, IconButton, InlineError, Input, ListRow, ListSection, Loaded, LoadingRow,
  MenuButton, Place, PlaceHead, Section, Segmented, SelectBar, ShelfRow, Toolbar, Tree, useContextMenu, usePicker, VirtualList,
  type BulkAction, type MenuItem, type Shelf, type ToolChoice, type TreeNode,
} from '../ui'
import { usePref } from '../lib/prefs'
import { LongEditor, TextHistory, type HistorySource } from './editor'
import { Markdown } from './Markdown'
import './Notebook.css'

/** a note as the tree has it: when it last changed and the line its index keeps of it */
type Node = NotebookNode & { updated_at?: string; line?: string | null }
type Note = { path: string; name: string; folder: string; line: string | null; updated_at: string; locked: boolean; config: boolean }
type Row = { key: string; head: string; n: number } | { key: string; note: Note }
type Ask =
  | { kind: 'new-note'; folder: string }
  | { kind: 'new-folder'; folder: string }
  | { kind: 'rename'; path: string }
  | { kind: 'move'; paths: string[] }
  | { kind: 'trash'; path: string }
type Editor = { text: string; saved: string; n: number }

const parentOf = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '')
const leaf = (p: string) => p.slice(p.lastIndexOf('/') + 1)
const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name)
const bare = (name: string) => name.replace(/\.(md|markdown)$/i, '')
const withMd = (name: string) => (/\.[a-z0-9]+$/i.test(name) ? name : `${name}.md`)
const notesNoun = (n: number) => count(n, 'note')
const DRAG = 'text/x-notebook-path'

function notesOf(nodes: Node[], out: Note[] = []): Note[] {
  for (const n of nodes) {
    if (n.kind === 'dir') notesOf(n.children ?? [], out)
    else out.push({ path: n.path, name: bare(n.name), folder: parentOf(n.path), line: n.line ?? null, updated_at: n.updated_at ?? '',
                    locked: !!n.protected || !!n.virtual, config: n.owner === 'config' || !!n.virtual })
  }
  return out
}

/** the folders as the kit's Tree, each with the number of notes in it */
function foldersOf(nodes: Node[]): TreeNode[] {
  return nodes.filter(n => n.kind === 'dir' && !n.virtual)
    .map(n => ({ id: n.path, label: n.name, icon: Folder, count: notesOf(n.children ?? []).length, children: foldersOf(n.children ?? []) }))
}

/** the notebook's own versions, as every long text's History (lib/texts): the text it has
    now leads the first page, then the texts it had before */
const historyOf = (path: string): HistorySource => ({
  list: (q, before) => request<HistoryPage & { current: TextVersion | null }>(`/notebook/versions${query({ path, q: q || undefined, before })}`)
    .then(({ current, ...page }) => ({ ...page, versions: current ? [current, ...page.versions] : page.versions })),
  get: id => request<TextVersionFull>(`/notebook/versions/${encodeURIComponent(String(id))}${query({ path })}`),
})

function download(name: string, text: string) {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }))
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
}

export function Notebook({ active, note, edit, history }: { active: boolean; note?: string; edit: boolean; history: boolean }) {
  const nav = useNav()
  const name = useName()
  const tree = useResource(() => notebookApi.tree().then(r => r.tree as Node[]), [],
    { events: ev => ev.type === 'tool.done' && typeof ev.name === 'string' && ev.name.startsWith('notebook__') })
  useEffect(() => { if (active) tree.reload() }, [active]) // eslint-disable-line react-hooks/exhaustive-deps
  const nodes = useMemo(() => tree.data ?? [], [tree.data])
  const all = useMemo(() => notesOf(nodes), [nodes])
  const notes = all.filter(n => !n.path.startsWith('tools/'))
  const guides = all.filter(n => n.path.startsWith('tools/'))
  const folders = useMemo(() => foldersOf(nodes), [nodes])

  const [view, setView] = useState<'notes' | 'guides'>('notes')
  const [folder, setFolder] = useState<string | null>(null)
  // the folders open in the sidebar, kept on this device
  const [openFolders, setOpenFolders] = usePref<string[]>('tree:notebook', [])
  const open = useMemo(() => new Set(openFolders), [openFolders])
  const [ask, setAsk] = useState<Ask | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editors, setEditors] = useState<Record<string, Editor>>({})
  const boxes = useRef<Record<string, HTMLDivElement | null>>({})
  const [leaving, setLeaving] = useState<string | null>(null)

  const goList = (f: string | null = folder) => { setFolder(f); setView('notes'); nav.go({ section: 'notebook' }) }
  const goNote = (path: string, then: { edit?: boolean; history?: boolean } = {}) => nav.go({ section: 'notebook', note: path, ...then })
  const act = async (p: Promise<unknown>) => {
    try { await p; setError(null); tree.reload() } catch (e) { setError(errorText(e)) }
  }

  const move = (paths: string[], into: string) => act((async () => {
    for (const src of paths) {
      const dst = join(into, leaf(src))
      if (dst !== src && !into.startsWith(src + '/')) await notebookApi.move(src, dst)
    }
  })())

  const noteMenu = (n: Note): MenuItem[] => {
    const base: MenuItem[] = [{ label: 'Open', icon: ArrowUpRight, onSelect: () => goNote(n.path) }]
    if (n.path.startsWith('tools/')) return base
    const readOnly: MenuItem = { label: 'Read-only', icon: Lock, current: n.locked, disabled: n.config,
                                 title: n.config ? 'Read-only' : undefined, onSelect: () => void act(notebookApi.protect(n.path, !n.locked)) }
    if (n.locked) return [...base, readOnly]
    return [...base,
      { label: 'Edit', icon: Pencil, onSelect: () => goNote(n.path, { edit: true }) },
      { label: 'Rename', icon: Pencil, onSelect: () => setAsk({ kind: 'rename', path: n.path }) },
      { label: 'Move to', icon: FolderInput, onSelect: () => setAsk({ kind: 'move', paths: [n.path] }) },
      { label: 'Duplicate', icon: Copy, onSelect: () => void duplicate(n.path) },
      { label: 'History', icon: History, onSelect: () => goNote(n.path, { history: true }) },
      readOnly, 'sep',
      { label: 'Select', icon: ListChecks, onSelect: () => picker.start(n.path) },
      { label: 'Move to Trash', icon: Trash2, danger: true, onSelect: () => setAsk({ kind: 'trash', path: n.path }) },
    ]
  }
  const folderMenu = (f: string): MenuItem[] => [
    { label: 'New note', icon: NotebookPen, onSelect: () => setAsk({ kind: 'new-note', folder: f }) },
    { label: 'New folder', icon: FolderPlus, onSelect: () => setAsk({ kind: 'new-folder', folder: f }) },
    ...(f ? ['sep' as const, { label: 'Rename', icon: Pencil, onSelect: () => setAsk({ kind: 'rename', path: f }) },
             { label: 'Move to', icon: FolderInput, onSelect: () => setAsk({ kind: 'move', paths: [f] }) },
             'sep' as const, { label: 'Move to Trash', icon: Trash2, danger: true, detail: f, onSelect: () => setAsk({ kind: 'trash', path: f }) }] : []),
  ]

  const duplicate = async (path: string) => {
    try {
      const { content } = await notebookApi.read(path)
      const copy = join(parentOf(path), `${bare(leaf(path))} copy.md`)
      await notebookApi.write(copy, content)
      tree.reload()
      goNote(copy)
    } catch (e) { setError(errorText(e)) }
  }

  // one picker for the list, and on the Trash for the trashed
  const [shelf, setShelf] = useState<Shelf>('list')
  const picker = usePicker(`${shelf}:${view}:${folder}`)

  const editor = (path: string) => (
    <EditorPage key={`${path}:${editors[path]!.n}`} path={path} ed={editors[path]!} shown={note === path && edit} name={name}
                box={el => { boxes.current[path] = el }}
                onSave={async text => {
                  await notebookApi.write(path, text)
                  setEditors(({ [path]: _, ...rest }) => rest)
                  tree.reload()
                  goNote(path)
                }}
                onCancel={() => {
                  const now = boxes.current[path]?.querySelector('textarea')?.value
                  if (now !== undefined && now !== editors[path]!.saved) setLeaving(path)
                  else { setEditors(({ [path]: _, ...rest }) => rest); goNote(path) }
                }} />
  )

  const side = (
    <>
      <ListRow density="sm" icon={NotebookPen} title="Notes" meta={tree.data && notes.length} selected={!note && view === 'notes' && folder === null}
               onClick={() => goList(null)} />
      <ListRow density="sm" icon={BookOpen} title="Guides" meta={tree.data && guides.length} selected={!note && view === 'guides'}
               onClick={() => { setView('guides'); nav.go({ section: 'notebook' }) }} />
      {folders.length > 0 && <>
        <ListSection label="Folders" />
        <Tree nodes={folders} open={open} label="Folders" selected={!note && view === 'notes' ? folder ?? undefined : undefined}
              onOpen={(id, on) => setOpenFolders(on ? [...open, id] : openFolders.filter(x => x !== id))}
              onSelect={id => goList(id)} menu={n => folderMenu(n.id)} onMove={(id, into) => void move([id], into)} />
      </>}
      {notes.length > 0 && <ListSection label="Recent" />}
      {[...notes].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 8).map(n => (
        <ListRow key={n.path} density="sm" leading={<FileText size={16} aria-hidden />} title={bare(leaf(n.path))} tooltip={n.path} selected={n.path === note}
                 href={format({ section: 'notebook', note: n.path })} />
      ))}
    </>
  )

  let page: React.ReactNode
  const opened = note ? all.find(n => n.path === note) : undefined
  if (note && history && opened && !opened.locked) {
    page = <HistoryOf path={note} name={opened.name}
                      onRestore={(text, now) => {
                        setEditors(e => ({ ...e, [note]: { text, saved: e[note]?.saved ?? now, n: (e[note]?.n ?? 0) + 1 } }))
                        goNote(note, { edit: true })
                      }} />
  } else if (note && !(edit && editors[note])) {
    page = <NotePage key={note} path={note} note={opened} name={name} menu={opened ? noteMenu(opened) : []}
                     onEdit={() => goNote(note, { edit: true })} onFolder={f => goList(f)}
                     onOpenEditor={(text) => setEditors(e => (e[note] ? e : { ...e, [note]: { text, saved: text, n: 0 } }))}
                     wantsEditor={edit} />
  } else if (!note) {
    page = view === 'guides'
      ? <GuidesList guides={guides} tree={tree} onOpen={p => goNote(p)} />
      : <NotesList notes={notes} folder={folder} shelf={shelf} onShelf={setShelf} picker={picker} tree={tree} menu={noteMenu} folderMenu={folderMenu}
                   onNew={() => setAsk({ kind: 'new-note', folder: folder ?? '' })}
                   onNewFolder={() => setAsk({ kind: 'new-folder', folder: folder ?? '' })}
                   onMove={move} onAsk={setAsk} onChanged={tree.reload} onError={e => setError(errorText(e))} />
  }

  return (
    <Place title="Notebook" side={side}
           action={<IconButton icon={Plus} label="New note" onClick={() => setAsk({ kind: 'new-note', folder: folder ?? '' })} />}>
      {error && <div className="list-page__note"><InlineError compact message={error} /></div>}
      {page}
      {Object.keys(editors).map(editor)}
      {ask && <Asked ask={ask} folders={folders} onClose={() => setAsk(null)}
                     onDone={async () => {
                       const a = ask
                       setAsk(null)
                       tree.reload()
                       if (a.kind === 'trash' && note && (note === a.path || note.startsWith(a.path + '/'))) goList()
                     }}
                     onCreated={path => { setAsk(null); tree.reload(); goNote(path, { edit: true }) }}
                     onMoved={async (paths, into) => { setAsk(null); await move(paths, into); picker.stop() }} />}
      {leaving && (
        <Dialog title="Unsaved changes" size="sm" onClose={() => setLeaving(null)}
                actions={<>
                  <Button onClick={() => { const p = leaving; setLeaving(null); setEditors(({ [p]: _, ...rest }) => rest); goNote(p) }}>Discard</Button>
                  <Button variant="primary" onClick={() => {
                    const p = leaving
                    const text = boxes.current[p]?.querySelector('textarea')?.value ?? ''
                    setLeaving(null)
                    void notebookApi.write(p, text).then(() => { setEditors(({ [p]: _, ...rest }) => rest); tree.reload(); goNote(p) },
                                                          e => setError(errorText(e)))
                  }}>Save</Button>
                </>}>
          <p className="nb-path">{leaving}</p>
        </Dialog>
      )}
    </Place>
  )
}

/* ── the list of notes: by folder or not, the Trash as a shelf of it ── */
function NotesList({ notes, folder, shelf, onShelf, picker, tree, menu, folderMenu, onNew, onNewFolder, onMove, onAsk, onChanged, onError }: {
  notes: Note[]
  folder: string | null
  shelf: Shelf
  onShelf: (s: Shelf) => void
  picker: ReturnType<typeof usePicker>
  tree: Resource<Node[]>
  menu: (n: Note) => MenuItem[]
  folderMenu: (f: string) => MenuItem[]
  onNew: () => void
  onNewFolder: () => void
  onMove: (paths: string[], into: string) => Promise<void>
  onAsk: (a: Ask) => void
  onChanged: () => void
  onError: (e: unknown) => void
}) {
  const [q, setQ] = useState('')
  const [sort, setSort] = useState('Recent')
  const [group, setGroup] = useState('Folder')
  const ctx = useContextMenu()
  const trash = useResource(() => notebookApi.trash().then(r => r.trash), [], { enabled: shelf === 'trash' })
  const needle = q.trim().toLowerCase()
  const purge = (id: string) => del<{ ok: boolean }>(`/notebook/trash/${encodeURIComponent(id)}`)

  const choice = (label: string, value: string, options: string[], onChange: (v: string) => void): ToolChoice =>
    ({ label, value, options: options.map(o => ({ value: o, label: o })), onChange })

  const shown = notes.filter(n => (folder === null || n.folder === folder || n.folder.startsWith(folder + '/')) && n.path.toLowerCase().includes(needle))
    .sort((a, b) => (sort === 'Name' ? a.name.localeCompare(b.name) : b.updated_at.localeCompare(a.updated_at)))
  const rows: Row[] = group === 'None' ? shown.map(n => ({ key: n.path, note: n }))
    : [...new Set(shown.map(n => n.folder))].sort().flatMap(f => {
      const these = shown.filter(n => n.folder === f)
      return [{ key: `f:${f}`, head: f, n: these.length }, ...these.map(n => ({ key: n.path, note: n }))]
    })
  const gone = (trash.data ?? []).filter(t => t.path.toLowerCase().includes(needle))
  picker.keys.current = shelf === 'trash' ? gone.map(t => t.id) : shown.map(n => n.path)

  const bulk: BulkAction[] = shelf === 'trash'
    ? [{ label: 'Restore', icon: RotateCcw, done: 'restored', run: keys => bulkApi.run('notes', 'restore', keys).then(r => { trash.reload(); onChanged(); return r }) },
       { label: 'Delete permanently', icon: Trash2, danger: true, done: 'deleted',
         ask: n => ({ title: `Delete ${notesNoun(n)} permanently?`, confirm: 'Delete permanently', body: 'This cannot be undone.' }),
         run: keys => bulkApi.run('notes', 'purge', keys).then(r => { trash.reload(); return r }) }]
    : [{ label: 'Move to', icon: FolderInput, done: 'moved', run: async keys => { onAsk({ kind: 'move', paths: keys }); return { done: [], skipped: [] } } },
       { label: 'Move to Trash', icon: Trash2, danger: true, done: 'moved',
         ask: n => ({ title: `Move ${notesNoun(n)} to Trash?`, confirm: 'Move to Trash' }),
         run: keys => bulkApi.run('notes', 'trash', keys).then(r => { onChanged(); return r }) }]

  const draw = (r: Row) => {
    if ('head' in r) {
      return <GroupHead icon={Folder} title={r.head || 'Notebook'} count={notesNoun(r.n)} menu={picker.on ? undefined : folderMenu(r.head)}
                        onDrop={e => { const p = e.dataTransfer.getData(DRAG); if (p) void onMove([p], r.head) }} />
    }
    const n = r.note
    const items = menu(n)
    return (
      <div draggable={!picker.on && !n.locked} onDragStart={e => e.dataTransfer.setData(DRAG, n.path)}>
        <ListRow icon={n.locked ? Lock : FileText} title={n.name} subtitle={n.line ?? undefined}
                 cell={group === 'None' ? n.folder ? <span className="nb-folder">{n.folder}</span> : null : undefined}
                 meta={listTime(n.updated_at)} tooltip={n.locked ? 'Read-only' : n.path} href={format({ section: 'notebook', note: n.path })}
                 pick={picker.row(n.path)} onContextMenu={e => ctx.open(e, items, n.name)} press={ctx.press(() => items, n.name)}
                 trailing={!picker.on && <MenuButton icon={MoreHorizontal} label="More" items={items} size="sm" />} />
      </div>
    )
  }

  const title = folder ? leaf(folder) : 'Notes'
  const nothing = shelf === 'list' ? !!tree.data && notes.length === 0 : !!trash.data && trash.data.length === 0
  return (
    <div className="list-page">
      <PlaceHead level={1} title={title} titleAttr={folder ?? undefined} count={shelf === 'list' ? shown.length : trash.data?.length}
                 search={{ value: q, onChange: setQ, placeholder: 'Search notes' }} picker={nothing ? undefined : picker}
                 shelf={{ trash: true, value: shelf, onChange: onShelf }}
                 more={[{ label: 'New folder', icon: FolderPlus, onSelect: onNewFolder }]}
                 newLabel="New note" onNew={onNew} />
      {(picker.on || shelf === 'list') && !nothing && (
        <div className="list-page__top">
          {picker.on ? <SelectBar picker={picker} noun={notesNoun} actions={bulk} />
            : <Toolbar sort={choice('Sort', sort, ['Recent', 'Name'], setSort)} group={choice('Group', group, ['Folder', 'None'], setGroup)} />}
        </div>
      )}
      {shelf === 'trash' ? (
        <Loaded page res={trash} icon={Trash2}
                empty={nothing ? { title: 'Trash is empty' }
                  : gone.length === 0 && { icon: SearchX, title: 'No notes match', action: <Button onClick={() => setQ('')}>Clear search</Button> }}>
          {() => (
            <div className="list-page__rows">
              <VirtualList items={gone} estimate={56} getKey={t => t.id} label="Trash" resetOn={q}
                           renderRow={t => (
                             <ShelfRow title={t.path} leading={t.kind === 'dir' ? <Folder size={16} aria-hidden /> : <FileText size={16} aria-hidden />}
                                       pick={picker.row(t.id)} onSelect={() => picker.start(t.id)}
                                       onRestore={() => void notebookApi.restoreTrashed(t.id).then(() => { trash.reload(); onChanged() }, onError)}
                                       onDelete={() => purge(t.id).then(trash.reload, onError)} />
                           )} />
            </div>
          )}
        </Loaded>
      ) : (
        <Loaded page res={tree} icon={NotebookPen}
                empty={nothing ? { title: 'No notes yet', action: <Button variant="primary" icon={NotebookPen} onClick={onNew}>New note</Button> }
                  : rows.length === 0 && { icon: SearchX, title: 'No notes match', action: needle ? <Button onClick={() => setQ('')}>Clear search</Button> : undefined }}>
          {() => (
            <div className="list-page__rows">
              <VirtualList items={rows} estimate={i => ('head' in rows[i]! ? 56 : 68)} getKey={r => r.key} renderRow={draw} label={title}
                           resetOn={`${q}|${folder}|${group}|${sort}`} />
            </div>
          )}
        </Loaded>
      )}
      {ctx.menu}
    </div>
  )
}

/* ── its guides: one per skill, read-only ── */
function GuidesList({ guides, tree, onOpen }: { guides: Note[]; tree: Resource<Node[]>; onOpen: (path: string) => void }) {
  const [q, setQ] = useState('')
  const ctx = useContextMenu()
  const shown = guides.filter(g => g.name.toLowerCase().includes(q.trim().toLowerCase()))
  const menu = (g: Note): MenuItem[] => [{ label: 'Open', icon: ArrowUpRight, onSelect: () => onOpen(g.path) }]
  return (
    <div className="list-page">
      <PlaceHead level={1} title="Guides" count={guides.length} search={{ value: q, onChange: setQ, placeholder: 'Search guides' }} />
      <Loaded page res={tree} icon={Lock} empty={shown.length === 0 && { icon: SearchX, title: 'No guides match', action: <Button onClick={() => setQ('')}>Clear search</Button> }}>
        {() => (
          <div className="list-page__rows">
            <VirtualList items={shown} estimate={68} getKey={g => g.path} label="Guides" resetOn={q}
                         renderRow={g => <ListRow icon={Lock} title={g.name.replace(/_/g, ' ')} subtitle={g.path} tooltip="Read-only"
                                                  href={format({ section: 'notebook', note: g.path })}
                                                  onContextMenu={e => ctx.open(e, menu(g), g.name)} press={ctx.press(() => menu(g), g.name)}
                                                  trailing={<MenuButton icon={MoreHorizontal} label="More" items={menu(g)} size="sm" />} />} />
          </div>
        )}
      </Loaded>
      {ctx.menu}
    </div>
  )
}

/* ── a note as it reads it ── */
function NotePage({ path, note, name, menu, onEdit, onFolder, onOpenEditor, wantsEditor }: {
  path: string
  note: Note | undefined
  name: string
  menu: MenuItem[]
  onEdit: () => void
  onFolder: (f: string) => void
  /** the text as stored, for the editor the route asks for */
  onOpenEditor: (text: string) => void
  wantsEditor: boolean
}) {
  const file = useResource(() => notebookApi.read(path), [path])
  const locked = note?.locked ?? !!file.data?.protected
  useEffect(() => { if (wantsEditor && file.data && !locked) onOpenEditor(file.data.content) }, [wantsEditor, file.data, locked]) // eslint-disable-line react-hooks/exhaustive-deps
  const folder = parentOf(path)
  const crumbs = [{ label: 'Notebook', onClick: () => onFolder('') },
                  ...(folder ? [{ label: folder, onClick: () => onFolder(folder) }] : [])]
  // its own notes in its words; "Who it is" for identity.md
  const title = path === 'identity.md' && name ? `Who ${name} is` : bare(leaf(path))
  const doc = file.data
  return (
    <div className="nb-page">
      <PlaceHead level={1} crumbs={crumbs} title={title} titleAttr={path}
                 detail={locked ? <><Lock size={14} aria-hidden />Read-only</> : note?.updated_at && stamp(note.updated_at)}
                 actions={<Segmented label="Show" value="view" disabled={locked || !doc} title={locked ? 'Read-only' : undefined}
                                     onChange={v => { if (v === 'edit') onEdit() }}
                                     options={[{ value: 'view', label: 'View', icon: Eye }, { value: 'edit', label: 'Edit', icon: Pencil }]} />}
                 more={[...menu.filter(m => m === 'sep' || !('label' in m) || (m.label !== 'Open' && m.label !== 'Edit')),
                        ...(doc ? [{ label: 'Download', icon: Download, onSelect: () => download(leaf(path), doc.content) }] : [])]} />
      <div className="page">
        <div className="page__col page__col--prose">
          {file.missing ? <EmptyState icon={NotebookPen} title="Not found" />
            : file.error ? <InlineError message={file.error} onRetry={file.reload} />
            : !doc ? <LoadingRow />
            : <Section><div className="prose nb-read">{doc.shown.trim() ? <Markdown text={doc.shown} /> : <p className="nb-quiet">Empty.</p>}</div></Section>}
        </div>
      </div>
    </div>
  )
}

/* ── a note in the large editor; mounted until Save or Cancel, shown only on its route ── */
function EditorPage({ path, ed, shown, name, box, onSave, onCancel }: {
  path: string
  ed: Editor
  shown: boolean
  name: string
  box: (el: HTMLDivElement | null) => void
  onSave: (text: string) => Promise<void>
  onCancel: () => void
}) {
  const title = bare(leaf(path))
  const folder = parentOf(path)
  return (
    <div className="nb-edit" hidden={!shown} ref={box}>
      <LongEditor crumbs={[{ label: 'Notebook', href: format({ section: 'notebook' }) }, ...(folder ? [{ label: folder }] : [])]}
                  title={title} of={`note:${path}`} history={format({ section: 'notebook', note: path, history: true })}
                  texts={[{ key: 'text', value: ed.text }]} hint={`Ask ${name}`} dirty={ed.text !== ed.saved}
                  onSave={v => onSave(v.text ?? '')} onCancel={onCancel} />
    </div>
  )
}

/* ── a note's History: the notebook's own versions, Restore this version into the editor ── */
function HistoryOf({ path, name, onRestore }: { path: string; name: string; onRestore: (text: string, now: string) => void }) {
  const file = useResource(() => notebookApi.read(path), [path])
  const source = useMemo(() => historyOf(path), [path])
  if (!file.data) return <Loaded res={file} />
  return (
    <div className="nb-page">
      <TextHistory crumbs={[{ label: 'Notebook', href: format({ section: 'notebook' }) }, { label: name, href: format({ section: 'notebook', note: path }) }]}
                   source={source} current={file.data.content} onRestore={t => onRestore(t, file.data!.content)} />
    </div>
  )
}

/* ── the dialogs: a name, a folder to move into, a move to the Trash ── */
function Asked({ ask, folders, onClose, onDone, onCreated, onMoved }: {
  ask: Ask
  folders: TreeNode[]
  onClose: () => void
  onDone: () => void
  onCreated: (path: string) => void
  onMoved: (paths: string[], into: string) => void
}) {
  const [value, setValue] = useState(ask.kind === 'rename' ? leaf(ask.path) : '')
  const [into, setInto] = useState('')
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setError(null)
    try { await fn() } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }

  if (ask.kind === 'trash') {
    return <ConfirmDialog title={`Move ${leaf(ask.path)} to Trash?`} confirmLabel="Move to Trash" danger onClose={onClose}
                          onConfirm={async () => { try { await notebookApi.remove(ask.path); onDone() } catch (e) { return errorText(e) } }} />
  }
  if (ask.kind === 'move') {
    return (
      <Dialog title={ask.paths.length === 1 ? `Move ${leaf(ask.paths[0]!)}` : `Move ${notesNoun(ask.paths.length)}`} size="sm" onClose={onClose}
              actions={<><Button onClick={onClose}>Cancel</Button>
                <Button variant="primary" icon={FolderInput} onClick={() => onMoved(ask.paths, into)}>Move</Button></>}>
        <Tree nodes={[{ id: '', label: 'Notebook', icon: NotebookPen, children: folders }]} open={new Set(['', ...open])} label="Folders"
              selected={into} onSelect={setInto}
              onOpen={(id, on) => setOpen(o => { const s = new Set(o); if (on) s.add(id); else s.delete(id); return s })} />
      </Dialog>
    )
  }
  const folderAsk = ask.kind === 'new-folder' || (ask.kind === 'rename' && !/\.[a-z0-9]+$/i.test(ask.path))
  const title = ask.kind === 'new-note' ? 'New note' : ask.kind === 'new-folder' ? 'New folder' : 'Rename'
  const where = ask.kind === 'rename' ? parentOf(ask.path) : ask.folder
  const a = ask
  const submit = () => run(async () => {
    const v = value.trim()
    if (a.kind === 'new-folder') { await notebookApi.mkdir(join(where, v)); onDone() }
    else if (a.kind === 'new-note') { const p = join(where, withMd(v)); await notebookApi.write(p, ''); onCreated(p) }
    else { await notebookApi.move(a.path, join(where, folderAsk ? v : withMd(v))); onDone() }
  })
  return (
    <Dialog title={title} size="sm" onClose={onClose}
            actions={<><Button onClick={onClose}>Cancel</Button>
              <Button variant="primary" loading={busy} disabled={!value.trim()} onClick={() => void submit()}>{ask.kind === 'rename' ? 'Save' : 'Add'}</Button></>}>
      <form onSubmit={e => { e.preventDefault(); if (value.trim()) void submit() }}>
        <Field label="Name" error={error}>
          <Input autoFocus value={value} onChange={e => setValue(e.target.value)} placeholder={folderAsk ? undefined : join(where, 'notes.md')} spellCheck={false} />
        </Field>
      </form>
    </Dialog>
  )
}
