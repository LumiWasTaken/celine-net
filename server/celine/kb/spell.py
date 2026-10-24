"""Typo and compound expansion for instant search, from the index's own vocabulary.

No dictionary and no model: the words the archive actually contains, with how many
messages each is in (fts5vocab over search_fts.body). Loaded per knowledge base, kept
in memory (~100k terms), reloaded when the index changed and the copy is older than
a minute.

- `typos(token)`: terms within an edit distance (OSA: a swap of two neighbours counts
  one) of 1 for 5+ characters, 2 for 8+; shorter tokens get none. The first letter
  must match. With `prefix=True` the token is compared to the terms' beginnings and
  the result is corrected prefixes (`minecarf` -> `minecraf`).
- `substrings(token)`: terms that contain the token (4+ characters), for German
  compounds (`feier` -> `geburtstagsfeier`).
Candidates are ranked by distance, then by how many messages they are in.
"""
from __future__ import annotations

import bisect
import sqlite3
import threading
import time
from collections import defaultdict

from rapidfuzz import process
from rapidfuzz.distance import OSA

RELOAD_AFTER = 60.0
PREFIX_SCAN = 4096


def budget(token: str) -> int:
    n = len(token)
    if n >= 8:
        return 2
    if n >= 5:
        return 1
    return 0


def _wordish(token: str) -> bool:
    return any(c.isalpha() for c in token) and not any(ord(c) > 0xFFFF for c in token)


class Vocab:
    def __init__(self, df: dict[str, int]):
        self.df = df
        self.terms = sorted(df)
        self._by_first: dict[str, list[str]] = defaultdict(list)
        for t in self.terms:
            self._by_first[t[0]].append(t)
        self._blob = "\n" + "\n".join(self.terms) + "\n"
        self._starts: list[int] = []
        pos = 1
        for t in self.terms:
            self._starts.append(pos)
            pos += len(t) + 1

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "Vocab":
        return cls({r[0]: r[1] for r in conn.execute(
            "SELECT term, doc FROM search_vocab WHERE col = 'body'")})

    def __len__(self) -> int:
        return len(self.terms)

    def freq(self, term: str) -> int:
        return self.df.get(term, 0)

    def has_prefix(self, p: str) -> bool:
        i = bisect.bisect_left(self.terms, p)
        return i < len(self.terms) and self.terms[i].startswith(p)

    def prefix_freq(self, p: str) -> int:
        """The biggest document frequency among terms starting with p (0: none).
        Looks at the first PREFIX_SCAN of them: long ranges only happen for short p."""
        i = bisect.bisect_left(self.terms, p)
        best = 0
        for t in self.terms[i:i + PREFIX_SCAN]:
            if not t.startswith(p):
                break
            best = max(best, self.df[t])
        return best

    def typos(self, token: str, *, prefix: bool = False, limit: int = 8,
              min_df: int = 1) -> list[tuple[str, int, int]]:
        """[(term or prefix, distance, df)], best first. The token itself is left out."""
        k = budget(token)
        if not k or not _wordish(token):
            return []
        pool = self._by_first.get(token[0], [])
        n = len(token)
        if prefix:
            # the beginnings of the terms, a little shorter or longer than the token
            best: dict[str, int] = {}
            for t in pool:
                for ln in range(max(1, n - k), n + k + 1):
                    if len(t) < ln:
                        break
                    p = t[:ln]
                    if self.df[t] > best.get(p, 0):
                        best[p] = self.df[t]
            cands = list(best)
            dfs = best
        else:
            cands = [t for t in pool if abs(len(t) - n) <= k]
            dfs = self.df
        found = process.extract(token, cands, scorer=OSA.distance, score_cutoff=k,
                                limit=None)
        out = [(t, int(d), dfs[t]) for t, d, _ in found
               if t != token and dfs[t] >= min_df]
        out.sort(key=lambda x: (x[1], -x[2]))
        return out[:limit]

    def substrings(self, token: str, *, limit: int = 20,
                   min_df: int = 1) -> list[tuple[str, int]]:
        """[(term, df)] of terms that contain the token but are not it, most used
        first."""
        if len(token) < 4 or not _wordish(token):
            return []
        seen: set[str] = set()
        i = self._blob.find(token)
        while i != -1:
            j = bisect.bisect_right(self._starts, i) - 1
            if 0 <= j < len(self.terms):
                t = self.terms[j]
                if t != token and self.df[t] >= min_df:
                    seen.add(t)
                i = self._starts[j] + len(t)        # next term
            i = self._blob.find(token, i + 1)
        out = sorted(((t, self.df[t]) for t in seen), key=lambda x: -x[1])
        return out[:limit]


class VocabCache:
    """One Vocab per knowledge base, reloaded when the index generation moved and
    the copy is older than RELOAD_AFTER seconds, or empty (the first import)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._vocab: Vocab | None = None
        self._gen = -1
        self._at = 0.0

    def get(self, conn: sqlite3.Connection, gen: int) -> Vocab:
        with self._lock:
            stale = self._vocab is None or (
                gen != self._gen and (not self._vocab
                                      or time.monotonic() - self._at > RELOAD_AFTER))
            if stale:
                self._vocab = Vocab.load(conn)
                self._gen = gen
                self._at = time.monotonic()
            return self._vocab
