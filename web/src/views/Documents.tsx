/* Documents: your files, shared with Celine. The place's sidebar
   holds All documents, the folder tree and Recent; the page is one folder (its folders as
   cards, then its documents as rows; Type and Sort in the Toolbar, Select, the Trash as a
   shelf) or one document (its content first, then Details: your title and description, the
   AI description, the mode). Drag a row or a card onto a folder (a card, the tree, All
   documents) to move it; Move to in the menu does the same. Files picked or dropped from
   outside go into the folder shown, with their progress as rows above the folders.
   URL: /documents?folder=<path>, /documents/<item id>?folder=<path>. */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowUpRight, BookOpen, Code, Database, Download, ExternalLink, Eye, File, FileText, Folder as FolderIcon, FolderInput, FolderPlus,
  FolderUp, Globe, Image, ListChecks, MoreHorizontal, NotebookPen, Pencil, RefreshCw, RotateCcw, ScanText, SearchX, Shapes,
  Subtitles, Trash2, Upload, X, type LucideIcon,
} from 'lucide-react'
import { documentsApi, type Folder, type Item, type ItemDetail, type Mode, type Sort, type UploadResult } from '../lib/documents'
import { bulkApi, type BulkDo } from '../lib/api'
import { useServerEvents } from '../lib/bus'
import { format, useNav, type Route } from '../lib/nav'
import { bytes, count, errorText, listTime, num, stamp } from '../lib/format'
import { useAction } from '../lib/useAction'
import { useResource } from '../lib/useResource'
import { usePref } from '../lib/prefs'
import {
  Button, Card, CardGrid, Choice, ConfirmDialog, cx, Dialog, EmptyState, Facts, Field, GroupHead, IconButton, InlineError, Input, ListRow,
  ListSection, Loaded, MenuButton, Notice, PageIndex, Place, PlaceHead, Section, Segmented, SelectBar, ShelfRow, StatusLine, TextArea,
  Toolbar, Tree, useContextMenu, usePicker, VirtualList, type BulkAction, type BulkResult, type MenuItem, type Shelf, type TreeNode,
} from '../ui'
import { Markdown } from '../components/Markdown'
import './Documents.css'

const TYPE_ICON: Record<string, LucideIcon> = {
  note: NotebookPen, text: FileText, pdf: FileText, image: Image, docx: FileText, epub: BookOpen, code: Code, data: Database,
  html: Globe, subtitles: Subtitles, file: File,
}
/* a type as a filter and a column says it */
const TYPE_WORD: Record<string, string> = {
  note: 'Notes', text: 'Text', pdf: 'PDF', image: 'Images', docx: 'Word', epub: 'Books', code: 'Code', data: 'Data',
  html: 'Web pages', subtitles: 'Subtitles', file: 'Other',
}
const SORTS: { value: Sort; label: string }[] = [
  { value: 'changed', label: 'Recent' }, { value: 'name', label: 'Name' }, { value: 'added', label: 'Created' },
  { value: 'size', label: 'Size' }, { value: 'type', label: 'Type' },
]
const MODES: { value: Mode; label: string }[] = [{ value: 'chunked', label: 'Passages' }, { value: 'full_file', label: 'Whole file' }]
const editable = (it: Item) => it.kind === 'note' || it.kind === 'text' || it.kind === 'code' || it.kind === 'data'
const docsNoun = (n: number) => count(n, 'document')
const itemsNoun = (n: number) => count(n, 'item')
const join = (folder: string, name: string) => (folder ? `${folder}/${name}` : name)
const parentOf = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '')
const nameOf = (p: string) => (p ? p.slice(p.lastIndexOf('/') + 1) : 'Documents')
const fileUrl = (it: Item) => documentsApi.fileUrl(it.id, it.kind !== 'pdf' && it.kind !== 'image' && !editable(it))
const stem = (name: string) => name.replace(/\.[a-z0-9]+$/i, '')
/** the path down to a folder, the top first, as a head's crumbs */
const crumbsOf = (path: string, go: (p: string) => void) => {
  const parts = path ? path.split('/') : []
  return [{ label: 'Documents', path: '' }, ...parts.map((p, i) => ({ label: p, path: parts.slice(0, i + 1).join('/') }))]
    .map(c => ({ label: c.label, href: format({ section: 'documents', folder: c.path || undefined }), onClick: (e: React.MouseEvent) => { e.preventDefault(); go(c.path) } }))
}

/* a row or card on the move: "i:<id>" or "f:<path>", as the tree's drag carries it */
const DRAG = 'text/x-tree'
type Ask =
  | { kind: 'new-folder' } | { kind: 'new-note' }
  | { kind: 'rename'; item: Item } | { kind: 'rename-folder'; folder: Folder }
  | { kind: 'move'; keys: string[] }
  | { kind: 'trash'; keys: string[]; name: string }

/* many at once: a key is f:<folder path> or i:<document id> */
async function both(action: BulkDo, keys: string[]): Promise<BulkResult> {
  const out: BulkResult = { done: [], skipped: [] }
  for (const [what, mark] of [['folders', 'f:'], ['documents', 'i:']] as const) {
    const ids = keys.filter(k => k.startsWith(mark)).map(k => k.slice(2))
    if (!ids.length) continue
    const r = await bulkApi.run(what, action, ids)
    out.done.push(...r.done.map(id => mark + id))
    out.skipped.push(...r.skipped.map(x => ({ ...x, id: mark + x.id })))
  }
  return out
}

/* what a drop holds, folders walked: each file with its path inside the drop */
async function dropped(dt: DataTransfer): Promise<{ files: File[]; paths: string[] }> {
  const entries = [...dt.items].map(i => i.webkitGetAsEntry?.()).filter((e): e is FileSystemEntry => !!e)
  if (!entries.length) return { files: [...dt.files], paths: [...dt.files].map(f => f.name) }
  const files: File[] = [], paths: string[] = []
  const walk = async (e: FileSystemEntry, at: string): Promise<void> => {
    if (e.isFile) {
      const f = await new Promise<File>((ok, no) => (e as FileSystemFileEntry).file(ok, no))
      files.push(f); paths.push(at + f.name)
      return
    }
    const reader = (e as FileSystemDirectoryEntry).createReader()
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((ok, no) => reader.readEntries(ok, no))
      if (!batch.length) return
      for (const c of batch) await walk(c, `${at}${e.name}/`)
    }
  }
  for (const e of entries) await walk(e, '')
  return { files, paths }
}

/** the browser's file picker, files or a whole folder */
function pickFiles(folder: boolean, then: (files: File[], paths: string[]) => void) {
  const input = document.createElement('input')
  input.type = 'file'
  input.multiple = true
  if (folder) input.setAttribute('webkitdirectory', '')
  input.onchange = () => {
    const files = [...(input.files ?? [])]
    if (files.length) then(files, files.map(f => f.webkitRelativePath || f.name))
  }
  input.click()
}

/** every folder, loaded folder by folder (note: one request per folder; a tree route when there are hundreds) */
async function loadTree(path = ''): Promise<TreeNode[]> {
  const page = await documentsApi.folder(path, { limit: 1 })
  return Promise.all(page.folders.map(async f => ({ id: f.path, label: f.name, icon: FolderIcon, count: f.items, children: await loadTree(f.path) })))
}

type Upload = { key: number; name: string; progress: number | null; error?: string; result?: UploadResult; retry?: () => void }

export function Documents({ active }: { active: boolean }) {
  const nav = useNav()
  // the page the URL names; while another place is on screen, the one it showed last
  const last = useRef<Extract<Route, { section: 'documents' }>>({ section: 'documents' })
  if (nav.route.section === 'documents') last.current = nav.route
  const itemId = last.current.item
  const folder = last.current.folder ?? ''
  const [version, setVersion] = useState(0)
  const bump = () => setVersion(v => v + 1)
  useServerEvents(ev => { if (ev.type === 'documents.changed' || ev.type === 'documents.deleted') bump() })
  const tree = useResource(() => loadTree(), [version])
  const top = useResource(() => documentsApi.folder('', { limit: 1 }), [version])
  const recent = useResource(() => documentsApi.items({ sort: 'changed', limit: 6 }), [version])
  // the folders open in the sidebar, kept on this device
  const [opened, setOpened] = usePref<string[]>('tree:documents', [])
  const open = useMemo(() => new Set(opened), [opened])
  const [ask, setAsk] = useState<Ask | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [uploads, setUploads] = useState<Upload[]>([])
  const [picked, setPicked] = useState<{ files: File[]; paths: string[] } | null>(null)
  const [drop, setDrop] = useState(false)

  const goFolder = (path: string) => nav.go({ section: 'documents', folder: path || undefined })
  const run = (p: Promise<unknown>) => void p.then(() => { setError(null); bump() }, e => setError(errorText(e)))
  const moveKeys = (keys: string[], into: string) => run((async () => {
    for (const k of keys) {
      if (k.startsWith('i:')) await documentsApi.patch(Number(k.slice(2)), { folder: into })
      else {
        const p = k.startsWith('f:') ? k.slice(2) : k
        if (p !== into && !into.startsWith(p + '/') && parentOf(p) !== into) await documentsApi.patchFolder(p, { folder: into })
      }
    }
  })())
  const dropOn = (into: string) => ({
    onDragOver: (e: React.DragEvent) => { if (e.dataTransfer.types.includes(DRAG)) { e.preventDefault(); e.currentTarget.classList.add('doc-drop-on') } },
    onDragLeave: (e: React.DragEvent) => e.currentTarget.classList.remove('doc-drop-on'),
    onDrop: (e: React.DragEvent) => {
      e.currentTarget.classList.remove('doc-drop-on')
      const k = e.dataTransfer.getData(DRAG)
      if (k) { e.preventDefault(); e.stopPropagation(); moveKeys([k], into) }
    },
  })

  const add = async (files: File[], paths: string[], modes: Mode[], title: string) => {
    setPicked(null)
    const key = Date.now()
    const name = files.length === 1 ? files[0]!.name : docsNoun(files.length)
    const go = async () => {
      setUploads(u => [...u.filter(x => x.key !== key), { key, name, progress: 0 }])
      try {
        const result = await documentsApi.upload(files, { folder, paths, modes, title },
          share => setUploads(u => u.map(x => (x.key === key ? { ...x, progress: share } : x))))
        // done: the row goes, unless something was not added or was there already
        setUploads(u => (result.rejected.length || result.existing.length
          ? u.map(x => (x.key === key ? { ...x, progress: null, result } : x)) : u.filter(x => x.key !== key)))
        bump()
      } catch (e) {
        setUploads(u => u.map(x => (x.key === key ? { ...x, progress: null, error: errorText(e), retry: () => void go() } : x)))
      }
    }
    await go()
  }

  const folderMenu = (f: Folder): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => goFolder(f.path) },
    { label: 'Rename', icon: Pencil, onSelect: () => setAsk({ kind: 'rename-folder', folder: f }) },
    { label: 'Move to', icon: FolderInput, onSelect: () => setAsk({ kind: 'move', keys: [`f:${f.path}`] }) },
    'sep',
    { label: 'Move to Trash', icon: Trash2, danger: true, onSelect: () => setAsk({ kind: 'trash', keys: [`f:${f.path}`], name: f.name }) },
  ]
  const itemMenu = (it: Item, select?: () => void): MenuItem[] => [
    { label: 'Open', icon: ArrowUpRight, onSelect: () => nav.go({ section: 'documents', item: it.id, folder: it.folder || undefined }) },
    { label: 'Rename', icon: Pencil, onSelect: () => setAsk({ kind: 'rename', item: it }) },
    { label: 'Move to', icon: FolderInput, onSelect: () => setAsk({ kind: 'move', keys: [`i:${it.id}`] }) },
    { label: 'Extract text', icon: ScanText, onSelect: () => run(documentsApi.reindex(it.id)) },
    { label: 'Download', icon: Download, onSelect: () => window.open(documentsApi.fileUrl(it.id, true), '_blank', 'noopener') },
    'sep',
    ...(select ? [{ label: 'Select', icon: ListChecks, onSelect: select }] : []),
    { label: 'Move to Trash', icon: Trash2, danger: true, detail: it.title, onSelect: () => setAsk({ kind: 'trash', keys: [`i:${it.id}`], name: it.title }) },
  ]

  const side = (
    <>
      <div className="doc-all" {...dropOn('')}>
        <ListRow density="sm" icon={FileText} title="All documents" meta={top.data && num(top.data.folder.items)}
                 selected={!itemId && !folder} href={format({ section: 'documents' })} />
      </div>
      {tree.data && tree.data.length > 0 && <>
        <ListSection label="Folders" />
        <Tree nodes={tree.data} open={open} label="Folders" selected={!itemId ? folder || undefined : undefined}
              onOpen={(id, on) => setOpened(on ? [...open, id] : opened.filter(x => x !== id))}
              onSelect={goFolder} onMove={(id, into) => moveKeys([id], into)}
              menu={n => folderMenu({ path: n.id, name: n.label, items: n.count ?? 0, size: 0, updated_at: 0, added_at: 0, trashed_at: null })} />
      </>}
      {(recent.data?.items.length ?? 0) > 0 && <ListSection label="Recent" />}
      {recent.data?.items.map(it => {
        const I = TYPE_ICON[it.kind] ?? File
        return <ListRow key={it.id} density="sm" leading={<I size={16} aria-hidden />} title={it.title} tooltip={it.path} selected={it.id === itemId}
                        href={format({ section: 'documents', item: it.id, folder: it.folder || undefined })} />
      })}
    </>
  )

  return (
    <Place title="Documents" side={side}
           action={<IconButton icon={Upload} label="Upload" onClick={() => pickFiles(false, (files, paths) => setPicked({ files, paths }))} />}>
      <div className="doc-main"
           onDragEnter={e => { if (e.dataTransfer.types.includes('Files')) setDrop(true) }}>
        {error && <div className="list-page__note"><InlineError compact message={error} /></div>}
        {itemId ? <DocumentPage key={itemId} id={itemId} folder={last.current.folder} active={active} menu={itemMenu} onFolder={goFolder} />
          : <FolderPage folder={folder} version={version} onChanged={bump} uploads={uploads}
                        onUploadDone={key => setUploads(u => u.filter(x => x.key !== key))}
                        folderMenu={folderMenu} itemMenu={itemMenu} dropOn={dropOn} onAsk={setAsk} onFolder={goFolder}
                        onUpload={() => pickFiles(false, (files, paths) => setPicked({ files, paths }))}
                        onUploadFolder={() => pickFiles(true, (files, paths) => setPicked({ files, paths }))} />}
        {drop && (
          <div className="doc-drop" onDragOver={e => e.preventDefault()} onDragLeave={e => { if (e.currentTarget === e.target) setDrop(false) }}
               onDrop={e => {
                 e.preventDefault(); setDrop(false)
                 void dropped(e.dataTransfer).then(d => { if (d.files.length) setPicked(d) }, err => setError(errorText(err)))
               }}>
            <EmptyState icon={Upload} title={`Upload to ${nameOf(folder)}`} />
          </div>
        )}
      </div>
      {picked && <UploadDialog files={picked.files} paths={picked.paths} into={nameOf(folder)} onClose={() => setPicked(null)}
                               onAdd={(modes, t) => void add(picked.files, picked.paths, modes, t)} />}
      {ask && <Asked ask={ask} folder={folder} tree={tree.data ?? []} onClose={() => setAsk(null)}
                     onMove={(keys, into) => { setAsk(null); moveKeys(keys, into) }}
                     onDone={it => {
                       const a = ask
                       setAsk(null); bump()
                       if (it) nav.go({ section: 'documents', item: it.id, folder: it.folder || undefined }, { replace: a.kind === 'rename' })
                       else if (a.kind === 'trash' && itemId && a.keys.includes(`i:${itemId}`)) goFolder(folder)
                       else if (a.kind === 'trash' && a.keys.some(k => k.startsWith('f:') && (folder === k.slice(2) || folder.startsWith(k.slice(2) + '/')))) goFolder('')
                     }} />}
    </Place>
  )
}

/* ── one folder: its folders as cards, its documents as rows; the Trash as a shelf ── */
function FolderPage({ folder, version, onChanged, uploads, onUploadDone, folderMenu, itemMenu, dropOn, onAsk, onFolder, onUpload, onUploadFolder }: {
  folder: string
  version: number
  onChanged: () => void
  uploads: Upload[]
  onUploadDone: (key: number) => void
  folderMenu: (f: Folder) => MenuItem[]
  itemMenu: (it: Item, select?: () => void) => MenuItem[]
  dropOn: (into: string) => object
  onAsk: (a: Ask) => void
  onFolder: (path: string) => void
  onUpload: () => void
  onUploadFolder: () => void
}) {
  const nav = useNav()
  const [q, setQ] = useState('')
  const [type, setType] = useState<string | null>(null)
  const [sort, setSort] = useState<Sort>('changed')
  const [shelf, setShelf] = useState<Shelf>('list')
  const ctx = useContextMenu()
  const headMenu: MenuItem[] = [{ label: 'New note', icon: NotebookPen, onSelect: () => onAsk({ kind: 'new-note' }) },
                                { label: 'New folder', icon: FolderPlus, onSelect: () => onAsk({ kind: 'new-folder' }) },
                                { label: 'Upload folder', icon: FolderUp, onSelect: onUploadFolder }]
  const needle = q.trim()
  // a type or words list the documents anywhere under the folder, without its folders;
  // All documents (the top) lists its folders, then every document anywhere
  const flat = !!needle || !!type
  const all = !folder && !flat
  const [pages, setPages] = useState(1)
  useEffect(() => setPages(1), [needle, type, folder, sort])
  type Listed = { folders: Folder[]; items: Item[]; next: string | null; here: Folder | null }
  const list = useResource<Listed>(async signal => {
    const page = (cursor?: string): Promise<Listed> => flat || (all && cursor)
      ? documentsApi.items({ q: needle || undefined, kind: type ?? undefined, folder, sort, limit: 200, cursor }, signal)
        .then(r => ({ folders: [], items: r.items, next: r.next, here: null }))
      : all
        ? Promise.all([documentsApi.folder('', { sort, limit: 1 }, signal), documentsApi.items({ sort, limit: 200 }, signal)])
          .then(([f, r]) => ({ folders: f.folders, items: r.items, next: r.next, here: f.folder }))
        : documentsApi.folder(folder, { sort, limit: 200, cursor }, signal).then(r => ({ ...r, here: r.folder }))
    const out = await page()
    for (let n = 1; n < pages && out.next; n++) {
      const r = await page(out.next)
      out.items.push(...r.items)
      out.next = r.next
    }
    return out
  }, [needle, type, folder, sort, version, pages], {
    debounce: needle ? 150 : 0,
    events: ev => ev.type === 'documents.item' && (flat || all || (ev.item as Item | undefined)?.folder === folder),
  })
  const here = useResource(() => documentsApi.folder(folder, { limit: 1 }), [folder, version])
  const trash = useResource(() => documentsApi.trash(), [version, shelf], { enabled: shelf === 'trash' })
  const picker = usePicker(`${folder}:${needle}:${type}:${shelf}`)
  const folders = list.data?.folders ?? []
  const items = list.data?.items ?? []
  picker.keys.current = shelf === 'trash'
    ? [...(trash.data?.folders ?? []).map(f => `f:${f.path}`), ...(trash.data?.items ?? []).map(it => `i:${it.id}`)]
    : [...folders.map(f => `f:${f.path}`), ...items.map(it => `i:${it.id}`)]

  const bulk: BulkAction[] = shelf === 'trash'
    ? [{ label: 'Restore', icon: RotateCcw, done: 'restored', run: keys => both('restore', keys).then(r => { onChanged(); return r }) },
       { label: 'Delete permanently', icon: Trash2, danger: true, done: 'deleted',
         ask: n => ({ title: `Delete ${itemsNoun(n)} permanently?`, confirm: 'Delete permanently', body: 'This cannot be undone.' }),
         run: keys => both('purge', keys).then(r => { onChanged(); return r }) }]
    : [{ label: 'Move to', icon: FolderInput, done: 'moved', run: async keys => { onAsk({ kind: 'move', keys }); return { done: [], skipped: [] } } },
       { label: 'Move to Trash', icon: Trash2, danger: true, done: 'moved',
         ask: n => ({ title: `Move ${itemsNoun(n)} to Trash?`, confirm: 'Move to Trash' }),
         run: keys => both('trash', keys).then(r => { onChanged(); return r }) }]

  // the types this folder holds (from its unfiltered page), and the one picked
  const [present, setPresent] = useState<Set<string>>(new Set())
  useEffect(() => { if (!flat && list.data) setPresent(new Set(list.data.items.map(i => i.kind))) }, [list.data, flat])
  const types = Object.keys(TYPE_WORD).filter(t => t === type || present.has(t))
  const drag = (key: string) => ({ draggable: !picker.on, onDragStart: (e: React.DragEvent) => e.dataTransfer.setData(DRAG, key) })

  const title = nameOf(folder)
  const total = here.data?.folder.items
  const nothing = shelf === 'list' ? !!list.data && !flat && !folders.length && !items.length && !uploads.length
    : !!trash.data && !trash.data.folders.length && !trash.data.items.length
  const row = (it: Item) => {
    const where = [(flat || all) && it.folder, stem(it.name) !== it.title && it.name].filter(Boolean).join(' / ')
    const menu = itemMenu(it, () => picker.start(`i:${it.id}`))
    return (
      <div {...drag(`i:${it.id}`)}>
        <ListRow icon={TYPE_ICON[it.kind] ?? File} title={it.title} tooltip={it.path}
                 href={format({ section: 'documents', item: it.id, folder: it.folder || undefined })}
                 subtitle={it.state === 'pending' ? 'Extracting text' : it.state === 'failed' ? 'The text could not be extracted.' : where || undefined}
                 column={TYPE_WORD[it.kind]} cell={it.pages ? count(it.pages, 'page') : bytes(it.size)} meta={listTime(it.updated_at)}
                 pick={picker.row(`i:${it.id}`)} onContextMenu={e => ctx.open(e, menu, it.title)}
                 trailing={!picker.on && <MenuButton icon={MoreHorizontal} label="More" items={menu} size="sm" />} />
      </div>
    )
  }
  const before = (
    <>
      {uploads.length > 0 && (
        <div className="doc-uploads">
          {uploads.map(u => (
            <ListRow key={u.key} icon={Upload} title={u.name} progress={u.progress}
                     subtitle={u.error ?? (u.result ? resultLine(u.result) : u.progress != null ? `${Math.round(u.progress * 100)}%` : undefined)}
                     cell={u.error ? 'Failed' : undefined} trailingAlways={!!(u.error || u.result)}
                     trailing={(u.error || u.result) && <>
                       {u.retry && <Button size="sm" variant="ghost" icon={RotateCcw} onClick={u.retry}>Retry</Button>}
                       <IconButton icon={X} label="Dismiss" size="sm" onClick={() => onUploadDone(u.key)} />
                     </>} />
          ))}
        </div>
      )}
      {folders.length > 0 && (
        <div className="doc-folders">
          <GroupHead icon={FolderIcon} title="Folders" count={count(folders.length, 'folder')} />
          <CardGrid className="doc-cards">
            {folders.map(f => (
              <div key={f.path} className="doc-card" {...drag(`f:${f.path}`)} {...dropOn(f.path)}>
                <Card media={<FolderIcon size={20} aria-hidden />} title={f.name} subtitle={`${docsNoun(f.items)}, ${bytes(f.size)}`}
                      meta={listTime(f.updated_at)} href={format({ section: 'documents', folder: f.path })} menu={folderMenu(f)}
                      pick={picker.row(`f:${f.path}`)} tooltip={f.path} />
              </div>
            ))}
          </CardGrid>
        </div>
      )}
      {items.length > 0 && !flat && <GroupHead icon={FileText} title="Documents" count={docsNoun(all ? total ?? items.length : items.length)} />}
    </>
  )

  return (
    <div className="list-page">
      <PlaceHead level={1} title={title} titleAttr={folder || undefined} count={shelf === 'list' ? total : (trash.data ? trash.data.folders.length + trash.data.items.length : undefined)}
                 crumbs={folder ? crumbsOf(parentOf(folder), onFolder) : undefined}
                 search={{ value: q, onChange: setQ, placeholder: folder ? `Search in ${title}` : 'Search documents', loading: list.loading && !!needle }}
                 picker={nothing ? undefined : picker} shelf={{ trash: true, value: shelf, onChange: setShelf }}
                 more={headMenu}
                 newLabel="Upload" onNew={onUpload} />
      {(picker.on || (shelf === 'list' && !nothing)) && (
        <div className="list-page__top">
          {picker.on ? <SelectBar picker={picker} noun={itemsNoun} actions={bulk} />
            : <Toolbar filters={types.length || type ? [{ label: 'Type', icon: Shapes, picked: type ? TYPE_WORD[type] : undefined, onClear: () => setType(null),
                                                              items: types.map(t => ({ label: TYPE_WORD[t], current: t === type, onSelect: () => setType(t) })) }] : []}
                       sort={{ label: 'Sort', value: sort, options: SORTS, onChange: v => setSort(v as Sort) }} />}
        </div>
      )}
      {shelf === 'trash' ? <TrashList trash={trash} picker={picker} onChanged={onChanged} onOpen={it => nav.go({ section: 'documents', item: it.id, folder: it.folder || undefined })} />
        : (
          <Loaded page res={list} icon={FileText}
                  empty={nothing ? { title: 'No documents yet', action: <Button variant="primary" icon={Upload} onClick={onUpload}>Upload</Button> }
                    : flat && !items.length && { icon: SearchX, title: 'No documents match', action: <Button onClick={() => { setQ(''); setType(null) }}>Clear search</Button> }}>
            {data => (
              <div className="list-page__rows" onContextMenu={e => { if (e.target === e.currentTarget || (e.target as HTMLElement).closest('.vlist') === e.target) ctx.open(e, headMenu, title) }}>
                <VirtualList items={items} estimate={68} getKey={it => it.id} renderRow={row} label={title} before={before}
                             resetOn={`${folder}|${needle}|${type}|${sort}`}
                             onEndReached={() => { if (data.next && !list.loading) setPages(n => n + 1) }} />
              </div>
            )}
          </Loaded>
        )}
      {ctx.menu}
    </div>
  )
}

/** what an upload did that is worth a line: what was not added, what was there already */
function resultLine(r: UploadResult): string {
  const parts = [
    r.rejected.length ? `Not added: ${r.rejected.map(x => `${x.name} (${x.reason})`).join(', ')}.` : '',
    r.existing.length ? `Already in Documents: ${r.existing.map(x => x.item.path).join(', ')}.` : '',
  ]
  return parts.filter(Boolean).join(' ')
}

function TrashList({ trash, picker, onChanged, onOpen }: {
  trash: ReturnType<typeof useResource<{ folders: Folder[]; items: Item[] }>>
  picker: ReturnType<typeof usePicker>
  onChanged: () => void
  onOpen: (it: Item) => void
}) {
  const act = useAction(async (p: Promise<unknown>) => { await p; onChanged() })
  const t = trash.data
  type Gone = { key: string; folder?: Folder; item?: Item }
  const rows: Gone[] = t ? [...t.folders.map(f => ({ key: `f:${f.path}`, folder: f })), ...t.items.map(it => ({ key: `i:${it.id}`, item: it }))] : []
  return (
    <Loaded page res={trash} icon={Trash2} empty={!rows.length && { title: 'Trash is empty' }}>{() => (
    <div className="list-page__rows">
      {act.error && <InlineError compact message={act.error} />}
      <VirtualList items={rows} estimate={56} getKey={r => r.key} label="Trash"
                   renderRow={r => r.folder
                     ? <ShelfRow title={r.folder.path} subtitle={docsNoun(r.folder.items)} leading={<FolderIcon size={16} aria-hidden />}
                                 pick={picker.row(r.key)} onSelect={() => picker.start(r.key)}
                                 onRestore={() => void act.run(documentsApi.patchFolder(r.folder!.path, { trashed: false }))}
                                 onDelete={() => act.run(documentsApi.removeFolder(r.folder!.path))} />
                     : <ShelfRow title={r.item!.title} subtitle={`${r.item!.folder || 'Documents'}, ${bytes(r.item!.size)}`}
                                 leading={(() => { const I = TYPE_ICON[r.item!.kind] ?? File; return <I size={16} aria-hidden /> })()}
                                 pick={picker.row(r.key)} onSelect={() => picker.start(r.key)} onClick={() => onOpen(r.item!)}
                                 onRestore={() => void act.run(documentsApi.patch(r.item!.id, { trashed: false }))}
                                 onDelete={() => act.run(documentsApi.remove(r.item!.id))} />} />
    </div>
    )}</Loaded>
  )
}

/* ── one document: its content, then its details; out of date and failed states ── */
function DocumentPage({ id, folder, active, menu, onFolder }: {
  id: number
  folder: string | undefined
  active: boolean
  menu: (it: Item) => MenuItem[]
  onFolder: (path: string) => void
}) {
  const nav = useNav()
  const res = useResource(() => documentsApi.item(id), [id], {
    events: ev => (ev.type === 'documents.item' && (ev.item as Item | undefined)?.id === id) || (ev.type === 'documents.deleted' && ev.id === id),
  })
  const reindex = useAction(async () => { const r = await documentsApi.reindex(id); res.set(d => d && { ...d, ...r.item }) })
  const restore = useAction(async () => { const r = await documentsApi.patch(id, { trashed: false }); res.set(d => d && { ...d, ...r }) })
  const [mode, setMode] = useState<'view' | 'edit'>('view')
  const it = res.data
  // opened from a link: the list shows the document's folder, as the server says
  useEffect(() => {
    if (it && folder === undefined && it.folder) nav.go({ section: 'documents', item: it.id, folder: it.folder }, { replace: true })
  }, [it, folder, nav])
  if (!it) return <Loaded page res={res} icon={FileText} />
  const canEdit = editable(it) && !it.trashed
  const own = menu(it).filter(m => m === 'sep' || !('label' in m) || !['Open', 'Download', 'Select'].includes(String(m.label)))
  const detail = [TYPE_WORD[it.kind], it.pages ? count(it.pages, 'page') : '', bytes(it.size), it.n_words ? count(it.n_words, 'word') : ''].filter(Boolean).join(', ')
  return (
    <div className="doc-page">
      <PlaceHead level={1} crumbs={crumbsOf(it.folder, onFolder)} title={it.title} titleAttr={it.path} detail={it.trashed ? 'Trash' : detail}
                 actions={<>
                   {canEdit && <Segmented label="Show" value={mode} onChange={setMode}
                                          options={[{ value: 'view', label: 'View', icon: Eye }, { value: 'edit', label: 'Edit', icon: Pencil }]} />}
                   {it.kind === 'pdf' || it.kind === 'image'
                     ? <IconButton icon={ExternalLink} label="Open" href={fileUrl(it)} target="_blank" rel="noopener" />
                     : <IconButton icon={Download} label="Download" href={fileUrl(it)} download={it.name} />}
                 </>}
                 more={it.trashed ? [{ label: 'Restore', icon: RotateCcw, onSelect: () => void restore.run() }] : own} />
      <div className="page">
        <div className="page__col page__col--wide">
          <PageIndex sections={[{ id: 'd-text', label: 'Text' }, { id: 'd-details', label: 'Details' }]}>
            {it.state === 'failed' && (
              <Notice tone="error" action={{ label: 'Extract text', icon: ScanText, onClick: () => void reindex.run(), busy: reindex.busy }}>
                {it.error ?? 'The text could not be extracted.'}
              </Notice>
            )}
            {(reindex.error || restore.error) && <InlineError compact message={reindex.error ?? restore.error} />}
            <Section id="d-text" title="Text">
              {it.state === 'pending' ? <StatusLine tone="running" spinner>Extracting text</StatusLine>
                : it.kind === 'image' ? <img className="doc-image" src={documentsApi.fileUrl(it.id)} alt={it.title} />
                : editable(it) ? <TextContent item={it} edit={canEdit && mode === 'edit'} active={active} onSaved={() => { setMode('view'); res.reload() }} />
                : it.state === 'ready' ? <Passages item={it} /> : null}
            </Section>
            <Section id="d-details" title="Details">
              <Details item={it} onSaved={next => res.set(d => d && { ...d, ...next })} onRewrite={() => void reindex.run()} rewriting={reindex.busy} />
            </Section>
          </PageIndex>
        </div>
      </div>
    </div>
  )
}

/* a text file: shown (Markdown set in type, the rest as code), or edited with Save */
function TextContent({ item, edit, active, onSaved }: { item: ItemDetail; edit: boolean; active: boolean; onSaved: () => void }) {
  const text = useResource(() => documentsApi.text(item.id), [item.id, item.updated_at])
  const [draft, setDraft] = useState<string | null>(null)
  const value = draft ?? text.data ?? ''
  const dirty = draft !== null && draft !== text.data
  const md = /\.(md|markdown)$/i.test(item.path)
  const save = useAction(async () => {
    if (draft === null) return
    await documentsApi.writeText(item.path, draft)
    text.set(draft); setDraft(null); onSaved()
  })
  useEffect(() => {
    if (!active || !edit) return
    const onKey = (e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && dirty) { e.preventDefault(); void save.run() } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dirty, save, active, edit])
  if (text.data === undefined) return <Loaded res={text} compact inline />
  if (edit) {
    return (
      <div className="doc-edit">
        <TextArea mono rows={16} max={60} value={value} autoFocus spellCheck aria-label="Text" onChange={e => setDraft(e.target.value)} />
        <div className="doc-edit__foot">
          <span className="doc-edit__state" role={save.error ? 'alert' : undefined}>{save.error ?? (dirty ? 'Unsaved changes' : '')}</span>
          <Button disabled={!dirty} onClick={() => setDraft(null)}>Cancel</Button>
          <Button variant="primary" loading={save.busy} disabled={!dirty} onClick={() => void save.run()}>Save</Button>
        </div>
      </div>
    )
  }
  return (
    !value.trim() ? <p className="doc-quiet">Empty.</p>
      : md ? <div className={cx('prose', 'doc-text')}><Markdown text={value} /></div>
      : <pre className="doc-plain">{value}</pre>
  )
}

/* a PDF's (or a book's) text as it was extracted, page by page; the rest behind Show more */
function Passages({ item }: { item: ItemDetail }) {
  const [limit, setLimit] = useState(200)
  const segs = useResource(() => documentsApi.segments(item.id, { limit }), [item.id, item.updated_at, limit])
  if (!segs.data) return <Loaded res={segs} compact inline />
  const all = segs.data.segments
  if (!all.length) return <p className="doc-quiet">Empty.</p>
  const groups: { head: string; segs: typeof all }[] = []
  for (const s of all) {
    const head = s.page ? `Page ${s.page}` : s.speaker ?? ''
    const last = groups.at(-1)
    if (last && last.head === head) last.segs.push(s); else groups.push({ head, segs: [s] })
  }
  return (
    <div className="doc-passages">
      {groups.map((g, i) => (
        <div key={i}>
          {g.head && <GroupHead title={g.head} />}
          <div className="prose">{g.segs.map(s => s.kind === 'heading' ? <h3 key={s.id}>{s.text}</h3> : <p key={s.id}>{s.text}</p>)}</div>
        </div>
      ))}
      {segs.data.has_after && limit < 2000 && <Button onClick={() => setLimit(l => Math.min(l + 400, 2000))}>Show more</Button>}
    </div>
  )
}

/* your title and description (saved when you leave the field), the AI description, the mode */
function Details({ item, onSaved, onRewrite, rewriting }: { item: ItemDetail; onSaved: (it: Item) => void; onRewrite: () => void; rewriting: boolean }) {
  const [title, setTitle] = useState(item.user_title ?? '')
  const [desc, setDesc] = useState(item.user_description ?? '')
  useEffect(() => setTitle(item.user_title ?? ''), [item.user_title])
  useEffect(() => setDesc(item.user_description ?? ''), [item.user_description])
  const save = useAction(async (body: { title?: string | null; description?: string | null; mode?: Mode }) => onSaved(await documentsApi.patch(item.id, body)))
  const saveTitle = () => { if (title.trim() !== (item.user_title ?? '')) void save.run({ title: title.trim() || null }) }
  const saveDesc = () => { if (desc.trim() !== (item.user_description ?? '')) void save.run({ description: desc.trim() || null }) }
  const ai = item.ai
  return (
    <div className="doc-details">
      <Field label="Your title">
        <Input value={title} placeholder={item.file_title || item.name} onChange={e => setTitle(e.target.value)} onBlur={saveTitle}
               onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); saveTitle() } }} />
      </Field>
      <Field label="Your description">
        <TextArea rows={2} max={12} value={desc} onChange={e => setDesc(e.target.value)} onBlur={saveDesc} />
      </Field>
      {save.error && <InlineError compact message={save.error} />}
      {ai.state !== 'skipped' && (
        <Field label="AI description" group>
          {ai.state === 'done' && item.ai_description ? <p className="doc-ai">{item.ai_description}</p>
            : ai.state === 'failed' ? <p className="doc-ai doc-quiet">{ai.error ?? 'The AI description failed.'}</p>
            : <StatusLine tone="running" spinner>Running</StatusLine>}
          {ai.state !== 'pending' && item.state === 'ready' && (
            <span className="doc-ai__row">
              {ai.model && ai.generated_at && <StatusLine>{`${ai.model}, ${stamp(ai.generated_at)}`}</StatusLine>}
              {ai.stale && <StatusLine tone="warn">Out of date</StatusLine>}
              <Button size="sm" variant={ai.stale ? 'secondary' : 'ghost'} icon={RefreshCw} loading={rewriting} onClick={onRewrite}>Regenerate</Button>
            </span>
          )}
        </Field>
      )}
      <Field label="Mode" group>
        <Choice label="Mode" value={item.mode} options={MODES} disabled={save.busy} onChange={mode => void save.run({ mode })} />
      </Field>
      <Facts items={[{ label: 'Created', value: stamp(item.added_at) },
                     ...(item.backlinks.length ? [{ label: 'Linked from', value: <LinkedFrom links={item.backlinks} /> }] : [])]} />
    </div>
  )
}

function LinkedFrom({ links }: { links: ItemDetail['backlinks'] }) {
  return <span className="doc-links">{links.map(l => <a key={l.id} href={format({ section: 'documents', item: l.id })} title={l.path}>{l.title}</a>)}</span>
}

/* ── the dialogs: a name, a folder to move into, a move to the Trash ── */
function Asked({ ask, folder, tree, onClose, onDone, onMove }: {
  ask: Ask
  folder: string
  tree: TreeNode[]
  onClose: () => void
  onDone: (it?: Item) => void
  onMove: (keys: string[], into: string) => void
}) {
  const [value, setValue] = useState(ask.kind === 'rename' ? ask.item.name : ask.kind === 'rename-folder' ? ask.folder.name : '')
  const [into, setInto] = useState(folder)
  const [open, setOpen] = useState<Set<string>>(new Set(['']))
  const go = useAction(async (fn: () => Promise<Item | void>) => onDone((await fn()) ?? undefined))
  const nodes = useMemo(() => [{ id: '', label: 'Documents', icon: FileText, children: tree }], [tree])

  if (ask.kind === 'trash') {
    return <ConfirmDialog title={`Move ${ask.keys.length === 1 ? ask.name : itemsNoun(ask.keys.length)} to Trash?`} confirmLabel="Move to Trash" danger
                          onClose={onClose}
                          onConfirm={async () => { try { await both('trash', ask.keys); onDone() } catch (e) { return errorText(e) } }} />
  }
  if (ask.kind === 'move') {
    return (
      <Dialog title={ask.keys.length === 1 ? 'Move to' : `Move ${itemsNoun(ask.keys.length)}`} size="sm" onClose={onClose}
              actions={<><Button onClick={onClose}>Cancel</Button>
                <Button variant="primary" icon={FolderInput} onClick={() => onMove(ask.keys, into)}>Move</Button></>}>
        <Tree nodes={nodes} open={open} label="Folders" selected={into} onSelect={setInto}
              onOpen={(id, on) => setOpen(o => { const s = new Set(o); if (on) s.add(id); else s.delete(id); return s })} />
      </Dialog>
    )
  }
  const a = ask
  const title = a.kind === 'new-folder' ? 'New folder' : a.kind === 'new-note' ? 'New note' : 'Rename'
  const ok = !!value.trim()
  const submit = () => void go.run(async () => {
    const v = value.trim()
    if (a.kind === 'new-folder') { await documentsApi.createFolder(join(folder, v)) }
    else if (a.kind === 'new-note') {
      const name = /\.(md|txt)$/i.test(v) ? v : `${v}.md`
      return (await documentsApi.writeText(join(folder, name), `# ${stem(name)}\n\n`)).item
    } else if (a.kind === 'rename') return documentsApi.patch(a.item.id, { name: v })
    else await documentsApi.patchFolder(a.folder.path, { name: v })
  })
  return (
    <Dialog title={title} size="sm" onClose={onClose}
            actions={<><Button onClick={onClose}>Cancel</Button>
              <Button variant="primary" loading={go.busy} disabled={!ok} onClick={submit}>{a.kind.startsWith('rename') ? 'Save' : 'Add'}</Button></>}>
      <form onSubmit={e => { e.preventDefault(); if (ok) submit() }}>
        <Field label="Name" error={go.error}>
          <Input autoFocus value={value} onChange={e => setValue(e.target.value)} spellCheck={false}
                 placeholder={a.kind === 'new-note' ? join(folder, 'notes.md') : undefined} />
        </Field>
      </form>
    </Dialog>
  )
}

/* files picked or dropped: your title (one file), and each file's mode, starting at the
   one the server would give it */
function UploadDialog({ files, paths, into, onClose, onAdd }: { files: File[]; paths: string[]; into: string; onClose: () => void; onAdd: (modes: Mode[], title: string) => void }) {
  const [title, setTitle] = useState('')
  const defaults = useResource(() => documentsApi.defaultModes(files.map(f => ({ name: f.name, size: f.size }))), [files])
  const [modes, setModes] = useState<Mode[] | null>(null)
  useEffect(() => { if (defaults.data) setModes(m => m ?? defaults.data!.modes) }, [defaults.data])
  return (
    <Dialog title={`Upload to ${into}`} size="md" onClose={onClose}
            actions={<><Button onClick={onClose}>Cancel</Button>
              <Button variant="primary" icon={Upload} disabled={!modes} onClick={() => modes && onAdd(modes, title.trim())}>Upload</Button></>}>
      {files.length === 1 && (
        <Field label="Title"><Input value={title} placeholder={files[0]!.name} onChange={e => setTitle(e.target.value)} /></Field>
      )}
      <div className="doc-picked">
        {files.map((f, i) => (
          <div key={`${paths[i]}:${i}`} className="doc-picked__row">
            <span className="doc-picked__name" title={paths[i]}>{paths[i]}</span>
            <span className="doc-quiet">{bytes(f.size)}</span>
            {modes && <Segmented label={`Mode of ${f.name}`} value={modes[i]!} options={MODES}
                                 onChange={mode => setModes(m => m && m.map((x, j) => (j === i ? mode : x)))} />}
          </div>
        ))}
      </div>
      {defaults.error && <InlineError compact message={defaults.error} onRetry={defaults.reload} />}
    </Dialog>
  )
}

