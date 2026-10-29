"""Mission rows: plain functions over the store (docs/ARCHITECTURE.md)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from celine import homes
from celine.core.ids import iso, now
from celine.terminals.store import free_name
from ..core.event_types import MISSION_STATE

# its plan is the conversation's (plans/store.py): a row reads it as `plan` (its text),
# `steps` and `step` (the number of the step it is on, None when none is)
FIELDS = ("name", "terminal", "brief", "artifact", "report_to", "until", "min_until", "wake_id",
          "check_in", "status", "note", "held_seconds", "started_by")
# what a row reads of its conversation: an unnamed mission is named as a chat is (the
# title role, after its first reply), and the automatic context switch is the
# conversation's (api/context.py)
_SELECT = ("SELECT m.*, c.title, c.title_auto, c.auto_context, c.created_at, c.updated_at, "
           "c.trashed_at, c.archived_at, p.text AS plan, p.steps AS steps "
           "FROM missions m JOIN conversations c ON c.id = m.conversation_id "
           "LEFT JOIN plans p ON p.conversation_id = m.conversation_id ")


async def terminal_name(store, base: str) -> str:
    """The first free name for a mission's terminal (terminals.store.free_name) among
    the open terminal rows and the terminals of missions that are active or need the
    user."""
    def fn(c: sqlite3.Connection) -> str:
        held = {r[0] for r in c.execute("SELECT name FROM terminals WHERE ended_at IS NULL")}
        held |= {r[0] for r in c.execute(
            "SELECT terminal FROM missions WHERE status IN ('active', 'needs_you')")}
        return free_name(base, held)

    return await store.read(fn)


async def get(store, cid: str) -> dict | None:
    return await store.read(lambda c: _row(c, cid))


async def list(store, status: str | None = None,  # noqa: A001
               trashed: bool = False, archived: bool = False) -> list[dict]:
    """Every mission, newest first; `status` narrows (active, needs_you, done, stopped);
    `trashed`: only the ones in the trash, `archived`: only the archived ones, else only
    the others."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        where = ("WHERE c.trashed_at IS NOT NULL " if trashed else
                 "WHERE c.trashed_at IS NULL AND c.archived_at IS " +
                 ("NOT NULL " if archived else "NULL "))
        where += "AND m.status=? " if status else ""
        return [_view(r) for r in c.execute(
            f"{_SELECT}{where}ORDER BY c.created_at DESC", (status,) if status else ())]
    return await store.read(fn)


async def counts(store) -> dict:
    """The numbers the Missions page shows: each status of the list (not archived, not in
    the trash), all of them, and the Archive and the Trash."""
    def fn(c: sqlite3.Connection) -> dict:
        out = {"all": 0, "active": 0, "needs_you": 0, "done": 0, "stopped": 0,
               "archived": 0, "trashed": 0}
        for r in c.execute(
                "SELECT m.status, c.trashed_at IS NOT NULL AS t, c.archived_at IS NOT NULL AS a, "
                "COUNT(*) AS n FROM missions m JOIN conversations c ON c.id = m.conversation_id "
                "GROUP BY 1, 2, 3"):
            if r["t"]:
                out["trashed"] += r["n"]
            elif r["a"]:
                out["archived"] += r["n"]
            else:
                out["all"] += r["n"]
                out[r["status"]] = out.get(r["status"], 0) + r["n"]
        return out
    return await store.read(fn)


def state_event(row: dict) -> dict:
    """`mission.state` on the global feed: a client keeps its list without a refetch."""
    return {"type": MISSION_STATE, "conversation_id": row["conversation_id"],
            "name": row.get("name"), "status": row.get("status"), "note": row.get("note"),
            "until": row.get("until"), "min_until": row.get("min_until"),
            "artifact": row.get("artifact"),
            "steps": row.get("steps") or [], "step": row.get("step"),
            "auto_context": bool(row.get("auto_context", True))}


async def create(store, cid: str, fields: dict) -> None:
    """The row of a new mission: name, terminal and brief at least; status
    active unless given."""
    cols = _known(fields)
    ts = now()
    cols.setdefault("status", "active")
    cols.update(conversation_id=cid, created_at=ts, updated_at=ts)
    names = ", ".join(cols)
    marks = ", ".join("?" * len(cols))
    await store.write(lambda c: c.execute(
        f"INSERT INTO missions({names}) VALUES({marks})", tuple(cols.values())))


async def update(store, cid: str, fields: dict) -> None:
    """The given fields only; updated_at follows."""
    cols = _known(fields)
    if not cols:
        return
    cols["updated_at"] = now()
    sets = ", ".join(f"{k}=?" for k in cols)
    await store.write(lambda c: c.execute(
        f"UPDATE missions SET {sets} WHERE conversation_id=?", (*cols.values(), cid)))


def set_status_sync(c: sqlite3.Connection, store, cid: str, status: str, note: str | None,
                    only_from: str | None = None) -> dict | None:
    """Inside a write: the one place a mission's status changes (missions/skill.record
    adds what follows a change), the open pages told. `only_from`: only a mission in that
    status changes. -> its row, None when nothing changed."""
    if not c.execute("UPDATE missions SET status=?, note=?, updated_at=? WHERE "
                     "conversation_id=? AND status = coalesce(?, status)",
                     (status, note, now(), cid, only_from)).rowcount:
        return None
    row = _row(c, cid)
    event = state_event(row)
    store.global_event(c, event.pop("type"), event)
    if status == "needs_you":
        from ..events import notice
        notice(store, c, "mission", row.get("name"), note or "Waiting for you",
               homes.of(c, cid)["link"], f"mission:{cid}")
    return row


def _known(fields: dict) -> dict:
    bad = set(fields) - set(FIELDS)
    if bad:
        raise ValueError(f"no such mission field: {', '.join(sorted(bad))}")
    return dict(fields)


def _view(r) -> dict:
    row = dict(r)
    row["plan"] = row["plan"] or ""
    row["steps"] = json.loads(row["steps"] or "[]")
    row["step"] = next((n for n, s in enumerate(row["steps"], 1) if s["state"] == "doing"),
                       None)
    if row.pop("title_auto") and row["title"]:
        row["name"] = row["title"]
    row["auto_context"] = bool(row["auto_context"])
    return row


def row_sync(c: sqlite3.Connection, cid: str) -> dict | None:
    """Inside a read or a write: the mission of conversation `cid`, None."""
    return _row(c, cid)


def _row(c: sqlite3.Connection, cid: str) -> dict | None:
    r = c.execute(f"{_SELECT}WHERE m.conversation_id=?", (cid,)).fetchone()
    return _view(r) if r else None


# the time limit's notice lands WRAP_UP before it, or a fifth of the time left when that
# is shorter
WRAP_UP = timedelta(minutes=15)
WAKE_TEXT = ("The time limit is in {n} min. Wrap up now: deliver what you have, report, "
             "and close the mission with missions__update(status=\"Done\") and a note on "
             "what is still open.")


async def book_time_limit(intercom, cid: str, until: datetime) -> int:
    """The notice that tells it to wrap up, WRAP_UP before the time limit (a fifth of
    the time left when that is shorter). -> its id."""
    lead = min(WRAP_UP, max(until - datetime.now(timezone.utc), timedelta(0)) / 5)
    text = WAKE_TEXT.format(n=max(1, round(lead.total_seconds() / 60)))
    out = await intercom.send(
        "system:app", f"agent:{cid}", text, kind="notice", due_at=iso(until - lead), data={"time_limit": True, "mission": cid})
    return out["intercom_id"]
