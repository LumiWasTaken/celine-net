"""A long text read in parts, one way for every tool that reads one: a web page, a
browser tab, a mail and its attachments, an artifact, an earlier tool call.

A part is about `cap` tokens, fewer when the turn's output room (ToolContext
max_output_tokens) is smaller, counted as a tool result is: the result backstop
(turns/tools._capped) cuts in that unit, and a part leaves room for the lines printed
with it, so every part passes the backstop whole. The room is ROOM, or what the lines
take when they take more: a head that changes a little from one read to the next (a
mail's "(was unread)") leaves the parts where they were. The parts joined are the text.
The parts are cut by the calibrated estimate (turns/tokens.py), which moves as the
model's answers are counted, so a read after such a move may cut a few lines earlier or
later than the read before it. A text of more than one part reads:

    <head lines>
    part k of n

    <the part>

    earlier: <call>, part=k-1); later: <call>, part=k+1)
    <after lines>

A text of one part is its head lines, a blank line, the text and its after lines.
"""
from __future__ import annotations

from typing import Sequence

PART_TOKENS = 6000      # one part of a long text a tool reads
ROOM = 300              # tokens kept for the lines printed with a part, at least
MIN_PART = 200          # the least a part holds, however long its head lines are


def split(text: str, part_tokens: int) -> list[str]:
    """`text` in parts of at most part_tokens, cut after a paragraph where possible,
    else after a line; the parts joined are the text."""
    from ..turns import tokens
    total = tokens.estimate(text, "tool")
    if total <= part_tokens:
        return [text]
    chars = max(1, int(len(text) * part_tokens / total))
    out, i = [], 0
    while i < len(text):
        end = min(len(text), i + chars)
        if end < len(text):
            cut = text.rfind("\n\n", i + chars // 2, end)
            cut = cut + 2 if cut >= 0 else text.rfind("\n", i + chars // 2, end) + 1
            if cut > i:
                end = cut
        out.append(text[i:end])
        i = end
    return out


def tail(k: int, n: int, call: str) -> str:
    """The line after part k of n. `call` is the tool call that reads the text, without
    its closing parenthesis: `web__read_page(url="web:3"`."""
    nav = [f"earlier: {call}, part={k - 1})"] if k > 1 else []
    if k < n:
        nav.append(f"later: {call}, part={k + 1})")
    return "; ".join(nav)


def fit(text: str, ctx, *, head: Sequence[str] = (), call: str, after: Sequence[str] = (),
        cap: int = PART_TOKENS) -> list[str]:
    """`text` in parts that each fit one result with the lines `show` prints around
    them."""
    budget = cap
    if ctx is not None and getattr(ctx, "max_output_tokens", None):
        from ..turns import tokens
        around = "\n".join([*head, "part 99998 of 99999", "", "", tail(99998, 99999, call),
                            *after])
        room = max(ROOM, tokens.estimate(around, "tool"))
        budget = max(MIN_PART, min(cap, int(ctx.max_output_tokens) - room))
    return split(text, budget) if text else [""]


def show(chunks: list[str], k: int, *, head: Sequence[str] = (), call: str,
         after: Sequence[str] = ()) -> str:
    """Part k (from 1) of `chunks` as the result reads it."""
    n = len(chunks)
    lines = [*head, f"part {k} of {n}"] if n > 1 else list(head)
    if lines:
        lines.append("")
    lines.append(chunks[k - 1])
    if n > 1:
        lines += ["", tail(k, n, call)]
    return "\n".join(lines + list(after))


def read(text: str, part: int | None, ctx, *, head: Sequence[str] = (), call: str,
         after: Sequence[str] = (), cap: int = PART_TOKENS) -> tuple[str, int, int]:
    """Part `part` of `text` (from 1, held to the last) as the result reads it: (the
    result, its number, how many parts)."""
    chunks = fit(text, ctx, head=head, call=call, after=after, cap=cap)
    k = max(1, min(int(part or 1), len(chunks)))
    return show(chunks, k, head=head, call=call, after=after), k, len(chunks)
