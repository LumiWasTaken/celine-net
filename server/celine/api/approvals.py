"""The approvals over HTTP (celine/approvals.py): every request that waits (the
vault's taps, Bastet's confirm asks) and the user's answer. A logged-in session only;
the answer is made as the Owner the session makes (guard/owner.py)."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import NotFound
from ..guard import owner
from .deps import json_body, person_only

router = APIRouter(prefix="/api")


@router.get("/approvals")
async def list_approvals(request: Request) -> dict:
    """{"approvals": every request that waits, vault and gate; "count": how many;
    "homes": {home: {count, link}}, by the place its conversation lives}."""
    person_only(request)
    a = request.app.state.services.approvals
    if a is None:
        return {"approvals": [], "count": 0, "homes": {}}
    return {"approvals": a.view(), "count": len(a.waiting), "homes": a.homes()}


@router.post("/approvals/{aid}/answer")
async def answer_approval(aid: str, request: Request) -> dict:
    """{"ok": true | false}: the user's answer. -> {"approvals": what still waits}."""
    by = owner.session(request)
    body = await json_body(request, need=("ok",), allow=("ok",))
    a = request.app.state.services.approvals
    if a is None or not a.decide(aid, body["ok"] is True, by):
        raise NotFound("request")
    return {"approvals": a.view()}
