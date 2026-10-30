"""A conversation's automatic context.

`conversations.auto_context` (1: on, Load automatically): with it on, a new message loads
the skills its words ask for (skills/meta.py message_context), brings up the graded sections of the
notes its keys name (`[your memory]`, skills/recall.py) and its mind block
(skills/mind.py: the time, its mood, how long since the last message). Off, it carries
the always notes alone. The switch holds for
the conversation and its sub-agents.

Routes: GET and PUT /conversations/{cid}/auto-context ({on}), GET
/conversations/{cid}/messages/{mid}/context (what a reply's message brought up). Event:
`conversation.auto_context` {id, on} for every conversation it changed."""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ..conversations.store import set_auto_context
from ..errors import NotFound
from .deps import services_of

router = APIRouter(prefix="/api")


class On(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: bool


class NotePath(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)


async def _exists(store, cid: str) -> bool:
    return await store.read(lambda c: c.execute(
        "SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone() is not None)


@router.get("/conversations/{cid}/auto-context")
async def get_auto_context(request: Request, cid: str) -> dict:
    r = await services_of(request).store.read(lambda c: c.execute(
        "SELECT auto_context FROM conversations WHERE id=?", (cid,)).fetchone())
    if r is None:
        raise NotFound("conversation")
    return {"on": bool(r[0])}


@router.put("/conversations/{cid}/auto-context")
async def put_auto_context(request: Request, cid: str, body: On) -> dict:
    store = services_of(request).store
    if not await _exists(store, cid):
        raise NotFound("conversation")
    await store.write(lambda c: set_auto_context(c, store, cid, body.on))
    return {"on": body.on}


@router.get("/conversations/{cid}/messages/{mid}/context")
async def reply_context(request: Request, cid: str, mid: str) -> dict:
    """The context block of the user message the reply `mid` (its first step) answers:
    {text, report} (turns/message_context.py; report.mind is On its mind,
    report.notebook its memory block, skills/recall.py). 404: no such
    reply, or that message has no block."""
    def fn(c: sqlite3.Connection):
        return c.execute(
            "WITH RECURSIVE up(id, parent_id, role) AS ("
            " SELECT id, parent_id, role FROM messages WHERE id=? AND conversation_id=?"
            " UNION ALL SELECT m.id, m.parent_id, m.role FROM messages m"
            " JOIN up ON m.id = up.parent_id WHERE up.role != 'user')"
            " SELECT x.text, x.report FROM up JOIN message_context x ON x.message_id = up.id"
            " WHERE up.role = 'user' AND x.text IS NOT NULL", (mid, cid)).fetchone()
    r = await services_of(request).store.read(fn)
    if r is None:
        raise NotFound("context")
    return {"text": r[0], "report": json.loads(r[1] or "{}")}


