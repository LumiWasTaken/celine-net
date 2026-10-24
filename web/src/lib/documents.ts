/* Documents: your drawer of files of any kind, in folders, shared with Celine. Each document has three layers Celine sees before reading
   it: your title, your description, and an AI description (high level, no spoilers),
   and a mode: chunked (passages it searches) or full_file (read as one piece). */
import { del, json, query, request, requestText, upload } from './api'

type DocumentKind = 'note' | 'text' | 'code' | 'data' | 'html' | 'pdf' | 'docx' | 'subtitles' | 'epub' | 'image' | 'file'

export type Item = {
  id: number
  path: string
  folder: string
  name: string
  /** your title, else the file's, else its name */
  title: string
  file_title?: string | null
  user_title: string | null
  user_description: string | null
  ai_description: string | null
  ai: { state: 'pending' | 'done' | 'failed' | 'skipped'; model?: string | null; generated_at?: number | null; error?: string | null; stale?: boolean }
  kind: DocumentKind
  mode: Mode
  mime?: string | null
  size: number
  pages?: number | null
  n_segments?: number
  n_chars?: number
  n_words?: number
  state: 'pending' | 'ready' | 'failed'
  error: string | null
  added_at: number
  updated_at: number
  /** who wrote the file: you, or Celine */
  author: 'user' | 'celine'
  /** in the trash: it still opens by its id */
  trashed: boolean
}

export type Mode = 'chunked' | 'full_file'

export type ItemDetail = Item & {
  backlinks: { id: number; path: string; title: string }[]
}

/** a folder ("" is the top); `items` and `size` count the documents anywhere under it */
export type Folder = { path: string; name: string; items: number; size: number; updated_at: number; added_at: number; trashed_at: number | null }

export type Sort = 'name' | 'added' | 'changed' | 'type' | 'size'
type Order = 'asc' | 'desc'

export type FolderPage = {
  folder: Folder
  breadcrumbs: { name: string; path: string }[]
  /** on the first page only */
  folders: Folder[]
  items: Item[]
  next: string | null
}

export type Segment = { id: number; ref: string; seq: number; kind: string; page?: number | null; heading?: string | null
  t_ms?: number | null; speaker?: string | null; text: string }

export type UploadResult = {
  items: Item[]
  /** already in Documents: not added again, and the item keeps its mode (`asked`: the
   *  mode this upload asked for, or null) */
  existing: { name: string; item: Item; asked: Mode | null }[]
  rejected: { name: string; reason: string }[]
  task: string | null
}


type Listing = { sort?: Sort; order?: Order; limit?: number; cursor?: string | null }

/** a folder's or file's path in the URL, each part encoded, the slashes kept ("" is the top) */
const at = (path: string) => path.split('/').map(encodeURIComponent).join('/')

export const documentsApi = {
  /** the mode each file gets without a choice (the server's rule, by kind and size) */
  defaultModes: (files: { name: string; size: number }[]) =>
    request<{ modes: Mode[] }>('/documents/default-modes', json('POST', { files })),
  /** one folder: its folders (first page), then its documents */
  folder: (path: string, p: Listing = {}, signal?: AbortSignal) =>
    request<FolderPage>(`/documents/folders/${at(path)}${query(p)}`, { signal }),
  createFolder: (path: string) =>
    request<Folder>('/documents/folders', json('POST', { path })),
  /** rename (`name`), move into (`folder`, "" is the top), into the trash with its documents or back */
  patchFolder: (path: string, body: { name?: string; folder?: string; trashed?: boolean }) =>
    request<Folder>(`/documents/folders/${at(path)}`, json('PATCH', body)),
  /** a trashed folder for good, with every file in it */
  removeFolder: (path: string) => del<{ deleted: number }>(`/documents/folders/${at(path)}`),
  /** the trash, newest first: the folders, then the documents not in one of them */
  trash: () => request<{ folders: Folder[]; items: Item[] }>('/documents?trashed=1'),
  /** documents anywhere under `folder` matching `q` */
  items: (p: Listing & { q?: string; folder?: string; kind?: string } = {}, signal?: AbortSignal) =>
    request<{ items: Item[]; next: string | null }>(`/documents${query(p)}`, { signal }),
  item: (id: number) => request<ItemDetail>(`/documents/${id}`),
  /** null or "" clears your title / description; `name` renames the file, `folder` moves it */
  patch: (id: number, body: { title?: string | null; description?: string | null; mode?: Mode; name?: string; folder?: string; trashed?: boolean }) =>
    request<Item>(`/documents/${id}`, json('PATCH', body)),
  /** for good: the file and everything about it */
  remove: (id: number) => del<{ deleted: number }>(`/documents/${id}`),
  reindex: (id: number) => request<{ ok: boolean; item: Item }>(`/documents/${id}/reindex`, json('POST', { describe: true })),
  segments: (id: number, p: { from_seq?: number; limit?: number } = {}) =>
    request<{ segments: Segment[]; has_before: boolean; has_after: boolean }>(`/documents/${id}/segments${query(p)}`),
  fileUrl: (id: number, download = false) => `/api/documents/${id}/file${download ? '?download=1' : ''}`,
  /** the text of a note or text file, for the editor */
  text: (id: number) => requestText(`/documents/${id}/file`),
  /** create or replace a Markdown/.txt file */
  writeText: (path: string, text: string) =>
    request<{ path: string; text: string; item: Item }>(`/documents/text-files/${at(path)}`, json('PUT', { text })),
  /** files into `folder`; `paths` keeps a dropped folder's folders; `title` is your title of each */
  upload: (files: File[], opts: { folder?: string; paths?: string[]; modes?: Mode[]; title?: string } = {}, onProgress?: (share: number) => void) => {
    const form = new FormData()
    for (const f of files) form.append('files', f, f.name)
    for (const p of opts.paths ?? []) form.append('paths', p)
    for (const m of opts.modes ?? []) form.append('modes', m)
    if (opts.folder) form.append('folder', opts.folder)
    if (opts.title) form.append('title', opts.title)
    return upload<UploadResult>('/documents', form, onProgress)
  },
}
