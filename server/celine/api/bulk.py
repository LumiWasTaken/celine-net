"""POST /api/bulk-actions: one action on many things at once, from a list's Select or its
"Archive older than". Each thing goes through its own route, so every rule of one holds
for many: what can be archived, what is busy (a live turn is never
touched), what is in the Trash to delete for good. What could not be done is skipped
with the sentence its route said."""
from __future__ import annotations

import functools
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..core.ids import iso
from ..errors import CelineError

router = APIRouter(prefix="/api")

What = Literal["conversations", "documents", "folders", "notes", "chats", "terminals"]
Action = Literal["archive", "unarchive", "trash", "restore", "purge"]


class BulkIn(BaseModel):
    what: What
    action: Action
    # a chat of the Messages archive is "<kb>:<chat id>", a note its path (trash) or its
    # Trash id (restore, purge), a folder its path
    ids: list[str] = Field(default_factory=list, max_length=5000)
    # with action archive on conversations: instead of ids, every one of `kind` not
    # pinned and last changed more than this many days ago
    older_than: int | None = Field(default=None, ge=1, le=3660)
    kind: Literal["chat", "mission"] | None = None
    # the ids it would act on, nothing done
    dry_run: bool = False


@functools.cache
def _routes() -> dict:
    """(what, action) -> the route that does it to one: (request, id) -> anything."""
    from ..conversations import api as conv
    from ..terminals import api as terms
    from . import connectors, documents as docs, kb, notebook

    def chat(fn):
        async def one(r: Request, key: str):
            kid, _, n = key.rpartition(":")
            return await fn(r, kid, int(n))
        return one

    return {
        ("conversations", "archive"): conv.archive_conversation,
        ("conversations", "unarchive"): conv.unarchive_conversation,
        ("conversations", "trash"): conv.delete_conversation,
        ("conversations", "restore"): conv.restore_conversation,
        ("conversations", "purge"): lambda r, i: conv.delete_conversation(
            r, i, for_good=True),
        ("documents", "trash"): lambda r, i: docs.patch_item(r, int(i), docs.ItemPatch(trashed=True)),
        ("documents", "restore"): lambda r, i: docs.patch_item(r, int(i), docs.ItemPatch(trashed=False)),
        ("documents", "purge"): lambda r, i: docs.delete_item(r, int(i)),
        ("folders", "trash"): lambda r, p: docs.patch_folder(r, p, docs.FolderPatch(trashed=True)),
        ("folders", "restore"): lambda r, p: docs.patch_folder(r, p, docs.FolderPatch(trashed=False)),
        ("folders", "purge"): docs.delete_folder,
        ("notes", "trash"): notebook.notebook_delete,
        ("notes", "restore"): notebook.notebook_untrash,
        ("notes", "purge"): notebook.notebook_purge,
        ("chats", "trash"): chat(lambda r, k, n: kb.patch_chat(r, k, n, kb.ChatPatch(trashed=True))),
        ("chats", "restore"): chat(lambda r, k, n: kb.patch_chat(r, k, n, kb.ChatPatch(trashed=False))),
        ("chats", "purge"): chat(connectors.delete_chat),
        ("terminals", "trash"): terms.delete_terminal,
        ("terminals", "restore"): terms.restore_terminal,
        ("terminals", "purge"): lambda r, i: terms.delete_terminal(r, i, for_good=True),
    }


async def older(request: Request, kind: str, days: int) -> list[str]:
    """The conversations of `kind` an "Archive older than" takes: in the list (not a
    sub-agent, not archived, not deleted, not pinned), last changed more than `days`
    days ago. The oldest first."""
    cutoff = iso(datetime.now(timezone.utc) - timedelta(days=days))
    return await request.app.state.services.store.read(lambda c: [r[0] for r in c.execute(
        "SELECT id FROM conversations WHERE agent_of IS NULL AND trashed_at IS NULL AND "
        "archived_at IS NULL AND pinned = 0 AND kind = ? AND updated_at < ? "
        "ORDER BY updated_at, id", (kind, cutoff))])


@router.post("/bulk-actions")
async def bulk(request: Request, body: BulkIn) -> dict:
    """{what, action, ids} or, to archive, {what: conversations, action: archive, kind,
    older_than}. -> {done: [ids], skipped: [{id, reason}]}; with `dry_run` only {ids}:
    what it would act on (the ones a rule refuses are found when it runs)."""
    run = _routes().get((body.what, body.action))
    if run is None:
        raise CelineError("bad_request", f"{body.what} cannot {body.action}.", 400)
    if body.older_than is not None:
        if (body.what, body.action) != ("conversations", "archive") or body.kind is None:
            raise CelineError("bad_request", "older_than archives conversations of a kind.",
                              400)
        ids = await older(request, body.kind, body.older_than)
    else:
        ids = list(dict.fromkeys(body.ids))
    if body.dry_run:
        return {"ids": ids}
    done, skipped = [], []
    for i in ids:
        try:
            await run(request, i)
        except CelineError as e:
            skipped.append({"id": i, "reason": str(e)})
        except ValueError:
            skipped.append({"id": i, "reason": "Not found."})
        else:
            done.append(i)
    return {"done": done, "skipped": skipped}
