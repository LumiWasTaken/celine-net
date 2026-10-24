"""Semantic search: sessions -> passages -> int8 vectors, per knowledge base.

The index lives in the knowledge base's own file: tables from
kb/migrations/, built by kb/passages.py, embedded through kb/embed.py. Built
by the scheduler below, incrementally: only chats with new, edited or deleted messages
are re-cut, only passages whose text changed are embedded. Celine searches it through kb/retrieve.

Public API
- `build(cfg, kb, ctx)`                       the task: cut, then embed
- `embed_passages(cfg, path, ctx)`            the embedding pass (chats and Documents)
- `embeddings_available(cfg)`                 a Topic search model is set: the one gate
- `query_embedder(cfg)`, `query_vector(cfg, q)`  the query side
- `search_sync(cfg, kbs, q, ...)`             plain passage search
- `run_scheduler(cfg, kbs)`                   new messages reach semantic topic search

A knowledge base whose passages are not ready yet (never built, or the first build is
still embedding) has no semantic search: dense search returns nothing there.

Which model embeds is the Embeddings task in Settings, Models: the first model of its
list that answers. An index remembers the fingerprint of the model that made it
(kb/embed.py); when the first model of the list gives other vectors, the next build
drops the old vectors and embeds everything again, and semantic topic search is off for that
knowledge base until it is done. While the first model is unreachable, a later one
that gives the same vectors (the same weights on another server) takes over; if none
does, the build waits for the next run. The index's shape (width, batch, prefix) is
`[embeddings]` in config/kb.toml.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from . import passages
from .db import kv
from .chat import ChatKB, chat_cards
from .embed import Pool, SemanticError, Vec, embed_sync, fingerprint, quantize
from .importers import ImportContext
from ..settings import general as general_mod
from .sessions import SessionParams

log = logging.getLogger("celine.kb.semantic")

__all__ = ["SemanticError", "build", "embed_passages", "embeddings_available",
           "query_embedder", "query_vector", "search_sync", "run_scheduler"]


def embeddings_available(cfg) -> bool:
    """A Topic search model is set (Settings, Models). The one gate for every embedding
    and every semantic search; whether it answers now is the router's next question."""
    from ..backends.router import app_router
    return bool(app_router(cfg).chain("embeddings"))


def _shape(cfg) -> dict[str, Any]:
    """[embeddings] in config/kb.toml: how the index is made, not where."""
    e = cfg.embeddings
    return {**e, "batch": int(e.get("batch", 64)), "workers": int(e.get("workers", 4)),
            "dims": int(e.get("dims", 1024)),
            "query_prefix": str(e.get("query_prefix", "")),
            "max_input_chars": int(e.get("max_input_chars", 3000))}


class Embedder:
    """One model of the Embeddings task: its request settings and its fingerprint."""

    def __init__(self, cfg, ref: str):
        from ..backends.router import app_router
        self.router = app_router(cfg)
        bc = self.router.backend_config(ref)
        if bc is None:
            raise SemanticError(f"{ref} is not a model in Settings.")
        self.ref = ref
        self.s = {**_shape(cfg), "url": f"{bc.base_url}/embeddings", "key": bc.api_key,
                  "headers": dict(bc.headers), "model": bc.model}
        try:
            self.fp = fingerprint(self.s, _embed)
        except SemanticError as e:
            self.router.note_failed(ref, "error", str(e))
            raise

    def embed(self, texts: list[str], **over) -> list[list[float]]:
        return _embed({**self.s, **over}, texts)


def _embed(s: dict[str, Any], texts: list[str]) -> list[list[float]]:
    return embed_sync(s, texts)            # looked up at call time: tests replace it


def _usable(cfg):
    """(the task's models that can answer now, in order, and the router's view)."""
    from ..backends.router import app_router
    router = app_router(cfg)
    return router.order_sync("embeddings"), router


def query_embedder(cfg) -> Embedder:
    """The first model of the Embeddings task that answers now."""
    ordered, _ = _usable(cfg)
    last: Exception | None = None
    for ref in ordered.refs:
        try:
            return Embedder(cfg, ref)
        except SemanticError as e:
            last = e
    raise SemanticError(ordered.sentence() if not ordered.refs else
                        f"The embedding model did not answer: {last}")


def build_embedder(cfg, path) -> Embedder:
    """The model that adds vectors to the index at `path`: the task's first model when
    it answers (a different model resets the index), else a later one with the same
    vectors. Raises Unreachable when none can do it now: the build waits."""
    from .db import connect
    from .embed import matches
    from .importers import Unreachable
    ordered, _ = _usable(cfg)
    if not ordered.chain:
        raise Unreachable("No embedding model is set. Choose one in Settings, Models.")
    try:
        conn = connect(path, readonly=True)
        try:
            meta = passages.vec_meta(conn) if passages.has_index(conn) else None
        finally:
            conn.close()
    except sqlite3.Error:
        meta = None
    stored = (meta or {}).get("probes")
    first = ordered.chain[0]
    for ref in ordered.refs:
        try:
            e = Embedder(cfg, ref)
        except SemanticError:
            continue
        if ref == first or matches(stored, e.fp):
            return e
    raise Unreachable(ordered.sentence() if ordered.skipped else
                      "The embedding model did not answer.")


def _chats(kb: ChatKB) -> dict[int, dict[str, Any]]:
    return {c["id"]: {"id": c["id"], "name": c["name"], "kind": c["kind"], "parent": c["parent"]}
            for c in kb.list_chats(trashed=None, limit=1_000_000)["chats"]}


# ---- build ---------------------------------------------------------------------------------

def build(cfg, kb: ChatKB, ctx: ImportContext) -> dict[str, Any]:
    """Cut what changed into passages, then embed every passage without a vector.
    Ends Unreachable (a normal end: the next run continues) when no embedding model
    can do it now."""
    s = _shape(cfg)
    t0 = time.monotonic()
    cut = passages.build(kb.file.path, platform=kb.platform, chats=_chats(kb),
                         sp=SessionParams.from_settings(s),
                         pp=passages.PassageParams.from_settings(s),
                         check=ctx.check, progress=ctx.progress)
    t1 = time.monotonic()
    embedded = embed_passages(cfg, kb.file.path, ctx,
                              retries=int(s.get("build_retries", 9)), kb_id=kb.id)
    out = {**cut, "embedded": embedded, "cut_s": round(t1 - t0, 1),
           "embed_s": round(time.monotonic() - t1, 1)}
    log.info("semantic index built", extra={"event": "kb.semantic.built", "kb": kb.id, **out})
    return out


def embed_passages(cfg, path, ctx: ImportContext, *, retries: int = 9,
                   kb_id: str = "") -> int:
    """Vectors for every passage of the index at `path` without one (chat archives
    and Documents)."""
    e = build_embedder(cfg, path)
    bulk = {**e.s, "retries": retries}
    log.info("semantic build model", extra={"event": "kb.semantic.endpoint", "kb": kb_id,
                                            "model": e.ref})
    return passages.embed(path, s=bulk, fp=e.fp, label=e.ref, pool=Pool(bulk, _embed),
                          check=ctx.check, progress=ctx.progress)


def due(conn: sqlite3.Connection) -> bool:
    """Does the scheduled refresh have work here: an index that was built once and has
    new or changed messages, vectors still missing (a rebuild for another model, a
    build that stopped), or passages written the old way (RENDER_VER)?"""
    st = passages.status(conn)
    if not st["passages"]:
        return False
    return not st["ready"] or st["embedded"] < st["passages"] or \
        bool(passages.pending_chats(conn)) or passages.stale_render(conn)


# ---- queries ---------------------------------------------------------------------------------

def query_vector(cfg, q: str, timeout: float | None = None) -> Vec:
    """Embed a query with the instruction prefix ([embeddings] query_prefix), cut and
    quantised like the index, carrying the model's fingerprint (an index made by
    another model is skipped). `timeout`: the most seconds the server may take."""
    e = query_embedder(cfg)
    over: dict[str, Any] = {"retries": 1, **({"timeout": timeout} if timeout else {})}
    v = e.embed([e.s["query_prefix"] + q], **over)[0]
    dims = min(e.s["dims"], len(v))
    return quantize(v, dims - dims % 8, e.fp)


def _ready(conn: sqlite3.Connection) -> bool:
    return kv(conn, "passages_ready") == "1" and passages.has_index(conn)


def _hydrate(kb: ChatKB, scored: list[tuple[int, float]]) -> list[dict[str, Any]]:
    if not scored:
        return []
    rows = kb.file.read_sync(lambda c: passages.load(c, [pid for pid, _ in scored]))
    chats = kb.file.read_sync(lambda c: chat_cards(c, {r["chat_id"] for r in rows.values()}))
    out = []
    for rank, (pid, score) in enumerate(scored):
        r = rows.get(pid)
        if r is None:
            continue
        first = r["messages"][0] if r["messages"] else None
        out.append({**r, "kb": kb.id, "ref": f"{kb.id}:{first}" if first else None,
                    "refs": [f"{kb.id}:{m}" for m in r["messages"]],
                    "chat": chats.get(r["chat_id"]), "ts": r["start_ts"],
                    "score": score, "rank": rank + 1})
    return out


# ---- plain passage search (the search baseline) ----------------------------------------

def search_sync(cfg, kbs, q: str, *, kb_ids=None, chat: str | None = None,
                limit: int = 20) -> dict[str, Any]:
    """Passage hits, best first, shaped like instant search hits: kb, passage, session,
    chat_id, chat, start_ts, end_ts, ts, header, text, messages (ids, time order),
    refs, ref (first message), people (person ids, -1 = me), score (cosine), rank
    (within its kb), snippet. Knowledge bases whose passages are not ready contribute
    nothing."""
    from . import _split
    t0 = time.perf_counter()
    qv = query_vector(cfg, q)
    chat_ref = _split(chat)
    targets = [k for k in kbs.pick(kb_ids) if isinstance(k, ChatKB)]
    if chat_ref:
        targets = [k for k in targets if k.id == chat_ref[0]]
    hits: list[dict[str, Any]] = []
    for kb in targets:
        chat_ids = [chat_ref[1]] if chat_ref else None
        scored = kb.file.read_sync(lambda c: passages.dense(c, qv, limit, chat_ids=chat_ids)
                                   if _ready(c) else [])
        for h in _hydrate(kb, scored):
            h["snippet"] = h["text"][:400]
            hits.append(h)
    hits.sort(key=lambda h: -h["score"])
    return {"hits": hits[:limit], "took_ms": round((time.perf_counter() - t0) * 1000, 1)}


# ---- the scheduled refresh ---------------------------------------------------------------

async def run_scheduler(cfg, kbs) -> None:
    """New messages reach semantic topic search on their own: every
    `smart_search_refresh_minutes` (Settings, General; 0 = off), and at once after a settings change,
    each chat knowledge base with work to do (`due`: new or changed messages, or
    vectors still missing after a model change) gets a build, and so does one whose
    last build waited for an embedding model (the wait may be over: a model was set).
    A quiet task: its job keeps one state (done, waiting for the embedding model,
    failed). Any other wake of the router (a model failing, a provider's state) is not
    a look: a build that finds no embedding model fails it, and would wake this loop to
    build again without pause, keeping the archive's writer busy."""
    from ..backends.router import app_router
    looked: tuple[Any, float] | None = None   # the settings and the time of the last look
    while True:
        settings = getattr(cfg, "settings", None)
        every = float(general_mod.value(settings.general if settings else {},
                                        "smart_search_refresh_minutes"))
        if every > 0 and (looked is None or looked[0] is not settings
                          or time.monotonic() - looked[1] >= every * 60):
            looked = (settings, time.monotonic())
            for kb in kbs.chats():
                if kbs.tasks.running(kb.id, "semantic"):
                    continue
                job = kbs.tasks.job(kb.id, "semantic")
                try:
                    if (job is not None and job["state"] == "unreachable") or \
                            await kb.file.read(due):
                        kbs.tasks.start(kb.id, "semantic", "Semantic topic search index",
                                        lambda ctx, kb=kb: build(cfg, kb, ctx), quiet=True)
                except Exception:
                    log.exception("scheduled semantic check failed")
        # a settings change (a new embedding model) is looked at right away
        await app_router(cfg).wait_change(max(60.0, every * 60) if every > 0 else 3600.0)
