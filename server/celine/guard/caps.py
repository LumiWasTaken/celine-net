"""What Bastet sends a judge, and what she writes to Celine, is capped here: her intent,
a call's arguments, a finished turn's lines and its replies. The hijack judge's outside
excerpts (judge.py) and the page check's page (page.py) keep their own sizes beside
the code that cuts them.

Every cut leaves MARK in place, so the reader sees a hole
where text was left out, never a silent one.
"""
from __future__ import annotations

import json

INTENT = 6000       # its intent in all, the tail kept (its newest words matter most)
CALLED = 600        # one "{{agent_name}} called <tool> <args>" line of the intent, the head kept
SAID = 1500         # one "{{agent_name}} said" line of the intent, the head kept
ARGS = 3000         # the action's arguments, head and tail kept
VALUE = 1200        # one string value inside the arguments, head and tail kept
REPLY = 600         # one reply of Bastet to Celine on the intercom
MARK = "[... {n} characters left out ...]"


def mark(n: int) -> str:
    return MARK.format(n=n)


def head(s: str, n: int) -> str:
    """The first n characters of s and the marker, or s as it is."""
    if len(s) <= n:
        return s
    return s[:n].rstrip() + " " + mark(len(s) - n)


def tail(s: str, n: int) -> str:
    """The marker and the last n characters of s, or s as it is."""
    if len(s) <= n:
        return s
    return mark(len(s) - n) + " " + s[len(s) - n:].lstrip()


def head_tail(s: str, n: int, tail_share: float = 1 / 3, keep: tuple[str, ...] = ()) -> str:
    """The head and the tail of s around the marker, n characters of s in all. `keep`: a
    text code flagged; found in the middle, a window around it stays too, so a judge
    never judges a call with the flagged part cut out."""
    if len(s) <= n:
        return s
    for k in keep:
        i, w = s.find(k), n // 3
        if k and i >= w and i + len(k) <= len(s) - w:
            a = max(w, i - max(0, w - len(k)) // 2)
            b = min(len(s) - w, a + max(w, len(k)))
            return (s[:w].rstrip() + " " + mark(a - w) + " " + s[a:b].strip() + " "
                    + mark(len(s) - w - b) + " " + s[len(s) - w:].lstrip())
    t = int(n * tail_share)
    h = n - t
    return s[:h].rstrip() + " " + mark(len(s) - n) + " " + s[len(s) - t:].lstrip()


def _values(v, n: int, keep: tuple[str, ...]):
    if isinstance(v, str):
        return head_tail(v, n, keep=keep)
    if isinstance(v, dict):
        return {k: _values(x, n, keep) for k, x in v.items()}
    if isinstance(v, list):
        return [_values(x, n, keep) for x in v]
    return v


def fit(a, n: int = ARGS, keep: tuple[str, ...] = ()):
    """The arguments within about n characters of JSON: each long string value cut
    first (the JSON stays valid, a short url, address or command stays whole); still
    too long, the JSON text itself, head and tail. `keep`: see head_tail."""
    a = a or {}
    if len(json.dumps(a, ensure_ascii=False)) <= n:
        return a
    cut = _values(a, min(VALUE, n), keep)
    text = json.dumps(cut, ensure_ascii=False)
    return cut if len(text) <= n + 2 * len(MARK) else head_tail(text, n, keep=keep)


def args(a, n: int = ARGS) -> str:
    """The arguments as text for a prompt (fit)."""
    got = fit(a, n)
    return got if isinstance(got, str) else json.dumps(got, ensure_ascii=False)


def lines(xs: list[str], n: int = ARGS) -> list[str]:
    """A list of lines within about n characters: the first and the last kept, a
    marker line where the middle was left out."""
    if sum(len(x) for x in xs) <= n:
        return xs
    head_, tail_, used = [], [], 0
    i, j = 0, len(xs) - 1
    while i <= j and used + len(xs[i]) <= n * 2 // 3:
        used += len(xs[i])
        head_.append(xs[i])
        i += 1
    while j >= i and used + len(xs[j]) <= n:
        used += len(xs[j])
        tail_.insert(0, xs[j])
        j -= 1
    gone = sum(len(x) for x in xs[i:j + 1])
    return head_ + ([mark(gone)] if gone else []) + tail_


def intent(xs: list[str]) -> str:
    """The agent's intent from its lines: each "{{agent_name}} called" and "said" line capped, then the
    whole, its tail kept."""
    out = []
    for x in xs:
        if x.startswith("{{agent_name}} called "):
            out.append(head(x, CALLED))
        elif x.startswith("{{agent_name}} said: "):
            out.append(head(x, SAID))
        else:
            out.append(x)
    return tail("\n".join(x for x in out if x.strip()), INTENT)
