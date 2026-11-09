"""Where a conversation lives in the app: the server's one answer, and where it sits
(`path`, its crumbs). Every list row, a sender on the Intercom and an event carry it; the client never
works it out.

A conversation's home follows its root, the top of its `agent_of` chain (both fixed at
creation, so the home is too): a mission's root is under Missions, a background root
(a run, what an event started) under Routines, anything else with it. A sub-agent opens
in its root's home: /mission/<id>, /routines/<night>/task/<id> or /chat/<id>."""
from __future__ import annotations

import sqlite3

_COLS = "id, kind, trigger_key, created_at"


def chain(c: sqlite3.Connection, cid: str, cols: str = _COLS) -> list[sqlite3.Row]:
    """`cid` and the conversations above it on its agent_of chain, itself first and its
    top last (`cols` of each), a loop in the chain stopping the walk: the one walk up
    the chain. [] when there is no such conversation."""
    out: list[sqlite3.Row] = []
    seen: set[str] = set()
    while cid and cid not in seen:
        seen.add(cid)
        r = c.execute(f"SELECT {cols}, agent_of FROM conversations WHERE id=?",
                      (cid,)).fetchone()
        if r is None:
            break
        out.append(r)
        cid = r["agent_of"]
    return out


def root(c: sqlite3.Connection, cid: str) -> sqlite3.Row | None:
    """The top of `cid`'s agent_of chain (itself when it has no parent). None: no such
    conversation."""
    up = chain(c, cid)
    return up[-1] if up else None


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
    return {"home": "agent", "link": f"/chat/{cid}"}


def _named(c: sqlite3.Connection, r) -> dict:
    """One conversation of a chain as a crumb: {id, kind (chat, mission, run, background,
    agent), name, link}."""
    kind, name = r["kind"], None
    if kind == "mission":
        m = c.execute("SELECT name FROM missions WHERE conversation_id=?", (r["id"],)).fetchone()
        name = m[0] if m else None
    elif kind == "agent":
        name = r["agent_name"]
    elif (r["trigger_key"] or "").startswith("run:"):
        run = c.execute("SELECT label FROM runs WHERE id=?", (r["trigger_key"][4:],)).fetchone()
        kind, name = "run", run[0] if run else None
    return {"id": r["id"], "kind": kind, "name": name or r["title"] or "Untitled",
            "link": of(c, r["id"])["link"]}


def path(c: sqlite3.Connection, cid: str, _seen: frozenset[str] = frozenset()) -> list[dict]:
    """Where `cid` sits, from the top down to itself, each a crumb (_named): a mission
    started from another conversation first carries that one's path (a run, a chat), so
    the path of a run's mission's sub-agent is [run, mission, sub-agent]. [] when it is
    gone."""
    up = chain(c, cid, "id, kind, title, trigger_key, agent_name")
    if not up:
        return []
    top, origin = up[-1], []
    if top["kind"] == "mission" and top["id"] not in _seen:
        by = c.execute("SELECT started_by FROM missions WHERE conversation_id=?",
                       (top["id"],)).fetchone()
        if by and by[0]:
            origin = path(c, by[0], _seen | {x["id"] for x in up})
    return origin + [_named(c, r) for r in reversed(up)]
