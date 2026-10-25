"""Documents over HTTP: /api/documents/*.

Stateless: every call reads or changes rows and files and returns the result; reading
files and writing AI descriptions happen in tasks (`kb.task` events; an upload's task
is listed, automatic runs are quiet), their progress on the items (`documents.item`
events). Every change to a folder's contents is `documents.changed {path}`.
"""
from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import anyio
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import box_files
from ..errors import CelineError
from ..kb.documents import MODES, DocumentsKB, DocumentsPathError
from ..kb.documents_extract import kind_of
from .cursor import page_cursor, read_cursor
from .deps import in_thread
from .kb import kb_changed

router = APIRouter(prefix="/api/documents")

INLINE = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/gif",
          "text/plain", "text/markdown"}
TEXTUAL = {"note", "text", "code", "data", "subtitles"}
Sort = Literal["name", "added", "changed", "type", "size"]
Order = Literal["asc", "desc"]


# ---- shapes -------------------------------------------------------------------------------

class AI(BaseModel):
    state: Literal["pending", "done", "failed", "skipped"]
    model: str | None
    generated_at: int | None
    error: str | None
    stale: bool


class Item(BaseModel):
    id: int
    path: str
    folder: str
    name: str
    title: str
    file_title: str
    user_title: str | None
    user_description: str | None
    ai_description: str | None
    ai: AI
    kind: str
    mode: Literal["chunked", "full_file"]
    mime: str | None
    size: int
    pages: int | None
    n_segments: int
    n_chars: int
    n_words: int
    state: Literal["pending", "ready", "failed"]
    error: str | None
    sha256: str
    added_at: int
    updated_at: int
    author: Literal["user", "celine"]
    trashed: bool


class Backlink(BaseModel):
    id: int
    path: str
    title: str


class ItemDetail(Item):
    backlinks: list[Backlink]


class Folder(BaseModel):
    path: str
    name: str
    items: int
    size: int
    updated_at: int
    added_at: int
    trashed_at: int | None


class Crumb(BaseModel):
    name: str
    path: str


class FolderPage(BaseModel):
    folder: Folder
    breadcrumbs: list[Crumb]
    folders: list[Folder]           # on the first page only
    items: list[Item]
    next: str | None


class ItemsPage(BaseModel):
    items: list[Item]
    next: str | None


class Existing(BaseModel):
    name: str
    item: Item
    asked: Literal["chunked", "full_file"] | None


class Rejected(BaseModel):
    name: str
    reason: str


class UploadResult(BaseModel):
    items: list[Item]
    existing: list[Existing]
    rejected: list[Rejected]
    task: str | None


class DefaultModes(BaseModel):
    modes: list[Literal["chunked", "full_file"]]


class Deleted(BaseModel):
    deleted: int
    id: int


class Reindexed(BaseModel):
    ok: bool
    item: Item


class Segment(BaseModel):
    id: int
    ref: str
    seq: int
    kind: str
    page: int | None
    heading: str | None
    t_ms: int | None
    speaker: str | None
    text: str


class Segments(BaseModel):
    segments: list[Segment]
    has_before: bool
    has_after: bool


class Written(BaseModel):
    path: str
    text: str
    item: Item | None


# ---- plumbing -----------------------------------------------------------------------------

def _kb(request: Request) -> DocumentsKB:
    kb = request.app.state.services.kbs.documents()
    if kb is None:
        raise CelineError("not_found", "Documents is not set up. Add a block with "
                                       "kind = \"documents\" to config/kb.toml.", 404)
    return kb


async def _call(fn, *args, **kw):
    """Run in a thread; map the store's exceptions to HTTP errors."""
    try:
        return await in_thread(fn, *args, **kw)
    except KeyError as e:
        raise CelineError("not_found", f"Document {e.args[0] if e.args else ''} "
                                       f"does not exist.", 404) from None
    except FileNotFoundError as e:
        raise CelineError("not_found", f"{e.args[0] if e.args else 'The file'} does not exist.",
                          404) from None
    except (DocumentsPathError, ValueError) as e:
        raise CelineError("bad_request", str(e), 400) from None


async def _item(kb: DocumentsKB, iid: int) -> dict[str, Any]:
    it = await anyio.to_thread.run_sync(kb.item, iid)
    if it is None:
        raise CelineError("not_found", f"Document {iid} does not exist.", 404)
    return it


def _offset(cursor: str | None, *key: str) -> int:
    """The opaque `next` of a page: where the next page starts, for this listing."""
    got = read_cursor(cursor)
    if got is None:
        return 0
    if got[1:] != key:
        raise CelineError("bad_request", "cursor belongs to another listing", 400)
    return max(0, got[0])


def _next(offset: int, shown: int, total: int, *key: str) -> str | None:
    return page_cursor((offset + shown, *key)) if offset + shown < total else None


# ---- folders ------------------------------------------------------------------------------

@router.get("/folders/{path:path}", response_model=FolderPage)
async def folder_page(request: Request, path: str, sort: Sort = "name",
                      order: Order | None = None, limit: int = 200,
                      cursor: str | None = None) -> dict:
    """One folder (/folders/ is the top): its folders (first page only), then its documents,
    sorted by `sort` (name, added, changed, type, size) in `order` (default: names and
    types A to Z, the rest newest and biggest first). Trashed ones are left out."""
    kb = _kb(request)
    key = (path.strip("/"), f"{sort}:{order or ''}")
    off = _offset(cursor, *key)
    desc = None if order is None else order == "desc"
    folder = await _call(kb.folder, path)
    page = await _call(kb.list_items, folder=path, inside=True, sort=sort,
                       desc=desc, limit=max(1, min(limit, 1000)), offset=off)
    return {"folder": folder, "breadcrumbs": kb.breadcrumbs(folder["path"]),
            "folders": await _call(kb.folders_in, path, sort=sort, desc=desc)
            if off == 0 else [],
            "items": page["items"],
            "next": _next(off, len(page["items"]), page["total"], *key)}


class NewFolder(BaseModel):
    path: str


@router.post("/folders", response_model=Folder)
async def create_folder(request: Request, body: NewFolder) -> dict:
    """A new folder (and the folders above it). 400 when it exists."""
    kb = _kb(request)
    return await _call(kb.make_folder, body.path)


class FolderPatch(BaseModel):
    name: str | None = None
    folder: str | None = None
    trashed: bool | None = None


@router.patch("/folders/{path:path}", response_model=Folder)
async def patch_folder(request: Request, path: str, body: FolderPatch) -> dict:
    """Rename a folder (`name`), move it into another (`folder`, "" is the top), put
    it in the trash with its documents or restore it (`trashed`). Fields left out are
    unchanged."""
    kb = _kb(request)
    out = await _call(kb.folder, path)
    if body.trashed is not None:
        out = await _call(kb.trash_folder, path, body.trashed)
    if body.name is not None or body.folder is not None:
        out = await _call(kb.move_folder, path, name=body.name, folder=body.folder)
    kb_changed(request, kb.id)
    return out


@router.delete("/folders/{path:path}")
async def delete_folder(request: Request, path: str) -> dict:
    """A trashed folder for good: the folder on disk and every file in it."""
    kb = _kb(request)
    out = await _call(kb.delete_folder, path)
    kb_changed(request, kb.id)
    return out


class Trash(BaseModel):
    folders: list[Folder]
    items: list[Item]


# ---- items --------------------------------------------------------------------------------

@router.get("", response_model=ItemsPage | Trash)
async def items(request: Request, q: str | None = None, folder: str = "",
                kind: str | None = None, sort: Sort = "name", order: Order | None = None,
                limit: int = 200, cursor: str | None = None, trashed: bool = False) -> dict:
    """Documents anywhere under `folder` (default all), matching `q` (names,
    descriptions, and the text of chunked documents) and `kind`; trashed ones left
    out. `trashed=1`: the trash instead, newest first: the folders, then the documents
    that are not in one of them (`{folders, items}`)."""
    kb = _kb(request)
    if trashed:
        return await _call(kb.trash)
    if folder.strip("/"):
        await _call(kb.folder_dir, folder)
    key = (folder.strip("/"), f"{q or ''}:{kind or ''}:{sort}:{order or ''}")
    off = _offset(cursor, *key)
    page = await _call(kb.list_items, folder=folder, q=q, kind=kind,
                       sort=sort, desc=None if order is None else order == "desc",
                       limit=max(1, min(limit, 1000)), offset=off)
    return {"items": page["items"],
            "next": _next(off, len(page["items"]), page["total"], *key)}


@router.post("", response_model=UploadResult)
async def upload(request: Request, files: list[UploadFile] = File(...),
                 paths: list[str] = Form([]), modes: list[str] = Form([]),
                 folder: str = Form(""), title: str = Form(""),
                 description: str = Form("")) -> dict:
    """Add files (several, a whole folder, drag and drop) into `folder`. `paths`: each
    file's path inside the upload, in the order of `files` ("stories/a.md" for a folder
    upload; missing or "": its name), so a folder keeps its folders. Each file is stored
    as it came; the same content twice is not copied again (`existing`). `modes`:
    "chunked" or "full_file" per file, in the order of `files` (missing or "": the
    default for its kind and size). `title`/`description` apply to every file of this
    request (meant for one)."""
    kb = _kb(request)
    bad = [m for m in modes if m and m not in MODES]
    if bad:
        raise CelineError("bad_request", f"The mode is chunked or full_file, not "
                                         f"{bad[0]!r}.", 400)
    base = folder.strip("/")
    if base:
        await _call(kb.resolve, base)                    # confined to the folder
    mb = request.app.state.services.cfg.r("max_upload_mb")   # Settings, Advanced
    limit = int(mb) * 1024 * 1024
    tmp_dir = kb.root / ".uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"items": [], "existing": [], "rejected": [], "task": None}
    touched: set[str] = set()
    for i, f in enumerate(files):
        mode = (modes[i] if i < len(modes) else "") or None
        inner = PurePosixPath((paths[i] if i < len(paths) else "") or f.filename or "file")
        name = inner.name or "file"
        sub = "/".join(p for p in (base, *inner.parent.parts) if p not in ("", "."))
        tmp = tmp_dir / f"{uuid.uuid4().hex}"
        size = 0
        too_big = False
        try:
            with tmp.open("wb") as fh:
                while chunk := await f.read(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        too_big = True
                        break
                    fh.write(chunk)
            if too_big:
                out["rejected"].append({"name": str(inner), "reason": f"The file is larger "
                                        f"than {mb} MB, the Largest upload in Settings, "
                                        f"Advanced."})
                continue
            if size == 0:
                out["rejected"].append({"name": str(inner), "reason": "The file is empty."})
                continue
            res = await _call(kb.add_file, tmp, name, folder=sub, title=title or None,
                              description=description or None, mode=mode)
        except CelineError as e:
            out["rejected"].append({"name": str(inner), "reason": e.message})
            continue
        except OSError as e:
            out["rejected"].append({"name": str(inner), "reason": f"The app could not store "
                                                                 f"the file: {e.strerror}."})
            continue
        finally:
            tmp.unlink(missing_ok=True)
            await f.close()
        it = res["item"]
        if res["existing"]:
            # the file is not added again, and the item keeps its own mode
            out["existing"].append({"name": str(inner), "item": it, "asked": mode})
        else:
            out["items"].append(it)
            touched.add(it["folder"])
    cfg = request.app.state.services.cfg
    for it in out["items"]:          # a copy in its box, ~/uploads/ (box_files.py)
        box_files.copy_later(cfg, kb.resolve(it["path"]), it["name"])
    if out["items"]:
        t = await _call(kb.kick, quiet=False)       # the user's upload: a listed task
        out["task"] = t.id if t is not None else None
        kb.changed(touched)
        kb_changed(request, kb.id)
    return out


class PickedFile(BaseModel):
    name: str
    size: int


class DefaultModesBody(BaseModel):
    files: list[PickedFile]


@router.post("/default-modes", response_model=DefaultModes)
async def default_modes(request: Request, body: DefaultModesBody) -> dict:
    """The mode each file would get without a choice, from its name and size: the
    upload dialog preselects exactly what the server would store."""
    kb = _kb(request)
    return {"modes": [kb.default_mode(kind_of(Path(f.name)), f.size) for f in body.files]}


@router.get("/{iid}", response_model=ItemDetail)
async def get_item(request: Request, iid: int) -> dict:
    """The item and the Markdown items linking to it (`backlinks`)."""
    kb = _kb(request)
    it = await _item(kb, iid)
    it["backlinks"] = await _call(kb.backlinks, iid)
    return it


class ItemPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    mode: Literal["chunked", "full_file"] | None = None
    name: str | None = None
    folder: str | None = None
    trashed: bool | None = None


@router.patch("/{iid}", response_model=Item)
async def patch_item(request: Request, iid: int, body: ItemPatch) -> dict:
    """The user's title and description (null or "" clears one), `mode` (a change
    reads the file again), `name` to rename the file, `folder` to move it into another
    folder ("" is the top), `trashed` to put it in the trash or restore it. Fields
    left out are unchanged."""
    kb = _kb(request)
    await _item(kb, iid)
    sent = body.model_dump(exclude_unset=True)
    meta = {k: sent[k] for k in ("title", "description") if k in sent}
    if meta:
        await _call(kb.set_meta, iid, **meta)
    if sent.get("mode"):
        await _call(kb.set_mode, iid, sent["mode"], quiet=False)
    if sent.get("trashed") is not None:
        await _call(kb.set_trashed, iid, sent["trashed"])
    if sent.get("name") is not None or sent.get("folder") is not None:
        await _call(kb.move_item, iid, name=sent.get("name"), folder=sent.get("folder"))
    kb_changed(request, kb.id)
    return await _item(kb, iid)


@router.delete("/{iid}", response_model=Deleted)
async def delete_item(request: Request, iid: int) -> dict:
    """Delete permanently: the file and everything about it (segments, descriptions)."""
    kb = _kb(request)
    out = await _call(kb.delete_item, iid)
    kb_changed(request, kb.id)
    return out


class ReindexBody(BaseModel):
    describe: bool = False


@router.post("/{iid}/reindex", response_model=Reindexed)
async def reindex_item(request: Request, iid: int, body: ReindexBody | None = None) -> dict:
    """Read the file again; `describe: true` also writes its AI description again."""
    kb = _kb(request)
    await _call(kb.reindex, iid, describe=True if body is None else body.describe,
                quiet=False)
    return {"ok": True, "item": await _item(kb, iid)}


@router.get("/{iid}/segments", response_model=Segments)
async def item_segments(request: Request, iid: int, from_seq: int = 0, limit: int = 500,
                        around: int | None = None) -> dict:
    kb = _kb(request)
    await _item(kb, iid)
    return await _call(kb.segments, iid, from_seq=from_seq, limit=max(1, min(limit, 2000)),
                       around=around)


@router.get("/{iid}/file")
async def item_file(request: Request, iid: int, download: bool = False):
    kb = _kb(request)
    it = await _item(kb, iid)
    p = await _call(kb.resolve, it["path"], must_exist=True)
    mime = it["mime"] or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if it["kind"] in TEXTUAL:
        mime = "text/plain"
    inline = not download and mime in INLINE
    # no-cache: asked again after a save, the browser checks the ETag instead of
    # answering from its cache with the text before
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
    return FileResponse(p, media_type=f"{mime}; charset=utf-8" if mime.startswith("text/")
                        else mime, filename=p.name,
                        content_disposition_type="inline" if inline else "attachment",
                        headers=headers)


# ---- Markdown written in the app ------------------------------------------------------------

class TextBody(BaseModel):
    text: str


@router.put("/text-files/{path:path}", response_model=Written)
async def write_text(request: Request, path: str, body: TextBody) -> dict:
    """Create or replace a Markdown (or .txt) file from the app's editor; `.md` is added
    to a name without a text suffix. Returns {"path", "text", "item"}."""
    kb = _kb(request)
    out = await _call(kb.write, path, body.text)
    kb_changed(request, kb.id)
    return out
