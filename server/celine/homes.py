"""Where a conversation lives in the app: the server's one answer. Every list row, a sender on the Intercom and an event carry it; the client never
works it out.

A conversation's home follows its root, the top of its `agent_of` chain (both fixed at
creation, so the home is too): a mission's root is under Missions, a background root
(a run, what an event started) under Routines, anything else with it. A sub-agent opens
in its root's home: /mission/<id>, /routines/<night>/task/<id> or /chat/<id>."""
from __future__ import annotations

import sqlite3

HOMES = ("her", "missions", "routines")
_ROW = "SELECT id, kind, agent_of, trigger_key, created_at FROM conversations WHERE id=?"


def root(c: sqlite3.Connection, cid: str) -> sqlite3.Row | None:
    """The top of `cid`'s agent_of chain (itself when it has no parent); None: no such
    conversation."""
    r = c.execute(_ROW, (cid,)).fetchone()
    seen = {cid}
    while r is not None and r["agent_of"] and r["agent_of"] not in seen:
        seen.add(r["agent_of"])
        up = c.execute(_ROW, (r["agent_of"],)).fetchone()
        if up is None:
            break
        r = up
    return r


def run_link(night: str, run_id: str) -> str:
    """Where a schedule's run opens."""
    return f"/routines/{night}/run/{run_id}"


def of(c: sqlite3.Connection, cid: str) -> dict:
    """{home: it | missions | routines, link: the app path that shows it}; both None
    for a conversation that is not there (gone permanently), so a caller may index it."""
    top = root(c, cid)
    if top is None:
        return {"home": None, "link": None}
    if top["kind"] == "mission":
        return {"home": "missions", "link": f"/mission/{cid}"}
    if top["kind"] == "background":
        key = top["trigger_key"] or ""
        run = c.execute("SELECT id, night FROM runs WHERE id=?",
                        (key.split(":")[1],)).fetchone() if key.startswith("run:") else None
        night = run["night"] if run else top["created_at"][:10]
        link = run_link(night, run["id"]) if run and top["id"] == cid \
            else f"/routines/{night}/task/{cid}"
        return {"home": "routines", "link": link}
    return {"home": "her", "link": f"/chat/{cid}"}
