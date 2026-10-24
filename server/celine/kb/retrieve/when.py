"""Free-text time ranges for Celine's tools -> (from_ms, to_ms, label).

One period is kb/query.py's `parse_when`, the date words of instant search (German and
English: "letzten Sommer", "2021", "vor zwei Jahren", "12. März 2021"). On top of it:
ranges ("2021-03..2021-05", "2021 bis 2022", "März bis Mai"), open ends ("since
2021", "seit März", "before 2020") and a leading "around". The model never does date
arithmetic: it passes what the user said and the tool echoes the range it used.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from .. import query as Q

_LEAD = re.compile(r"(?:around|ca\.?|circa|etwa)\s+", re.I)
_RANGE = re.compile(r"(.+?)\s*(?:\.\.|\u2013|\u2014|\s-\s|\sto\s|\sbis\s|\suntil\s)\s*(.+)", re.I)
_SINCE = re.compile(r"(since|seit|from|ab|after|nach)\s+(.+)", re.I)
_BEFORE = re.compile(r"(before|until|bis|vor)\s+(.+)", re.I)


def parse_when(text: str | None, now: datetime | None = None) -> tuple[int, int, str] | None:
    """-> (from_ms inclusive, to_ms exclusive, label) or None for no restriction.
    Raises ValueError with a sentence the model can act on."""
    if text is None or not str(text).strip():
        return None
    now = now or datetime.now()
    t = " ".join(str(text).split())
    if m := _LEAD.match(t):
        t = t[m.end():]

    def one(s: str) -> Q.When | None:
        return Q.parse_when(s, now=now)

    if w := one(t):
        return w.from_ms, w.to_ms, w.label
    if (m := _RANGE.fullmatch(t)) and (a := one(m[1])) and (b := one(m[2])) \
            and a.from_ms < b.to_ms:
        return a.from_ms, b.to_ms, f"{a.label} to {b.label}"
    if (m := _SINCE.fullmatch(t)) and (a := one(m[2])):
        after = m[1].casefold() in ("after", "nach")
        tomorrow = datetime.combine(now.date() + timedelta(days=1), time())
        return (a.to_ms if after else a.from_ms), int(tomorrow.timestamp() * 1000), \
            f"{'after' if after else 'since'} {a.label}"
    if (m := _BEFORE.fullmatch(t)) and (a := one(m[2])):
        return 0, a.from_ms, f"before {a.label}"
    raise ValueError(f"The date range when={text!r} is not clear. Use a year (2021), a "
                     f"month (2021-03), a day (2021-03-05), a range (2021-03..2021-05), or "
                     f"words such as 'last summer', 'the last 3 months' or 'letzten Sommer'.")
