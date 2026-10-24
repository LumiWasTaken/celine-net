"""Discord: fetch the files of media messages that were archived without them.

Attachment URLs are signed and expire (about a day), and DMs are synced without
downloading attachments, so most Discord photos have no file. A message fetched again
carries fresh URLs. The Discord link does the reading and the download (read only,
spaced like every Discord call, `POST /admin/media/refresh`), and the files are
copied from its media folder into `<kb>.media/<sha256>`.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .importers import ImportContext
from ..core.ids import now_ms


def candidates(kb, types: tuple[str, ...] = ("photo",), limit: int = 50) -> list[dict[str, Any]]:
    """Media messages of these types without a file and not tried before, newest first."""
    marks = ",".join("?" * len(types))

    def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            f"""SELECT m.id, m.key, m.ts, m.media, c.key AS channel
                FROM messages m JOIN chats c ON c.id = m.chat_id
                LEFT JOIN media_fetch f ON f.message_id = m.id
                WHERE m.kind = 'media' AND json_extract(m.media, '$.type') IN ({marks})
                  AND m.media NOT LIKE '%"sha256"%' AND f.message_id IS NULL
                ORDER BY m.ts DESC LIMIT ?""", (*types, limit))]
    return kb.file.read_sync(run)


def refresh(kb, link, ctx: ImportContext | None = None, *, limit: int = 50,
            types: tuple[str, ...] = ("photo",), max_mb: float = 25.0,
            dry_run: bool = False) -> dict[str, Any]:
    """Fetch the newest `limit` such messages again through the Discord link (its
    `/admin/media/refresh` reads the message with fresh links and downloads the files
    into its media folder), then copy the files here. `link`: kb/connectors.py."""
    todo = candidates(kb, types, limit)
    res: dict[str, Any] = {"candidates": len(todo), "fetched": 0, "files": 0, "gone": 0,
                           "failed": 0, "dry_run": dry_run}
    if dry_run:
        return res
    for i, m in enumerate(todo):
        if ctx is not None:
            ctx.check()
            ctx.progress(i, len(todo), "messages")
        code, got = link.request_sync("POST", "/admin/media/refresh",
                                      {"chat": m["channel"], "message": m["key"],
                                       "max_mb": max_mb}, timeout=180.0)
        if code != 200:
            res["failed"] += 1
            res["error"] = (got or {}).get("error") or f"The Discord connection answered {code}."
            break                                 # a login problem: stop, try again later
        state, err, files = got["state"], got.get("error"), got.get("files") or []
        if state == "ok":
            files = [f for f in files if not f.get("sha256") or link.pull(f["sha256"])]
            if not any(f.get("sha256") for f in files):
                state, err = "failed", "The app could not copy the files from the connection."
        res["fetched"] += 1
        if state == "gone":
            res["gone"] += 1
        elif state == "failed":
            res["failed"] += 1
        else:
            res["files"] += sum(1 for f in files if f.get("sha256"))

        def write(c: sqlite3.Connection, m=m, state=state, err=err, files=files) -> None:
            if files and any(f.get("sha256") for f in files):
                media = json.loads(m["media"]) if m["media"] else {}
                media["files"] = files
                c.execute("UPDATE messages SET media = ? WHERE id = ?",
                          (json.dumps(media, ensure_ascii=False), m["id"]))
            c.execute("INSERT OR REPLACE INTO media_fetch (message_id, source, at, state, error) "
                      "VALUES (?, 'discord', ?, ?, ?)", (m["id"], now_ms(), state, err))
        kb.file.write_sync(write)
    if ctx is not None:
        ctx.progress(len(todo), len(todo), "messages")
    return res
