"""Retrieval units of one chat knowledge base, behind one small interface.

A unit is what fusion and reranking rank: a passage (sessions -> passages,
`passages_schema.sql`) once a knowledge base's semantic index is ready, else a single
message (words only, no dense leg). Lexical message hits are lifted to the passage
that contains them, so a message hit and a dense hit on the same stretch of
conversation count as agreement in RRF.

Keys are strings, unique across knowledge bases: "whatsapp:p123", "discord:m44".
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .. import instant, passages
from .. import query as Q
from ..chat import fts_candidates, is_query_error
from ..db import in_batches
from ..embed import quantize
from .scope import Scope


@dataclass
class Span:
    key: str
    kb: str
    chat_id: int
    start_ts: int
    end_ts: int
    members: list[int] = field(default_factory=list)      # message ids, in time order


def _nonempty(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None


def detect(conn: sqlite3.Connection) -> str:
    """passage once the first index build has finished, else single messages."""
    if _nonempty(conn, "passages") and _nonempty(conn, "passage_members"):
        ready = conn.execute("SELECT value FROM kv WHERE key = 'passages_ready'").fetchone()
        if ready and ready[0] == "1":
            return "passage"
    return "message"


def _trashed(conn: sqlite3.Connection) -> list[int]:
    """Trashed chats, left out of every leg, even when the scope names them."""
    return [r[0] for r in conn.execute("SELECT id FROM chats WHERE trashed = 1")]


def is_sentence(q: str) -> bool:
    return len(q.split()) >= 4


def fts_variants(q: str) -> list[str]:
    """What to MATCH for one query variant: all words (the last as a prefix), then, for
    a short variant (up to three words), any word of 3+ letters, so a variant with one
    missing word still finds something. A sentence is for meaning and the reranker; ORing
    its words would match every "about" in the archive."""
    out = list(fts_candidates(q))
    words = [w for w in q.replace('"', " ").split() if len(w) >= 3 and w.upper()
             not in ("AND", "OR", "NOT", "NEAR")]
    if 1 < len(words) and not is_sentence(q):
        any_ = " OR ".join(f'"{w}"' for w in words)
        if any_ not in out:
            out.append(any_)
    return out


class Units:
    """One knowledge base's units. Every method takes a read connection of that kb."""

    def __init__(self, kb_id: str, kind: str, instant: bool = False):
        self.kb = kb_id
        self.kind = kind
        self.instant = instant          # the kb's normalised instant index is built
        self.letter = {"passage": "p", "message": "m"}[kind]

    def key(self, uid: int) -> str:
        return f"{self.kb}:{self.letter}{uid}"

    @staticmethod
    def uid(key: str) -> int:
        return int(key.rsplit(":", 1)[1][1:])

    # ---- lexical ---------------------------------------------------------------------
    def message_hits(self, conn: sqlite3.Connection, query: str, scope: Scope, n: int,
                     order: str = "best") -> tuple[list[tuple[int, int]], int]:
        """-> ([(message_id, ts)] ranked, total matches in scope). `order`: best (bm25),
        first (oldest first), last (newest first). A variant with all words and few
        hits falls back to any word."""
        fast = self._instant_hits(conn, query, scope, n, order)
        if fast is not None:
            return fast
        where, args = scope.sql(self.kb, "m")
        where.append("c.trashed = 0")
        w = " AND ".join(["messages_fts MATCH ?", *where])
        sort = {"first": "m.ts ASC, m.id ASC", "last": "m.ts DESC, m.id DESC"}.get(order, "rank")
        best: tuple[list[tuple[int, int]], int] = ([], 0)
        for match in fts_variants(query):
            try:
                rows = conn.execute(
                    f"""SELECT m.id, m.ts FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
                        JOIN chats c ON c.id = m.chat_id WHERE {w} ORDER BY {sort} LIMIT ?""",
                    [match, *args, n]).fetchall()
                total = conn.execute(
                    f"""SELECT COUNT(*) FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
                        JOIN chats c ON c.id = m.chat_id WHERE {w}""", [match, *args]).fetchone()[0]
            except sqlite3.OperationalError as e:
                if not is_query_error(e):
                    raise
                continue
            if len(rows) > len(best[0]):
                best = ([(r[0], r[1]) for r in rows], total)
            if len(rows) >= 5:
                break
        return best

    def _instant_hits(self, conn: sqlite3.Connection, text: str, scope: Scope, n: int,
                      order: str) -> tuple[list[tuple[int, int]], int] | None:
        """Instant search's normalised index (umlauts both ways, elongations, emoji,
        ß); None while it is not built, and messages_fts answers instead."""
        if not self.instant or not instant.is_ready(conn):
            return None
        chats = sorted(scope.chats.get(self.kb, ())) if scope.chats is not None else None
        trashed = _trashed(conn)
        who: list[str] = []
        if scope.filters_people:
            who = ([f"p{x}" for x in sorted((scope.senders or {}).get(self.kb, ()))]
                   + (["me"] if scope.me else []))
            if not who:
                return [], 0
        sort = {"first": "oldest", "last": "recent"}.get(order, "relevance")
        words = [w for w in text.replace('"', " ").split() if len(w) >= 3
                 and w.upper() not in ("AND", "OR", "NOT", "NEAR")]
        tries = [text]
        if 1 < len(words) and not is_sentence(text):
            tries.append(" OR ".join(words))
        best: tuple[list[tuple[int, int]], int] = ([], 0)
        for t in tries:
            q = Q.parse(t)
            if not q.clauses:
                continue
            p = instant.plan(q, instant.Filters(chats=chats, ts_from=scope.ts_from,
                                                ts_to=scope.ts_to, trashed=trashed))
            if p is None:
                return [], 0
            if who:
                p.match = f"({p.match}) AND (f : ({' OR '.join(who)}))"
            try:
                rows = instant.page(conn, p, sort=sort, limit=n)
                total = instant.count(conn, p, None)[0]
            except sqlite3.OperationalError as e:
                if not is_query_error(e):
                    raise
                continue
            sks = [r[0] for r in rows]
            ids: dict[int, int] = {}
            for part in in_batches(sks):
                ids.update(conn.execute(f"SELECT sk, id FROM search_map WHERE sk IN "
                                        f"({','.join('?' * len(part))})", part).fetchall())
            hits = [(ids[sk], sk >> 10) for sk in sks if sk in ids]
            if len(hits) > len(best[0]):
                best = (hits, total)
            if len(hits) >= 5:
                break
        return best

    def passage_hits(self, conn: sqlite3.Connection, query: str, scope: Scope,
                     n: int) -> list[int]:
        """Contextual BM25 over passages (header + text), ranked. Passages only."""
        if self.kind != "passage":
            return []
        where: list[str] = []
        args: list[Any] = []
        if scope.chats is not None:
            ids = sorted(scope.chats.get(self.kb, ()))
            where.append(f"p.chat_id IN ({','.join('?' * len(ids)) or 'NULL'})")
            args += ids
        if trashed := _trashed(conn):
            where.append(f"p.chat_id NOT IN ({','.join('?' * len(trashed))})")
            args += trashed
        if scope.filters_people:
            ors = []
            s = sorted((scope.senders or {}).get(self.kb, ())) + ([-1] if scope.me else [])
            if s:
                ors.append(f"EXISTS (SELECT 1 FROM passage_people pp WHERE pp.passage_id = p.id "
                           f"AND pp.person_id IN ({','.join('?' * len(s))}))")
                args += s
            where.append("(" + (" OR ".join(ors) or "0") + ")")
        if scope.ts_from is not None:
            where.append("p.end_ts >= ?")
            args.append(scope.ts_from)
        if scope.ts_to is not None:
            where.append("p.start_ts < ?")
            args.append(scope.ts_to)
        w = " AND ".join(["passages_fts MATCH ?", *where])
        for match in fts_variants(query):
            try:
                rows = conn.execute(
                    f"SELECT p.id FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid "
                    f"WHERE {w} ORDER BY rank LIMIT ?", [match, *args, n]).fetchall()
            except sqlite3.OperationalError as e:
                if not is_query_error(e):
                    raise
                continue
            if rows:
                return [r[0] for r in rows]
        return []

    def lift(self, conn: sqlite3.Connection, mids: list[int]) -> dict[int, int]:
        """message id -> the unit that contains it (message units: itself). A message in
        no unit yet (imported after the last index build) stands alone, as -id."""
        if self.kind == "message" or not mids:
            return {m: m for m in mids}
        out: dict[int, int] = {}
        for part in in_batches(mids):
            for r in conn.execute(
                    f"SELECT message_id, MIN(passage_id) FROM passage_members WHERE message_id IN "
                    f"({','.join('?' * len(part))}) GROUP BY message_id", part):
                out[r[0]] = r[1]
        return {m: out[m] if m in out else -m for m in mids}

    # ---- spans -----------------------------------------------------------------------
    def spans(self, conn: sqlite3.Connection, uids: list[int]) -> dict[int, Span]:
        out: dict[int, Span] = {}
        singles = [-u for u in uids if u < 0] + ([u for u in uids] if self.kind == "message" else [])
        if singles:
            for r in conn.execute(
                    f"SELECT id, chat_id, ts FROM messages WHERE id IN ({','.join('?' * len(singles))})",
                    singles):
                uid = r[0] if self.kind == "message" else -r[0]
                out[uid] = Span(self.key(uid), self.kb, r[1], r[2], r[2], [r[0]])
        rest = [u for u in uids if u > 0] if self.kind != "message" else []
        if not rest:
            return out
        marks = ",".join("?" * len(rest))
        heads = {r["id"]: r for r in conn.execute(
            f"SELECT id, chat_id, start_ts, end_ts FROM passages WHERE id IN ({marks})", rest)}
        mem: dict[int, list[tuple[int, int]]] = {}
        for r in conn.execute(
                f"SELECT x.passage_id, m.id, m.ts FROM passage_members x JOIN messages m "
                f"ON m.id = x.message_id WHERE x.passage_id IN ({marks})", rest):
            mem.setdefault(r[0], []).append((r[2], r[1]))
        for uid, h in heads.items():
            ms = [mid for _, mid in sorted(mem.get(uid, []))]
            out[uid] = Span(self.key(uid), self.kb, h["chat_id"], h["start_ts"], h["end_ts"], ms)
        return out

    # ---- dense -----------------------------------------------------------------------
    def dense(self, conn: sqlite3.Connection, qvec: list[float], scope: Scope,
              n: int, fp: tuple | None = None) -> list[int]:
        """Nearest units to one query vector, best first, within scope. `fp`: the
        fingerprint of the model that embedded the query (another model's index gives
        nothing)."""
        if self.kind == "passage":
            return self._dense_passages(conn, qvec, scope, n, fp)
        return []

    def _dense_passages(self, conn, qvec, scope, n, fp=None) -> list[int]:
        """Sign-bit prefilter, int8 cosine rescore (kb/passages.py)."""
        meta = passages.vec_meta(conn)
        if meta is None or len(qvec) < meta["dims"]:
            return []
        dims = meta["dims"]
        chats = sorted(scope.chats.get(self.kb, ())) if scope.chats is not None else None
        if chats is not None and not chats:
            return []
        # a passage starting just before the range can still reach into it
        lo = scope.ts_from - 6 * 3_600_000 if scope.ts_from is not None else None
        trashed = _trashed(conn)
        k = n * (3 if scope.filters_people or trashed else 1)
        hits = passages.dense(conn, quantize(qvec, dims, fp), k, chat_ids=chats, ts_from=lo,
                              ts_to=scope.ts_to)
        ids = [h[0] for h in hits]
        if trashed and ids:
            out = {r[0] for r in conn.execute(
                f"SELECT id FROM passages WHERE id IN ({','.join('?' * len(ids))}) AND chat_id IN "
                f"({','.join('?' * len(trashed))})", [*ids, *trashed])}
            ids = [i for i in ids if i not in out]
        if scope.filters_people and ids:
            marks = ",".join("?" * len(ids))
            who = sorted((scope.senders or {}).get(self.kb, ())) + ([-1] if scope.me else [])
            ok = {r[0] for r in conn.execute(
                f"SELECT DISTINCT passage_id FROM passage_people WHERE passage_id IN ({marks}) "
                f"AND person_id IN ({','.join('?' * len(who)) or 'NULL'})", [*ids, *who])}
            ids = [i for i in ids if i in ok]
        return ids[:n]
