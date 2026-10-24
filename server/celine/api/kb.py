"""The chat sources over HTTP: their chats, messages, people and tasks, and the
Messages archive's search (Documents: api/documents.py)."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..errors import CelineError
from ..kb import KBs
from ..kb.chat import ChatKB
from ..kb.identity import REF, kept_sync, snapshot
from ..kb.media import media_dir
from .attachments import SAFE_FILE_HEADERS, shown_inline
from ..core.event_types import KB_CHANGED
from .deps import in_thread

router = APIRouter(prefix="/api")


def _nf(message: str) -> CelineError:
    """404 with a sentence that says what is missing."""
    return CelineError("not_found", message, 404)


def _kbs(request: Request) -> KBs:
    return request.app.state.services.kbs


def chat_kb(request: Request, sid: str) -> ChatKB:
    kb = _kbs(request).get(sid)
    if kb is None:
        raise _nf(f"There is no source called {sid} in config/kb.toml.")
    if not isinstance(kb, ChatKB):
        raise CelineError("bad_request", f"{sid} is not a chat source.", 400)
    return kb


def kb_changed(request: Request, sid: str) -> None:
    """`kb.changed` on the global feed: the pages showing that source refetch."""
    request.app.state.services.store.emit_global({"type": KB_CHANGED, "kb": sid})


# ---- knowledge bases ------------------------------------------------------------------

@router.get("/sources")
async def list_kbs(request: Request) -> dict:
    out = []
    for kb in list(_kbs(request).all.values()):   # a runner may open one meanwhile
        out.append(await in_thread(kb.info))
    return {"kbs": out}


def _range(request: Request) -> tuple[int | None, int | None]:
    qp = request.query_params
    try:
        return (int(qp["from"]) if qp.get("from") else None,
                int(qp["to"]) if qp.get("to") else None)
    except ValueError:
        raise CelineError("bad_request", "The from and to values are milliseconds since the "
                          "epoch.", 400)


@router.get("/archive/search")
async def search(request: Request, q: str = "", kb: str | None = None,
                 chat: str | None = None, person: str | None = None,
                 sort: str = "recent", limit: int = Query(50, ge=1, le=200),
                 cursor: str | None = None, exact: bool = False,
                 top: int = Query(0, ge=0, le=10),
                 literal: bool = False) -> dict:
    """Instant search."""
    ts_from, ts_to = _range(request)
    ids = await snapshot(request.app.state.services.store)
    return await in_thread(_kbs(request).search, q, kbs=kb, chat=chat, person=person,
                      ts_from=ts_from, ts_to=ts_to, sort=sort, identities=ids,
                      limit=limit, cursor=cursor,
                      count_cap=None if exact else 1000, top=top,
                      correct=not literal)


@router.get("/archive/search/facets")
async def search_facets(request: Request, q: str = "", kb: str | None = None,
                        chat: str | None = None, person: str | None = None,
                        top: int = Query(10, ge=1, le=50), literal: bool = False) -> dict:
    ts_from, ts_to = _range(request)
    ids = await snapshot(request.app.state.services.store)
    return await in_thread(_kbs(request).facets, q, kbs=kb, chat=chat, person=person,
                      ts_from=ts_from, ts_to=ts_to, top=top, identities=ids,
                      correct=not literal)


@router.get("/archive/search/parse")
async def search_parse(request: Request, q: str = "") -> dict:
    return await in_thread(_kbs(request).parse, q, await snapshot(request.app.state.services.store))


# ---- chats ---------------------------------------------------------------------------

_CHAT_ORDER = {"name": lambda c: (c["name"] or "").casefold(),
               "count": lambda c: -c["messages"]}


@router.get("/archive/chats")
async def all_chats(request: Request, q: str = "", sort: str = "recent",
                    trashed: bool = False, limit: int = Query(200, ge=1, le=1000),
                    offset: int = Query(0, ge=0)) -> dict:
    """Every chat source's chats as one list (All messages), in the same orders."""

    def run() -> dict:
        # note: each source reads offset+limit rows; a merge cursor if paging gets deep
        pages = [kb.list_chats(q, sort, trashed, offset + limit, 0)
                 for kb in _kbs(request).chats()]
        rows = [c for p in pages for c in p["chats"]]
        rows.sort(key=_CHAT_ORDER.get(sort, lambda c: -(c["last_ts"] or 0)))
        return {"chats": rows[offset:offset + limit], "total": sum(p["total"] for p in pages)}
    return await in_thread(run)


@router.get("/sources/{sid}/chats")
async def chats(request: Request, sid: str, q: str = "", sort: str = "recent",
                trashed: bool = False, limit: int = Query(200, ge=1, le=1000),
                offset: int = Query(0, ge=0)) -> dict:
    """The chats; with `trashed` the trashed ones instead."""
    kb = chat_kb(request, sid)
    return await in_thread(kb.list_chats, q, sort, trashed, limit, offset)


@router.get("/sources/{sid}/chats/{chat_id}")
async def chat(request: Request, sid: str, chat_id: int) -> dict:
    kb = chat_kb(request, sid)
    c = await in_thread(kb.get_chat, chat_id)
    if c is None:
        raise _nf(f"There is no chat {chat_id} in {sid}.")
    return c


class ChatPatch(BaseModel):
    """Only the fields sent are changed; `name: null` clears your name."""
    model_config = {"extra": "forbid"}
    name: str | None = Field(default=None, max_length=200)
    trashed: bool | None = None


@router.patch("/sources/{sid}/chats/{chat_id}")
async def patch_chat(request: Request, sid: str, chat_id: int, body: ChatPatch) -> dict:
    kb = chat_kb(request, sid)
    fields = body.model_fields_set
    c = await in_thread(kb.update_chat, chat_id,
                   name=body.name if "name" in fields else ...,
                   trashed=body.trashed if "trashed" in fields and body.trashed is not None
                   else ...)
    if c is None:
        raise _nf(f"There is no chat {chat_id} in {sid}.")
    return c


@router.get("/sources/{sid}/chats/{chat_id}/messages")
async def messages(request: Request, sid: str, chat_id: int, before: int | None = None,
                   after: int | None = None, around: int | None = None,
                   limit: int = Query(100, ge=1, le=500)) -> dict:
    kb = chat_kb(request, sid)
    if sum(x is not None for x in (before, after, around)) > 1:
        raise CelineError("bad_request", "Use one of before, after or around.", 400)
    return await in_thread(kb.messages, chat_id, before=before, after=after, around=around,
                      limit=limit)


@router.get("/sources/{sid}/messages/{mid}")
async def message(request: Request, sid: str, mid: int) -> dict:
    kb = chat_kb(request, sid)
    m = await in_thread(kb.message, mid)
    if m is None:
        raise _nf(f"There is no message {sid}:{mid}.")
    return m


@router.get("/sources/{sid}/messages/{mid}/media/{sha}")
async def media(request: Request, sid: str, mid: int, sha: str):
    """A file of a message, stored by content hash, served with the content type the
    archive recorded for it (else the one its name implies). Only pictures are shown
    inline; everything else is a download, so nothing can run on this origin."""
    kb = chat_kb(request, sid)
    found = await in_thread(kb.message_file, mid, sha) \
        if len(sha) == 64 and all(c in "0123456789abcdef" for c in sha) else None
    p = media_dir(kb) / sha
    if found is None or not p.is_file():
        raise _nf("There is no such attachment.")
    mtype, f = found
    name = Path(str(f.get("name") or "")).name
    ctype = (str(f.get("content_type") or "").split(";")[0].strip().lower()
             or mimetypes.guess_type(name)[0] or "application/octet-stream")
    inline = shown_inline(ctype)
    if not name:            # a file the phone sent without a name: its type names it
        name = f"{mtype or 'file'}{mimetypes.guess_extension(ctype) or ''}"
    return FileResponse(p, media_type=ctype if inline else "application/octet-stream",
                        filename=name,
                        content_disposition_type="inline" if inline else "attachment",
                        headers={**SAFE_FILE_HEADERS,
                                 "Cache-Control": "private, max-age=604800, immutable"})


# ---- people --------------------------------------------------------------------------

@router.get("/sources/{sid}/people")
async def people(request: Request, sid: str, q: str = "",
                 limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)) -> dict:
    kb = chat_kb(request, sid)
    kept = frozenset(int(r.split(":", 1)[1]) for r in
                       await request.app.state.services.store.read(lambda c: kept_sync(c, sid)))
    return await in_thread(kb.people, q, limit, offset, kept)


class PersonPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)


@router.patch("/sources/{sid}/people/{pid}")
async def patch_person(request: Request, sid: str, pid: int, body: PersonPatch) -> dict:
    kb = chat_kb(request, sid)
    p = await in_thread(kb.update_person, pid, body.name)
    if p is None:
        raise _nf(f"no person {pid} in {sid}")
    return p


# ---- tasks ---------------------------------------------------------------------------

@router.get("/tasks")
async def tasks(request: Request) -> dict:
    """Visible tasks, and the last outcome of each scheduled job."""
    t = _kbs(request).tasks
    return {"tasks": t.list(), "jobs": t.jobs()}


@router.post("/tasks/{id}/stop")
async def cancel_task(request: Request, id: str) -> dict:
    if not _kbs(request).tasks.cancel(id):
        raise _nf(f"Task {id} is not running.")
    return {"ok": True}


@router.post("/tasks/{id}/retry")
async def retry_task(request: Request, id: str) -> dict:
    t = _kbs(request).tasks.get(id)
    if t is None or t.retry is None or t.state == "running":
        raise _nf(f"Task {id} is not finished, so you cannot retry it.")
    return {"task": t.retry().id}


@router.delete("/tasks/{id}")
async def dismiss_task(request: Request, id: str) -> dict:
    """Remove a finished task from the list."""
    if not _kbs(request).tasks.dismiss(id):
        raise _nf(f"Task {id} is not finished.")
    return {"ok": True}


@router.delete("/tasks")
async def clear_tasks(request: Request, state: str) -> dict:
    """`?state=finished`: remove every finished task; running ones stay."""
    if state != "finished":
        raise CelineError("bad_request", "Only state=finished can be removed.", 400)
    return {"removed": _kbs(request).tasks.clear()}


# ---- references --------------------------------------------------------------------

@router.get("/archive/refs")
async def refs(request: Request, refs: str = "") -> dict:
    """Cards for `[[...]]` references in Markdown and in Celine's answers:
    "whatsapp:48213" -> the message with its chat, "documents:812" -> the document
    segment with its item; a name ("Some file") -> the document it links to."""
    wanted = [r.strip() for r in refs.split(",") if r.strip()][:200]
    kbs = _kbs(request)
    resolved = await in_thread(kbs.resolve_refs, [r for r in wanted if REF.match(r)])
    kn = kbs.documents()
    items = {}
    if kn is not None:
        for r in wanted:
            if not REF.match(r):
                it = await in_thread(kn.find_item, r)
                items[r] = {"id": it["id"], "path": it["path"], "title": it["title"],
                            "kind": it["kind"]} if it else None
    return {"refs": resolved, "items": items}
