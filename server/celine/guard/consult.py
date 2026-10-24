"""The owner answers Bastet on the Intercom. Her consult
after a freeze (learn.py) carries the decision it is about (data.decision_id). Their
reply in that thread is their answer on that decision's banner (banners.answer: the
reader turns their words into a grant: unfreeze, keep, once, a pass), and its line goes
back into the same thread. So they answer it where it asked, and what they chose is a
precedent it reads next time (learn.precedents).

Only their own session reaches here (from_session makes the Owner).
"""
from __future__ import annotations

import logging

from . import banners, bastet, store as decisions
from .small_role import Unavailable

log = logging.getLogger("celine.guard")

NOT_READ = "I could not read that: {why} Say unfreeze, keep, or a pass for a while."


async def from_session(request, iid: int, text: str) -> None:
    """Their reply #iid on the Intercom (intercom/api.py): from their session, it is
    their answer to Bastet when the thread is hers about a decision; else a message,
    nothing more."""
    from . import owner
    by = owner.session_or_none(request)
    if by is not None:
        await replied(request.app.state.services, iid, text, by=by)


async def replied(app, iid: int, text: str, *, by) -> str | None:
    """Their message #iid, a reply in a thread with Bastet: answer the decision it is
    about and say what it did in the same thread. -> its line, None when the thread
    is about no decision of its own."""
    rid = await app.store.read(lambda c: decisions.about(c, iid))
    if rid is None:
        return None
    try:
        got = await banners.answer(app, rid, by=by, text=text)
        line = got["thread"][-1]["text"]
    except KeyError:                         # the decision is gone
        return None
    except Unavailable as e:
        line = bastet.fill(app.cfg, NOT_READ, why=str(e))
    from ..intercom import send
    await send(app.store, bastet.ADDR, "user", app.cfg.substitute(line), kind="notice",
               wake=False, reply_to=iid, data={"decision_id": rid})
    log.info("guard consult answered", extra={"event": "guard.consulted"})
    return line
