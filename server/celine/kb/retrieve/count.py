"""Exact counts for Celine: SQL, never estimates, never tallied by the model.

`by` groups by time (year, month, weekday, hour), by who wrote (person), by chat, or by
**co_participant**: everyone who wrote within ±`window_minutes` of a matching message
in the same chat ("who did I play chess with"). `unit` counts messages, distinct
days, or match windows (runs of matching messages in one chat with no pause longer than
`gap_minutes`). Accounts marked as the same person are counted as one.

Words are matched like chat_archive__search matches them: on instant search's normalised index
(umlauts both ways, ß, elongations), so "müller" and "mueller" count the same messages.
While a knowledge base's index is being built, the plain word index answers.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .. import instant, names
from .. import query as Q
from ..chat import chat_cards
from ..identity import Identities
from .scope import Scope

_WORDS = re.compile(r"\w+", re.UNICODE)

_BUCKET = {
    "year": "strftime('%Y', m.ts / 1000, 'unixepoch', 'localtime')",
    "month": "strftime('%Y-%m', m.ts / 1000, 'unixepoch', 'localtime')",
    "weekday": "strftime('%w', m.ts / 1000, 'unixepoch', 'localtime')",
    "hour": "strftime('%H', m.ts / 1000, 'unixepoch', 'localtime')",
    "person": "CASE WHEN m.from_me THEN -1 ELSE m.sender_id END",
    "chat": "m.chat_id",
    "total": "'all'",
}
_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
BY = ("year", "month", "weekday", "hour", "person", "chat", "co_participant", "total")


@dataclass
class Counted:
    rows: list[dict[str, Any]] = field(default_factory=list)   # key, label, refs, count
    total: int = 0          # matching messages (or days / match windows) in scope
    unit: str = "messages"


def _entries(queries: list[str] | None) -> list[list[str]]:
    return [w for w in (_WORDS.findall(str(q)) for q in queries or []) if w]


def match_expr(queries: list[str] | None) -> str | None:
    """For messages_fts. Entries ORed; one word matches as a prefix, several words as
    one exact phrase ("wish you would" is not any message with "you" and "would" in
    it)."""
    parts = ['"' + " ".join(w) + '"' if len(w) > 1 else f'"{w[0]}"*'
             for w in _entries(queries)]
    return " OR ".join(parts) or None


def instant_match(queries: list[str] | None) -> str | None:
    """The same entries for instant search's index, normalised as instant search
    normalises a query."""
    parts = []
    for w in _entries(queries):
        p = instant.plan(Q.parse('"' + " ".join(w) + '"' if len(w) > 1 else w[0]),
                         instant.Filters())
        if p is not None:
            parts.append(f"({p.match})")
    return " OR ".join(parts) or None


def _windows(rows: list[tuple[int, int, Any]], gap_ms: int) -> dict[Any, int]:
    """rows (chat_id, ts, key) sorted by chat, ts -> distinct match windows per key."""
    out: dict[Any, set[int]] = {}
    ep = -1
    prev: tuple[int, int] | None = None
    for chat, ts, key in rows:
        if prev is None or chat != prev[0] or ts - prev[1] > gap_ms:
            ep += 1
        prev = (chat, ts)
        out.setdefault(key, set()).add(ep)
    return {k: len(v) for k, v in out.items()}


def count_kb(conn: sqlite3.Connection, kid: str, scope: Scope, *, by: str, match: str | None,
             unit: str, window_ms: int, gap_ms: int,
             ix_match: str | None = None) -> tuple[dict[Any, int], int]:
    """`ix_match`: the instant index's expression, used when that index is ready;
    `match`: the plain word index's, used while it is built."""
    where, args = scope.sql(kid, "m")
    where.append("m.kind <> 'system'")
    where.append("c.trashed = 0")
    join = "JOIN chats c ON c.id = m.chat_id"
    if ix_match and instant.is_ready(conn):
        join += (" JOIN search_map sm ON sm.id = m.id"
                 " JOIN search_fts ON search_fts.rowid = sm.sk")
        where.insert(0, "search_fts MATCH ?")
        args.insert(0, ix_match)
    elif match:
        join += " JOIN messages_fts ON messages_fts.rowid = m.id"
        where.insert(0, "messages_fts MATCH ?")
        args.insert(0, match)
    w = " AND ".join(where) or "1"
    day = "date(m.ts / 1000, 'unixepoch', 'localtime')"

    if by == "co_participant":
        hits = f"SELECT m.id, m.chat_id, m.ts FROM messages m {join} WHERE {w} LIMIT 50000"
        key = "CASE WHEN o.from_me THEN -1 ELSE o.sender_id END"
        near = (f"FROM ({hits}) h JOIN messages o ON o.chat_id = h.chat_id "
                f"AND o.ts BETWEEN h.ts - {int(window_ms)} AND h.ts + {int(window_ms)} "
                f"WHERE o.kind <> 'system' AND (o.from_me = 1 OR o.sender_id IS NOT NULL)")
        if unit == "match_windows":
            rows = conn.execute(f"SELECT DISTINCT h.chat_id, h.ts, {key} AS k {near} "
                                f"ORDER BY h.chat_id, h.ts", args).fetchall()
            counts = _windows([(r[0], r[1], r[2]) for r in rows], gap_ms)
            tot = conn.execute(f"SELECT m.chat_id, m.ts FROM messages m {join} WHERE {w} "
                               f"ORDER BY m.chat_id, m.ts", args).fetchall()
            return counts, sum(_windows([(r[0], r[1], 0) for r in tot], gap_ms).values())
        what = ("COUNT(DISTINCT date(h.ts / 1000, 'unixepoch', 'localtime'))"
                if unit == "days" else "COUNT(DISTINCT h.id)")
        rows = conn.execute(f"SELECT {key} AS k, {what} AS n {near} GROUP BY k", args).fetchall()
        tot_what = f"COUNT(DISTINCT {day})" if unit == "days" else "COUNT(*)"
        total = conn.execute(f"SELECT {tot_what} FROM messages m {join} WHERE {w}",
                             args).fetchone()[0]
        return {r[0]: r[1] for r in rows}, total

    bucket = _BUCKET[by]
    if unit == "match_windows":
        rows = conn.execute(f"SELECT m.chat_id, m.ts, {bucket} AS k FROM messages m {join} "
                            f"WHERE {w} ORDER BY m.chat_id, m.ts", args).fetchall()
        counts = _windows([(r[0], r[1], r[2]) for r in rows], gap_ms)
        total = sum(_windows([(r[0], r[1], 0) for r in rows], gap_ms).values())
        return counts, total
    what = f"COUNT(DISTINCT {day})" if unit == "days" else "COUNT(*)"
    rows = conn.execute(f"SELECT {bucket} AS k, {what} AS n FROM messages m {join} "
                        f"WHERE {w} GROUP BY k", args).fetchall()
    total = conn.execute(f"SELECT {what} FROM messages m {join} WHERE {w}", args).fetchone()[0]
    return {r[0]: r[1] for r in rows}, total


def _person_labels(conn, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    return {r["id"]: names.resolve(r)[0] for r in conn.execute(
        f"SELECT * FROM people WHERE id IN ({','.join('?' * len(ids))})", ids)}


def _chat_labels(conn, ids: list[int]) -> dict[int, str]:
    # the kind with the name: "which group chats" is answered by one count by chat
    return {k: f"{v['name']} ({v.get('kind') or 'chat'})"
            for k, v in chat_cards(conn, set(ids)).items()}


def count(kbs, scope: Scope, *, by: str, queries: list[str] | None = None,
          unit: str = "messages", me: str = "me", identities: Identities | None = None,
          window_minutes: int = 30, gap_minutes: int = 30, limit: int = 60) -> Counted:
    if by not in BY:
        raise ValueError(f"cannot count by {by!r}; one of {', '.join(BY)}")
    if unit not in ("messages", "days", "match_windows"):
        raise ValueError(f"unit is messages, days or match_windows, not {unit!r}")
    ids = identities or Identities()
    match = match_expr(queries)
    if queries and not match:
        raise ValueError("queries has no words to match")
    ix_match = instant_match(queries)
    merged: dict[Any, dict[str, Any]] = {}
    total = 0
    for kid in scope.kbs:
        if not scope.kb_in_scope(kid):
            continue
        kb = kbs.get(kid)
        fast = bool(ix_match) and kb.search_ready
        if fast:
            kb.ix_catch_up()                # what an import just wrote is counted

        def run(conn, kid=kid, fast=fast):
            counts, tot = count_kb(conn, kid, scope, by=by, match=match, unit=unit,
                                   window_ms=window_minutes * 60_000,
                                   gap_ms=gap_minutes * 60_000,
                                   ix_match=ix_match if fast else None)
            labels: dict[Any, str] = {}
            if by in ("person", "co_participant"):
                labels = _person_labels(conn, [k for k in counts if k not in (None, -1)])
            elif by == "chat":
                labels = _chat_labels(conn, list(counts))
            return counts, tot, labels
        counts, tot, labels = kb.file.read_sync(run)
        total += tot
        for k, n in counts.items():
            if k is None:
                continue
            if by in ("person", "co_participant"):
                if k == -1:
                    mk, label, refs = "me", me, []
                else:
                    ref = f"{kid}:{k}"
                    ident = ids.same_person(ref)
                    mk = f"identity:{ident.id}" if ident else ref
                    label = ident.name if ident else labels.get(k, "unknown")
                    refs = [ref]
            elif by == "chat":
                mk, label, refs = f"{kid}:{k}", labels.get(k, "?"), [f"{kid}:{k}"]
            else:
                mk, label, refs = k, (_DAYS[int(k)] if by == "weekday" else str(k)), []
            row = merged.setdefault(mk, {"key": mk, "label": label, "refs": [], "count": 0})
            row["count"] += n
            row["refs"] += [r for r in refs if r not in row["refs"]]
    rows = list(merged.values())
    if by in ("person", "co_participant", "chat"):
        rows.sort(key=lambda r: -r["count"])
    else:
        rows.sort(key=lambda r: str(r["key"]))
    return Counted(rows=rows[:limit] if by in ("person", "co_participant", "chat") else rows,
                   total=total, unit=unit)
