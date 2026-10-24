"""The agent's bug reports for the user: the list, and closing one (on its Intercom thread)."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import CelineError, NotFound
from . import store as reports

router = APIRouter(prefix="/api")
STATES = ("open", "closed")


@router.get("/bugreports")
async def list_reports(request: Request, state: str | None = None,
                       thread: int | None = None) -> dict:
    """{reports}: newest first; `state` open or closed, none for all; `thread`: the one
    report that Intercom thread is (none when it is no report)."""
    if state is not None and state not in STATES:
        raise CelineError("bad_request", "state is open or closed", 400)

    def fn(c):
        if thread is None:
            return reports.listing(c, state)
        return [r] if (r := reports.of_thread(c, thread)) else []
    rows = await request.app.state.services.store.read(fn)
    return {"reports": rows}


@router.post("/bugreports/{rid}/close")
async def close(request: Request, rid: int) -> dict:
    """Closes an open report and reads what waits in its thread; a closed one stays as
    it is. The user's words on it go in the thread as a reply."""
    store = request.app.state.services.store
    row = await store.write(lambda c: reports.close(c, store, rid))
    if row is None:
        raise NotFound("bug report")
    return row
