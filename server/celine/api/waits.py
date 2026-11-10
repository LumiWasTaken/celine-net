"""Waiting for you over HTTP (celine/waits.py): GET /api/waits, a logged-in session
only."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import waits
from .deps import person_only

router = APIRouter(prefix="/api")


@router.get("/waits")
async def list_waits(request: Request) -> dict:
    """{items: everything that waits for you (waits.items), count: how many, counts:
    how many under each group's head}."""
    person_only(request)
    got = await waits.items(request.app.state.services)
    return {"items": got, "count": len(got), "counts": waits.counts(got)}
