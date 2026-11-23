"""The notebook index: a note's summary, size, content hash and last touch, its full
text search and its canonical sections (the semantic index's rows,
skills/notebook_semantic.py), next to the files in the notebook (skills/notebook.py),
the one notebook. Plain functions over the store.

The canonical cut here is the one thing indexing, fresh scoring and hydration share
(skills/notebook_semantic.py, skills/recall.py): a note is cut at its `## ` headings,
the lines before the first are a section, empty sections are dropped, and a section
longer than SECTION_CHARS is split on paragraph, then line boundaries, with a small
deterministic overlap and no lost tail. The identity of a chunk is (path, section
ordinal, chunk ordinal), so repeated headings never collide; a human label
("path#heading") is kept beside it. The embedding payload is path + summary + heading +
chunk text, after cfg.substitute, with nothing else (skills/notebook_semantic.py,
RENDER_VER: changing the algorithm forces a deliberate rebuild).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3

from ..core.ids import now

RENDER_VER = 2           # the canonical payload's version (notebook_meta.render)
SECTION_CHARS = 2000     # a longer section is cut into chunks at most this long
CHUNK_OVERLAP = 200      # characters a chunk repeats from the one before it
MIN_CHUNK = 40           # a shorter tail chunk joins the one before it

_HEADING = re.compile(r"^## +(.+?)\s*$", re.M)


def _identity(text: str) -> str:
    return text


def sections(text: str) -> list[tuple[str, str]]:
    """A note as (heading, body) sections, cut at its `## ` headings; the lines before the
    first heading are a section with the heading "". Empty sections are left out."""
    out: list[tuple[str, str]] = []
    matches = [m for m in _HEADING.finditer(text)]
    if not matches:
        body = text.strip()
        return [("", body)] if body else []
    pre = text[:matches[0].start()].strip()
    if pre:
        out.append(("", pre))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        if body:
            out.append((m[1], body))
    return out


def _chunks(body: str) -> list[str]:
    """A section body as chunks at most SECTION_CHARS long: cut at a paragraph break, else
    a line break, else the limit, the next chunk repeating CHUNK_OVERLAP characters. Every
    character is in exactly one chunk (plus the bounded overhang), so nothing is lost."""
    if len(body) <= SECTION_CHARS:
        return [body] if body.strip() else []
    out: list[str] = []
    start, n = 0, len(body)
    while start < n:
        end = min(start + SECTION_CHARS, n)
        if end < n:
            lo = start + SECTION_CHARS // 2
            at = body.rfind("\n\n", lo, end)
            if at < 0:
                at = body.rfind("\n", lo, end)
            if at > start:
                end = at
        piece = body[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        nxt = end - CHUNK_OVERLAP
        if nxt <= start:
            nxt = end
        start = nxt
    if len(out) > 1 and len(out[-1]) < MIN_CHUNK:
        # a tiny, unstable tail: fold it into the chunk before it
        tail = out.pop()
        if out:
            out[-1] = (out[-1] + "\n" + tail).strip()
        else:
            out.append(tail)
    return out


def payload(path: str, summary: str, heading: str, text: str, sub=_identity) -> str:
    """The canonical embedding payload: path + summary + heading + chunk text, after
    cfg.substitute, nothing else (no "rest via notebook__read" suffix)."""
    return sub(f"{path}\n{summary}\n{heading}\n{text}")


def content_hash(body: str) -> str:
    """The note text's hash, as notebook_index keeps it (reconcile, changed detection)."""
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def state_hash(body: str, summary: str) -> str:
    """What notebook_semantic_state records: the text and summary the sections were cut
    from, so a change in either is seen."""
    return hashlib.sha1(f"{body}\x00{summary}".encode()).hexdigest()[:16]


def cut(body: str, path: str, summary: str, sub=_identity) -> list[dict]:
    """A note's canonical chunks as rows: {nth, chunk, heading, chars, hash, sid, text,
    payload}. Identity is (path, nth, chunk); `sid` is the human label; `text` is the
    chunk; `payload` is what gets embedded and `hash` its hash. Shared by indexing,
    fresh scoring and hydration."""
    body = sub(body)
    summary = sub(summary)
    rows: list[dict] = []
    seen: set[str] = set()
    for nth, (heading, section) in enumerate(sections(body)):
        sid = f"{path}#{heading}" if heading else path
        if sid in seen:
            sid = f"{sid} [{nth + 1}]"
        seen.add(sid)
        for chunk, text in enumerate(_chunks(section)):
            pay = payload(path, summary, heading, text, sub)
            rows.append({"nth": nth, "chunk": chunk, "heading": heading,
                         "chars": len(text), "text": text, "payload": pay,
                         "path": path,
                         "hash": hashlib.sha1(pay.encode()).hexdigest()[:12],
                         "sid": sid + (f" [part {chunk + 1}]" if chunk else "")})
    return rows


def _set_sections(c: sqlite3.Connection, path: str, summary: str, body: str, sub) -> None:
    """The note's canonical chunk rows and its state, the stale ones out. A vector whose
    payload hash still exists is kept, so an edit elsewhere in the note (or a moved
    section) does not re-embed it."""
    keep = {r["hash"]: r for r in c.execute(
        "SELECT nth, chunk, hash, int8, bits FROM notebook_vecs WHERE path=?", (path,))}
    c.execute("DELETE FROM notebook_sections WHERE path=?", (path,))
    c.execute("DELETE FROM notebook_vecs WHERE path=?", (path,))
    for row in cut(body, path, summary, sub):
        c.execute("INSERT INTO notebook_sections(path,nth,chunk,heading,chars,hash) "
                  "VALUES(?,?,?,?,?,?)",
                  (path, row["nth"], row["chunk"], row["heading"], row["chars"],
                   row["hash"]))
        if (v := keep.get(row["hash"])) is not None:
            c.execute("INSERT INTO notebook_vecs(path,nth,chunk,hash,int8,bits) "
                      "VALUES(?,?,?,?,?,?)",
                      (path, row["nth"], row["chunk"], row["hash"], v["int8"], v["bits"]))


def _set_state(c: sqlite3.Connection, path: str, body: str, summary: str) -> None:
    c.execute("INSERT INTO notebook_semantic_state(path,content) VALUES(?,?) "
              "ON CONFLICT(path) DO UPDATE SET content=excluded.content",
              (path, state_hash(body, summary)))


async def upsert(store, path: str, summary: str, nbytes: int, body: str = "",
                 cfg=None) -> None:
    """Index one note. `cfg` (optional) supplies the cfg.substitute the canonical payload
    is built under; without it the payload is the text as written."""
    sub = cfg.substitute if cfg is not None else _identity

    def fn(c: sqlite3.Connection) -> None:
        ts = now()
        c.execute(
            "INSERT INTO notebook_index(path,summary,bytes,touched_at,created_at,content) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "summary=excluded.summary, bytes=excluded.bytes, touched_at=excluded.touched_at, "
            "content=excluded.content",
            (path, summary, nbytes, ts, ts, content_hash(body)))
        c.execute("DELETE FROM notebook_fts WHERE path=?", (path,))
        # the index is for finding a note, so the first 64 KB is enough
        c.execute("INSERT INTO notebook_fts(path,summary,body) VALUES(?,?,?)",
                  (path, summary, body[:65536]))
        _set_sections(c, path, summary, body, sub)
        _set_state(c, path, body, summary)

    await store.write(fn)


def _under(prefix: str) -> tuple[str, tuple]:
    """Paths under a folder, matched literally (LIKE would fold case and read `_` and `%`)."""
    p = prefix.rstrip("/") + "/"
    return "substr(path, 1, ?) = ?", (len(p), p)


async def delete(store, path: str) -> None:
    def fn(c: sqlite3.Connection) -> None:
        c.execute("DELETE FROM notebook_index WHERE path=?", (path,))
        c.execute("DELETE FROM notebook_fts WHERE path=?", (path,))
        c.execute("DELETE FROM notebook_semantic_state WHERE path=?", (path,))
        c.execute("DELETE FROM notebook_sections WHERE path=?", (path,))
        c.execute("DELETE FROM notebook_vecs WHERE path=?", (path,))

    await store.write(fn)


async def list(store, folder: str | None = None,  # noqa: A001 - the module's word
               order: str = "path") -> list[dict]:
    def fn(c: sqlite3.Connection) -> list[dict]:
        sql = "SELECT path, summary, bytes, touched_at, content FROM notebook_index"
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
            for table in ("notebook_index", "notebook_fts"):
                c.execute(f"UPDATE {table} SET path=? WHERE path=?", (new, r["path"]))
            # The canonical payload contains the path. Drop the old semantic rows so the
            # scheduler cuts and embeds the note under its new path instead of leaving a
            # matching hash that can never be hydrated.
            for table in ("notebook_semantic_state", "notebook_sections", "notebook_vecs"):
                c.execute(f"DELETE FROM {table} WHERE path=?", (r["path"],))
        return len(rows)

    return await store.write(fn)


async def delete_prefix(store, path: str) -> int:
    def fn(c: sqlite3.Connection) -> int:
        cond, a = _under(path)
        args = (path, *a)
        n = c.execute(f"DELETE FROM notebook_index WHERE path=? OR {cond}", args).rowcount
        for table in ("notebook_fts", "notebook_semantic_state", "notebook_sections",
                      "notebook_vecs"):
            c.execute(f"DELETE FROM {table} WHERE path=? OR {cond}", args)
        return n

    return await store.write(fn)


async def summary(store, path: str) -> str | None:
    def fn(c: sqlite3.Connection) -> str | None:
        r = c.execute("SELECT summary FROM notebook_index WHERE path=?", (path,)).fetchone()
        return r["summary"] if r else None

    return await store.read(fn)


async def reconcile(store, root, cfg=None, skip=None) -> dict[str, int]:
    """The files and the index agree: a text file the index does not know (restored from
    a backup, written outside the app) is indexed with a stand-in summary; a file whose
    content hash changed outside the app is re-indexed keeping its summary; an entry
    whose file is gone is dropped. `skip(rel)` leaves generated paths (notebook tools/)
    out; dot entries (.versions, .trash, the index) are always skipped. The note text is
    the source of truth, so an equal-length external edit is still seen."""
    rows = {r["path"]: r for r in await list(store)}
    added = changed = removed = 0
    if not root.is_dir():
        return {"added": 0, "changed": 0, "removed": 0}
    for f in sorted(root.rglob("*")):
        if not f.is_file() or any(part.startswith(".") for part in
                                  f.relative_to(root).parts):
            continue
        rel = f.relative_to(root).as_posix()
        if skip is not None and skip(rel):
            continue
        try:
            body = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if rel not in rows:
            await upsert(store, rel, "found on disk, no summary yet", len(body), body, cfg)
            added += 1
        elif not rows[rel]["content"]:
            # A v1 index gets its hash backfilled without rewriting the note's summary,
            # section rows or touched date. The semantic scheduler cuts it when enabled.
            digest = content_hash(body)
            await store.write(lambda c, rel=rel, digest=digest: c.execute(
                "UPDATE notebook_index SET content=? WHERE path=?", (digest, rel)))
        elif rows[rel]["content"] != content_hash(body):
            await upsert(store, rel, rows[rel]["summary"], len(body), body, cfg)
            changed += 1
    for rel in rows:
        if not (root / rel).is_file():
            await delete(store, rel)
            removed += 1
    return {"added": added, "changed": changed, "removed": removed}


_A, _B = "\x01", "\x02"       # highlight()'s marks around a matched word


_TERM = re.compile(r'"([^"]*)"|(\w+\*?)')
_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def match_any(query: str) -> str | None:
    """The FTS5 MATCH for a query: any of its words, a "quoted" part as one phrase, a
    word ending in * as a prefix. FTS5's operator words are dropped."""
    terms = []
    for phrase, word in _TERM.findall(query):
        if phrase:
            if words := re.findall(r"\w+", phrase):
                terms.append('"' + " ".join(words) + '"')
        elif word.rstrip("*") not in _OPERATORS:
            terms.append(f'"{word.rstrip("*")}"' + ("*" if word.endswith("*") else ""))
    return " OR ".join(dict.fromkeys(terms)) or None


async def search(store, query: str, limit: int = 10) -> list[dict]:
    """The notes that hold any word of the query (match_any), best first by FTS5's bm25
    rank: {path, summary, touched_at, terms, snippet}. `terms`: the words of its text the
    query matched, as written there (the porter tokenizer's matches), so a caller finds
    them on the note's lines; `snippet`: the best passage, its matches between \\x02 and
    \\x03 (kb.chat.snippet_html)."""
    match = match_any(query)

    def fn(c: sqlite3.Connection) -> list[dict]:
        rows = c.execute(
            "SELECT f.path, f.summary, i.touched_at, "
            f"highlight(notebook_fts, 2, '{_A}', '{_B}') AS marked, "
            "snippet(notebook_fts, -1, char(2), char(3), '...', 24) AS snippet "
            "FROM notebook_fts f LEFT JOIN notebook_index i ON i.path = f.path "
            "WHERE notebook_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit)).fetchall()
        return [{"path": r["path"], "summary": r["summary"],
                 "touched_at": r["touched_at"], "snippet": r["snippet"],
                 "terms": sorted({t.lower() for t in re.findall(
                     f"{_A}(.*?){_B}", r["marked"] or "", re.S)})}
                for r in rows]

    if not match:
        return []
    return await store.read(fn)
