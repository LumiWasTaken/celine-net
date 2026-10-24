"""Passes: the owner suspends one of Bastet's rules (or "all") in one conversation, or
everywhere, until a time (migration 0113, guard_passes). While a pass holds, a call
that rule would act on runs and its row says "passed". The agent's next result after a pass is
made says so in one line (`told`)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from ..core.ids import iso, now
from ..events import added
from ..settings.files import Invalid
from .owner import must
from .rules import ALL, RULES
from ..core.event_types import GUARD_PASSES

MOST_MINUTES = 7 * 24 * 60
_COLS = ("id", "rule", "conversation_id", "until", "by", "why", "created_at", "told_at")


def _row(r) -> dict:
    return {k: r[k] for k in _COLS}


def _at(at: str | None) -> str:
    return at or now()


async def make(store, *, by, rule: str, minutes: int,
               conversation_id: str | None = None, why: str = "",
               at: str | None = None) -> dict:
    """A pass from `at` (now) for `minutes`; `by`: the owner (guard/owner.py), the only
    one who makes a pass, anything else a TypeError; the row's `by` is where they made it
    (session or bridge). Invalid (rule or minutes) with one sentence on a bad rule or
    time. Every client hears it (global event guard.passes)."""
    must(by)
    if rule != ALL and rule not in RULES:
        raise Invalid("rule", f"'{rule}' is not a rule: {', '.join(RULES)} or all.")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or \
            not 1 <= minutes <= MOST_MINUTES:
        raise Invalid("minutes", f"This must be a whole number from 1 to {MOST_MINUTES}.")
    start = _at(at)
    until = iso(datetime.fromisoformat(start) + timedelta(minutes=minutes))

    def fn(c: sqlite3.Connection) -> dict:
        rid = c.execute("INSERT INTO guard_passes (rule, conversation_id, until, by, why, "
                        "created_at) VALUES (?,?,?,?,?,?)",
                        (rule, conversation_id, until, by.via, " ".join(why.split())[:300] or None,
                         start)).lastrowid
        added(store, c, "pass", conversation_id)
        return _row(c.execute("SELECT * FROM guard_passes WHERE id=?", (rid,)).fetchone())
    made = await store.write(fn)
    store.emit_global({"type": GUARD_PASSES})
    return made


async def end(store, pid: int, at: str | None = None) -> bool:
    """The pass ends now (global event guard.passes). -> False when there is no such
    pass still holding."""
    t = _at(at)
    ended = await store.write(lambda c: c.execute(
        "UPDATE guard_passes SET until=? WHERE id=? AND until > ?", (t, pid, t)).rowcount > 0)
    if ended:
        store.emit_global({"type": GUARD_PASSES})
    return ended


async def holding(store, conversation_id: str | None = None,
                  at: str | None = None) -> list[dict]:
    """The passes that hold now: everywhere ones, and this conversation's."""
    t = _at(at)
    return await store.read(lambda c: [_row(r) for r in c.execute(
        "SELECT * FROM guard_passes WHERE until > ? AND (conversation_id IS NULL OR "
        "conversation_id IS ?) ORDER BY id", (t, conversation_id))])


async def listing(store, at: str | None = None) -> list[dict]:
    """Every pass that holds now, anywhere."""
    t = _at(at)
    return await store.read(lambda c: [_row(r) for r in c.execute(
        "SELECT * FROM guard_passes WHERE until > ? ORDER BY id", (t,))])


def covers(passes: list[dict], rule: str) -> dict | None:
    return next((p for p in passes if p["rule"] in (rule, ALL)), None)


async def retell(store, ids: list[int]) -> None:
    """These passes were never told after all (the call did not run): told again."""
    if ids:
        await store.write(lambda c: c.execute(
            f"UPDATE guard_passes SET told_at=NULL WHERE id IN ({','.join('?' * len(ids))})",
            ids))


async def untold(store, conversation_id: str, at: str | None = None) -> list[dict]:
    """The passes that hold here and its results have not mentioned yet; marked told."""
    t = _at(at)

    def fn(c: sqlite3.Connection) -> list[dict]:
        rows = [_row(r) for r in c.execute(
            "UPDATE guard_passes SET told_at=? WHERE told_at IS NULL AND until > ? AND "
            "(conversation_id IS NULL OR conversation_id=?) RETURNING *",
            (t, t, conversation_id))]
        return sorted(rows, key=lambda r: r["id"])
    return await store.write(fn)
