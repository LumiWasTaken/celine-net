"""Episodes: ranked units grouped into contiguous windows of one chat, rendered with a
`[kb:id]` reference, a time and a resolved name on every line."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import names
from .units import Span

LINE_CHARS = 600


@dataclass
class Msg:
    id: int
    chat_id: int
    ts: int
    from_me: bool
    sender_id: int | None
    sender: str
    text: str
    kind: str


@dataclass
class Episode:
    kb: str
    chat_id: int
    start_ts: int
    end_ts: int
    first: tuple[int, int]           # (ts, id) of the first unit message
    last: tuple[int, int]
    score: float
    keys: list[str] = field(default_factory=list)
    hits: set[int] = field(default_factory=set)
    members: set[int] = field(default_factory=set)
    chat: dict[str, Any] | None = None
    lines: list[Msg] = field(default_factory=list)

    @property
    def refs(self) -> list[str]:
        return [f"{self.kb}:{m.id}" for m in self.lines]


SELECT = """SELECT m.id, m.chat_id, m.ts, m.from_me, m.sender_id, m.text, m.kind, m.media,
                    p.id AS p_id, p.key, p.phone, p.custom_name, p.contact_name, p.push_name,
                    p.display_name, p.username
             FROM messages m LEFT JOIN people p ON p.id = m.sender_id"""


def _text(r: sqlite3.Row) -> str:
    text = r["text"] or ""
    if r["kind"] == "media" or (not text and r["media"]):
        try:
            media = json.loads(r["media"]) if r["media"] else {}
        except ValueError:
            media = {}
        cap = media.get("caption") if media.get("caption") and media.get("caption") != text else ""
        text = f"[{media.get('type', 'attachment')}] {text or cap or ''}".strip()
    elif r["kind"] == "system":
        text = f"({text})"
    text = " ".join(text.split("\n"))
    return text if len(text) <= LINE_CHARS else text[:LINE_CHARS] + "…"


def to_msg(r: sqlite3.Row, me: str) -> Msg:
    if r["from_me"]:
        sender = me
    elif r["p_id"] is not None:
        sender = names.resolve(r)[0]
    else:
        sender = "someone"
    return Msg(r["id"], r["chat_id"], r["ts"], bool(r["from_me"]), r["sender_id"], sender,
               _text(r), r["kind"])


def window(conn: sqlite3.Connection, chat_id: int, first: tuple[int, int],
           last: tuple[int, int], pad: int, me: str, cap: int = 400,
           pad_ms: int = 3 * 3_600_000) -> list[Msg]:
    """Every message of the chat from `first` to `last` (inclusive), plus up to `pad` on
    each side that are no more than `pad_ms` away. At most `cap` in the middle."""
    before = conn.execute(f"{SELECT} WHERE m.chat_id = ? AND (m.ts, m.id) < (?, ?) "
                          f"AND m.ts >= ? ORDER BY m.ts DESC, m.id DESC LIMIT ?",
                          (chat_id, first[0], first[1], first[0] - pad_ms, pad)
                          ).fetchall()[::-1] if pad else []
    mid = conn.execute(f"{SELECT} WHERE m.chat_id = ? AND (m.ts, m.id) BETWEEN (?, ?) AND (?, ?) "
                       f"ORDER BY m.ts, m.id LIMIT ?",
                       (chat_id, first[0], first[1], last[0], last[1], cap)).fetchall()
    after = conn.execute(f"{SELECT} WHERE m.chat_id = ? AND (m.ts, m.id) > (?, ?) "
                         f"AND m.ts <= ? ORDER BY m.ts, m.id LIMIT ?",
                         (chat_id, last[0], last[1], last[0] + pad_ms, pad)
                         ).fetchall() if pad else []
    return [to_msg(r, me) for r in (*before, *mid, *after)]


def span_bounds(conn: sqlite3.Connection, span: Span) -> tuple[tuple[int, int], tuple[int, int]]:
    ids = span.members or []
    if not ids:
        return (span.start_ts, 0), (span.end_ts, 1 << 62)
    rows = conn.execute(f"SELECT ts, id FROM messages WHERE id IN ({','.join('?' * len(ids))}) "
                        f"ORDER BY ts, id", ids).fetchall()
    return (rows[0][0], rows[0][1]), (rows[-1][0], rows[-1][1])


def merge(ranked: list[tuple[Span, float, set[int], tuple, tuple]], *, gap_ms: int,
          max_members: int = 48) -> list[Episode]:
    """Group ranked spans: same chat, overlapping or within `gap_ms`, into one episode
    (its score is its best span's). Keeps rank order of first appearance."""
    eps: list[Episode] = []
    for span, score, hits, first, last in ranked:
        target = None
        for e in eps:
            if e.kb != span.kb or e.chat_id != span.chat_id:
                continue
            if span.start_ts <= e.end_ts + gap_ms and span.end_ts >= e.start_ts - gap_ms \
                    and len(e.members | set(span.members)) <= max_members:
                target = e
                break
        if target is None:
            eps.append(Episode(span.kb, span.chat_id, span.start_ts, span.end_ts, first, last,
                               score, [span.key], set(hits), set(span.members)))
            continue
        target.start_ts = min(target.start_ts, span.start_ts)
        target.end_ts = max(target.end_ts, span.end_ts)
        target.first = min(target.first, first)
        target.last = max(target.last, last)
        target.score = max(target.score, score)
        target.keys.append(span.key)
        target.hits |= hits
        target.members |= set(span.members)
    return eps


def clock(ts: int) -> datetime:
    return datetime.fromtimestamp(ts / 1000)


def render(ep: Episode, n: int | None = None, *, kb_title: str = "") -> str:
    """One episode for the model. Every line carries its reference; the first line of
    a day carries the date."""
    c = ep.chat or {}
    where = c.get("name") or "?"
    kind = c.get("kind") or "chat"
    if c.get("parent"):
        where = f"{c['parent']} #{where}"
    if not ep.lines:
        return ""
    a, b = clock(ep.lines[0].ts), clock(ep.lines[-1].ts)
    span = f"{a:%a %Y-%m-%d %H:%M} to {b:%H:%M}" if a.date() == b.date() else \
        f"{a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M}"
    head = (f"{'#' + str(n) + ' ' if n else ''}{kb_title or ep.kb}; {where} ({kind}, chat "
            f"{ep.kb}:{ep.chat_id}); {span}")
    out = [head]
    day = a.date()
    for m in ep.lines:
        t = clock(m.ts)
        stamp = f"{t:%H:%M}" if t.date() == day else f"{t:%Y-%m-%d %H:%M}"
        day = t.date()
        mark = ">" if m.id in ep.hits else " "
        out.append(f"{mark} [{ep.kb}:{m.id}] {stamp} {m.sender}: {m.text}")
    return "\n".join(out)


def tokens(text: str) -> int:
    """Real tokens, estimated: archive text runs ~2.2 characters per token (measured on
    the Qwen tokenizer), calibrated from the backend's usage. turns/tokens.py."""
    from ...turns import tokens as T
    return T.estimate(text, "tool") + 1
