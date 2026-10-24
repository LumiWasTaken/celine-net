"""Jobs over HTTP: the list, one job, the user's Stop."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..api.cursor import page_cursor, read_cursor
from ..errors import NotFound
from . import store as jobs

router = APIRouter(prefix="/api")


@router.get("/jobs")
async def list_jobs(request: Request, state: str | None = None,
                    conversation: str | None = None, limit: int = 100,
                    cursor: str | None = None) -> dict:
    """A page of jobs, running first, then the newest; `conversation` keeps one
    conversation's. `next` is the cursor of the following page, null on the last one."""
    limit = max(1, min(limit, 500))
    rows = await jobs.list(request.app.state.services.store, state, conversation=conversation,
                           limit=limit + 1, after=read_cursor(cursor))
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1] if rows else None
    return {"jobs": rows,
            "next": page_cursor((int(last["state"] == "running"), last["created_at"],
                                  last["id"])) if more else None}


@router.get("/jobs/{id}")
async def get_job(request: Request, id: str) -> dict:
    """One job, the same shape as a row of the list."""
    row = await jobs.get(request.app.state.services.store, id)
    if row is None:
        raise NotFound("job")
    return row


@router.post("/jobs/{id}/stop")
async def cancel_job(request: Request, id: str) -> dict:
    stopped = await request.app.state.services.dispatcher.jobs.cancel(id)
    return {"cancelled": stopped}
