"""The notebook index: a note's summary, size and last touch, and its full text search,
next to the files in the notebook (skills/notebook.py), the one notebook.
Plain functions over the store."""
from __future__ import annotations

import re
import sqlite3

from ..core.ids import now
from ..kb.chat import fts_candidates, is_query_error


async def upsert(store, path: str, summary: str, nbytes: int, body: str = "") -> None:
    def fn(c: sqlite3.Connection) -> None:
        ts = now()
        c.execute(
            "INSERT INTO notebook_index(path,summary,bytes,touched_at,created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "summary=excluded.summary, bytes=excluded.bytes, touched_at=excluded.touched_at",
            (path, summary, nbytes, ts, ts))
        c.execute("DELETE FROM notebook_fts WHERE path=?", (path,))
        # the index is for finding a note, so the first 64 KB is enough
        c.execute("INSERT INTO notebook_fts(path,summary,body) VALUES(?,?,?)",
                  (path, summary, body[:65536]))

    await store.write(fn)


def _under(prefix: str) -> tuple[str, tuple]:
    """Paths under a folder, matched literally (LIKE would fold case and read `_` and `%`)."""
    p = prefix.rstrip("/") + "/"
    return "substr(path, 1, ?) = ?", (len(p), p)


async def delete(store, path: str) -> None:
    def fn(c: sqlite3.Connection) -> None:
        c.execute("DELETE FROM notebook_index WHERE path=?", (path,))
        c.execute("DELETE FROM notebook_fts WHERE path=?", (path,))

    await store.write(fn)


async def list(store, folder: str | None = None,  # noqa: A001 - the module's word
               order: str = "path") -> list[dict]:
    def fn(c: sqlite3.Connection) -> list[dict]:
        sql = "SELECT path, summary, bytes, touched_at FROM notebook_index"
        args: list = []
        if folder:
            cond, a = _under(folder)
            sql += f" WHERE {cond}"
            args = [*a]
        sql += " ORDER BY touched_at DESC" if order == "touched" else " ORDER BY path"
        return [dict(r) for r in c.execute(sql, args)]

    return await store.read(fn)


async def move(store, src: str, dst: str) -> int:
    """src may be a file or a folder prefix."""
    def fn(c: sqlite3.Connection) -> int:
        cond, a = _under(src)
        rows = c.execute(f"SELECT path FROM notebook_index WHERE path=? OR {cond}",
                         (src, *a)).fetchall()
        for r in rows:
            new = dst if r["path"] == src else dst.rstrip("/") + r["path"][len(src.rstrip("/")):]
            c.execute("UPDATE notebook_index SET path=? WHERE path=?", (new, r["path"]))
            c.execute("UPDATE notebook_fts SET path=? WHERE path=?", (new, r["path"]))
        return len(rows)

    return await store.write(fn)


async def delete_prefix(store, path: str) -> int:
    def fn(c: sqlite3.Connection) -> int:
        cond, a = _under(path)
        args = (path, *a)
        n = c.execute(f"DELETE FROM notebook_index WHERE path=? OR {cond}", args).rowcount
        c.execute(f"DELETE FROM notebook_fts WHERE path=? OR {cond}", args)
        return n

    return await store.write(fn)


async def summary(store, path: str) -> str | None:
    def fn(c: sqlite3.Connection) -> str | None:
        r = c.execute("SELECT summary FROM notebook_index WHERE path=?", (path,)).fetchone()
        return r["summary"] if r else None

    return await store.read(fn)


_A, _B = "\x01", "\x02"       # highlight()'s marks around a matched word


async def search(store, query: str, limit: int = 10) -> list[dict]:
    """The query as written, then its bare words, as the Messages archive does, best
    first (FTS5 rank): {path, summary, touched_at, terms}. `terms`: the words of its text
    the query matched, as written there (the porter tokenizer's matches), so a caller
    finds them on the note's lines."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        for match in fts_candidates(query):
            try:
                rows = c.execute(
                    "SELECT f.path, f.summary, i.touched_at, "
                    f"highlight(notebook_fts, 2, '{_A}', '{_B}') AS marked "
                    "FROM notebook_fts f LEFT JOIN notebook_index i ON i.path = f.path "
                    "WHERE notebook_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit)).fetchall()
            except sqlite3.OperationalError as e:
                if not is_query_error(e):
                    raise
                continue
            if rows:
                return [{"path": r["path"], "summary": r["summary"],
                         "touched_at": r["touched_at"],
                         "terms": sorted({t.lower() for t in re.findall(
                             f"{_A}(.*?){_B}", r["marked"] or "", re.S)})}
                        for r in rows]
        return []

    return await store.read(fn)
