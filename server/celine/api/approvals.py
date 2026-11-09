"""The user's answer to an approval (celine/approvals.py) over HTTP; Waiting for you
(api/waits.py) lists the cards. A logged-in session only; the answer is made as the
Owner the session makes (guard/owner.py)."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import NotFound
from ..guard import owner
from .deps import json_body

router = APIRouter(prefix="/api")


@router.post("/approvals/{aid}/answer")
async def answer_approval(aid: str, request: Request) -> dict:
    """{"ok": true | false}: the user's answer. -> {"ok": true}; the card goes by the
    events approval.closed and approvals.changed."""
    by = owner.session(request)
    body = await json_body(request, need=("ok",), allow=("ok",))
    a = request.app.state.services.approvals
    if a is None or not a.decide(aid, body["ok"] is True, by):
        raise NotFound("request")
    return {"ok": True}
