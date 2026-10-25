"""kind = "documents": the user's drawer, files of any kind in folders, shared with Celine.

A document is one file in the Documents folder (`var/documents/` by default; an
Obsidian vault works). The folder is the content and files are kept byte for byte. A
folder is a directory: its path is its identity. Markdown files can also be written
here from the app and by Celine (`write`), and their `[[Other file]]` /
`[[whatsapp:123]]` links are recorded for backlinks. `<data>/kb/<id>.db`
(var/kb/documents.db) holds:

- `items`: one row per file: id (stable across edits, and across moves when the
  content is unchanged), path, sha256, the kind of file, its mode, the three layers
  Celine sees before reading (the user's title and description, and an AI-written,
  high-level, non-spoiler description with its model, time and the file hash it was
  made from, so it is written again when the file changes), the reading state, who
  wrote the file (`author`: user, or celine for one it wrote) and `trashed_at`.
- `trashed_folders`: the folders in the trash, by path; their documents are trashed with
  them (the same `trashed_at`).
- `segments`: the text read out of the file (the extracted-text cache), cut into
  quotable units. `[[<kb id>:<segment id>]]` cites one; ids survive re-reading a
  changed file for every segment whose text did not change.
- `search_fts`: instant search. rowid = segment id (chunked files only); rowid =
  -item id is the item's names and descriptions (every file).
- `passages` + vectors: the semantic index, the same tables and code as the chat
  archive (kb/passages.py), chunked files only. chat_id = item id, message ids =
  segment ids.

A trashed document or folder is out of the listings, search and its tools; Restore
brings it back, Delete permanently removes the file.
Every change to a folder's contents is the global event `documents.changed {path}`
(the folder's path, "" for the top), so clients list that folder again.

**Mode.** A chunked file is split into passages that go into the word index and the
semantic topic search index; Celine searches them and reads the ones it needs. A
full file has neither: it is found by its title and descriptions, and Celine reads it
as one piece. The default is chunked for a text file larger than `chunk_over_kb`,
full_file otherwise;
the upload sets it, and changing it reads the file again.

Work happens in two background tasks per documents base, both cancellable:
"documents" reads new and changed files (fast, local), then "describe" writes the AI
descriptions through the LLM queue at background priority (kb/documents_describe.py).
Both resume where they stopped: state is in the rows. Automatic runs are quiet tasks
(listed only when they fail); a run the user started (an upload) is listed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import normalize
from . import passages as P
from ..core.ids import now_ms
from .db import DOCUMENTS_MIGRATIONS, KBFile, in_batches, kv, set_kv
from .documents_extract import EXTRACTOR, MIME, Segment, extract, kind_of, limits
from .identity import REF
from ..core.event_types import DOCUMENTS_CHANGED, DOCUMENTS_DELETED, DOCUMENTS_ITEM

log = logging.getLogger("celine.kb.documents")


PASSAGE_CHARS = 1200      # prose needs more context per vector than chat (450)
PASSAGE_BREAK = 400       # a heading or a new page starts a new passage past this
_LINK = re.compile(r"\[\[([^\[\]|#\n]+)(?:[|#][^\[\]\n]*)?\]\]")
WRITABLE = {".md", ".markdown", ".txt"}
MODES = ("chunked", "full_file")
TEXTLIKE = {"note", "text", "code", "data", "html", "pdf", "docx", "subtitles", "epub"}
CHUNK_OVER_KB = 32        # a text file larger than this is chunked by default
# the sorts of a folder's listing; a folder sorts by its own name, size, newest change
# and first document added
SORTS = ("name", "added", "changed", "type", "size")
_ORDER = {"name": "lower(COALESCE(i.user_title, i.title))", "added": "i.added_at",
          "changed": "MAX(i.updated_at, i.mtime)", "type": "i.kind", "size": "i.size"}


def _like(prefix: str) -> str:
    """A LIKE pattern for paths under `prefix` (ESCAPE '\\')."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"


def parent_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


class DocumentsPathError(ValueError):
    """A path outside the Documents folder, hidden, or the folder itself."""


def link_targets(text: str) -> set[str]:
    out = set()
    for raw in _LINK.findall(text):
        t = raw.strip()
        out.add(t if REF.match(t) else t.lower().removesuffix(".md"))
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _seg_hash(s: Segment) -> str:
    return hashlib.sha1(f"{s.kind}\x1f{s.speaker or ''}\x1f{s.text}".encode()).hexdigest()[:20]


def _hidden(parts: Iterable[str]) -> bool:
    return any(p.startswith(".") for p in parts)


def match_expr(q: str) -> tuple[str | None, set[str], set[str]]:
    """An FTS5 MATCH over search_fts for a plain query: every word must be there, the
    last one as a prefix (search as you type). Returns (expr, exact, prefixes) for the
    highlighter, expr None when nothing is searchable."""
    toks = normalize.tokens(q or "")
    if not toks:
        return None, set(), set()
    ends_word = bool(q) and not q[-1].isalnum()
    exact = set(toks[:-1] if not ends_word else toks)
    prefixes = set() if ends_word else {toks[-1]}
    parts = [f'"{t}"' for t in toks[:-1]]
    last = toks[-1].replace('"', "")
    parts.append(f'"{last}"' if ends_word or len(last) < 2 else f'"{last}"*')
    return " ".join(parts), exact, prefixes


def match_any(q: str) -> str | None:
    """An FTS5 MATCH where any word of three letters or more is enough (bm25 ranks the
    ones matching more words first): a question against titles and descriptions."""
    toks = sorted({t.replace('"', "") for t in normalize.tokens(q or "") if len(t) >= 3})
    return " OR ".join(f'"{t}"' for t in toks) or None


@dataclass
class DocumentsKB:
    id: str
    title: str
    root: Path
    path: Path
    options: dict[str, Any] = field(default_factory=dict)
    kind: str = "documents"
    platform: str = "documents"
    app_cfg: Any = None            # the app's Config: embeddings, backends, [media]
    tasks: Any = None              # kb/tasks.py Tasks; None: nothing runs by itself
    llm: Any = None                # the app's LLM layer; None: describing cannot run
    work: Any = None               # the app's work queue (work.py): the next describe run
    # tests and scripts replace these: (system, user) -> (text, model); (bytes, mime) ->
    # {"caption", "visible_text", "model"}
    describer: Callable[[str, str], tuple[str, str]] | None = None
    captioner: Callable[[bytes, str], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = KBFile(self.path, DOCUMENTS_MIGRATIONS)
        self.file.write_sync(self._recut_passages)
        self._lock = threading.Lock()
        self._active: dict[str, bool] = {}
        self._again: dict[str, bool] = {}
        self._sync_lock = threading.Lock()

    # ---- events ----------------------------------------------------------------------
    def _emit(self, ev: dict[str, Any]) -> None:
        if self.tasks is not None:
            self.tasks.emit(ev)               # it keeps a failing publish to the log

    def changed(self, folders: Iterable[str]) -> None:
        """`documents.changed {path}` once per folder whose contents changed, and for
        each folder above it (their counts changed)."""
        out: set[str] = set()
        for f in folders:
            parts = f.split("/") if f else []
            out.update("/".join(parts[:i]) for i in range(len(parts) + 1))
        for f in sorted(out):
            self._emit({"type": DOCUMENTS_CHANGED, "path": f})

    def item_event(self, item_id: int) -> None:
        if self.tasks is None:
            return
        it = self.item(item_id)
        if it is not None:
            self._emit({"type": DOCUMENTS_ITEM, "kb": self.id, "item": it})

    # ---- options ----------------------------------------------------------------------
    def opt(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def chunk_over_bytes(self) -> int:
        return int(float(self.opt("chunk_over_kb", CHUNK_OVER_KB)) * 1024)

    def default_mode(self, kind: str, size: int) -> str:
        """Chunked for a text file larger than `chunk_over_kb`, full_file otherwise."""
        return "chunked" if kind in TEXTLIKE and size > self.chunk_over_bytes() \
            else "full_file"

    # ---- paths -----------------------------------------------------------------------
    def resolve(self, rel: str, *, must_exist: bool = False) -> Path:
        """A path inside the folder. Refuses `..`, hidden parts, and the folder itself."""
        rel = (rel or "").strip().lstrip("/")
        if not rel:
            raise DocumentsPathError("a path is required")
        root = self.root.resolve()
        p = (self.root / rel).resolve()
        try:
            inner = p.relative_to(root)
        except ValueError:
            raise DocumentsPathError(f"{rel} is outside the Documents folder") from None
        if p == root:
            raise DocumentsPathError(f"{rel!r} is the Documents folder itself")
        if _hidden(inner.parts):
            raise DocumentsPathError("hidden files and folders are not part of Documents")
        if must_exist and not p.exists():
            raise FileNotFoundError(rel)
        return p

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            raise DocumentsPathError(f"{p.name} is outside the Documents folder") from None

    def folder_dir(self, rel: str) -> Path:
        """An existing folder ("" is the top). FileNotFoundError for an unknown one."""
        rel = (rel or "").strip().strip("/")
        p = self.resolve(rel) if rel else self.root
        if not p.is_dir():
            raise FileNotFoundError(f"The folder {rel}")
        return p

    def folder_path(self, rel: str) -> str:
        """An existing folder's path as rows keep it: "a/./b/" is "a/b", "" the top."""
        p = self.folder_dir(rel)
        return "" if p == self.root else self.rel(p)

    # ---- writing Markdown (the app's editor, and Celine) ----------------------------------
    def _text_file(self, rel: str) -> Path:
        """The file a write lands in: `.md` added when the name has no text suffix, and
        the result confined again, so the suffix can never leave the folder."""
        p = self.resolve(rel)
        if p.suffix.lower() not in WRITABLE:
            p = self.resolve(self.rel(p.with_name(p.name + ".md")))
        if p.is_dir():
            raise DocumentsPathError(f"{rel} is a folder")
        return p

    def write(self, rel: str, text: str, *, author: str = "user") -> dict[str, Any]:
        """Create or replace a Markdown/text file, its folders created as needed.
        `author` "celine" replaces only a file it wrote: one the user put there is
        theirs (DocumentsPathError). Returns its item (read at once)."""
        p = self._text_file(rel)
        if author == "celine" and p.exists():
            self.sync()
            old = self.find_path(self.rel(p))
            if old is not None and old["author"] != "celine":
                raise DocumentsPathError(f"{self.rel(p)} is a file the user put in "
                                         f"Documents. Write to a new path.")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(text)
        tmp.replace(p)
        self.sync()
        r = self.rel(p)
        self.file.write_sync(lambda c: c.execute(
            "UPDATE items SET author = ? WHERE path = ?", (author, r)))
        it = self.find_path(r)
        if it is not None and it["state"] == "pending":
            self.read_item(it["id"])            # small: read now, the answer shows it
            it = self.item(it["id"])
            self.kick()
        return {"path": r, "text": text, "item": it}       # sync said documents.changed

    def find_path(self, rel: str) -> dict[str, Any] | None:
        iid = self.file.read_sync(lambda c: c.execute(
            "SELECT id FROM items WHERE path = ?", (rel,)).fetchone())
        return self.item(iid[0]) if iid else None

    def backlinks(self, iid: int) -> list[dict[str, Any]]:
        """Markdown items that link to this one ([[its name]] or [[its path]])."""
        it = self.item(iid)
        if it is None:
            return []
        name = Path(it["path"]).stem.lower()
        full = it["path"].lower().removesuffix(".md")
        rows = self.file.read_sync(lambda c: c.execute(
            "SELECT DISTINCT i.id, i.path, COALESCE(i.user_title, i.title) AS title FROM links l "
            "JOIN items i ON i.id = l.src WHERE l.target IN (?, ?) AND l.src <> ? "
            "ORDER BY title", (name, full, iid)).fetchall())
        return [dict(r) for r in rows]

    # ---- the folder -> items -------------------------------------------------------------
    def rescan(self) -> dict[str, int]:
        """Bring the items in line with the folder, then read what changed."""
        k = self.sync()
        self.kick()
        return k

    def _walk(self) -> dict[str, tuple[int, int]]:
        root = self.root.resolve()
        out: dict[str, tuple[int, int]] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not p.is_file():
                    continue
                out[p.relative_to(root).as_posix()] = (int(st.st_mtime * 1000), st.st_size)
        return out

    def sync(self) -> dict[str, int]:
        """Bring `items` in line with the folder. Hashes only files whose size or time
        changed. A file that disappeared from one path and appeared at another with the
        same content is the same item (a move in a file manager): its id and
        descriptions stay. A file whose content changed outside the app is the user's
        (`author`): Celine may not overwrite their edit."""
        with self._sync_lock:
            on_disk = self._walk()
            known = self.file.read_sync(lambda c: {
                r["path"]: dict(r) for r in c.execute(
                    "SELECT id, path, sha256, size, mtime FROM items")})
            gone = {p: r for p, r in known.items() if p not in on_disk}
            fresh: list[tuple[str, str, int, int]] = []      # path, sha, mtime, size
            changed: list[tuple[int, str, int, int]] = []    # id, sha, mtime, size
            touched: list[tuple[int, int, int]] = []         # id, mtime, size
            folders: set[str] = set()                        # whose contents changed
            for rel, (mtime, size) in on_disk.items():
                row = known.get(rel)
                if row is not None and (row["mtime"], row["size"]) == (mtime, size):
                    continue
                try:
                    sha = sha256_file(self.root / rel)
                except OSError:
                    continue
                if row is None:
                    fresh.append((rel, sha, mtime, size))
                elif row["sha256"] == sha:
                    touched.append((row["id"], mtime, size))
                else:
                    changed.append((row["id"], sha, mtime, size))
                    folders.add(parent_of(rel))
            folders.update(parent_of(p) for p in gone)
            folders.update(parent_of(f[0]) for f in fresh)
            gone_by_sha: dict[str, list[dict]] = {}
            for r in gone.values():
                gone_by_sha.setdefault(r["sha256"], []).append(r)

            def run(conn: sqlite3.Connection) -> dict[str, int]:
                now = now_ms()
                moved = new = 0
                for rel, sha, mtime, size in fresh:
                    cands = gone_by_sha.get(sha)
                    if cands:
                        r = cands.pop(0)
                        gone.pop(r["path"], None)
                        conn.execute("UPDATE items SET path = ?, mtime = ?, size = ?, "
                                     "updated_at = ? WHERE id = ?", (rel, mtime, size, now, r["id"]))
                        self.index_meta(conn, r["id"])
                        moved += 1
                        continue
                    self._insert(conn, rel, sha, mtime, size, now)
                    new += 1
                for iid, mtime, size in touched:
                    conn.execute("UPDATE items SET mtime = ?, size = ? WHERE id = ?",
                                 (mtime, size, iid))
                for iid, sha, mtime, size in changed:
                    conn.execute("UPDATE items SET sha256 = ?, mtime = ?, size = ?, "
                                 "state = 'pending', error = NULL, updated_at = ?, "
                                 "author = 'user' WHERE id = ?",
                                 (sha, mtime, size, now, iid))
                for r in gone.values():
                    self._delete_rows(conn, r["id"])
                return {"new": new, "changed": len(changed), "moved": moved,
                        "removed": len(gone), "total": len(on_disk)}
            res = self.file.write_sync(run)
        if any(res[k] for k in ("new", "changed", "moved", "removed")):
            log.info("documents folder synced", extra={"event": "documents.synced",
                                                       "kb": self.id, **res})
        self.changed(folders)
        return res

    def _insert(self, conn: sqlite3.Connection, rel: str, sha: str, mtime: int, size: int,
                now: int, mode: str | None = None) -> int:
        p = Path(rel)
        kind = kind_of(p)
        mime = MIME.get(kind) or mimetypes.guess_type(p.name)[0]
        cur = conn.execute(
            "INSERT INTO items (path, sha256, size, mtime, kind, mode, mime, title, state, "
            "added_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (rel, sha, size, mtime, kind, mode or self.default_mode(kind, size), mime,
             p.stem, now, now))
        iid = int(cur.lastrowid)
        if above := self._trashed_above(conn, rel):
            conn.execute("UPDATE items SET trashed_at = (SELECT trashed_at FROM trashed_folders "
                         "WHERE path = ?) WHERE id = ?", (above, iid))
        self.index_meta(conn, iid)
        return iid

    def _delete_rows(self, conn: sqlite3.Connection, iid: int) -> None:
        ids = [r[0] for r in conn.execute("SELECT id FROM segments WHERE item_id = ?", (iid,))]
        conn.executemany("DELETE FROM search_fts WHERE rowid = ?", [(i,) for i in ids])
        conn.execute("DELETE FROM search_fts WHERE rowid = ?", (-iid,))
        conn.execute("DELETE FROM segments WHERE item_id = ?", (iid,))
        P.delete_passages(conn, [r[0] for r in conn.execute(
            "SELECT id FROM passages WHERE chat_id = ?", (iid,))])
        conn.execute("DELETE FROM links WHERE src = ?", (iid,))
        conn.execute("DELETE FROM describe_parts WHERE item_id = ?", (iid,))
        conn.execute("DELETE FROM items WHERE id = ?", (iid,))

    def index_meta(self, conn: sqlite3.Connection, iid: int) -> None:
        r = conn.execute("SELECT path, title, user_title, user_description, ai_description "
                         "FROM items WHERE id = ?", (iid,)).fetchone()
        conn.execute("DELETE FROM search_fts WHERE rowid = ?", (-iid,))
        if r is None:
            return
        name = Path(r["path"]).name
        # an image's caption (with the text seen in it) is its description too
        caps = [x[0] for x in conn.execute(
            "SELECT text FROM segments WHERE item_id = ? AND kind = 'caption'", (iid,))]
        body = normalize.index_text(" ".join(x for x in (
            r["user_title"], r["title"], r["user_description"], r["ai_description"], *caps)
            if x),
            [name, Path(r["path"]).stem.replace("_", " ").replace("-", " ")])
        conn.execute("INSERT INTO search_fts (rowid, body) VALUES (?, ?)", (-iid, body))

    # ---- reading files: the "documents" task ------------------------------------------------
    def _start(self, kind: str, label: str, work: Callable, *, quiet: bool = True,
               say: Callable[[dict[str, Any]], str] | None = None) -> Any:
        """One task of `kind` at a time; a kick while one runs makes it go round again.
        `quiet`: an automatic run, listed only if it fails (kb/tasks.py). `say` turns
        the result into the one sentence the task list shows (`result.summary`)."""
        if self.tasks is None:
            return None
        with self._lock:
            if self._active.get(kind):
                self._again[kind] = True
                return self.tasks.running(self.id, kind)
            self._active[kind] = True
            self._again[kind] = False

        def body(ctx):
            try:
                total: dict[str, Any] = {}
                while True:
                    res = work(ctx) or {}
                    for k, v in res.items():
                        total[k] = total.get(k, 0) + v if isinstance(v, (int, float)) \
                            and not isinstance(v, bool) else v
                    with self._lock:
                        if not self._again.get(kind) or res.get("stopped"):
                            self._active[kind] = False
                            if say is not None:
                                total["summary"] = say(total)
                            return total
                        self._again[kind] = False
            except BaseException:
                with self._lock:
                    self._active[kind] = False
                raise
        try:
            return self.tasks.start(self.id, kind, label, body, quiet=quiet)
        except Exception:
            with self._lock:
                self._active[kind] = False
            raise

    def kick(self, *, quiet: bool = True) -> Any:
        """Start reading pending files and embedding passages without a vector, then
        describing, when there is anything to do. Returns the reading task (the one
        that runs already, which goes round again), None when there is nothing to read."""
        def todo(c: sqlite3.Connection) -> bool:
            return c.execute("SELECT 1 FROM items WHERE state = 'pending'").fetchone() is not None \
                or (self._embeds() and c.execute(
                    "SELECT 1 FROM passages WHERE embedded = 0").fetchone() is not None)
        if self.file.read_sync(todo):
            return self._start("documents", "Add documents", self.process,
                               quiet=quiet, say=_say_read)
        self.kick_describe()
        return None

    def kick_describe(self) -> Any:
        if not self.describe_enabled():
            return None
        n = self.file.read_sync(lambda c: c.execute(
            "SELECT COUNT(*) FROM items WHERE state = 'ready' AND ai_state = 'pending'"
        ).fetchone()[0])
        if not n:
            return None
        from . import documents_describe
        return self._start("describe", "Write AI descriptions",
                           lambda ctx: documents_describe.run(self, ctx), quiet=True,
                           say=_say_described)

    def describe_enabled(self) -> bool:
        if self.describer is not None or self.captioner is not None:
            return True
        # the models come from the Titles and descriptions task through the queue; none
        # free now ends the run softly and the files wait for the next one
        return self.app_cfg is not None and bool(self.opt("describe", True))

    def process(self, ctx) -> dict[str, Any]:
        """Read every pending file, newest first. Each file is its own transaction."""
        done = failed = 0
        todo = self.file.read_sync(lambda c: [r[0] for r in c.execute(
            "SELECT id FROM items WHERE state = 'pending' ORDER BY mtime DESC")])
        ctx.progress(0, len(todo), "files")
        for i, iid in enumerate(todo):
            ctx.check()
            try:
                ok = self.read_item(iid)
            except Exception as e:             # noqa: BLE001 - one bad file never stops the rest
                err = f"{type(e).__name__}: {e}"
                log.warning("document failed", extra={
                    "event": "documents.read_failed", "kb": self.id, "item": iid,
                    "error": err})
                self.file.write_sync(lambda c: c.execute(
                    "UPDATE items SET state = 'failed', error = ? WHERE id = ?",
                    (err[:300], iid)))
                ok = False
            done += ok is True
            failed += ok is False
            self.item_event(iid)
            ctx.progress(i + 1, len(todo), "files")
        out: dict[str, Any] = {"read": done, "failed": failed}
        emb = self.embed(ctx)
        if emb:
            out.update(emb)
        self.kick_describe()
        return out

    def read_item(self, iid: int) -> bool | None:
        """Read one file: True when its text is in, False when extraction failed, None
        when it is gone or changed while it was read (it stays pending for the next run)."""
        row = self.file.read_sync(lambda c: c.execute(
            "SELECT * FROM items WHERE id = ?", (iid,)).fetchone())
        if row is None:
            return None
        p = self.root / row["path"]
        if not p.is_file():
            return None
        sha = row["sha256"]
        ex = extract(p, limits(self.app_cfg))
        segs = [s for s in ex.segments if s.text.strip()]
        n_chars = sum(len(s.text) for s in segs)
        n_words = sum(len(s.text.split()) for s in segs)
        # read but empty for a known reason (a scan without a text layer) is ready,
        # with the reason in `error`; a file that could not be opened at all failed
        state = "failed" if ex.error and not segs and ex.kind != "image" and \
            "text layer" not in ex.error else "ready"
        meta = dict(ex.meta)
        links: set[str] = set()
        if ex.kind == "note":
            from .documents_extract import read_text
            links = link_targets(read_text(p))

        def run(conn: sqlite3.Connection) -> bool | None:
            cur = conn.execute("SELECT sha256, ai_source_hash FROM items WHERE id = ?",
                               (iid,)).fetchone()
            if cur is None or cur["sha256"] != sha:
                return None                   # changed while it was read: read again
            ids = self._store_segments(conn, iid, segs)
            conn.execute("DELETE FROM links WHERE src = ?", (iid,))
            if links:
                conn.executemany("INSERT OR IGNORE INTO links (src, target) VALUES (?, ?)",
                                 [(iid, t) for t in links])
            title = (ex.title or "").strip()[:300] or Path(row["path"]).stem
            ai_pending = cur["ai_source_hash"] != sha
            if ex.kind == "file" or (not segs and ex.kind != "image"):
                ai = ("skipped", "nothing to read in this file")
            elif ai_pending:
                ai = ("pending", None)
            else:
                ai = None
            conn.execute(
                "UPDATE items SET kind = ?, mime = COALESCE(?, mime), title = ?, state = ?, "
                "error = ?, extractor = ?, read_hash = ?, pages = ?, n_segments = ?, "
                "n_chars = ?, n_words = ?, meta = ? WHERE id = ?",
                (ex.kind, MIME.get(ex.kind), title, state, ex.error, EXTRACTOR, sha,
                 ex.pages, len(ids), n_chars, n_words, json.dumps(meta) if meta else None,
                 iid))
            if ai is not None:
                conn.execute("UPDATE items SET ai_state = ?, ai_error = ?, ai_attempts = 0 "
                             "WHERE id = ?", (ai[0], ai[1], iid))
            self.index_meta(conn, iid)
            self._apply_mode(conn, iid)
            return state == "ready"
        return self.file.write_sync(run)

    def _store_segments(self, conn: sqlite3.Connection, iid: int,
                        segs: list[Segment]) -> list[int]:
        """Replace an item's segments, keeping the id of every segment whose text is
        unchanged (matched by hash and occurrence, in order), so citations survive an
        edit elsewhere in the file."""
        old: dict[tuple[str, int], int] = {}
        seen: dict[str, int] = {}
        for r in conn.execute("SELECT id, hash FROM segments WHERE item_id = ? ORDER BY seq",
                              (iid,)):
            n = seen.get(r["hash"], 0)
            seen[r["hash"]] = n + 1
            old[(r["hash"], n)] = r["id"]
        keep: set[int] = set()
        ids: list[int] = []
        seen = {}
        for seq, s in enumerate(segs):
            h = _seg_hash(s)
            n = seen.get(h, 0)
            seen[h] = n + 1
            sid = old.get((h, n))
            if sid is not None:
                keep.add(sid)
                conn.execute("UPDATE segments SET seq = ?, page = ?, heading = ?, t_ms = ? "
                             "WHERE id = ?", (seq, s.page, s.heading, s.t_ms, sid))
            else:
                sid = int(conn.execute(
                    "INSERT INTO segments (item_id, seq, kind, page, heading, t_ms, speaker, "
                    "text, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (iid, seq, s.kind, s.page, s.heading, s.t_ms, s.speaker, s.text, h)
                ).lastrowid)
            ids.append(sid)
        drop = [(i,) for i in old.values() if i not in keep]
        conn.executemany("DELETE FROM search_fts WHERE rowid = ?", drop)
        conn.executemany("DELETE FROM segments WHERE id = ?", drop)
        return ids

    def add_caption(self, iid: int, caption: str, visible: str, model: str) -> None:
        """An image's description becomes its one `caption` segment and its AI layer."""
        text = caption + (f"\nText in the image: {visible}" if visible else "")

        def run(conn: sqlite3.Connection) -> None:
            self._store_segments(conn, iid, [Segment("caption", text)])
            conn.execute("UPDATE items SET n_segments = 1, n_chars = ?, n_words = ? "
                         "WHERE id = ?", (len(text), len(text.split()), iid))
            self.index_meta(conn, iid)
            self._apply_mode(conn, iid)
        self.file.write_sync(run)

    # ---- the mode: word index and passages for chunked files, nothing for full files --------
    def _apply_mode(self, conn: sqlite3.Connection, iid: int) -> None:
        """A chunked file's segments go into the word index and its passages are cut;
        a full file's are taken out of both (its names and descriptions stay)."""
        r = conn.execute("SELECT mode FROM items WHERE id = ?", (iid,)).fetchone()
        if r is None:
            return
        chunked = r["mode"] == "chunked"
        rows = conn.execute("SELECT id, speaker, text FROM segments WHERE item_id = ?",
                            (iid,)).fetchall()
        conn.executemany("DELETE FROM search_fts WHERE rowid = ?", [(x["id"],) for x in rows])
        if chunked:
            conn.executemany("INSERT INTO search_fts (rowid, body) VALUES (?, ?)", [
                (x["id"], normalize.index_text(
                    f"{x['speaker']}: {x['text']}" if x["speaker"] else x["text"]))
                for x in rows])
            self._cut_passages(conn, iid)
        else:
            P.delete_passages(conn, [x[0] for x in conn.execute(
                "SELECT id FROM passages WHERE chat_id = ?", (iid,))])

    def set_mode(self, iid: int, mode: str, *, quiet: bool = True) -> dict[str, Any]:
        """Chunked or full_file. A change reads the file again (its description stays)."""
        if mode not in MODES:
            raise ValueError(f"The mode is chunked or full_file, not {mode!r}.")
        n = self.file.write_sync(lambda c: c.execute(
            "UPDATE items SET mode = ?, updated_at = ? WHERE id = ? AND mode <> ?",
            (mode, now_ms(), iid, mode)).rowcount)
        if self.item(iid) is None:
            raise KeyError(iid)
        if n:
            self.reindex(iid, describe=False, quiet=quiet)
            self.item_event(iid)
        return self.item(iid)

    # ---- semantic passages -------------------------------------------------------------------
    def _cut_passages(self, conn: sqlite3.Connection, iid: int) -> None:
        """Windows of consecutive segments (at most PASSAGE_CHARS, one segment of
        overlap, a new window at a heading or a page once PASSAGE_BREAK is reached).
        A passage whose hash is unchanged keeps its row and vector."""
        it = conn.execute("SELECT path, title, user_title, kind, mtime FROM items WHERE id = ?",
                          (iid,)).fetchone()
        segs = conn.execute("SELECT id, kind, page, heading, t_ms, speaker, text FROM segments "
                            "WHERE item_id = ? ORDER BY seq", (iid,)).fetchall()
        wins: list[list[sqlite3.Row]] = []
        cur: list[sqlite3.Row] = []
        size = 0
        for s in segs:
            line = len(s["text"]) + (len(s["speaker"]) + 2 if s["speaker"] else 0)
            brk = cur and size >= PASSAGE_BREAK and (
                s["kind"] == "heading" or (s["page"] is not None and s["page"] != cur[-1]["page"]))
            if cur and (size + line > PASSAGE_CHARS or brk):
                wins.append(cur)
                cur = [] if brk or cur[-1]["kind"] == "heading" else [cur[-1]]
                size = sum(len(x["text"]) for x in cur)
            cur.append(s)
            size += line
        if cur:
            wins.append(cur)
        title = it["user_title"] or it["title"]
        want: dict[str, tuple] = {}
        for seq, w in enumerate(wins):
            text = "\n".join(f"{s['speaker']}: {s['text']}" if s["speaker"] else s["text"]
                             for s in w)
            if len(text) < 20:
                continue
            bits = [f"Documents: {title} ({it['kind']})"]
            if w[0]["page"] is not None:
                bits.append(f"page {w[0]['page']}")
            if w[0]["heading"]:
                bits.append(w[0]["heading"][:120])
            header = "\n".join(bits)
            ids = [s["id"] for s in w]
            h = hashlib.sha1(f"{header}\x1f{text}\x1f{ids}".encode()).hexdigest()
            want[h] = (seq, ids, header, text)
        have = {r["hash"]: r["id"] for r in conn.execute(
            "SELECT id, hash FROM passages WHERE chat_id = ?", (iid,))}
        P.delete_passages(conn, [pid for h, pid in have.items() if h not in want])
        for h, (seq, ids, header, text) in want.items():
            if h in have:
                conn.execute("UPDATE passages SET seq = ? WHERE id = ?", (seq, have[h]))
                continue
            pid = conn.execute(
                "INSERT INTO passages (session_id, chat_id, seq, first_id, last_id, start_ts, "
                "end_ts, n_msgs, header, text, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (iid, iid, seq, ids[0], ids[-1], it["mtime"], it["mtime"], len(ids), header,
                 text, h)).lastrowid
            conn.executemany("INSERT OR IGNORE INTO passage_members (message_id, passage_id) "
                             "VALUES (?, ?)", [(i, pid) for i in ids])

    def _embeds(self) -> bool:
        from .semantic import embeddings_available
        return self.app_cfg is not None and bool(self.opt("embed", True)) \
            and embeddings_available(self.app_cfg)

    def _recut_passages(self, conn: sqlite3.Connection) -> None:
        """Once after passages.RENDER_VER changed: every file's passages cut again (their
        vectors follow with the next kick)."""
        if kv(conn, "passages_render") == str(P.RENDER_VER):
            return
        for (iid,) in conn.execute("SELECT DISTINCT p.chat_id FROM passages p "
                                   "JOIN items i ON i.id = p.chat_id").fetchall():
            self._cut_passages(conn, iid)
        set_kv(conn, "passages_render", str(P.RENDER_VER))

    def embed(self, ctx) -> dict[str, Any] | None:
        """Vectors for passages without one, with the Topic search model.
        Optional: failing here leaves instant search and reading intact."""
        cfg = self.app_cfg
        if not self._embeds():
            return None
        pending = self.file.read_sync(lambda c: c.execute(
            "SELECT COUNT(*) FROM passages WHERE embedded = 0").fetchone()[0])
        if not pending:
            return None
        from .importers import Cancelled, Unreachable
        try:
            from . import semantic
            n = semantic.embed_passages(cfg, self.path, ctx, retries=3, kb_id=self.id)
            return {"embedded": n}
        except Unreachable as e:               # no embedding model now: next scan
            return {"embed_waiting": str(e)}
        except Cancelled:
            raise
        except Exception as e:                 # noqa: BLE001
            log.warning("documents embedding failed", extra={
                "event": "documents.embed_failed", "kb": self.id, "error": str(e)[:300]})
            return {"embed_error": str(e)[:300]}

    def semantic_hits(self, q: str, item_ids: list[int] | None = None,
                      k: int = 20) -> list[dict[str, Any]]:
        """Passages by meaning; [] when there is no index or no embedding endpoint."""
        cfg = self.app_cfg
        from . import semantic
        if cfg is None or not semantic.embeddings_available(cfg) \
                or not self.file.read_sync(P.has_index):
            return []
        try:
            qvec = semantic.query_vector(cfg, q)
        except semantic.SemanticError:
            return []                          # no embedding model answers now

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            scored = P.dense(conn, qvec, k, chat_ids=item_ids or None)
            gone = {r[0] for r in conn.execute("SELECT id FROM items WHERE trashed_at IS NOT NULL")}
            out = []
            for pid, score in scored:
                r = conn.execute("SELECT chat_id FROM passages WHERE id = ?", (pid,)).fetchone()
                if r and r[0] in gone:
                    continue
                mem = [x[0] for x in conn.execute(
                    "SELECT pm.message_id FROM passage_members pm JOIN segments s "
                    "ON s.id = pm.message_id WHERE pm.passage_id = ? ORDER BY s.seq", (pid,))]
                if r and mem:
                    out.append({"passage": pid, "item_id": r[0], "segments": mem, "score": score})
            return out
        return self.file.read_sync(run)

    # ---- items: reading ----------------------------------------------------------------------
    def _item_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        path = r["path"]
        return {
            "id": r["id"], "path": path, "folder": parent_of(path), "name": Path(path).name,
            "title": r["user_title"] or r["title"], "file_title": r["title"],
            "user_title": r["user_title"], "user_description": r["user_description"],
            "ai_description": r["ai_description"],
            "ai": {"state": r["ai_state"], "model": r["ai_model"],
                   "generated_at": r["ai_generated_at"], "error": r["ai_error"],
                   "stale": bool(r["ai_description"]) and r["ai_source_hash"] != r["sha256"]},
            "kind": r["kind"], "mode": r["mode"], "mime": r["mime"], "size": r["size"],
            "pages": r["pages"],
            "n_segments": r["n_segments"], "n_chars": r["n_chars"], "n_words": r["n_words"],
            "state": r["state"], "error": r["error"], "sha256": r["sha256"],
            "added_at": r["added_at"], "updated_at": max(r["updated_at"], r["mtime"]),
            "author": r["author"], "trashed": r["trashed_at"] is not None}

    def item(self, iid: int) -> dict[str, Any] | None:
        r = self.file.read_sync(lambda c: c.execute(
            "SELECT * FROM items WHERE id = ?", (iid,)).fetchone())
        return self._item_dict(r) if r is not None else None

    @staticmethod
    def _check_sort(sort: str, desc: bool | None) -> bool:
        """The direction: `desc`, else names and types A to Z, the rest newest and
        biggest first."""
        if sort not in SORTS:
            raise ValueError(f"sort is one of {', '.join(SORTS)}, not {sort!r}.")
        return desc if desc is not None else sort not in ("name", "type")

    def list_items(self, *, folder: str = "", inside: bool = False, q: str | None = None,
                   kind: str | None = None, sort: str = "name",
                   desc: bool | None = None, limit: int = 200,
                   offset: int = 0) -> dict[str, Any]:
        """Documents under `folder` ("" is the top), all of them or, `inside`, only the
        ones directly in it; trashed ones left out. {"items", "total"}."""
        desc = self._check_sort(sort, desc)
        where, args = ["1"], []
        f = (folder or "").strip("/")
        if f:
            where.append("i.path LIKE ? ESCAPE '\\'")
            args.append(_like(f))
        if inside:
            where.append("instr(substr(i.path, ?), '/') = 0")
            args.append(len(f) + 2 if f else 1)
        where.append("i.trashed_at IS NULL")
        if kind:
            where.append("i.kind = ?")
            args.append(kind)
        if q and q.strip():
            expr, _, _ = match_expr(q)
            if expr is None:
                return {"items": [], "total": 0}
            where.append("i.id IN (SELECT -rowid FROM search_fts WHERE search_fts MATCH ? "
                         "AND rowid < 0 UNION SELECT s.item_id FROM segments s WHERE s.id IN "
                         "(SELECT rowid FROM search_fts WHERE search_fts MATCH ? AND rowid > 0))")
            args += [expr, expr]
        way = "DESC" if desc else "ASC"
        then = "" if sort == "name" else f", {_ORDER['name']} ASC"
        order = f"{_ORDER[sort]} {way}{then}, i.id {way}"
        sql_where = " AND ".join(where)

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            try:
                total = conn.execute(f"SELECT COUNT(*) FROM items i WHERE {sql_where}",
                                     args).fetchone()[0]
                rows = conn.execute(
                    f"SELECT i.* FROM items i WHERE {sql_where} ORDER BY {order} "
                    f"LIMIT ? OFFSET ?", [*args, int(limit), int(offset)]).fetchall()
            except sqlite3.OperationalError:
                return {"items": [], "total": 0}
            return {"items": [self._item_dict(r) for r in rows], "total": total}
        return self.file.read_sync(run)

    # ---- folders -----------------------------------------------------------------------------
    def folders_in(self, folder: str = "", *, sort: str = "name", desc: bool | None = None,
                   trashed: bool = False) -> list[dict[str, Any]]:
        """The folders directly in `folder`, each with the documents under it (count,
        size, newest change, first added; trashed documents left out), sorted like the
        documents. Trashed folders left out, or with `trashed` kept in."""
        desc = self._check_sort(sort, desc)
        f = (folder or "").strip("/")
        d = self.folder_dir(f)
        names = sorted(p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
        start = len(f) + 2 if f else 1

        def run(conn: sqlite3.Connection):
            agg = {r[0]: r for r in conn.execute(
                "SELECT substr(path, ?, instr(substr(path, ?), '/') - 1) AS child, "
                "COUNT(*), SUM(size), MAX(MAX(updated_at, mtime)), MIN(added_at) FROM items "
                "WHERE trashed_at IS NULL AND instr(substr(path, ?), '/') > 0"
                + (" AND path LIKE ? ESCAPE '\\'" if f else "") + " GROUP BY child",
                [start, start, start] + ([_like(f)] if f else []))}
            gone = {r[0]: r[1] for r in conn.execute("SELECT path, trashed_at FROM trashed_folders")}
            return agg, gone
        agg, gone = self.file.read_sync(run)
        out = []
        for n in names:
            path = f"{f}/{n}" if f else n
            if path in gone and not trashed:
                continue
            a = agg.get(n)
            mtime = int((d / n).stat().st_mtime * 1000)
            out.append({"path": path, "name": n, "items": a[1] if a else 0,
                        "size": a[2] if a else 0,
                        "updated_at": max(a[3], mtime) if a else mtime,
                        "added_at": a[4] if a else mtime, "trashed_at": gone.get(path)})
        key = {"name": lambda x: x["name"].lower(), "type": lambda x: x["name"].lower(),
               "added": lambda x: x["added_at"], "changed": lambda x: x["updated_at"],
               "size": lambda x: x["size"]}[sort]
        out.sort(key=key, reverse=desc)
        return out

    def folder(self, rel: str) -> dict[str, Any]:
        """One folder ("" is the top, named after the knowledge base), counted like
        `folders_in`. FileNotFoundError for an unknown one."""
        f = (rel or "").strip().strip("/")
        if not f:
            self.folder_dir("")
            n = self.file.read_sync(lambda c: c.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(MAX(MAX(updated_at, "
                "mtime)), 0), COALESCE(MIN(added_at), 0) FROM items WHERE trashed_at IS NULL"
            ).fetchone())
            return {"path": "", "name": self.title, "items": n[0], "size": n[1],
                    "updated_at": n[2], "added_at": n[3], "trashed_at": None}
        self.folder_dir(f)
        up = parent_of(f)
        got = next((x for x in self.folders_in(up, trashed=True) if x["path"] == f), None)
        if got is None:
            raise FileNotFoundError(f"The folder {f}")
        return got

    def breadcrumbs(self, rel: str) -> list[dict[str, str]]:
        """The path down to a folder: the top first, then each folder in it."""
        parts = [p for p in (rel or "").strip("/").split("/") if p]
        return [{"name": self.title, "path": ""}] + [
            {"name": p, "path": "/".join(parts[:i + 1])} for i, p in enumerate(parts)]

    def make_folder(self, rel: str) -> dict[str, Any]:
        p = self.resolve(rel)
        if p.exists():
            raise DocumentsPathError(f"{self.rel(p)} already exists")
        p.mkdir(parents=True)
        r = self.rel(p)
        self.changed([parent_of(r)])
        return self.folder(r)

    def _moved_to(self, rel: str, name: str | None, folder: str | None) -> str:
        """The path after a rename (`name`) and/or a move into `folder`."""
        if name is not None:
            name = name.strip()
            if not name or "/" in name or name in (".", ".."):
                raise DocumentsPathError(f"{name!r} is not a name. A name has no slash.")
        new_folder = parent_of(rel) if folder is None else self.folder_path(folder)
        new = name if name is not None else rel.rsplit("/", 1)[-1]
        return f"{new_folder}/{new}" if new_folder else new

    def move_folder(self, rel: str, *, name: str | None = None,
                    folder: str | None = None) -> dict[str, Any]:
        """Rename a folder (`name`) or move it into another (`folder`). Its documents
        keep their ids."""
        f = self.folder_path(rel)
        a = self.folder_dir(f)
        if not f:
            raise DocumentsPathError("The top of Documents cannot move.")
        dst = self._moved_to(f, name, folder)
        if dst == f:
            return self.folder(f)
        if dst.startswith(f + "/"):
            raise DocumentsPathError(f"{f} cannot move into itself.")
        b = self.resolve(dst)
        if b.exists():
            raise DocumentsPathError(f"{dst} already exists")
        with self._sync_lock:
            os.rename(a, b)

            def run(c: sqlite3.Connection) -> None:
                for table in ("items", "trashed_folders"):
                    c.execute(f"UPDATE {table} SET path = ? || substr(path, ?) "
                              f"WHERE path LIKE ? ESCAPE '\\'", (dst, len(f) + 1, _like(f)))
                c.execute("UPDATE trashed_folders SET path = ? WHERE path = ?", (dst, f))
            self.file.write_sync(run)
        self.changed([parent_of(f), parent_of(dst), dst])
        return self.folder(dst)

    def _trashed_above(self, c: sqlite3.Connection, path: str) -> str | None:
        """The trashed folder `path` is in, or None."""
        return next((r[0] for r in c.execute("SELECT path FROM trashed_folders")
                     if path.startswith(r[0] + "/")), None)

    def trash_folder(self, rel: str, trashed: bool) -> dict[str, Any]:
        """A folder into the trash with the documents in it (the same time on each), or
        back with the ones that went with it. A folder or document in a trashed folder
        comes back with that one."""
        f = self.folder_path(rel)
        if not f:
            raise DocumentsPathError("The top of Documents cannot go to the trash.")
        self.folder_dir(f)

        def run(c: sqlite3.Connection) -> None:
            above = self._trashed_above(c, f)
            if above:
                raise DocumentsPathError(f"{f} is in {above}, which is in the trash. Restore that one.")
            at = c.execute("SELECT trashed_at FROM trashed_folders WHERE path = ?", (f,)).fetchone()
            if trashed and at is None:
                now = now_ms()
                c.execute("INSERT INTO trashed_folders (path, trashed_at) VALUES (?, ?)", (f, now))
                c.execute("UPDATE items SET trashed_at = ? WHERE trashed_at IS NULL AND path LIKE ? "
                          "ESCAPE '\\'", (now, _like(f)))
            elif not trashed and at is not None:
                c.execute("DELETE FROM trashed_folders WHERE path = ?", (f,))
                c.execute("UPDATE items SET trashed_at = NULL WHERE trashed_at = ? AND path LIKE ? "
                          "ESCAPE '\\'", (at[0], _like(f)))
        self.file.write_sync(run)
        self.changed([parent_of(f), f])
        return self.folder(f)

    def delete_folder(self, rel: str) -> dict[str, Any]:
        """A trashed folder for good: its files and everything about them."""
        f = self.folder_path(rel)
        d = self.folder_dir(f)
        if not f or self.folder(f)["trashed_at"] is None:
            raise DocumentsPathError(f"{f or 'The top'} is not in the trash.")
        ids = self.ids_under(f)
        with self._sync_lock:
            shutil.rmtree(d)

            def run(c: sqlite3.Connection) -> None:
                for iid in ids:
                    self._delete_rows(c, iid)
                c.execute("DELETE FROM trashed_folders WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                          (f, _like(f)))
            self.file.write_sync(run)
        for iid in ids:
            self._emit({"type": DOCUMENTS_DELETED, "kb": self.id, "id": iid})
        self.changed([parent_of(f)])
        return {"deleted": len(ids), "path": f}

    def trash(self) -> dict[str, Any]:
        """What is in the trash, newest first: the folders, and the documents not in
        one of them."""
        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            folders = [r[0] for r in conn.execute(
                "SELECT path FROM trashed_folders ORDER BY trashed_at DESC")]
            items = [self._item_dict(r) for r in conn.execute(
                "SELECT * FROM items WHERE trashed_at IS NOT NULL ORDER BY trashed_at DESC")]
            return {"folders": folders, "items": [
                i for i in items if not any(i["path"].startswith(f + "/") for f in folders)]}
        got = self.file.read_sync(run)
        folders = []
        for f in got["folders"]:
            try:
                folders.append(self.folder(f))
            except FileNotFoundError:
                continue                       # gone from the disk meanwhile
        return {"folders": folders, "items": got["items"]}

    def ids_under(self, folder: str) -> list[int]:
        f = (folder or "").strip("/")
        if not f:
            return self.file.read_sync(lambda c: [r[0] for r in c.execute(
                "SELECT id FROM items")])
        return self.file.read_sync(lambda c: [r[0] for r in c.execute(
            "SELECT id FROM items WHERE path LIKE ? ESCAPE '\\'", (_like(f),))])

    def find_item(self, ref: str | int) -> dict[str, Any] | None:
        """An item by id ("42", "item 42", "documents:item/42"), path, file name or title;
        a trashed one is not found."""
        s = str(ref).strip()
        m = re.fullmatch(r"(?:[a-z0-9_-]+:)?(?:item[/ ]?)?(\d+)", s, re.I)
        if m:
            it = self.item(int(m.group(1)))
            if it is not None:
                return None if it["trashed"] else it
        low = s.lower().strip("/")

        def run(conn: sqlite3.Connection):
            rows = conn.execute("SELECT id, path, title, user_title FROM items "
                                "WHERE trashed_at IS NULL").fetchall()
            for key in (lambda r: r["path"].lower(),
                        lambda r: Path(r["path"]).name.lower(),
                        lambda r: Path(r["path"]).stem.lower(),
                        lambda r: (r["user_title"] or "").lower(),
                        lambda r: r["title"].lower()):
                hit = [r["id"] for r in rows if key(r) == low]
                if hit:
                    return hit[0]
            hit = [r["id"] for r in rows if low and low in (
                f"{r['user_title'] or ''} {r['title']} {r['path']}").lower()]
            return hit[0] if len(hit) == 1 else None
        iid = self.file.read_sync(run)
        return self.item(iid) if iid is not None else None

    def segments(self, iid: int, *, from_seq: int = 0, limit: int = 500,
                 around: int | None = None) -> dict[str, Any]:
        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            start = max(0, int(from_seq))
            if around is not None:
                r = conn.execute("SELECT seq FROM segments WHERE id = ? AND item_id = ?",
                                 (around, iid)).fetchone()
                if r is not None:
                    start = max(0, r[0] - limit // 2)
            rows = conn.execute("SELECT * FROM segments WHERE item_id = ? AND seq >= ? "
                                "ORDER BY seq LIMIT ?", (iid, start, int(limit) + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            return {"segments": [self._seg_dict(r) for r in rows],
                    "has_before": start > 0 and bool(rows), "has_after": more}
        return self.file.read_sync(run)

    def _seg_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        return {"id": r["id"], "ref": f"{self.id}:{r['id']}", "seq": r["seq"],
                "kind": r["kind"], "page": r["page"], "heading": r["heading"],
                "t_ms": r["t_ms"], "speaker": r["speaker"], "text": r["text"]}

    def resolve_segments(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        """Segment id -> the shape the citation verifier and the web read: text,
        sender (a subtitle speaker, or "description" for an image caption), item."""
        def run(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
            out: dict[int, dict[str, Any]] = {}
            chunk = [int(i) for i in ids][:2000]
            if not chunk:
                return out
            rows = conn.execute(
                f"SELECT s.*, i.path, i.title AS i_title, i.user_title, i.kind AS i_kind "
                f"FROM segments s JOIN items i ON i.id = s.item_id "
                f"WHERE s.id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            for r in rows:
                sender = ({"name": r["speaker"]} if r["speaker"] else
                          {"name": "description"} if r["kind"] == "caption" else None)
                out[r["id"]] = {"id": r["id"], "ref": f"{self.id}:{r['id']}", "kb": self.id,
                                "text": r["text"], "from_me": False, "sender": sender,
                                "kind": "document", "segment": self._seg_dict(r),
                                "item": {"id": r["item_id"], "path": r["path"],
                                         "title": r["user_title"] or r["i_title"],
                                         "kind": r["i_kind"]}}
            return out
        return self.file.read_sync(run)

    # ---- search ------------------------------------------------------------------------------
    def search_segments(self, q: str, *, item_ids: list[int] | None = None,
                        limit: int = 30, any_word: bool = False) -> dict[str, Any]:
        """Instant search over the text of chunked files and the names/descriptions of
        every file, grouped per item, best first. Every hit carries its segment ref.
        `any_word`: names and descriptions match on any word of the query (a question),
        the text still on all of them."""
        expr, exact, prefixes = match_expr(q)
        if expr is None:
            return {"items": [], "total_items": 0, "total_hits": 0}
        loose = match_any(q) if any_word else None
        scope = fts_scope = ""
        args: list[Any] = [expr]
        if item_ids:
            ids = ",".join(str(int(i)) for i in item_ids)
            scope = f" AND s.item_id IN ({ids})"
            # narrow before the cap, or a big library crowds the scoped hits out
            fts_scope = (f" AND (f.rowid IN (SELECT id FROM segments WHERE item_id IN ({ids}))"
                         f" OR -f.rowid IN ({ids}))")

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            try:
                rows = conn.execute(
                    "SELECT f.rowid AS sid, bm25(search_fts) AS score FROM search_fts f "
                    f"WHERE search_fts MATCH ?{fts_scope} ORDER BY score LIMIT 2000", args).fetchall()
            except sqlite3.OperationalError:
                return {"items": [], "total_items": 0, "total_hits": 0}
            seg_ids = [r["sid"] for r in rows if r["sid"] > 0]
            meta_ids = {-r["sid"]: r["score"] for r in rows if r["sid"] < 0}
            if loose:
                for r in conn.execute(
                        "SELECT rowid AS sid, bm25(search_fts) AS score FROM search_fts "
                        "WHERE search_fts MATCH ? AND rowid < 0 ORDER BY score LIMIT 200",
                        (loose,)):
                    meta_ids[-r["sid"]] = min(meta_ids.get(-r["sid"], 0.0), r["score"])
            segs: dict[int, sqlite3.Row] = {}
            for part in in_batches(seg_ids):
                for s in conn.execute(
                        f"SELECT s.* FROM segments s WHERE s.id IN ({','.join('?' * len(part))})"
                        f"{scope}", part):
                    segs[s["id"]] = s
            groups: dict[int, dict[str, Any]] = {}
            for r in rows:
                if r["sid"] > 0:
                    s = segs.get(r["sid"])
                    if s is None:
                        continue
                    g = groups.setdefault(s["item_id"], {"score": 0.0, "hits": [], "n": 0,
                                                         "name_match": False})
                    g["n"] += 1
                    g["score"] += r["score"] if g["n"] <= 3 else 0.0
                    if len(g["hits"]) < 5:
                        d = self._seg_dict(s)
                        d["snippet"] = normalize.highlight(s["text"], exact, prefixes, 240)
                        g["hits"].append(d)
            gone = {r[0] for r in conn.execute("SELECT id FROM items WHERE trashed_at IS NOT NULL")}
            groups = {k: v for k, v in groups.items() if k not in gone}
            for iid, sc in meta_ids.items():
                if item_ids and iid not in item_ids or iid in gone:
                    continue
                g = groups.setdefault(iid, {"score": 0.0, "hits": [], "n": 0,
                                            "name_match": False})
                g["name_match"] = True
                g["score"] += sc * 2
            order = sorted(groups.items(), key=lambda kv: kv[1]["score"])
            items = []
            for iid, g in order[:limit]:
                r = conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
                if r is None:
                    continue
                items.append({"item": self._item_dict(r), "hits": g["n"],
                              "name_match": g["name_match"], "best": g["hits"]})
            return {"items": items, "total_items": len(groups),
                    "total_hits": sum(g["n"] for g in groups.values())}
        return self.file.read_sync(run)

    # ---- items: changing ---------------------------------------------------------------------
    def set_meta(self, iid: int, **fields: Any) -> dict[str, Any]:
        """`title` / `description`: the user's layers. None or "" clears one."""
        cols = {"title": "user_title", "description": "user_description"}
        sets = [(cols[k], (str(v).strip() or None) if v is not None else None)
                for k, v in fields.items() if k in cols]

        def run(conn: sqlite3.Connection) -> bool:
            if conn.execute("SELECT 1 FROM items WHERE id = ?", (iid,)).fetchone() is None:
                return False
            for col, v in sets:
                conn.execute(f"UPDATE items SET {col} = ?, updated_at = ? WHERE id = ?",
                             (v, now_ms(), iid))
            self.index_meta(conn, iid)
            if any(c == "user_title" for c, _ in sets):
                self._apply_mode(conn, iid)
            return True
        if not self.file.write_sync(run):
            raise KeyError(iid)
        self.item_event(iid)
        return self.item(iid)

    def move_item(self, iid: int, *, name: str | None = None,
                  folder: str | None = None) -> dict[str, Any]:
        """Rename a document's file (`name`) or move it into another folder (`folder`,
        "" is the top). Its id and layers stay."""
        it = self.item(iid)
        if it is None:
            raise KeyError(iid)
        a = self.resolve(it["path"], must_exist=True)
        dst = self._moved_to(it["path"], name, folder)
        if dst == it["path"]:
            return it
        b = self.resolve(dst)
        if b.exists():
            raise DocumentsPathError(f"{dst} already exists")
        with self._sync_lock:
            shutil.move(a, b)
            # a title taken from the file's name follows the new name
            self.file.write_sync(lambda c: (c.execute(
                "UPDATE items SET path = ?, updated_at = ?, title = CASE WHEN title = ? "
                "THEN ? ELSE title END WHERE id = ?",
                (dst, now_ms(), Path(it["path"]).stem, Path(dst).stem, iid)),
                self.index_meta(c, iid)))
        self.item_event(iid)
        self.changed([parent_of(it["path"]), parent_of(dst)])
        return self.item(iid)

    def set_trashed(self, iid: int, trashed: bool) -> dict[str, Any]:
        """Into the trash (out of the listings, search and its tools), or back. One in a
        trashed folder comes back with the folder."""
        def run(c: sqlite3.Connection) -> int:
            r = c.execute("SELECT path FROM items WHERE id = ?", (iid,)).fetchone()
            if r is None:
                return 0
            above = self._trashed_above(c, r[0])
            if above and not trashed:
                raise DocumentsPathError(f"{r[0]} is in {above}, which is in the trash. Restore that one.")
            return c.execute("UPDATE items SET trashed_at = ? WHERE id = ?",
                             (now_ms() if trashed else None, iid)).rowcount
        if not self.file.write_sync(run):
            raise KeyError(iid)
        it = self.item(iid)
        self.item_event(iid)
        self.changed([it["folder"]])
        return it

    def delete_item(self, iid: int) -> dict[str, Any]:
        """The user's delete: the file and everything about it."""
        it = self.item(iid)
        if it is None:
            raise KeyError(iid)
        p = self.resolve(it["path"])
        if p.is_file():
            p.unlink()
        self.file.write_sync(lambda c: self._delete_rows(c, iid))
        self._emit({"type": DOCUMENTS_DELETED, "kb": self.id, "id": iid})
        self.changed([it["folder"]])
        return {"deleted": 1, "id": iid}

    def reindex(self, iid: int, *, describe: bool = True, quiet: bool = True) -> Any:
        """Read one item again (and describe it again)."""
        def run(conn: sqlite3.Connection) -> int:
            n = conn.execute("UPDATE items SET state = 'pending', error = NULL WHERE id = ?",
                             (iid,)).rowcount
            if describe:
                conn.execute("UPDATE items SET ai_state = 'pending', ai_attempts = 0, "
                             "ai_error = NULL, ai_source_hash = NULL WHERE id = ?", (iid,))
                conn.execute("DELETE FROM describe_parts WHERE item_id = ?", (iid,))
            return n
        if not self.file.write_sync(run):
            raise KeyError(iid)
        return self.kick(quiet=quiet)

    def add_file(self, src: Path, name: str, *, folder: str = "",
                 title: str | None = None, description: str | None = None,
                 mode: str | None = None, move: bool = True) -> dict[str, Any]:
        """A file into the Documents folder, chunked or full_file (`mode`; None: the
        default for its kind and size). The same content twice is refused with the
        item that has it (`existing`). Returns {"item": Item, "existing": bool}."""
        if mode is not None and mode not in MODES:
            raise ValueError(f"The mode is chunked or full_file, not {mode!r}.")
        sha = sha256_file(src)
        dup = self.file.read_sync(lambda c: c.execute(
            "SELECT id FROM items WHERE sha256 = ? ORDER BY id LIMIT 1", (sha,)).fetchone())
        if dup is not None and (self.root / self.item(dup[0])["path"]).is_file():
            if move:
                src.unlink(missing_ok=True)
            return {"item": self.item(dup[0]), "existing": True}
        name = Path(name).name.strip() or "file"
        if name.startswith("."):
            name = name.lstrip(".") or "file"
        rel = f"{folder.strip('/')}/{name}" if folder.strip("/") else name
        dest = self.resolve(rel)
        stem, suffix = dest.stem, dest.suffix
        n = 2
        while dest.exists():
            dest = dest.with_name(f"{stem} ({n}){suffix}")
            n += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part")
        if move:
            shutil.move(src, tmp)
        else:
            shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        rel = self.rel(dest)
        st = dest.stat()

        def run(conn: sqlite3.Connection) -> int:
            r = conn.execute("SELECT id FROM items WHERE path = ?", (rel,)).fetchone()
            if r is not None:
                return r[0]
            iid = self._insert(conn, rel, sha, int(st.st_mtime * 1000), st.st_size, now_ms(),
                               mode)
            if title or description:
                conn.execute("UPDATE items SET user_title = ?, user_description = ? "
                             "WHERE id = ?", ((title or "").strip() or None,
                                              (description or "").strip() or None, iid))
                self.index_meta(conn, iid)
            return iid
        with self._sync_lock:
            iid = self.file.write_sync(run)
        self.item_event(iid)
        return {"item": self.item(iid), "existing": False}

    # ---- summary ------------------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            r = conn.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(state = 'pending') AS reading, SUM(state = 'failed') AS failed, "
                "SUM(state = 'ready' AND ai_state = 'pending') AS describing, "
                "SUM(ai_state = 'done') AS described FROM items").fetchone()
            return {"id": self.id, "kind": self.kind, "title": self.title,
                    "platform": self.platform, "items": r["n"] or 0,
                    "reading": r["reading"] or 0, "failed": r["failed"] or 0,
                    "describing": r["describing"] or 0, "described": r["described"] or 0,
                    "semantic": P.status(conn)}
        return self.file.read_sync(run)


def _n(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _say_read(r: dict[str, Any]) -> str:
    s = f"Added {_n(r.get('read', 0), 'document', 'documents')}."
    if r.get("failed"):
        s += f" Text extraction failed for {_n(r['failed'], 'document', 'documents')}."
    return s


def _say_described(r: dict[str, Any]) -> str:
    s = f"Wrote {_n(r.get('described', 0), 'AI description', 'AI descriptions')}."
    if r.get("failed"):
        s += f" {r['failed']} failed."
    if r.get("stopped"):
        s += f" Stopped: {r['stopped']}."
    return s


def size_line(it: dict[str, Any]) -> str:
    bits = [it["kind"]]
    if it.get("pages"):
        bits.append(f"{it['pages']} {'page' if it['pages'] == 1 else 'pages'}"
                    if it["kind"] != "epub" else f"{it['pages']} chapters")
    if it.get("n_words"):
        bits.append(f"{it['n_words']:,} words")
    b = it.get("size") or 0
    bits.append(f"{b / 1048576:.1f} MB" if b >= 1048576 else f"{max(1, b // 1024)} KB")
    return ", ".join(bits)


def item_line(it: dict[str, Any], *, full: bool = True) -> str:
    """One document as Celine sees it before opening it: the three layers and its mode."""
    head = (f"- document {it['id']}: \"{it['title']}\"; {it['mode']}; "
            f"{size_line(it)}; {it['path']}")
    if it.get("state") == "pending":
        head += "; text extraction still runs"
    elif it.get("state") == "failed":
        head += f"; text extraction failed ({it.get('error') or 'error'})"
    lines = [head]
    if full:
        if it.get("user_description"):
            lines.append(f"    Their description: {it['user_description']}")
        ai = it.get("ai") or {}
        if it.get("ai_description"):
            stale = " (written for an older version of the file)" if ai.get("stale") else ""
            lines.append(f"    AI description{stale}: {it['ai_description']}")
        elif ai.get("state") == "pending":
            lines.append("    AI description: not written yet")
    return "\n".join(lines)
