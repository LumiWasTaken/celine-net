"""Freeze: everything she does stops until the owner unfreezes her. Bastet freezes her in mode On (a rule whose `does` is freeze); the owner
can by hand in any mode (POST /api/guard/freeze, `!freeze` in the bridged chat).

Frozen: every live turn is stopped like a Stop, its reason "frozen"; its jobs (and with
the turns its sub-agents) are stopped; no worker claims a turn while frozen
(hold.py, before paused), so what is queued, routines included, runs after the unfreeze;
the work queue claims nothing (work.claim), no tool runs (turns/tools.py), the model
proxy refuses its shellbox (api/llm_proxy.py) and a named token gets no action (auth/
gate.py). Only the owner unfreezes (owner.py: their session or their own bridged message).
kv "frozen" holds the open freeze's id; the table `freezes` keeps every one (migration
0113). Both calls are idempotent. The global event `frozen.changed` carries the view;
the freeze banners of its turn change with it (banners.py).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from ..core.ids import now
from ..events import added
from . import bastet, learn, store as decisions
from .owner import must
from ..core.event_types import FROZEN_CHANGED

log = logging.getLogger("celine.guard")

COMMAND = re.compile(r"^!(freeze|unfreeze)(?:\s+(.+))?$", re.I | re.S)


def _one(c: sqlite3.Connection, fid: int) -> dict:
    return dict(c.execute("SELECT * FROM freezes WHERE id=?", (fid,)).fetchone())


async def frozen(store) -> dict | None:
    """The open freeze, None while it is not frozen."""
    return await store.read(decisions.open_freeze)


async def freeze(app, *, by: str, why: str, conversation_id: str | None = None,
                 turn_id: str | None = None, decision_id: int | None = None) -> dict:
    """Freeze it now. `app`: the running services (core/services.py); `by`: "guard" or
    "user"; `why`: one plain sentence the owner reads. -> the freeze (the open one when
    it is frozen already)."""
    store = app.store
    why = " ".join((why or "").split())[:500] or "no reason given"

    def fn(c: sqlite3.Connection) -> tuple[dict, bool]:
        if (got := decisions.open_freeze(c)) is not None:
            return got, False
        rid = c.execute("INSERT INTO freezes (at, by, why, conversation_id, turn_id, "
                        "decision_id) VALUES (?,?,?,?,?,?)",
                        (now(), by, why, conversation_id, turn_id, decision_id)).lastrowid
        decisions.kv_set(c, decisions.FROZEN, str(rid))
        added(store, c, "freeze", conversation_id)
        decisions.emit_turn(c, store, turn_id)
        return _one(c, rid), True
    row, made = await store.write(fn)
    if not made:
        return row
    log.warning("frozen", extra={"event": "guard.frozen", "by": by})
    store.emit_global({"type": FROZEN_CHANGED, "frozen": row})
    await _stop_all(app)
    if by == "guard":
        # their own past unfreezes of the same kind lift it, else it asks them
        if (lift := await learn.after_freeze(app, row)) is not None:
            await unfreeze(app, by=lift)
            await learn.lifted(app, row, lift)
    else:
        await bastet.tell_owner(app, bastet.FROZEN_BY_YOU, conversation_id, turn_id,
                                phone=True)
    return row


async def _stop_all(app) -> None:
    """Every live turn ends like a Stop, its reason frozen; every job is stopped."""
    d = getattr(app, "dispatcher", None)
    if d is None:
        return
    tokens = d.tokens.live()
    ids = [t.turn_id for t in tokens]
    if ids:
        marks = ",".join("?" * len(ids))
        await app.store.write(lambda c: c.execute(
            f"UPDATE turns SET abort_requested=1 WHERE id IN ({marks}) AND state IN "
            f"('preparing','streaming','tools')", ids))
    for t in tokens:
        t.cancel("frozen")
    jobs = getattr(d, "jobs", None)
    for handle in list(getattr(jobs, "handles", {}).values()):
        if not handle.done.is_set():
            await jobs.cancel(handle.id)


async def unfreeze(app, *, by) -> dict | None:
    """The agent goes on: what was queued runs. `by`: the owner (guard/owner.py), the only one
    who unfreezes (their precedent is Bastet's lift, guard/learn.py); anything else is a
    TypeError. -> the freeze that ended, None when it
    was not frozen."""
    must(by, unfreeze=True)
    store = app.store
    lifted = by.lifts_only               # Bastet: their past unfreezes, a second look

    def fn(c: sqlite3.Connection) -> dict | None:
        got = decisions.open_freeze(c)
        if got is None:
            return None
        c.execute("UPDATE freezes SET unfrozen_at=?, unfrozen_by=?, precedents=? WHERE id=?",
                  (now(), "bastet" if lifted else "user",
                   json.dumps(list(by.ids)) if lifted else None, got["id"]))
        decisions.kv_del(c, decisions.FROZEN)
        added(store, c, "freeze", got["conversation_id"])
        decisions.emit_turn(c, store, got["turn_id"])
        return _one(c, got["id"])
    row = await store.write(fn)
    if row is None:
        return None
    log.warning("unfrozen", extra={"event": "guard.unfrozen", "by": by.via})
    store.emit_global({"type": FROZEN_CHANGED, "frozen": None})
    d = getattr(app, "dispatcher", None)
    if d is not None:
        d.notify()
        if (pool := getattr(d, "work", None)) is not None:
            pool.notify()        # the work queue's workers wait out a freeze too
    return row


async def command(app, text: str, by) -> str | None:
    """`!freeze <why>` or `!unfreeze` from the owner's own bridged message (`by`, the
    Owner the bridge made). -> the answer for that chat, None when the text is no such
    command."""
    m = COMMAND.match((text or "").strip())
    if not m:
        return None
    if m[1].lower() == "freeze":
        was = await frozen(app.store)
        row = await freeze(app, by="user", why=m[2] or "frozen from the bridged chat")
        return "Frozen already." if was else f"Frozen: {row['why']}"
    return "Unfrozen." if await unfreeze(app, by=by) else "Not frozen."
