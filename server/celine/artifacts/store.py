"""The agent's artifacts as rows (the artifact tables): plain functions over the
store, as terminals/store.py. A version is never changed or removed; every change adds
one. Trash sets trashed_at; only the user deletes, from the trash (purge)."""
from __future__ import annotations

import re
import secrets
import sqlite3

from celine import homes
from celine.core.ids import now

MAX_BYTES = 1_000_000     # one version, stored and served: a cap no model reaches
KINDS = ("html", "markdown")
ID = re.compile(r"^[a-z0-9]{6}$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

# the newest version of each artifact, joined to its row
_NEWEST = """
SELECT a.id, a.kind, a.conversation_id, a.created_at, a.trashed_at,
       v.n, v.title, v.note, v.created_at AS updated_at
FROM artifacts a
JOIN artifact_versions v ON v.artifact_id = a.id
 AND v.n = (SELECT max(n) FROM artifact_versions WHERE artifact_id = a.id)
"""


class Refused(Exception):
    """add_version said no: `code` is unknown (no such id) or kind (another kind than
    the artifact has)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def add_version(store, id: str | None, kind: str, title: str, content: str,
                      note: str, conversation_id: str | None,
                      tool_call_id: str | None) -> dict:
    """A new artifact with version 1 (id None), or version n+1 of an existing one, in one
    transaction. An empty title keeps the one before. Returns {id, n, kind, title,
    bytes}."""
    def fn(c: sqlite3.Connection) -> dict:
        at = now()
        if id is None:
            aid, n = _new_id(c), 1
            c.execute("INSERT INTO artifacts(id, kind, conversation_id, tool_call_id, "
                      "created_at) VALUES(?,?,?,?,?)",
                      (aid, kind, conversation_id, tool_call_id, at))
            name = title
        else:
            row = c.execute(_NEWEST + " WHERE a.id = ?", (id,)).fetchone()
            if row is None:
                raise Refused("unknown")
            if row["kind"] != kind:
                raise Refused("kind")
            aid, n, name = id, row["n"] + 1, title or row["title"]
        c.execute("INSERT INTO artifact_versions(artifact_id, n, title, content, note, "
                  "created_at) VALUES(?,?,?,?,?,?)", (aid, n, name, content, note, at))
        return {"id": aid, "n": n, "kind": kind, "title": name,
                "bytes": len(content.encode())}

    return await store.write(fn)


def _new_id(c: sqlite3.Connection) -> str:
    while True:
        aid = "".join(secrets.choice(_ALPHABET) for _ in range(6))
        if not c.execute("SELECT 1 FROM artifacts WHERE id=?", (aid,)).fetchone():
            return aid


async def get(store, id: str) -> dict | None:
    """The artifact with its newest title and version, and every version without its
    content: {id, kind, conversation_id, link (homes.of: where its conversation opens,
    None without one), created_at, trashed_at, n, title, note, updated_at,
    versions: [{n, title, note, created_at, bytes}]}."""
    def fn(c: sqlite3.Connection) -> dict | None:
        row = c.execute(_NEWEST + " WHERE a.id = ?", (id,)).fetchone()
        if row is None:
            return None
        versions = [dict(v) for v in c.execute(
            "SELECT n, title, note, created_at, length(CAST(content AS BLOB)) AS bytes "
            "FROM artifact_versions WHERE artifact_id = ? ORDER BY n", (id,))]
        cid = row["conversation_id"]
        return {**dict(row), "link": homes.of(c, cid)["link"] if cid else None,
                "versions": versions}

    return await store.read(fn)


async def version(store, id: str, n: int | None = None) -> dict | None:
    """One version with its content, the newest when n is None: {id, kind, n, title,
    note, created_at, content}."""
    def fn(c: sqlite3.Connection) -> dict | None:
        row = c.execute(
            "SELECT a.id, a.kind, v.n, v.title, v.note, v.created_at, v.content "
            "FROM artifacts a JOIN artifact_versions v ON v.artifact_id = a.id "
            "WHERE a.id = ? AND v.n = coalesce(?, (SELECT max(n) FROM artifact_versions "
            "WHERE artifact_id = a.id))", (id, n)).fetchone()
        return dict(row) if row else None

    return await store.read(fn)


async def find(store, conversation_id: str, title: str) -> str | None:
    """A visible artifact of this conversation whose newest title is this one."""
    def fn(c: sqlite3.Connection) -> str | None:
        row = c.execute(_NEWEST + " WHERE a.conversation_id = ? AND a.trashed_at IS NULL "
                        "AND v.title = ?",
                        (conversation_id, title)).fetchone()
        return row["id"] if row else None

    return await store.read(fn)



async def listing(store, conversation_id: str | None, trashed: bool) -> list[dict]:
    """Newest change first: this conversation's artifacts, or every one when
    conversation_id is None; with `trashed` the trashed ones instead."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        rows = c.execute(
            f"SELECT * FROM ({_NEWEST}) WHERE (? IS NULL OR conversation_id = ?) "
            "AND (trashed_at IS NOT NULL) = ? ORDER BY updated_at DESC",
            (conversation_id, conversation_id, trashed))
        return [dict(r) for r in rows]

    return await store.read(fn)


async def purge(store, id: str) -> bool:
    """A trashed artifact for good, every version of it; False when it is not in the
    trash."""
    def fn(c: sqlite3.Connection) -> bool:
        if not c.execute("SELECT 1 FROM artifacts WHERE id = ? AND trashed_at IS NOT NULL",
                         (id,)).fetchone():
            return False
        for table in ("artifact_errors", "artifact_data", "artifact_versions"):
            c.execute(f"DELETE FROM {table} WHERE artifact_id = ?", (id,))
        c.execute("DELETE FROM artifacts WHERE id = ?", (id,))
        return True
    return await store.write(fn)


async def trash(store, id: str, trashed: bool) -> bool:
    """Into the trash or restored; False when there is no such artifact."""
    def fn(c: sqlite3.Connection) -> bool:
        return c.execute("UPDATE artifacts SET trashed_at = ? WHERE id = ?",
                         (now() if trashed else None, id)).rowcount > 0

    return await store.write(fn)
