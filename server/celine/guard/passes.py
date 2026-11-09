"""Passes: the owner suspends one of Bastet's rules (or "all") in one conversation, or
everywhere, until a time (guard_passes). A pass with a `piece` ("Pass on this" on a
banner) covers only the calls whose evidence for that rule is that exact piece. While a
pass holds, a call it covers runs and its row says "passed". Bastet tells the agent of a
pass made on a banner at once (banners.py); a pass nobody told it of yet is a line in its
next result (`untold`)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from ..core.ids import iso, now
from ..events import added
from ..settings.files import Invalid
from .owner import must
from .rules import ALL, RULES, label
from ..core.event_types import GUARD_PASSES

MOST_MINUTES = 7 * 24 * 60
_COLS = ("id", "rule", "piece", "conversation_id", "until", "by", "why", "created_at",
         "told_at")
PIECE = 300


def _row(r) -> dict:
    return {k: r[k] for k in _COLS}


def _at(at: str | None) -> str:
    return at or now()


async def make(store, *, by, rule: str, minutes: int,
               conversation_id: str | None = None, why: str = "",
               piece: str | None = None, at: str | None = None) -> dict:
    """A pass from `at` (now) for `minutes`; `by`: the owner (guard/owner.py), the only
    one who makes a pass, anything else a TypeError; the row's `by` is where they made it
    (session). `piece`: only calls with that exact evidence. Invalid (rule or minutes)
    with one sentence on a bad rule or time. Every client hears it (global event
    guard.passes)."""
    must(by)
    if rule != ALL and rule not in RULES:
        raise Invalid("rule", f"'{rule}' is not a rule: {', '.join(RULES)} or all.")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or \
            not 1 <= minutes <= MOST_MINUTES:
        raise Invalid("minutes", f"This must be a whole number from 1 to {MOST_MINUTES}.")
    start = _at(at)
    until = iso(datetime.fromisoformat(start) + timedelta(minutes=minutes))

    def fn(c: sqlite3.Connection) -> dict:
        rid = c.execute("INSERT INTO guard_passes (rule, piece, conversation_id, until, by, "
                        "why, created_at) VALUES (?,?,?,?,?,?,?)",
                        (rule, (piece or "")[:PIECE] or None, conversation_id, until, by.via,
                         " ".join(why.split())[:300] or None, start)).lastrowid
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


def covers(passes: list[dict], rule: str, piece: str | None = None) -> dict | None:
    """The pass that lets a call through on `rule`, its evidence `piece`: one on that
    rule (or all) with no piece, or with exactly that piece."""
    return next((p for p in passes if p["rule"] in (rule, ALL) and
                 (p["piece"] is None or p["piece"] == (piece or "")[:PIECE])), None)


async def told(store, pid: int, at: str | None = None) -> None:
    """Bastet told the agent of pass `pid` herself (banners.py): no result repeats it."""
    t = _at(at)
    await store.write(lambda c: c.execute(
        "UPDATE guard_passes SET told_at=? WHERE id=? AND told_at IS NULL", (t, pid)))


def until_words(cfg, until: str, at: str | None = None) -> str:
    """When a pass ends, as people read it: the time today, else the day and the time."""
    from ..clock import zone
    tz = zone(cfg)
    end = datetime.fromisoformat(until).astimezone(tz)
    near = end - datetime.fromisoformat(_at(at)).astimezone(tz) < timedelta(hours=20)
    return f"{end:%H:%M}" if near else f"{end:%a %d %b %H:%M}"


async def shown(store, cfg, at: str | None = None) -> list[dict]:
    """Every pass that holds now, as Settings, Guard lists it: its rule's label, the
    piece it lets through, and one line saying where and until when."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        out = []
        for r in c.execute("SELECT p.*, v.title FROM guard_passes p LEFT JOIN conversations "
                           "v ON v.id = p.conversation_id WHERE p.until > ? ORDER BY p.id",
                           (_at(at),)):
            name = label(r["rule"])
            where = "Everywhere" if r["conversation_id"] is None else \
                f"In {r['title'] or 'a chat without a title'}"
            out.append({**_row(r), "label": name[:1].upper() + name[1:],
                        "line": f"{where} until {until_words(cfg, r['until'], at)}"})
        return out
    return await store.read(fn)


async def retell(store, ids: list[int]) -> None:
    """These passes were never told after all (the call did not run): told again."""
    if ids:
        await store.write(lambda c: c.execute(
            f"UPDATE guard_passes SET told_at=NULL WHERE id IN ({','.join('?' * len(ids))})",
            ids))


async def untold(store, conversation_id: str, at: str | None = None) -> list[dict]:
    """The passes that hold here and its results have not mentioned yet; marked told. A
    read first: most calls have none, and they need no write."""
    t = _at(at)
    sql = ("told_at IS NULL AND until > ? AND (conversation_id IS NULL OR "
           "conversation_id=?)")
    if not await store.read(lambda c: c.execute(
            f"SELECT 1 FROM guard_passes WHERE {sql} LIMIT 1",
            (t, conversation_id)).fetchone()):
        return []

    def fn(c: sqlite3.Connection) -> list[dict]:
        rows = [_row(r) for r in c.execute(
            f"UPDATE guard_passes SET told_at=? WHERE {sql} RETURNING *",
            (t, t, conversation_id))]
        return sorted(rows, key=lambda r: r["id"])
    return await store.write(fn)
