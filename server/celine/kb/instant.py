"""The instant search index: a normalised, time-ordered shadow of `messages`.

Tables in kb/instant.sql. Triggers on `messages` put changed ids into `search_dirty`;
`drain()` re-indexes them in short transactions. A new or changed index version
(`kv.search_index`) rebuilds from scratch in batches (`backfill()`), resumable, in the
background for a big knowledge base; until it is done, search runs plain FTS over
`messages_fts` (ChatKB.search_unindexed). No model, no network.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import media_text
from .db import kv, set_kv
from . import normalize as N
from .query import Query, Term
from .spell import VocabCache

log = logging.getLogger("celine.kb.instant")

SCHEMA = (Path(__file__).parent / "instant.sql").read_text()
VERSION = "2"                 # bump when normalize.py or filter tokens change
BATCH = 2000
INLINE_BUILD = 20_000         # below this many messages the first build runs inline
INLINE_DRAIN = 3_000          # a search drains at most this many dirty rows itself

_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")


def sk_of(ts: int, mid: int) -> int:
    return int(ts) * 1024 + (int(mid) & 1023)


def sk_floor(ts: int) -> int:
    """The smallest sk a message at `ts` can have."""
    return int(ts) * 1024


def _ym(ts: int) -> int:
    d = _dt.datetime.fromtimestamp(ts / 1000)
    return d.year * 100 + d.month


def filter_tokens(r: sqlite3.Row) -> str:
    f = ["all", f"c{r['chat_id']}"]
    if r["from_me"]:
        f.append("me")
    elif r["sender_id"] is not None:
        f.append(f"p{r['sender_id']}")
    f.append(f"k{r['kind']}")
    media = media_text.parse(r["media"])
    if media:
        t = str(media.get("type") or "file")
        f.append(f"m{t}")
        if t == "document":
            f.append("mfile")
        f.append("mmedia")
    if r["text"] and _URL.search(r["text"]):
        f.append("link")
    if r["reply_key"]:
        f.append("reply")
    if r["edited"]:
        f.append("edit")
    if r["deleted_at"] is not None:
        f.append("del")
    return " ".join(f)


def body_of(r: sqlite3.Row, described: str | None = None) -> str:
    """`described`: what an image caption says (kb/media_text.py), when there is one."""
    media = media_text.parse(r["media"]) or {}
    more = [media.get("name") or ""]
    cap = media.get("caption")
    if cap and cap != r["text"]:
        more.append(cap)
    if described:
        more.append(described)
    return N.index_text(r["text"] or "", more)


def is_ready(conn: sqlite3.Connection) -> bool:
    return kv(conn, "search_index") == VERSION


def _index_ids(conn: sqlite3.Connection, ids: list[int]) -> int:
    """(Re)index these message ids: drop what the index has for them, add what the
    messages table has now. Idempotent."""
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    for (sk,) in conn.execute(f"SELECT sk FROM search_map WHERE id IN ({marks})", ids).fetchall():
        conn.execute("DELETE FROM search_fts WHERE rowid = ?", (sk,))
        conn.execute("DELETE FROM search_map WHERE sk = ?", (sk,))
    rows = conn.execute(
        f"SELECT id, chat_id, sender_id, from_me, ts, text, kind, media, reply_key, edited, "
        f"deleted_at FROM messages WHERE id IN ({marks})", ids).fetchall()
    described = media_text.for_rows(conn, rows)          # image captions (kb/media.py)
    for r in rows:
        sk = sk_of(r["ts"], r["id"])
        person = -1 if r["from_me"] else r["sender_id"]
        while True:
            try:
                conn.execute("INSERT INTO search_map (sk, id, chat_id, person, ym) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (sk, r["id"], r["chat_id"], person, _ym(r["ts"])))
                break
            except sqlite3.IntegrityError:
                sk += 1
        conn.execute("INSERT INTO search_fts (rowid, body, f) VALUES (?, ?, ?)",
                     (sk, body_of(r, described.get(r["id"])), filter_tokens(r)))
    return len(rows)


def drain(conn: sqlite3.Connection, limit: int | None = None) -> int:
    """Re-index dirty rows, BATCH per transaction. Returns how many were done."""
    done = 0
    while limit is None or done < limit:
        n = BATCH if limit is None else min(BATCH, limit - done)
        conn.execute("BEGIN IMMEDIATE")
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM search_dirty ORDER BY id LIMIT ?", (n,))]
            if not ids:
                conn.commit()
                break
            _index_ids(conn, ids)
            conn.execute(f"DELETE FROM search_dirty WHERE id IN ({','.join('?' * len(ids))})",
                         ids)
            _bump(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        done += len(ids)
    return done


def forget(conn: sqlite3.Connection, ids: list[int]) -> None:
    """Take deleted messages out of the index now (a deleted chat), inside the
    caller's transaction."""
    for i in range(0, len(ids), BATCH):
        part = ids[i:i + BATCH]
        _index_ids(conn, part)
        conn.execute(f"DELETE FROM search_dirty WHERE id IN ({','.join('?' * len(part))})",
                     part)
    if ids:
        _bump(conn)


def _bump(conn) -> None:
    g = int(kv(conn, "search_gen") or 0) + 1
    set_kv(conn, "search_gen", str(g))


def generation(conn: sqlite3.Connection) -> int:
    return int(kv(conn, "search_gen") or 0)


def backfill(conn: sqlite3.Connection, *, pause: float = 0.0) -> bool:
    """Build the index from scratch, resumable: `kv.search_backfill` holds the last
    message id done. Each batch is its own short transaction; `pause` seconds between
    batches leave the write lock to importers. Returns True when complete."""
    if is_ready(conn):
        return True
    state = kv(conn, "search_backfill")
    if state is None or not state.startswith(VERSION + ":"):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM search_fts")
        conn.execute("DELETE FROM search_map")
        set_kv(conn, "search_backfill", f"{VERSION}:0")
        conn.commit()
        state = f"{VERSION}:0"
    last = int(state.split(":", 1)[1])
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM messages WHERE id > ? ORDER BY id LIMIT ?", (last, BATCH))]
            if not ids:
                set_kv(conn, "search_index", VERSION)
                _bump(conn)
                conn.commit()
                return True
            _index_ids(conn, ids)
            last = ids[-1]
            set_kv(conn, "search_backfill", f"{VERSION}:{last}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if pause:
            time.sleep(pause)


_INDEXERS: dict[tuple[str, int], "Indexer"] = {}
_INDEXERS_LOCK = threading.Lock()


def indexer_for(path: Path, connect) -> "Indexer":
    """One Indexer per database file (a config reload builds new ChatKBs; they share
    it, so two builds never run side by side)."""
    import os
    try:
        ino = os.stat(path).st_ino
    except FileNotFoundError:
        ino = -1
    key = (str(Path(path).resolve()), ino)
    with _INDEXERS_LOCK:
        ix = _INDEXERS.get(key)
        if ix is None:
            ix = _INDEXERS[key] = Indexer(Path(path), connect)
            ix.open()
        return ix


class Indexer:
    """Per knowledge base: runs the first build and big drains in a background
    thread, one at a time."""

    def __init__(self, path: Path, connect):
        self.path = path
        self.connect = connect
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.ready = False
        self.vocab = VocabCache()

    def open(self) -> None:
        conn = self.connect(self.path)
        conn.isolation_level = None
        try:
            conn.executescript(SCHEMA)
            self.ready = is_ready(conn)
            if self.ready:
                return
            n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            if n <= INLINE_BUILD:
                self.ready = backfill(conn)
                drain(conn)
                return
        finally:
            conn.close()
        self.kick()

    def kick(self) -> None:
        """Start the background worker unless it is running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name=f"instant-{self.path.stem}",
                                            daemon=True)
            self._thread.start()

    def _run(self) -> None:
        t0 = time.perf_counter()
        try:
            conn = self.connect(self.path)
            conn.isolation_level = None
            try:
                if not is_ready(conn):
                    log.info("instant index build started",
                             extra={"event": "kb.instant.build", "kb": self.path.stem})
                    backfill(conn, pause=0.02)
                    log.info("instant index built",
                             extra={"event": "kb.instant.built", "kb": self.path.stem,
                                    "seconds": round(time.perf_counter() - t0, 1)})
                self.ready = True
                while drain(conn, BATCH * 5):
                    time.sleep(0.02)
                # warm the typo vocabulary so the first zero-hit search is quick
                self.vocab.get(conn, generation(conn))
            finally:
                conn.close()
        except Exception as e:          # never take the server down; search falls back
            log.warning("instant index failed",
                        extra={"event": "kb.instant.failed", "kb": self.path.stem,
                               "error": str(e)})

    def catch_up(self) -> None:  # noqa: D401
        """Called before a search: drain a few dirty rows inline, so what an import
        just wrote is findable; a big backlog goes to the background worker."""
        if not self.ready:
            return
        conn = self.connect(self.path)
        conn.isolation_level = None
        # never make a keystroke wait for an import's write lock: when it is taken, the
        # background worker indexes the rows as soon as the lock is free
        conn.execute("PRAGMA busy_timeout = 0")
        try:
            n = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM search_dirty LIMIT ?)",
                             (INLINE_DRAIN + 1,)).fetchone()[0]
            if n == 0:
                return
            if n > INLINE_DRAIN:
                self.kick()
                return
            drain(conn)
        except sqlite3.OperationalError as e:
            # busy: an import holds the lock; search what is indexed
            if "readonly" in str(e) or "read-only" in str(e):
                return                  # a read-only open: search what is indexed
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            self.kick()
        finally:
            conn.close()


# ---- search ------------------------------------------------------------------------------

HAS_TOKENS = {"voice": ["mvoice"], "audio": ["maudio"], "photo": ["mphoto"],
              "video": ["mvideo"], "link": ["link"], "file": ["mfile"],
              "media": ["mmedia"], "sticker": ["msticker"], "gif": ["mgif"],
              "location": ["mlocation"], "contact": ["mcontact"], "reply": ["reply"]}


@dataclass
class Filters:
    """Per knowledge base, already resolved to ids."""
    chats: list[int] | None = None       # OR; None = any
    people: list[int] | None = None      # OR; None = any
    ts_from: int | None = None
    ts_to: int | None = None
    trashed: list[int] = field(default_factory=list)    # left out, even when named


@dataclass
class Plan:
    match: str
    lo: int | None
    hi: int | None
    exact: set[str]
    prefixes: set[str]


def _q(tok: str) -> str:
    return '"' + tok.replace('"', '') + '"'


def _term_expr(t: Term) -> str:
    s = _q(" ".join(t.tokens))
    return s + "*" if t.prefix and not t.phrase else s


def plan(query: Query, flt: Filters,
         expansions: dict[int, list[Term]] | None = None) -> Plan | None:
    """The FTS5 expression and rowid bounds for one knowledge base. None when the
    filters rule this knowledge base out."""
    if flt.chats is not None and not flt.chats:
        return None
    if flt.people is not None and not flt.people and not query.is_me:
        return None
    pos: list[str] = []
    neg: list[str] = []
    exact: set[str] = set()
    prefixes: set[str] = set()
    for i, c in enumerate(query.clauses):
        terms = list(c.terms) + list((expansions or {}).get(i, []))
        expr = " OR ".join(_term_expr(t) for t in terms)
        if c.negate:
            neg.append(f"body : ({expr})")
            continue
        pos.append(f"body : ({expr})")
        for t in terms:
            if t.prefix and not t.phrase:
                prefixes.update(t.tokens[-1:])
                exact.update(t.tokens[:-1])
            else:
                exact.update(t.tokens)
    fil: list[str] = []
    if flt.chats:
        fil.append("f : (" + " OR ".join(f"c{int(x)}" for x in flt.chats) + ")")
    who = []
    if flt.people:
        who += [f"p{int(x)}" for x in flt.people]
    if query.is_me:
        who.append("me")
    if who:
        fil.append("f : (" + " OR ".join(who) + ")")
    for h in query.has:
        fil.append("f : (" + " OR ".join(HAS_TOKENS.get(h, ["m" + h])) + ")")
    if query.is_deleted:
        fil.append("f : del")
    if query.is_edited:
        fil.append("f : edit")
    if flt.trashed:
        neg.append("f : (" + " OR ".join(f"c{int(x)}" for x in flt.trashed) + ")")
    parts = pos + fil
    if not parts:
        parts = ["f : all"]
    match = " AND ".join(f"({p})" for p in parts)
    if neg:
        match = f"({match}) NOT ({' OR '.join(neg)})"
    lo = sk_floor(flt.ts_from) if flt.ts_from is not None else None
    hi = sk_floor(flt.ts_to) if flt.ts_to is not None else None
    return Plan(match, lo, hi, exact, prefixes)


def _bounds(p: Plan, cursor: int | None, sort: str, inclusive: bool) -> tuple[str, list]:
    where, args = [], []
    if p.lo is not None:
        where.append("rowid >= ?"); args.append(p.lo)
    if p.hi is not None:
        where.append("rowid < ?"); args.append(p.hi)
    if cursor is not None:
        if sort == "oldest":
            where.append("rowid >= ?" if inclusive else "rowid > ?")
        else:
            where.append("rowid <= ?" if inclusive else "rowid < ?")
        args.append(cursor)
    return "".join(f" AND {w}" for w in where), args


def page(conn: sqlite3.Connection, p: Plan, *, sort: str, limit: int,
         cursor: int | None = None, inclusive: bool = False) -> list[tuple[int, float]]:
    """[(sk, score)] of one page. recent/oldest walk the index in time order and stop
    after `limit`; best/relevance rank by bm25 over the body."""
    extra, args = _bounds(p, cursor, sort, inclusive)
    if sort in ("recent", "oldest"):
        order = "DESC" if sort == "recent" else "ASC"
        rows = conn.execute(
            f"SELECT rowid FROM search_fts WHERE search_fts MATCH ?{extra} "
            f"ORDER BY rowid {order} LIMIT ?", [p.match, *args, limit]).fetchall()
        return [(r[0], 0.0) for r in rows]
    rows = conn.execute(
        f"SELECT rowid, bm25(search_fts, 1.0, 0.0) AS b FROM search_fts "
        f"WHERE search_fts MATCH ?{extra} ORDER BY b LIMIT ?",
        [p.match, *args, limit]).fetchall()
    return [(r[0], r[1]) for r in rows]


def best(conn: sqlite3.Connection, p: Plan, *, limit: int, pool: int = 200,
         now_ms: int | None = None, half_life_days: float = 365.0) -> list[tuple[int, float]]:
    """Top hits: bm25 over the best `pool` candidates, times a recency factor that
    halves the weight of a hit per `half_life_days` of age, floored at a third (an
    old perfect match still beats a new weak one). Over BEST_SCAN matches (a one- or
    two-letter prefix) only the newest BEST_SCAN are ranked: bm25 over 90k matches
    costs 80 ms and tells nothing."""
    now_ms = now_ms or int(time.time() * 1000)
    n, capped = count(conn, p, BEST_SCAN)
    if capped:
        extra, args = _bounds(p, None, "recent", False)
        rows = conn.execute(
            f"SELECT rowid, bm25(search_fts, 1.0, 0.0) FROM search_fts "
            f"WHERE search_fts MATCH ?{extra} ORDER BY rowid DESC LIMIT ?",
            [p.match, *args, BEST_SCAN]).fetchall()
        cands = sorted(((r[0], r[1]) for r in rows), key=lambda x: x[1])[:pool]
    else:
        cands = page(conn, p, sort="relevance", limit=pool)
    out = []
    for sk, b in cands:
        age = max(0.0, (now_ms - (sk >> 10)) / 86_400_000)
        decay = 1 / 3 + 2 / 3 * math.pow(0.5, age / half_life_days)
        out.append((sk, -b * decay))
    out.sort(key=lambda x: -x[1])
    return out[:limit]


BEST_SCAN = 5000


def count(conn: sqlite3.Connection, p: Plan, cap: int | None) -> tuple[int, bool]:
    extra, args = _bounds(p, None, "recent", False)
    if cap is None:
        n = conn.execute(f"SELECT COUNT(*) FROM search_fts WHERE search_fts MATCH ?{extra}",
                         [p.match, *args]).fetchone()[0]
        return n, False
    n = conn.execute(f"SELECT COUNT(*) FROM (SELECT rowid FROM search_fts WHERE "
                     f"search_fts MATCH ?{extra} LIMIT ?)", [p.match, *args, cap + 1]).fetchone()[0]
    return (cap, True) if n > cap else (n, False)


FACET_CAP = 20_000


def facets(conn: sqlite3.Connection, p: Plan, *, top: int = 10) -> dict[str, Any]:
    """Counts per chat (the `top` most), per person (all of them, so the accounts of a
    same person can be summed across knowledge bases) and per month over the match set.
    Over FACET_CAP matches only the newest FACET_CAP are counted (`approximate`)."""
    total, approximate = count(conn, p, FACET_CAP)
    extra, args = _bounds(p, None, "recent", False)
    sub = (f"SELECT rowid FROM search_fts WHERE search_fts MATCH ?{extra} "
           f"ORDER BY rowid DESC LIMIT {FACET_CAP}")
    rows = conn.execute(f"SELECT s.chat_id, s.person, s.ym, COUNT(*) FROM ({sub}) t "
                        f"JOIN search_map s ON s.sk = t.rowid GROUP BY 1, 2, 3",
                        [p.match, *args]).fetchall()
    chats: dict[int, int] = {}
    people: dict[int, int] = {}
    months: dict[int, int] = {}
    for c, pe, ym, n in rows:
        chats[c] = chats.get(c, 0) + n
        if pe is not None:
            people[pe] = people.get(pe, 0) + n
        months[ym] = months.get(ym, 0) + n
    return {"total": total, "approximate": approximate,
            "chats": sorted(chats.items(), key=lambda x: -x[1])[:top],
            "people": sorted(people.items(), key=lambda x: -x[1]),
            "months": sorted(months.items())}


# ---- one search over several knowledge bases -----------------------------------------------

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="instant")
FEW = 50                # fewer hits than this: look for a "did you mean"
SORTS = ("recent", "oldest", "best", "relevance")


def _fan(fn, items):
    items = list(items)
    if len(items) <= 1:
        return [fn(x) for x in items]
    return list(_POOL.map(fn, items))


def encode_cursor(sort: str, hit: dict[str, Any] | None, offset: int) -> str | None:
    if sort in ("recent", "oldest"):
        return f"{hit['sk']}.{hit['kb']}" if hit else None
    return f"o{offset}"


def decode_cursor(sort: str, cursor: str | None) -> tuple[int | None, str | None, int]:
    """-> (sk, kb, offset)"""
    if not cursor:
        return None, None, 0
    if cursor.startswith("o") and cursor[1:].isdigit():
        return None, None, int(cursor[1:])
    sk, _, kb = cursor.partition(".")
    if sk.lstrip("-").isdigit() and sort in ("recent", "oldest"):
        return int(sk), kb or "", 0
    return None, None, 0


def _replace_words(raw: str, repl: dict[str, str]) -> str:
    out = raw
    for old, new in repl.items():
        out = re.sub(r"(?<![^\s(])" + re.escape(old) + r"(?![^\s)*])", new, out, count=1)
    return out


def run(targets: list[tuple[Any, Filters]], query: Query, *, sort: str = "recent",
        limit: int = 50, cursor: str | None = None, count_cap: int | None = 1000,
        top: int = 0, correct: bool = True) -> dict[str, Any]:
    """Instant search over `targets` [(ChatKB, Filters)]. The tiers:
    1. the query as written (exact words, the last one a prefix);
    2. no hits at all: unknown words widened to their typo neighbours and the
       compounds that contain them -> `corrected` (the UI offers the original);
    3. a few hits: `did_you_mean` when a word is rare and a close one is common.
    A query with hits is never rewritten."""
    sort = sort if sort in SORTS else "recent"
    c_sk, c_kb, offset = decode_cursor(sort, cursor)
    plans = []
    for kb, flt in targets:
        p = plan(query, flt)
        if p is not None:
            plans.append((kb, flt, p))

    def first(item):
        kb, flt, p = item
        return kb.ix_run(p, sort=sort, limit=offset + limit + 1, cursor=c_sk,
                         inclusive=_inclusive(sort, kb.id, c_kb), count_cap=count_cap)
    results = _fan(first, plans)
    total = sum(r["total"] for r in results)
    out: dict[str, Any] = {}
    if total == 0 and correct and not query.literal and query.has_text and plans:
        widened = widen_plans(plans, query)
        if widened is not None:
            w_results = _fan(first, widened[0])        # same page and cursor as tier 1
            if sum(r["total"] for r in w_results):
                results, (plans, out["corrected"]) = w_results, widened
                total = sum(r["total"] for r in results)
    elif 0 < total < FEW and correct and not query.literal and cursor is None:
        dym = _did_you_mean(plans, query)
        if dym:
            out["did_you_mean"] = dym
    rows = []
    for (kb, flt, p), r in zip(plans, results):
        rows += [(sk, score, kb, p) for sk, score in r["rows"]]
    if sort == "recent":
        rows.sort(key=lambda x: (x[0], x[2].id), reverse=True)
    elif sort == "oldest":
        rows.sort(key=lambda x: (x[0], x[2].id))
    elif sort == "best":
        rows.sort(key=lambda x: -x[1])
    else:
        rows.sort(key=lambda x: x[1])              # bm25: lower is better
    more = len(rows) > offset + limit
    rows = rows[offset:offset + limit]
    hits = _hydrate(rows)
    out.update({
        "hits": hits, "total": total,
        "total_capped": any(r["capped"] for r in results),
        "next_cursor": (encode_cursor(sort, hits[-1] if hits else None, offset + limit)
                        if more else None),
    })
    if top and cursor is None and total and sort in ("recent", "oldest"):
        out["top_hits"] = top_hits(plans, top)
    return out


def _inclusive(sort: str, kid: str, c_kb: str | None) -> bool:
    if c_kb is None:
        return False
    return kid < c_kb if sort == "recent" else kid > c_kb


def _hydrate(rows: list[tuple[int, float, Any, Plan]]) -> list[dict[str, Any]]:
    by_kb: dict[str, list] = {}
    for sk, score, kb, p in rows:
        by_kb.setdefault(kb.id, []).append((sk, score, kb, p))
    got: dict[tuple[str, int], dict[str, Any]] = {}

    def one(items):
        kb, p = items[0][2], items[0][3]
        return kb.id, kb.ix_hydrate([(sk, sc) for sk, sc, _, _ in items], p)
    for kid, hits in _fan(one, by_kb.values()):
        for h in hits:
            got[(kid, h["sk"])] = h
    return [got[(kb.id, sk)] for sk, _, kb, _ in rows if (kb.id, sk) in got]


def top_hits(plans, n: int) -> list[dict[str, Any]]:
    """The `best` strip over the timeline. A huge match set (a one- or two-letter
    prefix) ranks only its newest BEST_SCAN matches (best())."""
    def one(item):
        kb, flt, p = item
        return [(sk, sc, kb, p) for sk, sc in kb.ix_best(p, n)]
    rows = [r for part in _fan(one, plans) for r in part]
    rows.sort(key=lambda x: -x[1])
    return _hydrate(rows[:n])


def widen_plans(plans, query: Query):
    """The plans with unknown words widened to their typo neighbours and the compounds
    that contain them, and the `corrected` answer. None when no word could be widened.
    Facets use it too, so their counts follow the corrected search."""
    new_plans, choices = [], {}
    any_exp = False
    for kb, flt, p in plans:
        vocab = kb.ix_vocab()
        exps: dict[int, list[Term]] = {}
        for i, c in enumerate(query.clauses):
            if c.negate:
                continue
            for t in c.terms:
                if t.phrase or len(t.tokens) != 1:
                    continue
                tok = t.tokens[0]
                known = vocab.has_prefix(tok) if t.prefix else vocab.freq(tok) > 0
                if known:
                    continue
                alts: list[Term] = []
                for cand, dist, df in vocab.typos(tok, prefix=t.prefix, limit=6):
                    alts.append(Term(raw=cand, tokens=[cand], prefix=t.prefix))
                    choices.setdefault(t.raw, []).append((dist, -df, cand))
                for cand, df in vocab.substrings(tok, limit=12):
                    alts.append(Term(raw=cand, tokens=[cand]))
                    choices.setdefault(t.raw, []).append((1.5, -df, cand))
                if alts:
                    exps.setdefault(i, []).extend(alts)
        if exps:
            any_exp = True
        new_plans.append((kb, flt, plan(query, flt, exps) if exps else p))
    if not any_exp:
        return None
    repl = {raw: min(c)[2] for raw, c in choices.items()}
    variants = {raw: sorted({x[2] for x in c}) for raw, c in choices.items()}
    corrected = {"query": _replace_words(query.raw, repl), "original": query.raw,
                 "words": repl, "variants": variants}
    return new_plans, corrected


def _did_you_mean(plans, query: Query) -> str | None:
    """A quiet suggestion: a word whose close neighbour is in at least 20 times as many
    messages (and 5 or more). Document frequencies are summed over the kbs."""
    vocabs = [kb.ix_vocab() for kb, _, _ in plans]
    repl: dict[str, str] = {}
    for c in query.clauses:
        if c.negate:
            continue
        for t in c.terms:
            if t.phrase or len(t.tokens) != 1:
                continue
            tok = t.tokens[0]
            df = sum((v.prefix_freq(tok) if t.prefix else v.freq(tok)) for v in vocabs)
            cands: dict[str, list[int]] = {}
            for v in vocabs:
                for cand, dist, cdf in v.typos(tok, prefix=t.prefix, limit=5):
                    e = cands.setdefault(cand, [dist, 0])
                    e[1] += cdf
            ok = [(d, -n, cand) for cand, (d, n) in cands.items() if n >= max(5, 20 * df)]
            if ok:
                repl[t.raw] = min(ok)[2]
    return _replace_words(query.raw, repl) if repl else None
