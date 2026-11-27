"""The one retrieval pipeline Celine uses.

    words (literal words and exact phrases) and about (sentences, by meaning)
      → per knowledge base: lexical (bm25 over the instant index, or messages_fts while
        it is built, lifted to units, and over passages_fts) ‖ dense (query
        embeddings → unit KNN, skipped with a note for an index of another model)
      → RRF (k = 60) over every list
      → cross-encoder rerank of the top ~50, each with its neighbours
      → episodes: contiguous windows per chat, `[kb:id]` on every line

Sync; the caller runs it in a worker thread. The embedder and the reranker are
passed in, so tests use fakes and a missing model degrades instead of failing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .. import passages
from ..chat import chat_cards
from . import episodes as E
from .count import count_kb, instant_match, match_expr
from .fusion import rrf
from .rerank import Reranker
from .scope import Scope
from .units import Units, detect

log = logging.getLogger("celine.kb.retrieve")

class Embed(Protocol):
    """Query vectors, and the fingerprint of the model that makes them (None: any
    index is taken as this model's)."""
    fp: str | None

    def __call__(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class Settings:
    rrf_k: int = 60
    lexical_n: int = 200          # per list
    dense_n: int = 100            # per list
    candidates: int = 50          # reranked
    rerank_queries: int = 3       # the first N `about` sentences score each candidate; max wins
    rerank_pad: int = 2           # neighbours on each side in the reranked text
    rerank_floor: float = -4.0    # below this a candidate does not fit and is left out
    episode_gap_minutes: int = 30
    episode_pad: int = 2          # neighbours shown around an episode
    best_episodes: int = 8
    all_episodes: int = 25
    query_prefix: str = ""


@dataclass
class Result:
    episodes: list[E.Episode] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)     # literal matches per variant
    years: dict[str, int] = field(default_factory=dict)      # literal matches per year
    absent: list[str] = field(default_factory=list)          # literal variants with no hit
    more: int = 0                                            # episodes found, not returned
    below: int = 0                                           # candidates under the floor
    notes: list[str] = field(default_factory=list)
    reranked: bool = False
    took_ms: float = 0.0


def _years(conn, units: Units, variants: list[str], scope: Scope) -> dict[str, int]:
    """Literal matches per year for the union of the variants (mode=all coverage)."""
    ids: set[int] = set()
    for q in variants:
        hits, _ = units.message_hits(conn, q, scope, 100_000, order="first")
        ids.update(m for m, _ in hits)
    out: dict[str, int] = {}
    if not ids:
        return out
    lst = sorted(ids)
    for i in range(0, len(lst), 900):
        part = lst[i:i + 900]
        for r in conn.execute(
                f"SELECT strftime('%Y', ts / 1000, 'unixepoch', 'localtime') AS y, COUNT(*) "
                f"FROM messages WHERE id IN ({','.join('?' * len(part))}) GROUP BY y", part):
            out[r[0]] = out.get(r[0], 0) + r[1]
    return dict(sorted(out.items()))


def phrase(word: str) -> str:
    """A literal entry as it means it: one word (a prefix), or several words as one
    exact phrase ("wish you would" is those words in that order, not any of them)."""
    w = " ".join(str(word).replace('"', " ").split())
    return f'"{w}"' if len(w.split()) > 1 else w


def exact_total(conn, word: str, scope: Scope, kb: str) -> int:
    """Messages in scope with this exact word or phrase (no fallback, no typos), on the
    index the hits come from (count_kb)."""
    match = match_expr([word])
    if not match:
        return 0
    return count_kb(conn, kb, scope, by="total", match=match, ix_match=instant_match([word]),
                    unit="messages", window_ms=0, gap_ms=0)[1]


def retrieve(kbs, words: list[str], scope: Scope, *, about: list[str] | None = None,
             order: str = "best", mode: str = "best", me: str = "me",
             embed: Embed | None = None, reranker: Reranker | None = None,
             settings: Settings | None = None, embed_why: str | None = None) -> Result:
    """`words`: literal words or phrases, searched in the word index (a phrase is exact).
    `about`: sentences describing what is wanted, searched by meaning (vectors) and used
    by the reranker. Without `about`, the words are embedded too. `embed_why`: why there
    is no `embed`, for the note that says so."""
    s = settings or Settings()
    t0 = time.perf_counter()
    res = Result()
    variants = [phrase(q) for q in words if q and str(q).strip()][:8]
    sentences = [" ".join(str(q).split()) for q in (about or []) if q and str(q).strip()][:5]
    if not variants and not sentences:
        raise ValueError("Pass `about`: 1 to 3 sentences that describe what you look for. "
                         "Add `words` when people would have typed a telling word.")
    dense_q = sentences or [v.strip('"') for v in variants]

    targets = [kbs.get(k) for k in scope.kbs if scope.kb_in_scope(k)]
    targets = [k for k in targets if k is not None]
    kinds = {k.id: k.file.read_sync(detect) for k in targets}

    # each kb's units, and its index caught up, before anything is searched
    units_of: dict[str, Units] = {}
    for kb in targets:
        fast = kb.search_ready
        if not fast and getattr(kb, "search_error", None):
            res.notes.append(f"The instant index of {kb.id} failed ({kb.search_error}); "
                             f"searched its plain word index.")
        if fast:
            try:
                kb.ix_catch_up()        # what an import just wrote is findable
            except Exception as e:
                fast = False
                res.notes.append(f"Indexing new messages of {kb.id} failed ({e}); searched "
                                 f"its plain word index.")
        units_of[kb.id] = Units(kb.id, kinds[kb.id], instant=fast)

    # exact totals for the literal words, before any query embedding: a words-only search
    # whose every literal variant is absent needs no semantic retrieval at all
    if variants:
        for kb in targets:
            def totals(conn, kb=kb):
                return {q: exact_total(conn, q, scope, kb.id) for q in variants}
            for q, n in kb.file.read_sync(totals).items():
                res.totals[q] = res.totals.get(q, 0) + n
        if targets:
            res.absent = [q for q in variants if not res.totals.get(q)]
            if not sentences and len(res.absent) == len(variants):
                res.notes.append("No indexed message in scope has these exact "
                                 "words/phrases: " + ", ".join(res.absent) + ".")
                res.took_ms = round((time.perf_counter() - t0) * 1000, 1)
                return res

    qvecs: list[list[float]] = []
    if embed is not None and any(v != "message" for v in kinds.values()):
        try:
            qvecs = embed([s.query_prefix + q for q in dense_q])
        except Exception as e:                       # semantic topic search is a bonus
            res.notes.append(f"Semantic topic search unavailable ({e}). Word search only.")
    elif targets and embed is None and embed_why:
        res.notes.append(f"Semantic topic search unavailable ({embed_why}). Word search only.")
    elif targets and all(v == "message" for v in kinds.values()):
        res.notes.append("No semantic topic search index yet. Word search only.")
    elif targets and embed is None:
        res.notes.append("Semantic topic search unavailable (no embedding model answers "
                         "now). Word search only.")

    fp = embed.fp if embed is not None else None
    lists: list[list[str]] = []
    weights: list[float] = []
    chrono: list[list[str]] = []
    chrono_words: list[list[str]] = []
    hits_of: dict[str, set[int]] = {}

    for kb in targets:
        units = units_of[kb.id]

        def run(conn, units=units):
            out_lists, out_chrono, out_chrono_words = [], [], []
            for q in variants:
                hits, _ = units.message_hits(conn, q, scope, s.lexical_n, order="best")
                lifted = units.lift(conn, [m for m, _ in hits])
                keys = []
                for m, _ in hits:
                    k = units.key(lifted[m])
                    hits_of.setdefault(k, set()).add(m)
                    keys.append(k)
                out_lists.append((keys, 1.0))
                if order in ("first", "last"):
                    # a literal word reserves the chronological slots as well as `about`
                    ch, _ = units.message_hits(conn, q, scope, 30, order=order)
                    lifted_c = units.lift(conn, [m for m, _ in ch])
                    ck = []
                    for m, _ in ch:
                        k = units.key(lifted_c[m])
                        hits_of.setdefault(k, set()).add(m)
                        ck.append(k)
                    if ck:
                        out_chrono.append(ck)
                        out_chrono_words.append(ck)
                if units.kind == "passage":
                    out_lists.append(([units.key(p) for p in
                                       units.passage_hits(conn, q, scope, s.lexical_n)], 1.0))
            for q in sentences:
                if units.kind == "passage":
                    out_lists.append(([units.key(p) for p in
                                       units.passage_hits(conn, q, scope, s.lexical_n)], 1.0))
                if order in ("first", "last"):
                    ch, _ = units.message_hits(conn, q, scope, 30, order=order)
                    lifted_c = units.lift(conn, [m for m, _ in ch])
                    ck = []
                    for m, _ in ch:
                        k = units.key(lifted_c[m])
                        hits_of.setdefault(k, set()).add(m)
                        ck.append(k)
                    out_chrono.append(ck)
            if qvecs and units.kind == "passage" and fp is not None \
                    and not passages.matches(conn, fp):
                res.notes.append(f"Semantic topic search skipped in {units.kb}: another "
                                 f"embedding model made its index. Word search only there.")
            else:
                for qv in qvecs:
                    out_lists.append(([units.key(u) for u in units.dense(
                        conn, qv, scope, s.dense_n, fp=fp)], 1.0))
            years = _years(conn, units, variants, scope) if mode == "all" else {}
            return out_lists, out_chrono, out_chrono_words, years

        ol, oc, ocw, years = kb.file.read_sync(run)
        for keys, w in ol:
            if keys:
                lists.append(keys)
                weights.append(w)
        chrono.extend(c for c in oc if c)
        chrono_words.extend(c for c in ocw if c)
        for y, n in years.items():
            res.years[y] = res.years.get(y, 0) + n

    if res.absent:
        res.notes.append("No indexed message in scope has these exact words/phrases: "
                         + ", ".join(res.absent) + ".")

    protected = {k for c in chrono_words for k in c}
    fused = [k for k, _ in rrf(lists, k=s.rrf_k, weights=weights)]
    cand = fused[: s.candidates]
    if chrono:
        # the earliest (latest) literal matches get their own slots: RRF over relevance
        # lists would otherwise push a first mention below the cut
        extra = [k for k, _ in rrf(chrono, k=s.rrf_k) if k not in cand]
        cand = fused[: max(s.candidates - 15, 10)]
        cand += [k for k in extra if k not in cand][:15]
        cand += [k for k in fused if k not in cand][: max(0, s.candidates - len(cand))]
    if not cand:
        res.took_ms = round((time.perf_counter() - t0) * 1000, 1)
        return res

    # spans and the text each candidate is scored on
    by_kb: dict[str, list[str]] = {}
    for k in cand:
        by_kb.setdefault(k.split(":", 1)[0], []).append(k)
    spans: dict[str, Any] = {}
    bounds: dict[str, tuple] = {}
    docs: dict[str, str] = {}
    chats: dict[tuple[str, int], dict] = {}
    for kid, keys in by_kb.items():
        kb = kbs.get(kid)
        units = units_of[kid]

        def load(conn, keys=keys, units=units, kid=kid):
            sp = units.spans(conn, [Units.uid(k) for k in keys])
            out = {}
            for uid, span in sp.items():
                first, last = E.span_bounds(conn, span)
                ctx = E.window(conn, span.chat_id, first, last, s.rerank_pad, me)[:60]
                out[span.key] = (span, first, last, ctx)
            info = chat_cards(conn, {v[0].chat_id for v in out.values()})
            return out, info
        loaded, info = kb.file.read_sync(load)
        for key, (span, first, last, ctx) in loaded.items():
            spans[key] = span
            bounds[key] = (first, last)
            c = info.get(span.chat_id, {})
            head = (f"{c.get('name', '')} ({c.get('kind', '')}), "
                    f"{datetime.fromtimestamp(span.start_ts / 1000):%Y-%m-%d}")
            docs[key] = head + "\n" + "\n".join(f"{m.sender}: {m.text}" for m in ctx)
        for cid, c in info.items():
            chats[(kid, cid)] = c
    cand = [k for k in cand if k in spans]

    scores: dict[str, float] = {}
    if reranker is not None and cand:
        try:
            # the sentences say best what it is about; without them the longest words
            if sentences:
                rq = sentences[: max(1, s.rerank_queries)]
            else:
                rq = sorted(variants, key=lambda q: -len(q.split()))[: max(1, s.rerank_queries)]
            per_q = [reranker.score(q.strip('"'), [docs[k] for k in cand]) for q in rq]
            scores = {k: max(sc[i] for sc in per_q) for i, k in enumerate(cand)}
            res.reranked = True
        except Exception as e:                       # never fail a search on it
            log.warning("rerank skipped", extra={"event": "kb.rerank_skipped",
                                                 "error": str(e)[:200]})
            res.notes.append(f"Reranker unavailable ({e}). Ranked by fusion.")
    elif reranker is None and cand:
        res.notes.append("No reranker is configured: fusion supplied the ranking; no "
                         "reranker-floor abstention signal is available.")
    if not scores:
        fused_score = dict(rrf(lists, k=s.rrf_k, weights=weights))
        scores = {k: fused_score.get(k, 0.0) for k in cand}

    ranked = sorted(cand, key=lambda k: -scores[k])
    if res.reranked and getattr(reranker, "scale", "logit") == "relevance" and \
            s.rerank_floor < 0:
        # a relevance model answers 0..1, so a below-zero floor keeps every candidate:
        # say so. Set chat_archive.rerank_floor between 0 and 1 for such a model.
        res.notes.append("This reranker scores 0 to 1, but the lowest-score setting is "
                         "below 0, so nothing is filtered. Set it between 0 and 1.")
    if res.reranked:
        # what the reranker scores under the floor does not fit the description and is
        # left out. A passage with one of the exact `words` in it is a match by that
        # word, whatever its score.
        keep = [k for k in ranked if scores[k] >= s.rerank_floor or hits_of.get(k)
                or k in protected]
        res.below = len(ranked) - len(keep)
        ranked = keep

    eps = E.merge([(spans[k], scores[k], hits_of.get(k, set()), *bounds[k]) for k in ranked],
                  gap_ms=s.episode_gap_minutes * 60_000)
    if order == "first":
        eps.sort(key=lambda e: e.first)
    elif order == "last":
        eps.sort(key=lambda e: e.last, reverse=True)
    limit = s.all_episodes if mode == "all" else s.best_episodes
    res.more = max(0, len(eps) - limit)
    eps = eps[:limit]

    pad = s.episode_pad if mode != "all" else max(1, s.episode_pad - 1)
    by_kb_eps: dict[str, list[E.Episode]] = {}
    for e in eps:
        by_kb_eps.setdefault(e.kb, []).append(e)
    for kid, lst in by_kb_eps.items():
        kb = kbs.get(kid)

        def fill(conn, lst=lst):
            for e in lst:
                e.lines = E.window(conn, e.chat_id, e.first, e.last, pad, me)
        kb.file.read_sync(fill)
        for e in lst:
            e.chat = chats.get((kid, e.chat_id))
    res.episodes = eps
    res.took_ms = round((time.perf_counter() - t0) * 1000, 1)
    return res

