"""One text normaliser for instant search, used at index time and at query time.

It handles chat text in Latin script, German umlaut spellings folded: typos, `jaaaa`,
emoji glued to words, `ß` next to `ss`, `ü` next to `ue` and `u`. SQLite's unicode61
tokenizer folds `ä` to `a` but not `ß`, keeps `geburtstag🎂` as one token and treats
every elongation as its own word. Python's sqlite3 cannot register a custom FTS5 tokenizer, so instead the text is
normalised here and a shadow FTS table (kb/instant.sql) indexes the result.

`normalize(text)` returns the string to index: casefolded (ß -> ss), German umlauts
folded to their two-letter spelling (ü -> ue, so `müller` and `mueller` meet), other
diacritics stripped, emoji split into their own tokens, runs of three or more equal
letters squeezed to two (`jaaaa` -> `jaa`), laughs squeezed (`hahahaha` -> `haha`).
URLs need nothing: unicode61 already splits them on `/ . ? = :`.

`extras(text)` are extra tokens appended after the body so a folded form still
matches: `jaaaa` also indexes `ja`, `müller` also indexes `muller`.

`words(text)` yields (start, end, normalised token) over the original text, which is
what the highlighter uses to put `<mark>` on `Müller` when `mueller` was searched.
"""
from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable, Iterator
from functools import lru_cache

_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})
_UMLAUT_BARE = str.maketrans({"ä": "a", "ö": "o", "ü": "u"})

# unicode61 treats emoji and symbols as separators, but private-use characters as
# token characters. So every emoji or symbol is mapped to a private-use code point
# (BMP symbols to plane 15, astral emoji to plane 16) and spaced off: `geburtstag🎂`
# becomes two tokens and both can be searched. Modifiers (skin tones, variation
# selectors, ZWJ) go.
_DROP = {0xFE0E, 0xFE0F, 0x200D, 0x20E3}
_SKIN = range(0x1F3FB, 0x1F400)
_BMP_SYMBOLS = (0x2190, 0x2BFF)
_PUA = 0xF0000           # + BMP code point
_PUA16 = 0x100000 - 0x10000   # + astral code point (0x1F602 -> 0x10F602)

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_ELONG = re.compile(r"([^\W\d_])\1{2,}")
_LAUGH = re.compile(r"([^\W\d_]{2})\1{2,}")


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    if cp > 0xFFFF:
        return 0x1F000 <= cp <= 0x1FFFF and unicodedata.category(ch) in ("So", "Sk", "Sm", "Cn")
    return _BMP_SYMBOLS[0] <= cp <= _BMP_SYMBOLS[1] and unicodedata.category(ch) == "So"


def _fold_chars(s: str) -> str:
    """Casefold, fold umlauts to two letters, strip other diacritics, space emoji."""
    s = unicodedata.normalize("NFKC", s).casefold().translate(_UMLAUT)
    out: list[str] = []
    for ch in unicodedata.normalize("NFD", s):
        cp = ord(ch)
        if cp in _DROP or cp in _SKIN:
            continue
        cat = unicodedata.category(ch)
        if cat == "Mn":
            continue
        if _is_emoji(ch):
            out.append(" " + chr(_PUA + cp if cp <= 0xFFFF else _PUA16 + cp) + " ")
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def squeeze(word: str) -> str:
    """`jaaaa` -> `jaa`, `hahahaha` -> `haha`. Digits are left alone (`1000`)."""
    return _ELONG.sub(r"\1\1", _LAUGH.sub(r"\1\1", word))


def normalize(text: str) -> str:
    if not text:
        return ""
    return squeeze(_fold_chars(text))


def tokens(text: str) -> list[str]:
    """The tokens of normalised text, the way unicode61 would cut them (emoji and
    plane-15 symbols are single tokens)."""
    out = []
    for part in normalize(text).split():
        out.extend(_split(part))
    return out


def _split(part: str) -> Iterator[str]:
    buf = []
    for ch in part:
        if ord(ch) > 0xFFFF:
            if buf:
                yield from _WORD.findall("".join(buf))
                buf = []
            yield ch
        else:
            buf.append(ch)
    if buf:
        yield from _WORD.findall("".join(buf))


def extras(text: str) -> list[str]:
    """Other spellings a word should also be found under: the elongation squeezed to
    one letter (`jaaaa` -> `ja`) and the umlaut folded to the bare vowel (`müller` ->
    `muller`)."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    low = unicodedata.normalize("NFKC", text).casefold()
    for w in _WORD.findall(low):
        cands = []
        if _ELONG.search(w):
            one = _ELONG.sub(r"\1", w)
            if len(one) >= 2:
                cands.append(one)
        if any(c in w for c in "äöü"):
            cands.append(w.translate(_UMLAUT_BARE))
        for c in cands:
            n = normalize(c)
            if n and n not in seen:
                seen.add(n)
                out.append(n)
    return out


def index_text(text: str, more: Iterable[str] = ()) -> str:
    """What goes into the shadow index's body column: the normalised text, then the
    extra spellings and `more` (file names), after the text so phrases stay intact."""
    parts = [normalize(text)]
    ex = extras(text)
    for m in more:
        if m:
            parts.append(normalize(m))
            ex += extras(m)
    if ex:
        parts.append(" ".join(ex))
    return " ".join(p for p in parts if p).strip()


_SEG = re.compile(r"[^\W_]+|[^\w\s]")


@lru_cache(maxsize=65536)
def _forms(seg: str) -> frozenset[str]:
    if len(seg) == 1 and not seg.isalnum() and not _is_emoji(seg):
        return frozenset()
    return frozenset(tokens(seg)) | frozenset(extras(seg))


def words(text: str) -> Iterator[tuple[int, int, frozenset[str]]]:
    """(start, end, forms) for every word and emoji of the ORIGINAL text; `forms` are
    the normalised tokens this span is indexed under."""
    for m in _SEG.finditer(text or ""):
        forms = _forms(m.group(0))
        if forms:
            yield m.start(), m.end(), forms


# ---- highlighting -----------------------------------------------------------------------

def highlight(text: str, exact: set[str], prefixes: set[str] = frozenset(),
              window: int = 160) -> str:
    """Escaped HTML of `text` with <mark> around every word that is one of `exact` or
    starts with one of `prefixes` (all normalised). Long text is cut to a window
    around the first match."""
    text = text or ""
    spans: list[tuple[int, int]] = []
    for a, b, forms in words(text):
        if forms & exact or any(f.startswith(p) for f in forms for p in prefixes):
            spans.append((a, b))
    lo, hi = 0, len(text)
    if len(text) > window:
        centre = spans[0][0] if spans else 0
        lo = max(0, centre - window // 3)
        hi = min(len(text), lo + window)
        lo = max(0, hi - window)
        # do not cut a word in half
        while lo > 0 and text[lo - 1].isalnum():
            lo -= 1
        while hi < len(text) and text[hi].isalnum():
            hi += 1
    out = ["…" if lo > 0 else ""]
    pos = lo
    for a, b in spans:
        if b <= lo or a >= hi:
            continue
        out.append(html.escape(text[pos:a], quote=False))
        out.append("<mark>" + html.escape(text[a:b], quote=False) + "</mark>")
        pos = b
    out.append(html.escape(text[pos:hi], quote=False))
    if hi < len(text):
        out.append("…")
    return "".join(out)
