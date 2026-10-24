"""Hashline anchors: each line of a text as `<n>:<hash>| <text>`, and anchored edits
(replace, insert, delete) checked against the text as it is now. The notebook's notes
(notebook__edit) uses them."""
from __future__ import annotations

import re
import zlib
from typing import Annotated, Literal, Required, TypedDict

# a note's first line is its Last changed stamp (notebook.py), which an edit cannot touch;
# it is shown plain, with no anchor to copy
STAMP_KEPT = "Line 1, the Last changed line, is kept up to date automatically."
_STAMP = re.compile(r"^\s*last changed:", re.I)


def is_stamp(line: str) -> bool:
    return bool(_STAMP.match(line))


class Edit(TypedDict, total=False):
    """One change of notebook__edit, anchored on `n:hash` from notebook__read."""
    op: Required[Annotated[Literal["replace", "insert", "delete"],
                           "replace: lines start to end become `text`. insert: `text` goes "
                           "below `after`. delete: lines start to end go."]]
    start: Annotated[str, "replace, delete: the anchor of the first line, such as "
                          "\"12:ab\"."]
    end: Annotated[str, "replace, delete: the anchor of the last line. Leave it out for "
                        "one line."]
    after: Annotated[str, "insert: the anchor of the line to insert below, or \"start\" "
                          "for the top of the note."]
    text: Annotated[str, "replace, insert: the new lines, plain, without anchors."]


# Hashline anchors: `<n>:<hash>`, the hash two base32 characters of the line's text.
# A stale anchor (the line changed or moved since it read it) no longer matches.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_ANCHOR = re.compile(r"^\s*(\d+)\s*:\s*([0-9a-z]{2})\b")


class EditRefused(Exception):
    pass


def line_hash(line: str) -> str:
    h = zlib.crc32(line.rstrip().encode())
    return _ALPHABET[h & 31] + _ALPHABET[(h >> 5) & 31]


def lines_of(text: str) -> tuple[list[str], str, bool]:
    """(lines without their ends, the file's line end, whether it ends with one)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split("\n")
    trailing = len(lines) > 1 and lines[-1] == ""
    if lines[-1] == "":
        lines.pop()
    return [ln.removesuffix("\r") for ln in lines], newline, trailing


def numbered(lines: list[str], first: int, last: int) -> list[str]:
    """Lines first..last (1-based, inclusive) as `n:hash| text`; a Last changed stamp on
    line 1 as it is."""
    return [lines[0] if i == 1 and is_stamp(lines[0]) else
            f"{i}:{line_hash(lines[i - 1])}| {lines[i - 1]}"
            for i in range(max(1, first), min(last, len(lines)) + 1)]


def _near(lines: list[str], n: int) -> str:
    return "\n".join(numbered(lines, n - 2, n + 2))


def _anchor(ref, lines: list[str], bad: list[str]) -> int:
    """The line number of an `n:hash` anchor, or 0 after noting what is wrong in `bad`."""
    m = _ANCHOR.match(str(ref or ""))
    if not m:
        raise EditRefused(f"{ref!r} is not an anchor. An anchor is `n:hash` from "
                          f"notebook__read, e.g. \"12:ab\".")
    n, h = int(m[1]), m[2]
    if 1 <= n <= len(lines) and line_hash(lines[n - 1]) == h:
        return n
    if not 1 <= n <= len(lines):
        where = f"the note has {len(lines)} lines"
        around = _near(lines, len(lines)) if lines else "(the note is empty)"
    else:
        where = "that line is different now"
        around = _near(lines, n)
    moved = [i for i, ln in enumerate(lines, 1) if line_hash(ln) == h and i != n]
    if moved:
        i = min(moved, key=lambda i: abs(i - n))
        where += f"; the line with hash {h} nearest to it is {i}"
        around = around + "\n...\n" + _near(lines, i)
    bad.append(f"{m[1]}:{h} does not match ({where}). The lines there now:\n{around}")
    return 0


def _number(ref) -> int | None:
    m = _ANCHOR.match(str(ref or ""))
    return int(m[1]) if m else None


def plan(edits, lines: list[str], has_stamp: bool) -> list[tuple[int, int, list[str]]]:
    """Check every edit against the file as it is. Returns (first, last, new lines) per
    edit, sorted from the top: lines first..last are replaced; an insert is (a+1, a).
    With a stamp on line 1, the top of the note is below it; the stamp's own anchor
    only counts as a place to insert after, whatever its hash (it changes every save)."""
    if not isinstance(edits, list) or not edits:
        raise EditRefused("`edits` needs at least one edit.")
    bad: list[str] = []
    planned = []
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            raise EditRefused(f"edit {i} is not an object.")
        op = e.get("op")
        text = e.get("text")
        if op in ("replace", "insert") and not isinstance(text, str):
            raise EditRefused(f"edit {i} ({op}) needs `text`.")
        new = text.replace("\r\n", "\n").split("\n") if text else []
        if new and new[-1] == "" and len(new) > 1:
            new.pop()
        if op == "insert":
            after = e.get("after")
            if str(after).strip().lower() in ("start", "0") or (has_stamp
                                                                and _number(after) == 1):
                a = 1 if has_stamp else 0
            else:
                a = _anchor(after, lines, bad)
                if not a:
                    continue
            planned.append((a + 1, a, new))
        elif op in ("replace", "delete"):
            if e.get("start") is None:
                raise EditRefused(f"edit {i} ({op}) needs `start`.")
            if has_stamp and 1 in (_number(e["start"]), _number(e.get("end"))):
                raise EditRefused(STAMP_KEPT)
            s = _anchor(e["start"], lines, bad)
            end = _anchor(e["end"], lines, bad) if e.get("end") is not None else s
            if not s or not end:
                continue
            if end < s:
                raise EditRefused(f"edit {i}: `end` ({end}) is above `start` ({s}).")
            planned.append((s, end, [] if op == "delete" else new))
        else:
            raise EditRefused(f"edit {i}: `op` is \"replace\", \"insert\" or \"delete\".")
    if bad:
        raise EditRefused("\n\n".join(bad) + "\n\nUse these anchors and send all the "
                          "edits again.")
    planned.sort(key=lambda x: (x[0], x[1]))
    for (s1, e1, _), (s2, e2, _) in zip(planned, planned[1:]):
        # an insert after line a is (a+1, a): it clashes with an edit that changes line a
        if s2 <= e1 or (e2 < s2 and s2 - 1 <= e1):
            raise EditRefused(f"two edits touch the same lines ({_span(s1, e1)} and "
                              f"{_span(s2, e2)}). Put them into one edit.")
    return planned


def _span(s: int, e: int) -> str:
    return f"after line {e}" if e < s else f"line {s}" if s == e else f"lines {s} to {e}"


def apply(lines: list[str], planned) -> tuple[list[str], list[tuple[int, int]]]:
    """Apply from the bottom up, so the line numbers of the edits above hold. Returns
    the new lines and, per edit, the new lines to show (one line of context each side,
    close regions merged)."""
    out = list(lines)
    for s, e, new in reversed(planned):
        out[s - 1:e] = new
    regions: list[tuple[int, int]] = []
    shift = 0
    for s, e, new in planned:
        a = s + shift
        b = a + len(new) - 1
        shift += len(new) - (e - s + 1)
        a, b = max(1, a - 1), min(len(out), max(b, a) + 1)
        if regions and a <= regions[-1][1] + 1:
            regions[-1] = (regions[-1][0], max(b, regions[-1][1]))
        else:
            regions.append((a, b))
    return out, regions
