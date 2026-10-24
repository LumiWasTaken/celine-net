"""The bug report rows (table bugreports). Each report is an Intercom thread: filing one
sends its message to the user from the conversation that filed it, so their reply reaches
it there like any reply; they close it on that thread. Every change tells the Events
list (`events.added {kind: bugreport}`) and the open pages (`intercom.state`)."""
from __future__ import annotations

import sqlite3

from ..core.ids import now
from ..intercom import store as intercom
from ..core.event_types import INTERCOM_STATE

SEVERITIES = ("high", "medium", "low")
WORDS = {"high": "High", "medium": "Medium", "low": "Low"}
TITLE_MAX = 120
TEXT_MAX = 4000


def view(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "conversation_id": r["conversation_id"], "title": r["title"],
            "severity": r["severity"], "what": r["what"], "where": r["place"],
            "wish": r["wish"], "state": r["state"], "thread": r["thread"],
            "created_at": r["created_at"], "closed_at": r["closed_at"]}


def message(r: dict) -> tuple[str, str]:
    """The first message of report `r`'s thread: (subject, text)."""
    lines = [f"{WORDS[r['severity']]}: {r['what']}"]
    if r["where"]:
        lines.append(f"Where: {r['where']}")
    if r["wish"]:
        lines.append(f"Wish: {r['wish']}")
    return f"Bug #{r['id']}: {r['title']}", "\n\n".join(lines)


def _changed(c: sqlite3.Connection, store, cid: str | None, iids: list[int]) -> None:
    from ..events import added
    added(store, c, "bugreport", cid)
    for iid in iids:
        store.global_event(c, INTERCOM_STATE, {"intercom_id": iid, "conversation_id": None})


def file(c: sqlite3.Connection, store, cid: str, title: str, severity: str, what: str,
         where: str, wish: str) -> dict:
    """Inside a write: a new open report and its thread, a message from conversation
    `cid` to the user. -> its view."""
    rid = c.execute(
        "INSERT INTO bugreports(conversation_id, title, severity, what, place, wish, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (cid, title, severity, what, where, wish, now())).lastrowid
    subject, text = message(get(c, rid))
    iid = intercom.insert(c, f"agent:{cid}", "user", "message", text, subject=subject,
                          store=store)
    c.execute("UPDATE bugreports SET thread = ? WHERE id = ?", (iid, rid))
    _changed(c, store, cid, [iid])
    return get(c, rid)


def get(c: sqlite3.Connection, rid: int) -> dict | None:
    r = c.execute("SELECT * FROM bugreports WHERE id = ?", (rid,)).fetchone()
    return view(r) if r else None


def of_thread(c: sqlite3.Connection, tid: int) -> dict | None:
    """The report thread `tid` is, or None."""
    r = c.execute("SELECT * FROM bugreports WHERE thread = ?", (tid,)).fetchone()
    return view(r) if r else None


def listing(c: sqlite3.Connection, state: str | None = None) -> list[dict]:
    """Newest first; `state` open or closed, None for all."""
    sql, args = "SELECT * FROM bugreports", []
    if state:
        sql, args = sql + " WHERE state = ?", [state]
    return [view(r) for r in c.execute(sql + " ORDER BY created_at DESC, id DESC", args)]


def close(c: sqlite3.Connection, store, rid: int) -> dict | None:
    """Inside a write: an open report closed, and what waits for the user in its thread
    read (it is done). -> its view, None when there is no such report; a closed one
    stays as it is."""
    r = get(c, rid)
    if r is None or r["state"] == "closed":
        return r
    c.execute("UPDATE bugreports SET state = 'closed', closed_at = ? WHERE id = ?",
              (now(), rid))
    done = intercom.read(c, [i for (i,) in c.execute(
        "SELECT id FROM intercom WHERE thread = ?", (r["thread"],))])
    _changed(c, store, r["conversation_id"], [r["thread"], *done])
    return get(c, rid)
