"""Sessions: a chat cut into stretches of conversation by the pauses between messages.

Pure functions over (id, ts, chars) rows of one chat in time order; no database here.
The same rows always give the same sessions, so a rebuild only changes the sessions a
new, edited or deleted message actually touches (kb/passages.py compares signatures).

1. Cut wherever the pause between two messages is longer than `gap_ms`.
2. A tiny session (fewer than `tiny_messages` messages or `tiny_chars` characters of
   text) joins the nearer neighbour of the same chat when that pause is at most
   `merge_ms`, so "ok" and "👍" do not become passages of their own.
3. A session longer than `max_messages` is split at its largest inner pause, again and
   again, keeping at least `min_part` messages on each side.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class SessionParams:
    gap_ms: int = 90 * 60_000
    merge_ms: int = 6 * 3_600_000
    tiny_messages: int = 3
    tiny_chars: int = 60
    max_messages: int = 120
    min_part: int = 16

    @classmethod
    def from_settings(cls, s: dict[str, Any]) -> "SessionParams":
        return cls(gap_ms=int(float(s.get("session_gap_minutes", 90)) * 60_000),
                   merge_ms=int(float(s.get("session_merge_hours", 6)) * 3_600_000),
                   tiny_messages=int(s.get("session_tiny_messages", 3)),
                   tiny_chars=int(s.get("session_tiny_chars", 60)),
                   max_messages=int(s.get("session_max_messages", 120)),
                   min_part=int(s.get("session_min_part", 16)))


def _chars(r: Any) -> int:
    return int(r["chars"]) if not isinstance(r, tuple) else int(r[2])


def _ts(r: Any) -> int:
    return int(r["ts"]) if not isinstance(r, tuple) else int(r[1])


def _id(r: Any) -> int:
    return int(r["id"]) if not isinstance(r, tuple) else int(r[0])


def cut(rows: Sequence[Any], gap_ms: int) -> list[list[Any]]:
    out: list[list[Any]] = []
    for r in rows:
        if out and _ts(r) - _ts(out[-1][-1]) <= gap_ms:
            out[-1].append(r)
        else:
            out.append([r])
    return out


def _tiny(s: list[Any], p: SessionParams) -> bool:
    return len(s) < p.tiny_messages or sum(_chars(r) for r in s) < p.tiny_chars


def merge_tiny(sessions: list[list[Any]], p: SessionParams) -> list[list[Any]]:
    """Each tiny session joins the neighbour with the shorter pause, if that pause is
    within `merge_ms`. A run of tiny sessions can chain into one neighbour."""
    out: list[list[Any]] = []
    carry: list[Any] = []                      # tiny rows waiting for the next session
    for i, s in enumerate(sessions):
        s = carry + s
        carry = []
        if not _tiny(s, p):
            out.append(s)
            continue
        gap_prev = _ts(s[0]) - _ts(out[-1][-1]) if out else None
        nxt = sessions[i + 1] if i + 1 < len(sessions) else None
        gap_next = _ts(nxt[0]) - _ts(s[-1]) if nxt else None
        ok_prev = gap_prev is not None and gap_prev <= p.merge_ms
        ok_next = gap_next is not None and gap_next <= p.merge_ms
        if ok_prev and (not ok_next or gap_prev <= gap_next):
            out[-1].extend(s)
        elif ok_next:
            carry = s
        else:
            out.append(s)
    if carry:
        out.append(carry)
    return out


def split_long(s: list[Any], p: SessionParams) -> list[list[Any]]:
    if len(s) <= p.max_messages or len(s) < 2 * p.min_part:
        return [s]
    lo, hi = p.min_part, len(s) - p.min_part       # split before index i, lo <= i <= hi
    best = max(range(lo, hi + 1), key=lambda i: (_ts(s[i]) - _ts(s[i - 1]), -abs(i - len(s) // 2)))
    return split_long(s[:best], p) + split_long(s[best:], p)


def sessionize(rows: Sequence[Any], p: SessionParams) -> list[list[Any]]:
    """Rows of one chat, ordered by (ts, id) -> sessions, each a list of rows."""
    out: list[list[Any]] = []
    for s in merge_tiny(cut(rows, p.gap_ms), p):
        out.extend(split_long(s, p))
    return out


def signature(ids: Sequence[int]) -> str:
    """Same member ids, same session."""
    return hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:20]
