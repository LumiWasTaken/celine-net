"""Smart reads: what the journal already holds of a chat is answered from it, and only
the gaps are asked of Discord.

Discord's message ids (snowflakes) rise with time, so a page Discord returns holds
every message between its two ends. `covered` stores that range as a span (state.py),
after the page is journaled; the sync's pages, the tools' pages and the live gateway
all add to them, and spans that touch become one. `plan` answers a read from the spans
it lands in and names the one request for the rest, starting at the first gap: never
more requests than reading it all from Discord, and none for a span read before.

The newest end: a span reaches the present only while the live connection has seen
every message after its top (`Gateway.live`). Otherwise a read of the newest messages
asks Discord. Edits and deletes the gateway journals show in what is answered.
"""
from __future__ import annotations

from typing import Any, Iterable

from .state import State

TOP = 2 ** 63 - 1               # above every id: the present


def cover(state: State, chat: str, lo: int, hi: int, ids: Iterable[int] = (),
          holes: Iterable[int] = ()) -> None:
    """A page's messages (`ids`) from lo to hi are journaled, except the holes (messages
    a subscription left out). A stretch between two holes with no message in it is
    left alone."""
    marks = sorted(h for h in set(holes) if lo <= h <= hi)
    start = lo
    for i, h in enumerate(marks):
        if start <= h - 1 and (i == 0 or any(start <= x < h for x in ids)):
            state.cover(chat, start, h - 1)
        start = h + 1
    if start <= hi:
        state.cover(chat, start, hi)


def covered(state: State, chat: str, params: dict[str, Any], msgs: list[dict],
            holes: Iterable[int] = ()) -> None:
    """Store what one page of GET /channels/<chat>/messages covers, asked with these
    params, once its messages are journaled. A page shorter than asked reached the start
    of the chat (newest or before); a page never covers ids above the newest message
    the connector knows, which may not exist yet."""
    ids = sorted(int(m["id"]) for m in msgs)
    limit = int(params.get("limit", 50))
    if "around" in params or "after" in params:
        if ids:
            cover(state, chat, int(params["after"]) + 1 if "after" in params else ids[0],
                  ids[-1], ids, holes)
        return
    if "before" in params:
        hi = min(int(params["before"]) - 1, max(ids[-1] if ids else 0, state.top(chat)))
    elif ids:
        hi = ids[-1]
    else:
        return
    lo = 0 if len(ids) < limit else ids[0]
    if hi > 0:
        cover(state, chat, lo, hi, ids, holes)


def plan(state: State, chat: str, *, before: str | None = None, after: str | None = None,
         limit: int, live: int | None = None) -> tuple[list[dict], dict[str, Any] | None]:
    """A read of `limit` messages (the newest, or next to before or after) as the
    records the journal answers and the params of the one request left to ask Discord
    (None: nothing). `live`: the newest id the live connection has seen of the chat,
    when it would have seen every message after it."""
    if before and after:                        # Discord's own answer: no span is used
        return [], {"limit": limit, "before": before, "after": after}
    if after is None:                           # down from before, or from the present
        if before is None:
            span = state.span_of(chat, live) if live is not None else None
            cur = TOP
        else:
            cur = int(before) - 1
            span = state.span_of(chat, cur)
        if span is None:
            return [], {"limit": limit, **({"before": before} if before else {})}
        got = state.archived(chat, span[0], cur, limit, newest=True)
        if len(got) >= limit or span[0] == 0:
            return got, None
        return got, {"limit": limit - len(got), "before": str(span[0])}
    cur = int(after) + 1                        # up from after
    span = state.span_of(chat, cur)
    if span is None:
        return [], {"limit": limit, "after": after}
    now = live is not None and span[0] <= live <= span[1]
    got = state.archived(chat, cur, TOP if now else span[1], limit, newest=False)
    if len(got) >= limit or now:
        return got, None
    return got, {"limit": limit - len(got), "after": str(span[1])}


def around(state: State, chat: str, mid: str, limit: int,
           live: int | None = None) -> list[dict] | None:
    """Messages around one, half before it and the rest from it on, as Discord answers
    `around`, when the journal holds them all; else None (one request asks for them)."""
    older, ask = plan(state, chat, before=mid, limit=limit // 2)
    if ask is not None:
        return None
    newer, ask = plan(state, chat, after=str(int(mid) - 1), limit=limit - len(older),
                      live=live)
    return None if ask is not None else older + newer
