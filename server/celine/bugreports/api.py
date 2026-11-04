"""The agent's bug reports for the user: the list, and closing one (on its Intercom thread),
with the reason as the app's line in the thread so the agent reads why."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import CelineError, NotFound
from . import store as reports
from .skill import closed_line

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
    """`{why?}`. Closes an open report and reads what waits in its thread; a closed one
    stays as it is. `why` goes into the thread as the app's line ("Report #3 closed:
    ..."), delivered to the conversation that filed it, so the agent reads why and does
    not file it again."""
    services = request.app.state.services
    body = await request.json() if await request.body() else {}
    why = " ".join(str((body or {}).get("why") or "").split())
    was = await services.store.read(lambda c: reports.get(c, rid))
    row = await services.store.write(lambda c: reports.close(c, services.store, rid))
    if row is None:
        raise NotFound("bug report")
    if why and was and was["state"] == "open" and row["conversation_id"]:
        await services.intercom.send("system:app", f"agent:{row['conversation_id']}",
                                     closed_line(rid, why), reply_to=row["thread"])
    return row
