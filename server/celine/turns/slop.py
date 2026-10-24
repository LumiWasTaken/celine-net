"""The one slop finder.

The agent's voice (settings/voice.py) names `phrases` (plain text, matched case-
insensitively on word boundaries) and `patterns` (a regex each, case-insensitive, `^`
and `$` at line ends; a pattern keeps its own span short, one sentence or a list's two
lines, and is only counted, never stopped mid-stream). Code (fenced or inline), quotes
and quoted lines are skipped: that is someone's own wording (_KEEP).

`profile(its own, theirs)` is slop-forensics' ratio: the words, word pairs and word triples
it writes far more often than the user does in the same chats, so the topic cancels out.
"""
from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from typing import TypedDict

# code (fenced and inline), quoted text and quoted lines: someone's own wording
_KEEP = re.compile(r"(?s:```.*?(?:```|\Z))|`[^`\n]*`|\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d"
                   r"|\u201e[^\u201c\u201d\n]*[\u201c\u201d]|(?<!\w)'[^'\n\u2013\u2014]{0,60}'(?!\w)"
                   r"|^[ \t]*>.*$", re.M)


class Hit(TypedDict):
    what: str           # the phrase, or the pattern as written
    kind: str           # "phrase" or "pattern"
    start: int          # offsets into the text
    end: int


@lru_cache(maxsize=512)
def _phrase(p: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(p.strip()) + r"(?!\w)", re.I)


@lru_cache(maxsize=512)
def _pattern(p: str) -> re.Pattern:
    return re.compile(p, re.I | re.M)


def find(text: str, voice: dict) -> list[Hit]:
    """Every hit of `voice`'s phrases and patterns in `text`, in order of `start`."""
    if not text:
        return []
    kept = [(m.start(), m.end()) for m in _KEEP.finditer(text)]

    def outside(a: int, b: int) -> bool:
        return not any(a < y and x < b for x, y in kept)
    hits: list[Hit] = []
    for kind, words, rx in (("phrase", voice.get("phrases") or [], _phrase),
                            ("pattern", voice.get("patterns") or [], _pattern)):
        for w in words:
            hits += [Hit(what=w, kind=kind, start=m.start(), end=m.end())
                     for m in rx(w).finditer(text) if m.end() > m.start()
                     and outside(m.start(), m.end())]
    return sorted(hits, key=lambda h: (h["start"], h["end"]))


_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _grams(texts: list[str]) -> Counter:
    out: Counter = Counter()
    for t in texts:
        w = _WORD.findall(_KEEP.sub(" ", t.lower().replace("’", "'")))
        for n in (1, 2, 3):
            out.update(" ".join(w[i:i + n]) for i in range(len(w) - n + 1))
    return out


def profile(hers: list[str], theirs: list[str], top: int = 20,
            min_count: int = 3) -> list[tuple[str, float]]:
    """The n-grams over-represented in its texts against theirs: (n-gram, ratio), the most
    over-represented first. ratio = its share of it / their share (their count plus one, so
    a word the user never writes counts), only for n-grams it wrote at least `min_count`
    times."""
    a, b = _grams(hers), _grams(theirs)
    na, nb = sum(a.values()) or 1, sum(b.values()) + 1
    scored = [(g, round((c / na) / ((b[g] + 1) / nb), 2)) for g, c in a.items()
              if c >= min_count]
    return sorted(scored, key=lambda x: (-x[1], x[0]))[:top]

