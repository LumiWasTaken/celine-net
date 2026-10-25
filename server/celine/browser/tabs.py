"""The agent's browser tabs as rows: the box lists them by name; a row follows
each from when it appears until it is gone, with the conversation that opened it (the
conversation of the newest call naming it in the OPENER seconds before it appeared,
the user's console has none) and when it was last used (its calls on it, or its address
changed, which is also the user browsing by hand, or the user has the browser). A tab unused for IDLE seconds closes; the tabs a mission or a sub-agent opened
close when it ends (leftovers.py)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import httpx

from .. import homes
from ..core.ids import now
from . import sync

IDLE = 24 * 3600        # seconds unused before a tab closes
OPENER = 600            # seconds before a tab appears that the call opening it started

_CALLS = ("FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
          "WHERE tc.name LIKE 'browser\\_\\_%' ESCAPE '\\' AND json_valid(tc.arguments) "
          "AND json_extract(tc.arguments, '$.tab') = ?")


def _note(c: sqlite3.Connection, listed: list[dict], at: str) -> None:
    open_rows = {r["name"]: r for r in c.execute("SELECT * FROM tabs WHERE closed_at IS NULL")}
    names = {str(t.get("name")) for t in listed if t.get("name")}
    for name, r in open_rows.items():
        if name not in names:
            c.execute("UPDATE tabs SET closed_at=? WHERE id=?", (at, r["id"]))
    for t in listed:
        name, url = t.get("name"), t.get("url")
        if not name:
            continue
        r = open_rows.get(name)
        if r is None:
            since = (datetime.fromisoformat(at) - timedelta(seconds=OPENER)).isoformat(
                timespec="milliseconds")
            who = c.execute(f"SELECT m.conversation_id {_CALLS} AND tc.started_at >= ? "
                            "ORDER BY tc.started_at DESC, tc.rowid DESC LIMIT 1",
                            (name, since)).fetchone()
            c.execute("INSERT INTO tabs(name, opened_by, opened_at, used_at, url) "
                      "VALUES(?,?,?,?,?)", (name, who[0] if who else None, at, at, url))
            continue
        last = c.execute(f"SELECT max(tc.started_at) {_CALLS}", (name,)).fetchone()[0]
        used = max(r["used_at"], last or "", at if url != r["url"] else "")
        if used != r["used_at"] or url != r["url"]:
            c.execute("UPDATE tabs SET used_at=?, url=? WHERE id=?", (used, url, r["id"]))


async def note(store, listed: list[dict], at: str | None = None) -> None:
    """The box's tabs ({name, url, ...}) into rows, at `at` (now)."""
    when = at or now()
    await store.write(lambda c: _note(c, listed, when))


async def follow(cfg, store, status: dict, at: str | None = None) -> None:
    """One look's status: the rows follow its tabs while the browser is up, then every
    tab unused for IDLE seconds closes."""
    if status.get("state") != "up":
        return                       # a starting or down browser lists none: rows stay
    when = at or now()
    await note(store, status.get("tabs") or [], when)
    cutoff = (datetime.fromisoformat(when) - timedelta(seconds=IDLE)).isoformat(
        timespec="milliseconds")
    if status.get("taken"):          # the user has the browser: every tab is in use
        await store.write(lambda c: c.execute(
            "UPDATE tabs SET used_at=? WHERE closed_at IS NULL", (when,)))
        return
    idle = await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT name FROM tabs WHERE closed_at IS NULL AND used_at < ?", (cutoff,))])
    for name in idle:
        await _close(cfg, store, name, when)


async def close_opened_by(cfg, store, cid: str) -> None:
    """Every open tab conversation `cid` opened closes. The box is asked first, so a tab
    opened since its last look has its row."""
    if not sync.url(cfg) or not await store.read(lambda c: c.execute(
            "SELECT 1 FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
            "WHERE m.conversation_id=? AND tc.name LIKE 'browser\\_\\_%' ESCAPE '\\' "
            "LIMIT 1", (cid,)).fetchone()):
        return                       # it never used the browser: nothing to ask the box
    try:
        await follow(cfg, store, await sync.status(cfg))
    except (httpx.HTTPError, sync.BrowserRefused):
        return
    names = await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT name FROM tabs WHERE opened_by=? AND closed_at IS NULL", (cid,))])
    for name in names:
        await _close(cfg, store, name, now())


async def openers(store) -> dict[str, dict]:
    """The conversation that opened each open tab, by tab name: {id, title, kind
    (mission, sub_agent or conversation), link (homes.of: where it opens)}; the user's
    own tabs are left out."""
    return await store.read(lambda c: {r["name"]: {"id": r["id"], "title": r["title"],
                                                   "kind": r["kind"],
                                                   "link": homes.of(c, r["id"])["link"]}
                                       for r in c.execute(
        "SELECT t.name, c.id, c.title, CASE WHEN mi.conversation_id IS NOT NULL "
        "THEN 'mission' WHEN c.agent_of IS NOT NULL THEN 'sub_agent' "
        "ELSE 'conversation' END AS kind FROM tabs t "
        "JOIN conversations c ON c.id = t.opened_by "
        "LEFT JOIN missions mi ON mi.conversation_id = c.id WHERE t.closed_at IS NULL")})


async def _close(cfg, store, name: str, at: str) -> None:
    """The box closes the tab and its row ends; a busy one is left for the next look."""
    if await sync.close_tab(cfg, name):
        await store.write(lambda c: c.execute(
            "UPDATE tabs SET closed_at=? WHERE name=? AND closed_at IS NULL", (at, name)))
