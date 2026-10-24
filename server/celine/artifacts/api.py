"""The agent's artifacts over HTTP: the list, one artifact with its versions, a version as the
page the frame shows (page.py sets its headers) or as the text it wrote, and your two
changes: restore an old version as the newest, trash or restore, and delete a trashed one
permanently."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..errors import CelineError, NotFound
from . import page
from . import store as artifacts
from ..core.event_types import ARTIFACT_UPDATED
from ..api.deps import store_of

router = APIRouter()


def _changed(request: Request, a: dict) -> None:
    """Your change on the global bus, as its writes are (skills/artifacts.py); `yours`
    keeps the UI from opening it as its first write."""
    request.app.state.services.store.emit_global({"type": ARTIFACT_UPDATED, "id": a["id"],
                                         "n": a["n"], "conversation": a["conversation_id"],
                                         "yours": True})


@router.get("/api/artifacts")
async def artifact_list(request: Request, conversation: str | None = None,
                        trashed: bool = False) -> dict:
    """Newest change first: one conversation's, or all; the trashed ones instead with
    trashed=1."""
    return {"artifacts": await artifacts.listing(store_of(request), conversation, trashed)}


@router.get("/api/artifacts/{id}")
async def artifact(request: Request, id: str) -> dict:
    """The newest title and version, and every version without its content."""
    a = await artifacts.get(store_of(request), id)
    if a is None:
        raise NotFound("artifact")
    return a


async def _version(request: Request, id: str, n: int) -> dict:
    v = await artifacts.version(store_of(request), id, n)
    if v is None:
        raise NotFound("artifact version")
    return v


@router.get("/api/artifacts/{id}/versions/{n}")
async def version_page(request: Request, id: str, n: int) -> Response:
    """The page, sandboxed by its own headers; a markdown document as its text."""
    v = await _version(request, id, n)
    body, headers = (page.render if v["kind"] == "html" else page.source)(v["content"])
    return Response(body, headers=headers)


@router.get("/api/artifacts/{id}/versions/{n}/source")
async def version_source(request: Request, id: str, n: int) -> Response:
    """What it wrote, as plain text."""
    body, headers = page.source((await _version(request, id, n))["content"])
    return Response(body, headers=headers)


@router.post("/api/artifacts/{id}/versions/{n}/restore")
async def restore(request: Request, id: str, n: int) -> dict:
    """An old version copied to the top as a new one; the history stays as it was."""
    v = await _version(request, id, n)
    await artifacts.add_version(store_of(request), id, v["kind"], v["title"], v["content"],
                                f"restored from v{n}", None, None)
    a = await artifacts.get(store_of(request), id)
    _changed(request, a)
    return a


@router.post("/api/artifacts/{id}/restore")
async def untrash(request: Request, id: str) -> dict:
    """Out of the trash again."""
    return await _set_trashed(request, id, False)


async def _set_trashed(request: Request, id: str, trashed: bool) -> dict:
    """Into the trash, or restored. A trashed one still opens by its id."""
    if not await artifacts.trash(store_of(request), id, trashed):
        raise NotFound("artifact")
    a = await artifacts.get(store_of(request), id)
    _changed(request, a)
    return a


@router.delete("/api/artifacts/{id}")
async def delete_artifact(request: Request, id: str, for_good: bool = False) -> dict:
    """Into the trash; for_good=1: a trashed artifact and every version of it, gone."""
    if not for_good:
        return await _set_trashed(request, id, True)
    a = await artifacts.get(store_of(request), id)
    if a is None:
        raise NotFound("artifact")
    if not await artifacts.purge(store_of(request), id):
        raise CelineError("conflict", "It is not in the Trash.", 409)
    _changed(request, {**a, "n": 0})
    return {"ok": True}
