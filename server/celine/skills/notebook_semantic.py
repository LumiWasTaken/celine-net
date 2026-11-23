"""The notebook's semantic index: canonical chunks of the live notes as int8 vectors, so
recall (skills/recall.py) and notebook__search find a note by what it means, not only by
its words.

The note files stay the source of truth. notebook_index.cut writes each note's canonical
chunk rows (notebook_sections) and its payload hash on every write and reconcile; the
scheduler here embeds every chunk whose vector is missing or stale with its payload
(notebook_vecs), in the app's background LLM queue like every index build. The identity
of the model that made the vectors (kb/embed.py fingerprint probes), its actual width and
the payload render version live in notebook_meta: the first configured Embeddings model
owns the fingerprint. A later endpoint may build or search only while its fingerprint
matches; a mismatch on a secondary abstains and never resets. A fingerprint change on the
primary, or another render version, drops the vectors and builds again, readiness false
until that pass finished. Readiness stays true across later edits: a changed note simply
reaches the index on the next pass.

Query side: `query_vector` embeds a query with the notebook instruction (kb.toml
[embeddings] notebook_query_prefix) and quantises it like the index; `dense` scores every
stored chunk exactly (a few thousand at most) with sqlite-vec's cosine and returns the
ones whose payload hash still matches its section; `embed_texts` quantises fresh texts
for the key candidates dense did not cover. The index shape ([embeddings] in
config/kb.toml: width, batch) is the index's, but the width is taken from the vectors the
model actually returns, so a configured 4096 with a 2560-dimensional model works.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import struct
from typing import Any

log = logging.getLogger("celine.skills.notebook_semantic")

MODEL_KEY = "model"
READY_KEY = "ready"
RENDER_KEY = "render"
IDLE_SECONDS = 10.0       # between passes
BACKOFF_SECONDS = 60.0    # after a pass that found no model
PASS_NOTES = 64           # notes re-cut and embedded at most in one pass
QUERY_SECONDS = 10.0      # a query's most seconds on the embedding server


async def probe(cfg, services) -> dict[str, str]:
    """Settings, Features: an Embeddings model is configured, plus local index progress."""
    from ..kb import semantic
    if not semantic.embeddings_available(cfg):
        return {"state": "unset", "detail": "Pick an Embeddings model in Settings, Models."}
    store = getattr(services, "store", None)
    if store is None:
        return {"state": "ready", "detail": "An Embeddings model is picked."}
    def status(c: sqlite3.Connection) -> dict[str, Any]:
        return {"ready": ready_of(c),
                "sections": c.execute("SELECT COUNT(*) FROM notebook_sections").fetchone()[0],
                "vectors": c.execute("SELECT COUNT(*) FROM notebook_vecs").fetchone()[0]}
    st = await store.read(status)
    if st["ready"]:
        noun = "section" if st["vectors"] == 1 else "sections"
        detail = f"Ready: {st['vectors']} {noun} indexed."
    elif st["sections"]:
        noun = "section" if st["sections"] == 1 else "sections"
        detail = f"Indexing notes: {st['vectors']} of {st['sections']} {noun}."
    else:
        detail = "Preparing the notebook index."
    return {"state": "ready", "detail": detail}


# ---- the index's identity ---------------------------------------------------------------

def meta_of(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """{dims, probes, label} of the model that owns the vectors, None before the first."""
    r = conn.execute("SELECT value FROM notebook_meta WHERE key=?", (MODEL_KEY,)).fetchone()
    return json.loads(r[0]) if r else None


def ready_of(conn: sqlite3.Connection) -> bool:
    """Whether the initial (or rebuilt) index finished its full pass."""
    r = conn.execute("SELECT value FROM notebook_meta WHERE key=?", (READY_KEY,)).fetchone()
    return bool(r and r[0] == "1")


def render_of(conn: sqlite3.Connection) -> int:
    r = conn.execute("SELECT value FROM notebook_meta WHERE key=?", (RENDER_KEY,)).fetchone()
    return int(r[0]) if r else 0


def state_of(conn: sqlite3.Connection) -> dict[str, Any]:
    """{model, ready, render} for the query-side gates."""
    return {"model": meta_of(conn), "ready": ready_of(conn), "render": render_of(conn)}


def _put(c: sqlite3.Connection, key: str, value: str) -> None:
    c.execute("INSERT INTO notebook_meta(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _set_meta(c: sqlite3.Connection, dims: int, probes: list, label: str) -> None:
    _put(c, MODEL_KEY, json.dumps({"dims": dims, "probes": probes, "label": label}))


def _set_ready(c: sqlite3.Connection, value: bool) -> None:
    _put(c, READY_KEY, "1" if value else "0")


def _set_render(c: sqlite3.Connection, value: int) -> None:
    _put(c, RENDER_KEY, str(value))


def _reset(c: sqlite3.Connection) -> None:
    """The owner changed: the vectors and the fingerprint out, readiness false, the next
    pass embeds everything again under the new model."""
    c.execute("DELETE FROM notebook_vecs")
    c.execute("DELETE FROM notebook_meta WHERE key IN (?, ?)", (MODEL_KEY, READY_KEY))


def _drop_orphans(c: sqlite3.Connection) -> None:
    """Rows for notes the index no longer knows (a file deleted outside the app)."""
    for table in ("notebook_semantic_state", "notebook_sections", "notebook_vecs"):
        c.execute(f"DELETE FROM {table} WHERE path NOT IN (SELECT path FROM notebook_index)")


def _ensure_render(c: sqlite3.Connection) -> None:
    """Another payload render version: the old vectors and their section rows are no
    scores, so they are dropped and built again under the current cut."""
    from . import notebook_index
    if render_of(c) == notebook_index.RENDER_VER:
        return
    c.execute("DELETE FROM notebook_vecs")
    c.execute("DELETE FROM notebook_sections")
    c.execute("DELETE FROM notebook_semantic_state")
    _set_render(c, notebook_index.RENDER_VER)
    _set_ready(c, False)


def effective_dims(cfg, actual: int) -> int:
    """The index width: the configured [embeddings] dims, or the model's actual width
    when that is smaller (a configured 4096 with a 2560-dimensional model keeps 2560)."""
    e = getattr(cfg, "embeddings", None) or {}
    want = int(e.get("dims", actual))
    dims = min(want, actual)
    return max(8, dims - dims % 8)


# ---- the query side ---------------------------------------------------------------------

def _int8_values(b: bytes) -> list[int]:
    return list(struct.unpack(f"{len(b)}b", b)) if b else []


def score(qv, v) -> float:
    """Cosine of two quantised vectors, the same measure sqlite-vec scores stored ones
    with, so stored and fresh candidates compare before memory_floor."""
    from ..kb.embed import cosine
    return cosine(_int8_values(qv.int8), _int8_values(v.int8))


def query_vector(cfg, q: str, timeout: float | None = QUERY_SECONDS) -> Any:
    """The query as kb/embed.Vec: the notebook instruction (kb.toml
    notebook_query_prefix), cut and quantised to the index width, the model's fingerprint
    on. SemanticError when no model of the Embeddings task answers."""
    from ..kb import semantic
    e = semantic.query_embedder(cfg)
    prefix = str(e.s.get("notebook_query_prefix", ""))
    v = e.embed([prefix + q], **({"timeout": timeout} if timeout else {}))[0]
    return semantic.quantize(v, effective_dims(cfg, len(v)), e.fp)


def embed_texts(cfg, texts: list[str], timeout: float | None = QUERY_SECONDS) -> list[Any]:
    """Canonical payloads as quantised vectors of the model that answers now, cut and
    quantised exactly like the stored ones. SemanticError when none answers."""
    from ..kb import semantic
    if not texts:
        return []
    e = semantic.query_embedder(cfg)
    vecs = e.embed(list(texts), **({"timeout": timeout} if timeout else {}))
    dims = effective_dims(cfg, len(vecs[0]))
    return [semantic.quantize(v, dims, e.fp) for v in vecs]


def dense(conn: sqlite3.Connection, qv, folder: str | None = None,
          k: int | None = None) -> list[dict]:
    """Every stored chunk scored for the query vector `qv`, best first:
    {path, nth, chunk, heading, hash, summary, touched_at, score}. Only a vector whose
    payload hash still equals its current section's is a score. Empty when the index is
    not ready, another model (its fingerprint) owns it, or a secondary's fingerprint does
    not match (it abstains, it does not reset)."""
    from ..kb import embed as embed_mod
    meta = meta_of(conn)
    if not ready_of(conn) or meta is None or qv.fp is None:
        return []
    if not embed_mod.matches(meta["probes"], qv.fp) or meta["dims"] != qv.dims:
        return []
    where, args = ["v.hash = s.hash"], []
    if folder:
        p = folder.rstrip("/") + "/"
        where.append("substr(s.path, 1, ?) = ?")
        args += [len(p), p]
    sql = ("SELECT s.path, s.nth, s.chunk, s.heading, s.hash, i.summary, i.touched_at, "
           "1 - vec_distance_cosine(vec_int8(v.int8), vec_int8(?)) AS score "
           "FROM notebook_sections s JOIN notebook_vecs v ON v.path=s.path "
           "AND v.nth=s.nth AND v.chunk=s.chunk "
           "JOIN notebook_index i ON i.path=s.path "
           f"WHERE {' AND '.join(where)} ORDER BY score DESC")
    if k is not None:
        sql += " LIMIT ?"
        args = [*args, k]
    rows = conn.execute(sql, (qv.int8, *args)).fetchall()
    return [{"path": r["path"], "nth": r["nth"], "chunk": r["chunk"],
             "heading": r["heading"], "hash": r["hash"], "summary": r["summary"],
             "touched_at": r["touched_at"], "score": round(r["score"], 3)} for r in rows]


# ---- the build ---------------------------------------------------------------------------

def _dirty(c: sqlite3.Connection, most: int) -> list[str]:
    """The notes with work: never cut (no state), or a chunk whose vector is missing or
    stale, in path order."""
    return [r["path"] for r in c.execute(
        "SELECT i.path FROM notebook_index i "
        "LEFT JOIN notebook_semantic_state st ON st.path=i.path "
        "WHERE st.path IS NULL OR EXISTS ("
        "  SELECT 1 FROM notebook_sections s LEFT JOIN notebook_vecs v "
        "    ON v.path=s.path AND v.nth=s.nth AND v.chunk=s.chunk "
        "  WHERE s.path=i.path AND (v.path IS NULL OR v.hash != s.hash)) "
        "ORDER BY i.path LIMIT ?", (most,))]


def _recut(c: sqlite3.Connection, path: str, body: str, sub) -> list[dict]:
    """Cut one note's chunks from its file as it is now, keeping a vector whose payload
    hash still exists; -> the rows that still need embedding."""
    from . import notebook_index
    r = c.execute("SELECT summary FROM notebook_index WHERE path=?", (path,)).fetchone()
    summary = r["summary"] if r else ""
    keep = {v["hash"]: v for v in c.execute(
        "SELECT nth, chunk, hash, int8, bits FROM notebook_vecs WHERE path=?", (path,))}
    c.execute("DELETE FROM notebook_sections WHERE path=?", (path,))
    c.execute("DELETE FROM notebook_vecs WHERE path=?", (path,))
    jobs: list[dict] = []
    for row in notebook_index.cut(body, path, summary, sub):
        c.execute("INSERT INTO notebook_sections(path,nth,chunk,heading,chars,hash) "
                  "VALUES(?,?,?,?,?,?)",
                  (path, row["nth"], row["chunk"], row["heading"], row["chars"], row["hash"]))
        if (v := keep.get(row["hash"])) is not None:
            c.execute("INSERT INTO notebook_vecs(path,nth,chunk,hash,int8,bits) "
                      "VALUES(?,?,?,?,?,?)",
                      (path, row["nth"], row["chunk"], row["hash"], v["int8"], v["bits"]))
        else:
            jobs.append(row)
    notebook_index._set_state(c, path, body, summary)
    return jobs


def _note_body(cfg, path: str) -> str | None:
    try:
        return (cfg.agent.notebook / path).read_text(newline="")
    except OSError:
        return None


async def _embedder(cfg, store, meta: dict | None):
    """The model that adds vectors now: (Embedder, width, took_ownership). The first
    configured Embeddings model owns the index; when it gives other vectors (or another
    width) the vectors are dropped and everything is embedded again. A later endpoint may
    add or search only while its fingerprint matches; a mismatch abstains (Unreachable)
    and never resets. With no stored owner only the first model may take ownership."""
    from ..kb import semantic
    from ..kb import embed as embed_mod
    from ..kb.embed import SemanticError
    from ..kb.importers import Unreachable
    ordered, _ = await asyncio.to_thread(semantic._usable, cfg)
    if not ordered.refs:
        raise Unreachable("No embedding model is set. Choose one in Settings, Models.")
    primary = ordered.chain[0] if getattr(ordered, "chain", None) else ordered.refs[0]
    for ref in ordered.refs:
        try:
            e = semantic.Embedder(cfg, ref)
        except SemanticError:
            continue
        dims = effective_dims(cfg, len(e.fp[0]))
        if meta is None:
            if ref == primary:
                return e, dims, True
            continue                      # no owner yet: a secondary may not take it
        if embed_mod.matches(meta.get("probes"), e.fp) and meta.get("dims") == dims:
            return e, dims, False
        if ref == primary:
            await store.write(_reset)     # the owner changed: rebuild under it
            return e, dims, True
        # a secondary that does not match abstains here; never reset
    raise Unreachable("the embedding model did not answer")


def _save(c: sqlite3.Connection, batch: list[dict], qs: list) -> None:
    c.executemany(
        "INSERT INTO notebook_vecs(path,nth,chunk,hash,int8,bits) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(path,nth,chunk) DO UPDATE SET hash=excluded.hash, "
        "int8=excluded.int8, bits=excluded.bits",
        [(r["path"], r["nth"], r["chunk"], r["hash"], q.int8, q.bits)
         for r, q in zip(batch, qs)])


async def _pass(cfg, store, loop: asyncio.AbstractEventLoop) -> int:
    """One build pass: reconcile the section rows with the files, embed every chunk whose
    vector is missing or stale in batches, each batch committed as it lands, and mark the
    index ready once the full pass is done. -> the chunks embedded. Unreachable when no
    model can do it now: the next pass continues."""
    from ..settings import features
    if not features.on(cfg, "vector_notebook"):
        return 0                          # the feature is off: never an embedding call
    from ..kb import semantic
    from ..kb.embed import Pool, SemanticError, quantize, stored_probes
    if not semantic.embeddings_available(cfg):
        return 0
    await store.write(_ensure_render)
    await store.write(_drop_orphans)
    meta = await store.read(meta_of)
    e, dims, owner = await _embedder(cfg, store, meta)
    if owner:
        await store.write(lambda c: (_set_meta(
            c, dims, stored_probes(e.fp, dims), e.ref), _set_ready(c, False)))
    paths = await store.read(lambda c: _dirty(c, PASS_NOTES))

    jobs: list[dict] = []
    for path in paths:
        body = await asyncio.to_thread(_note_body, cfg, path)
        if body is None:                  # the note is gone, its rows stay behind
            await store.write(lambda c, path=path: (
                c.execute("DELETE FROM notebook_index WHERE path=?", (path,)),
                c.execute("DELETE FROM notebook_fts WHERE path=?", (path,)),
                c.execute("DELETE FROM notebook_semantic_state WHERE path=?", (path,)),
                c.execute("DELETE FROM notebook_sections WHERE path=?", (path,)),
                c.execute("DELETE FROM notebook_vecs WHERE path=?", (path,))))
            continue
        jobs.extend(await store.write(
            lambda c, path=path, body=body: _recut(c, path, body, cfg.substitute)))

    if jobs:
        task = asyncio.current_task()

        def check() -> None:
            if task is not None and task.cancelling():
                raise asyncio.CancelledError

        size = max(1, int(e.s.get("batch", 64)))
        batches = [jobs[i:i + size] for i in range(0, len(jobs), size)]
        expected_width = len(e.fp[0])

        def run_pool() -> None:
            """Network batches off the event loop; each finished batch commits before the
            next one is needed, so a cancelled build resumes where it stopped."""
            def on_batch(i: int, vecs: list[list[float]]) -> None:
                if {len(v) for v in vecs} != {expected_width}:
                    raise SemanticError("the embedding server returned vectors of an "
                                        "inconsistent width")
                batch = batches[i]
                qs = [quantize(v, dims) for v in vecs]
                asyncio.run_coroutine_threadsafe(
                    store.write(lambda c, batch=batch, qs=qs: _save(c, batch, qs)),
                    loop).result()
            Pool(e.s, semantic._embed).run(
                [[r["payload"] for r in batch] for batch in batches], on_batch, check)

        await asyncio.to_thread(run_pool)

    # the initial (or rebuilt) pass is done once nothing is left to embed
    if not await store.read(lambda c: _dirty(c, 1)):
        await store.write(lambda c: _set_ready(c, True))
    return len(jobs)


async def run_scheduler(cfg, store) -> None:
    """The notebook's index builds itself in the background: the first pass runs as soon
    as the runner starts, then one every IDLE_SECONDS; a pass that found no model waits
    BACKOFF_SECONDS. The feature being off stops every embedding call."""
    loop = asyncio.get_running_loop()
    from ..kb.importers import Unreachable
    from ..kb.embed import SemanticError
    delay = 0.0
    while True:
        if delay:
            await asyncio.sleep(delay)
        try:
            n = await _pass(cfg, store, loop)
        except (Unreachable, SemanticError) as e:
            log.debug("notebook semantic index waits", extra={"reason": str(e)})
            delay = BACKOFF_SECONDS
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("notebook semantic index pass failed",
                        extra={"event": "notebook.semantic.failed"})
            delay = BACKOFF_SECONDS
            continue
        delay = IDLE_SECONDS
        if n:
            log.info("notebook sections embedded",
                     extra={"event": "notebook.semantic.embedded", "sections": n})
