"""Query understanding for instant search and Celine's tools. Deterministic, no model.

    parse(q, now=None) -> Query                 syntax + dates, no database
    understand(q, kbs, now=None, identities=None) -> Query
                                                parse + resolve people, chats, kbs
    parse_when(text, now=None) -> When | None   "letzten Sommer", "2021", "vor zwei Jahren"
    resolve_people(kbs, name, identities=None) -> Resolution
                                                candidates, never a guess
    resolve_chats(kbs, name) -> Resolution

Syntax (English and German operator names):

    from:alex  von:alex  from:"Alex M"  from:me     who wrote it (me/ich/mir = you)
    in:carclub  im:  chat:                         which chat (name, part of it)
    kb:whatsapp  kb:wa  kb:discord  kb:documents   which knowledge base
    before:2021  vor:  bis:                        before the start of that period
    after:2020-06  nach:  seit:  ab:               from the start of that period on
    during:"last spring"  am:  um:  wann:  when:   inside that period
    has:voice|photo|video|link|file|media|sticker|gif|location|reply   hat:sprachnachricht|foto|...
    is:me  is:deleted  is:edited                   ist:ich|gelöscht|bearbeitet
    "exact phrase"   -exclude   a OR b   pre*      words; the last bare word is a prefix

Date values take a following word or two when that makes a date: `after:letzten Sommer
celine` is after the start of last summer, then `celine`. Dates in plain words are not
applied; they come back as `suggestions` for the UI to offer as a chip.

A name that matches several people is ambiguous: the search runs over all of them
(what `from:alex` literally says), and the chip carries the candidates so the UI can
narrow it. Nothing picks one of them silently. With `identities` (kb/identity.py) a
person is every account marked as the same person, and a same person's name or an
alias you gave anyone resolves like an exact name.
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, NamedTuple

from . import normalize as N

log = logging.getLogger("celine.kb")

# ---- operators ----------------------------------------------------------------------------

OPS = {
    "from": "from", "von": "from", "by": "from",
    "in": "in", "im": "in", "chat": "in",
    "kb": "kb", "source": "kb", "quelle": "kb",
    "before": "before", "vor": "before", "bis": "before", "until": "before",
    "after": "after", "nach": "after", "seit": "after", "ab": "after", "since": "after",
    "during": "during", "am": "during", "um": "during", "wann": "during", "when": "during",
    "on": "during", "date": "during", "datum": "during",
    "has": "has", "hat": "has", "mit": "has", "with": "has",
    "is": "is", "ist": "is",
}

HAS = {
    "voice": "voice", "sprachnachricht": "voice", "sprachnachrichten": "voice",
    "sprache": "voice", "vm": "voice", "voicemail": "voice",
    "audio": "audio",
    "photo": "photo", "photos": "photo", "foto": "photo", "fotos": "photo", "bild": "photo",
    "bilder": "photo", "image": "photo", "images": "photo", "img": "photo", "pic": "photo",
    "picture": "photo",
    "video": "video", "videos": "video",
    "link": "link", "links": "link", "url": "link",
    "file": "file", "files": "file", "datei": "file", "dateien": "file",
    "document": "file", "dokument": "file", "doc": "file", "pdf": "file",
    "media": "media", "medien": "media", "attachment": "media", "anhang": "media",
    "sticker": "sticker", "gif": "gif",
    "location": "location", "standort": "location", "ort": "location",
    "contact": "contact", "kontakt": "contact",
    "reply": "reply", "antwort": "reply", "replies": "reply",
}

IS = {"me": "me", "ich": "me", "mine": "me", "mir": "me",
      "deleted": "deleted", "geloescht": "deleted", "gelöscht": "deleted", "del": "deleted",
      "edited": "edited", "bearbeitet": "edited", "editiert": "edited"}

ME = {"me", "ich", "mir", "mich", "myself", "i"}

KB_ALIAS = {"wa": "whatsapp", "whatsapp": "whatsapp", "dc": "discord", "discord": "discord",
            "documents": "documents", "docs": "documents", "dokumente": "documents"}


# ---- structures -----------------------------------------------------------------------------

@dataclass
class Term:
    """One word or phrase of the text part. `tokens` are normalised (normalize.py)."""
    raw: str
    tokens: list[str]
    prefix: bool = False
    phrase: bool = False


@dataclass
class Clause:
    """Alternatives (OR) that together must match; `negate` excludes them."""
    terms: list[Term]
    negate: bool = False


class When(NamedTuple):
    """A period: from_ms inclusive, to_ms exclusive, a label, and its ISO dates."""
    from_ms: int
    to_ms: int
    label: str
    start: str          # ISO date, inclusive
    end: str            # ISO date, exclusive

    def as_dict(self) -> dict[str, Any]:
        return dict(self._asdict())


@dataclass
class Resolution:
    """What a name resolved to. `refs` are used for filtering: one person or chat per
    knowledge base when unambiguous, every candidate when not. `candidates` carry names
    and counts for the UI to pick from."""
    name: str
    refs: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: bool = False
    match: str = "none"            # exact | prefix | fuzzy | none

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Chip:
    kind: str                       # from in kb before after during has is exclude phrase
    raw: str
    value: str
    label: str = ""
    refs: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: bool = False
    error: str | None = None
    when: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], "") or k in ("kind", "raw", "value")}


@dataclass
class Query:
    raw: str
    clauses: list[Clause] = field(default_factory=list)
    kbs: list[str] | None = None
    people: list[Resolution] = field(default_factory=list)    # OR
    chats: list[Resolution] = field(default_factory=list)     # OR
    ts_from: int | None = None
    ts_to: int | None = None
    has: list[str] = field(default_factory=list)              # AND
    is_me: bool = False
    is_deleted: bool = False
    is_edited: bool = False
    chips: list[Chip] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    literal: bool = False          # quoted phrases only: never corrected

    # --- views -----------------------------------------------------------------------------
    @property
    def text(self) -> str:
        """The text part, as it could be typed again (no operators)."""
        parts = []
        for c in self.clauses:
            alts = []
            for t in c.terms:
                s = f'"{t.raw}"' if t.phrase else t.raw
                alts.append(s)
            s = " OR ".join(alts)
            parts.append(("-" if c.negate else "") + (f"({s})" if len(alts) > 1 and c.negate else s))
        return " ".join(parts)

    @property
    def has_text(self) -> bool:
        return any(not c.negate for c in self.clauses)

    @property
    def has_filters(self) -> bool:
        return bool(self.people or self.chats or self.ts_from is not None
                    or self.ts_to is not None or self.has or self.is_me
                    or self.is_deleted or self.is_edited)

    @property
    def empty(self) -> bool:
        return not self.clauses and not self.has_filters

    def refs_in(self, kid: str, what: str) -> list[int] | None:
        """Ids in one kb for the people or chats filter; None when there is no such
        filter, [] when the filter excludes this kb entirely."""
        res = self.people if what == "people" else self.chats
        if not res:
            return None
        out = []
        for r in res:
            for ref in r.refs:
                k, _, i = ref.partition(":")
                if k == kid and i.lstrip("-").isdigit():
                    out.append(int(i))
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw, "text": self.text,
            "terms": [{"raw": t.raw, "tokens": t.tokens, "prefix": t.prefix,
                       "phrase": t.phrase, "negate": c.negate, "group": i}
                      for i, c in enumerate(self.clauses) for t in c.terms],
            "kbs": self.kbs,
            "people": [r.as_dict() for r in self.people],
            "chats": [r.as_dict() for r in self.chats],
            "from": self.ts_from, "to": self.ts_to,
            "has": self.has, "is_me": self.is_me, "is_deleted": self.is_deleted,
            "is_edited": self.is_edited,
            "chips": [c.as_dict() for c in self.chips],
            "suggestions": self.suggestions,
        }


# ---- lexing ------------------------------------------------------------------------------

_LEX = re.compile(r'''
    (?P<op>(?P<neg1>-)?(?P<name>[^\W\d_]+):(?:"(?P<qv>[^"]*)"?|(?P<v>[^\s"]*)))
  | (?P<phrase>(?P<neg2>-)?"(?P<pv>[^"]*)"?)
  | (?P<word>\S+)
''', re.VERBOSE | re.UNICODE)


def parse(q: str, *, now: dt.datetime | None = None) -> Query:
    """Syntax and dates only; people, chats and kbs stay names until `resolve`."""
    q = q or ""
    out = Query(raw=q)
    items: list[tuple] = []       # ("clause", Clause) | ("or",) | ("and",)
    ms = list(_LEX.finditer(q))
    i = 0
    bare_words: list[Term] = []
    while i < len(ms):
        m = ms[i]
        i += 1
        if m.group("op") and m.group("name").casefold() in OPS:
            op = OPS[m.group("name").casefold()]
            value = m.group("qv") if m.group("qv") is not None else (m.group("v") or "")
            raw = m.group(0)
            if op in ("before", "after", "during") and m.group("qv") is None:
                # take following words while they make a longer date
                best = None
                for extra in range(0, 4):
                    if i + extra > len(ms):
                        break
                    words = [value] + [x.group(0) for x in ms[i:i + extra]]
                    if any(x.group("op") or x.group("phrase") for x in ms[i:i + extra]):
                        break
                    cand = " ".join(w for w in words if w)
                    if cand and parse_when(cand, now=now) is not None:
                        best = (extra, cand)
                if best:
                    value = best[1]
                    raw = " ".join([raw] + [x.group(0) for x in ms[i:i + best[0]]])
                    i += best[0]
            _apply_op(out, op, value, raw, negate=bool(m.group("neg1")), now=now)
            continue
        if m.group("phrase"):
            text = m.group("pv") or ""
            toks = N.tokens(text)
            if toks:
                t = Term(raw=text, tokens=toks, phrase=True)
                c = Clause([t], negate=bool(m.group("neg2")))
                items.append(("clause", c))
                out.chips.append(Chip(kind="exclude" if c.negate else "phrase",
                                      raw=m.group(0), value=text, label=text))
            continue
        w = m.group(0)
        if w in ("OR", "|"):
            items.append(("or",))
            continue
        if w in ("AND", "&", "&&"):
            continue
        negate = False
        if w == "NOT":
            items.append(("not",))
            continue
        if w.startswith("-") and len(w) > 1:
            negate, w = True, w[1:]
        star = w.endswith("*")
        w = w.rstrip("*")
        toks = N.tokens(w)
        if not toks:
            continue
        t = Term(raw=w, tokens=toks, prefix=star)
        items.append(("clause", Clause([t], negate=negate)))
        if negate:
            out.chips.append(Chip(kind="exclude", raw=m.group(0), value=w, label=w))
        else:
            bare_words.append(t)

    # the last bare word is a prefix while typing, unless quoted or followed by space
    if bare_words and not q.endswith((" ", "\t")):
        last_clause = next((x[1] for x in reversed(items) if x[0] == "clause"), None)
        if last_clause is not None and bare_words[-1] in last_clause.terms \
                and not last_clause.negate:
            bare_words[-1].prefix = True

    # fold OR / NOT markers into clauses
    clauses: list[Clause] = []
    pending_or = pending_not = False
    for it in items:
        if it[0] == "or":
            pending_or = True
            continue
        if it[0] == "not":
            pending_not = True
            continue
        c = it[1]
        if pending_not:
            c.negate = True
            pending_not = False
        if pending_or and clauses and not clauses[-1].negate and not c.negate:
            clauses[-1].terms.extend(c.terms)
        else:
            clauses.append(c)
        pending_or = False
    out.clauses = clauses
    out.literal = bool(clauses) and all(t.phrase for c in clauses for t in c.terms)
    out.suggestions = _date_suggestions(clauses, now)
    return out


def _apply_op(out: Query, op: str, value: str, raw: str, *, negate: bool,
              now: dt.datetime | None) -> None:
    v = value.strip()
    if not v:
        out.chips.append(Chip(kind=op, raw=raw, value="", error="empty"))
        return
    if negate:
        # the filters only narrow; "-from:alex" applied as "from:alex" would find the
        # opposite of what was asked
        out.chips.append(Chip(kind=op, raw=raw, value=v, error=f"{op}: cannot be excluded"))
        return
    low = N.normalize(v)
    if op == "from":
        if low in ME:
            out.is_me = True
            out.chips.append(Chip(kind="from", raw=raw, value=v, label="me"))
        else:
            out.people.append(Resolution(name=v))
            out.chips.append(Chip(kind="from", raw=raw, value=v, label=v))
    elif op == "in":
        out.chats.append(Resolution(name=v))
        out.chips.append(Chip(kind="in", raw=raw, value=v, label=v))
    elif op == "kb":
        out.kbs = (out.kbs or []) + [KB_ALIAS.get(low, low)]
        out.chips.append(Chip(kind="kb", raw=raw, value=v, label=KB_ALIAS.get(low, v)))
    elif op in ("before", "after", "during"):
        w = parse_when(v, now=now)
        if w is None:
            out.chips.append(Chip(kind=op, raw=raw, value=v, error="not a date I understand"))
            return
        if op == "before":
            out.ts_to = w.from_ms if out.ts_to is None else min(out.ts_to, w.from_ms)
            label = f"before {w.label}"
        elif op == "after":
            out.ts_from = w.from_ms if out.ts_from is None else max(out.ts_from, w.from_ms)
            label = f"from {w.label}"
        else:
            out.ts_from = w.from_ms if out.ts_from is None else max(out.ts_from, w.from_ms)
            out.ts_to = w.to_ms if out.ts_to is None else min(out.ts_to, w.to_ms)
            label = w.label
        out.chips.append(Chip(kind=op, raw=raw, value=v, label=label, when=w.as_dict()))
    elif op == "has":
        h = HAS.get(low) or HAS.get(v.casefold())
        if h is None:
            out.chips.append(Chip(kind="has", raw=raw, value=v,
                                  error=f"has: one of {', '.join(sorted(set(HAS.values())))}"))
            return
        if h not in out.has:
            out.has.append(h)
        out.chips.append(Chip(kind="has", raw=raw, value=v, label=h))
    elif op == "is":
        s = IS.get(low) or IS.get(v.casefold())
        if s == "me":
            out.is_me = True
        elif s == "deleted":
            out.is_deleted = True
        elif s == "edited":
            out.is_edited = True
        else:
            out.chips.append(Chip(kind="is", raw=raw, value=v,
                                  error="is: me, deleted or edited"))
            return
        out.chips.append(Chip(kind="is", raw=raw, value=v, label=s))


def _date_suggestions(clauses: list[Clause], now) -> list[dict[str, Any]]:
    """Date phrases in the plain words ("letzten sommer", "2021"), offered, not applied."""
    words = [t.raw for c in clauses if not c.negate for t in c.terms if not t.phrase]
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(words):
        for n in (4, 3, 2, 1):
            if i + n > len(words):
                continue
            text = " ".join(words[i:i + n])
            if n == 1 and not (re.fullmatch(r"(19|20)\d\d", text) or _single_word_date(text)):
                continue
            w = parse_when(text, now=now, fallback=False)
            if w is not None:
                out.append({"kind": "during", "text": text, "value": f'during:"{text}"',
                            "when": w.as_dict(), "label": w.label})
                i += n - 1
                break
        i += 1
    return out


def _single_word_date(text: str) -> bool:
    return N.normalize(text) in _SINGLE_DATES


# ---- dates -------------------------------------------------------------------------------

_MONTHS = {
    "januar": 1, "january": 1, "jan": 1, "jaenner": 1, "janner": 1,
    "februar": 2, "february": 2, "feb": 2,
    "maerz": 3, "marz": 3, "march": 3, "mrz": 3, "maer": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "june": 6, "jun": 6,
    "juli": 7, "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "december": 12, "dez": 12, "dec": 12,
}
_SEASONS = {
    "fruehling": 3, "fruehjahr": 3, "spring": 3, "fruhling": 3, "fruhjahr": 3,
    "sommer": 6, "summer": 6,
    "herbst": 9, "autumn": 9, "fall": 9,
    "winter": 12,
}
_SEASON_NAME = {3: "spring", 6: "summer", 9: "autumn", 12: "winter"}
_LAST = {"last", "letzten", "letzter", "letztes", "letzte", "vergangenen", "vergangener",
         "vergangenes", "vergangene", "previous", "past", "voriges", "vorigen", "voriger",
         "vorige", "vorletzten"}
_THIS = {"this", "diesen", "dieser", "dieses", "diese", "current", "aktuellen"}
_FILLER = {"in", "im", "am", "um", "on", "at", "of", "the", "during", "waehrend",
           "wahrend", "zu", "zur", "an", "den", "dem", "der", "des", "die", "das", "anno",
           "jahr", "year", "ueber", "over"}
_NUM = {"ein": 1, "eine": 1, "einem": 1, "einer": 1, "einen": 1, "one": 1, "a": 1,
        "zwei": 2, "two": 2, "couple": 2, "paar": 2, "drei": 3, "three": 3,
        "vier": 4, "four": 4, "fuenf": 5, "funf": 5, "five": 5, "sechs": 6, "six": 6,
        "sieben": 7, "seven": 7, "acht": 8, "eight": 8, "neun": 9, "nine": 9,
        "zehn": 10, "ten": 10, "elf": 11, "eleven": 11, "zwoelf": 12, "zwolf": 12,
        "twelve": 12}
_UNITS = {"jahr": "y", "jahre": "y", "jahren": "y", "jahres": "y", "year": "y", "years": "y",
          "monat": "m", "monate": "m", "monaten": "m", "month": "m", "months": "m",
          "woche": "w", "wochen": "w", "week": "w", "weeks": "w",
          "tag": "d", "tage": "d", "tagen": "d", "day": "d", "days": "d"}
_WEEKDAYS = {"montag": 0, "monday": 0, "dienstag": 1, "tuesday": 1, "mittwoch": 2,
             "wednesday": 2, "donnerstag": 3, "thursday": 3, "freitag": 4, "friday": 4,
             "samstag": 5, "sonnabend": 5, "saturday": 5, "sonntag": 6, "sunday": 6}
_EARLY = {"early": 0, "anfang": 0, "beginning": 0, "start": 0, "fruh": 0, "frueh": 0,
          "mid": 1, "mitte": 1, "middle": 1, "late": 2, "ende": 2, "end": 2, "spaet": 2,
          "spat": 2}
_SINGLE_DATES = {"heute", "today", "gestern", "yesterday", "vorgestern",
                 "weihnachten", "christmas", "xmas", "silvester", "halloween", "ostern",
                 "easter", "neujahr", "valentinstag"}


def _local(d: dt.datetime) -> int:
    """A naive local datetime -> ms since epoch."""
    return int(d.astimezone().timestamp() * 1000) if d.tzinfo is None else \
        int(d.timestamp() * 1000)


def _mk(start: dt.date, end: dt.date, label: str) -> When:
    a = dt.datetime.combine(start, dt.time())
    b = dt.datetime.combine(end, dt.time())
    return When(_local(a), _local(b), label, start.isoformat(), end.isoformat())


def _month_range(y: int, m: int, n: int = 1) -> tuple[dt.date, dt.date]:
    start = dt.date(y, m, 1)
    em = m - 1 + n
    end = dt.date(y + em // 12, em % 12 + 1, 1)
    return start, end


def _fmt(d: dt.date) -> str:
    return f"{d.day} {calendar.month_abbr[d.month]} {d.year}"


def _span_label(a: dt.date, b: dt.date) -> str:
    last = b - dt.timedelta(days=1)
    if a == last:
        return _fmt(a)
    return f"{_fmt(a)} to {_fmt(last)}"


def _season(y: int, m0: int) -> tuple[dt.date, dt.date]:
    return _month_range(y, m0, 3)


def _easter(y: int) -> dt.date:
    a = y % 19
    b, c = divmod(y, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    j, k = divmod(c, 4)
    m = (a + 11 * h) // 319
    r = (2 * e + 2 * j - k - h + m + 32) % 7
    n = (h - m + r + 90) // 25
    p = (h - m + r + n + 19) % 32
    return dt.date(y, n, p)


def _holiday(name: str, y: int) -> tuple[dt.date, dt.date, str] | None:
    if name in ("weihnachten", "christmas", "xmas", "weihnacht"):
        return dt.date(y, 12, 20), dt.date(y, 12, 28), f"Christmas {y}"
    if name in ("silvester", "nye", "newyearseve"):
        return dt.date(y, 12, 31), dt.date(y + 1, 1, 2), f"New Year's Eve {y}"
    if name in ("neujahr", "newyear"):
        return dt.date(y, 1, 1), dt.date(y, 1, 2), f"New Year {y}"
    if name in ("ostern", "easter"):
        e = _easter(y)
        return e - dt.timedelta(days=3), e + dt.timedelta(days=2), f"Easter {y}"
    if name == "halloween":
        return dt.date(y, 10, 31), dt.date(y, 11, 1), f"Halloween {y}"
    if name in ("valentinstag", "valentine", "valentines"):
        return dt.date(y, 2, 14), dt.date(y, 2, 15), f"Valentine's Day {y}"
    return None


def parse_when(text: str, *, now: dt.datetime | None = None,
               fallback: bool = True) -> When | None:
    """A time expression in German or English -> the period it names, in local time.
    `from_ms` inclusive, `to_ms` exclusive. None when it is not a date.

    Years, months, seasons (winter Y = Dec Y to Feb Y+1; `winter 2020/21` works),
    `last/this <unit|season|month>`, `vor N Jahren` / `N years ago` (a window around
    that point: +-6 months, +-15 days, +-3 days, the day), early/mid/late, holidays,
    weekdays (`letzten Dienstag`), ISO and German numeric dates; dateparser for the
    rest (`12. März 2021`) when `fallback`."""
    if not text or not text.strip():
        return None
    now = now or dt.datetime.now()
    today = now.date()
    s = N.normalize(text).replace("’", "'").replace("'", "")
    s = re.sub(r"[,]", " ", s)
    words = [w for w in s.split() if w]
    raw = text.strip()

    # --- numeric forms first, on the text as typed
    r = raw.strip().strip(",")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", r)
    if m:
        return _day(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", r) or re.fullmatch(r"(\d{4})/(\d{1,2})", r)
    if m and 1 <= int(m[2]) <= 12:
        a, b = _month_range(int(m[1]), int(m[2]))
        return _mk(a, b, f"{calendar.month_name[a.month]} {a.year}")
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})", r)
    if m:
        y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        return _day(y, int(m[2]), int(m[1]))
    m = re.fullmatch(r"(\d{1,2})[./](\d{4})", r)
    if m and 1 <= int(m[1]) <= 12:
        a, b = _month_range(int(m[2]), int(m[1]))
        return _mk(a, b, f"{calendar.month_name[a.month]} {a.year}")
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?", r)
    if m and 1 <= int(m[2]) <= 12 and 1 <= int(m[1]) <= 31:
        y = today.year
        try:
            if dt.date(y, int(m[2]), int(m[1])) > today:
                y -= 1
        except ValueError:
            return None
        return _day(y, int(m[2]), int(m[1]))

    # filler words that do not change the meaning; "jahr 2021" is the year
    words = [w for w in words if w not in _FILLER or w in ("jahr", "year")]
    if len(words) == 2 and words[0] in ("jahr", "year"):
        words = words[1:]
    if not words:
        return None
    w = words

    # a year alone, or a year range 2020/21
    if len(w) == 1 and re.fullmatch(r"(19|20)\d\d", w[0]):
        y = int(w[0])
        return _mk(dt.date(y, 1, 1), dt.date(y + 1, 1, 1), str(y))

    # relative days
    one = " ".join(w)
    if one in ("heute", "today"):
        return _day(today.year, today.month, today.day, "today")
    if one in ("gestern", "yesterday"):
        d = today - dt.timedelta(days=1)
        return _day(d.year, d.month, d.day, "yesterday")
    if one in ("vorgestern", "day before yesterday"):
        d = today - dt.timedelta(days=2)
        return _day(d.year, d.month, d.day, "the day before yesterday")

    # last / this <unit>
    if len(w) == 2 and (w[0] in _LAST or w[0] in _THIS) and w[1] in _UNITS:
        last = w[0] in _LAST
        u = _UNITS[w[1]]
        if u == "y":
            y = today.year - (1 if last else 0)
            return _mk(dt.date(y, 1, 1), dt.date(y + 1, 1, 1), str(y))
        if u == "m":
            y, m_ = today.year, today.month
            if last:
                y, m_ = (y, m_ - 1) if m_ > 1 else (y - 1, 12)
            a, b = _month_range(y, m_)
            return _mk(a, b, f"{calendar.month_name[m_]} {y}")
        if u == "w":
            monday = today - dt.timedelta(days=today.weekday())
            if last:
                monday -= dt.timedelta(days=7)
            end = monday + dt.timedelta(days=7)
            return _mk(monday, end, ("last week " if last else "this week ")
                       + f"({_span_label(monday, end)})")
        if u == "d":
            d = today - dt.timedelta(days=1 if last else 0)
            return _day(d.year, d.month, d.day)

    # N units ago: "vor zwei jahren", "2 years ago", "vor einem monat"
    ago = None
    if len(w) == 3 and w[0] == "vor" and w[2] in _UNITS:
        ago = (w[1], _UNITS[w[2]])
    elif len(w) == 3 and w[2] == "ago" and w[1] in _UNITS:
        ago = (w[0], _UNITS[w[1]])
    if ago:
        n = int(ago[0]) if ago[0].isdigit() else _NUM.get(ago[0])
        if n is None or n > 100:
            return None
        u = ago[1]
        if u == "y":
            c = _add_months(today, -12 * n)
            a, b = _add_months(c, -6), _add_months(c, 6)
        elif u == "m":
            c = _add_months(today, -n)
            a, b = c - dt.timedelta(days=15), c + dt.timedelta(days=16)
        elif u == "w":
            c = today - dt.timedelta(weeks=n)
            a, b = c - dt.timedelta(days=3), c + dt.timedelta(days=4)
        else:
            c = today - dt.timedelta(days=n)
            a, b = c, c + dt.timedelta(days=1)
        return _mk(a, b, f"around {_fmt(c)} ({_span_label(a, b)})")

    # the last N units up to today: "last 12 months", "letzte drei Wochen", "past 2 years"
    if len(w) == 3 and w[0] in _LAST and w[2] in _UNITS:
        n = int(w[1]) if w[1].isdigit() else _NUM.get(w[1])
        if n is not None and 0 < n <= 100:
            u = _UNITS[w[2]]
            a = (_add_months(today, -12 * n) if u == "y" else _add_months(today, -n)
                 if u == "m" else today - dt.timedelta(weeks=n if u == "w" else 0,
                                                       days=n if u == "d" else 0))
            b = today + dt.timedelta(days=1)
            unit = {"y": "year", "m": "month", "w": "week", "d": "day"}[u]
            return _mk(a, b, f"the last {n} {unit}{'' if n == 1 else 's'} "
                             f"({_span_label(a, b)})")

    # early / mid / late <year | month [year] | season [year]>
    part = None
    if w[0] in _EARLY and len(w) >= 2:
        part = _EARLY[w[0]]
        w = w[1:]

    base = _named_period(w, today)
    if base is None:
        # weekdays: "letzten dienstag", "dienstag"
        if 1 <= len(w) <= 2 and w[-1] in _WEEKDAYS and (len(w) == 1 or w[0] in _LAST):
            back = (today.weekday() - _WEEKDAYS[w[-1]]) % 7 or 7
            d = today - dt.timedelta(days=back)
            return _day(d.year, d.month, d.day, f"{calendar.day_name[d.weekday()]}, {_fmt(d)}")
        if fallback:
            return _dateparser(raw, now)
        return None
    a, b, label = base
    if part is not None:
        days = (b - a).days
        third = days // 3
        if part == 0:
            b = a + dt.timedelta(days=third)
            label = f"early {label}"
        elif part == 1:
            a, b = a + dt.timedelta(days=third), a + dt.timedelta(days=days - third)
            label = f"mid {label}"
        else:
            a = a + dt.timedelta(days=days - third)
            label = f"late {label}"
    return _mk(a, b, f"{label} ({_span_label(a, b)})" if "(" not in label and
               not re.fullmatch(r"(early |mid |late )?\d{4}", label) else label)


def _add_months(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _day(y: int, m: int, d: int, label: str | None = None) -> When | None:
    try:
        a = dt.date(y, m, d)
    except ValueError:
        return None
    return _mk(a, a + dt.timedelta(days=1), label or _fmt(a))


def _year_of(tok: str) -> int | None:
    if re.fullmatch(r"(19|20)\d\d", tok):
        return int(tok)
    if re.fullmatch(r"'?\d\d", tok):
        return 2000 + int(tok.strip("'"))
    return None


def _named_period(w: list[str], today: dt.date) -> tuple[dt.date, dt.date, str] | None:
    """[last|this] <month|season|holiday> [year] and <year>."""
    if not w:
        return None
    if len(w) == 1 and re.fullmatch(r"(19|20)\d\d", w[0]):
        y = int(w[0])
        return dt.date(y, 1, 1), dt.date(y + 1, 1, 1), str(y)
    qual = None
    if w[0] in _LAST:
        qual, w = "last", w[1:]
    elif w[0] in _THIS:
        qual, w = "this", w[1:]
    if not w:
        return None
    name = w[0]
    year = None
    # "winter 2020/21" and "winter 2020 21" name the winter that starts in 2020
    if len(w) == 2:
        m = re.fullmatch(r"((?:19|20)\d\d)/\d\d", w[1])
        year = int(m[1]) if m else _year_of(w[1])
        if year is None:
            return None
    elif len(w) == 3:
        year = _year_of(w[1])
        if year is None or _year_of(w[2]) is None:
            return None
    elif len(w) > 3:
        return None
    if qual and year is not None:
        return None

    if name in _MONTHS:
        mo = _MONTHS[name]
        if year is None:
            y = today.year if mo <= today.month else today.year - 1
            if qual == "last" and mo == today.month:
                y -= 1
            if qual == "this":
                y = today.year
            year = y
        a, b = _month_range(year, mo)
        return a, b, f"{calendar.month_name[mo]} {year}"
    if name in _SEASONS:
        m0 = _SEASONS[name]
        if year is None:
            # the season in progress or the last one that began
            y = today.year
            start = dt.date(y, m0, 1)
            if m0 == 12 and today.month <= 2:
                start = dt.date(y - 1, 12, 1)
            if start > today:
                start = dt.date(start.year - 1, m0, 1)
            end = _season(start.year, m0)[1]
            if qual == "last" and end > today:          # in it: the one before
                start = dt.date(start.year - 1, m0, 1)
            year = start.year
        a, b = _season(year, m0)
        name_en = _SEASON_NAME[m0]
        lab = f"{name_en} {year}" if m0 != 12 else f"winter {year}/{(year + 1) % 100:02d}"
        return a, b, lab
    hol_name = name
    probe = _holiday(hol_name, today.year)
    if probe is not None:
        if year is None:
            a, b, lab = probe
            if a > today or qual == "last" and b > today:
                a, b, lab = _holiday(hol_name, today.year - 1)
            return a, b, lab
        return _holiday(hol_name, year)
    return None


_DP_LOCK = threading.Lock()


def _dateparser(text: str, now: dt.datetime) -> When | None:
    """Anything with a digit or a month name the rules did not take: one day."""
    # only what looks like a day: a month name, or day.month / month/day numbers. A
    # year next to a word ("2021 auto", while typing after before:) must not reach
    # dateparser, which costs ~1 ms warm and ~200 ms on its first call.
    norm = N.normalize(text)
    words = norm.split()
    if len(words) > 4:
        return None
    if not any(t.strip(".,") in _MONTHS for t in words) and \
            not re.search(r"\b\d{1,2}[./]\d{1,2}\b", text):
        return None
    import dateparser
    with _DP_LOCK:
        d = dateparser.parse(text, languages=["de", "en"], settings={
            "PREFER_DATES_FROM": "past", "RELATIVE_BASE": now,
            "REQUIRE_PARTS": ["day", "month"], "DATE_ORDER": "DMY"})
    if d is None:
        return None
    return _day(d.year, d.month, d.day)


# ---- people, chats, kbs -----------------------------------------------------------------------

class _NameCache:
    """Per chat kb: people and chats with their normalised names.

    A cheap fingerprint of the table (row count, max id, total length of the name
    columns; under 1 ms) is checked on every lookup, so a new person, a new chat or a
    rename is seen at once. Message counts (only used to order candidates) cost 20-40
    ms on a big kb; they are reloaded in a background thread once older than TTL."""
    TTL = 60.0

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], tuple[tuple, list[dict[str, Any]]]] = {}
        self._counts: dict[str, tuple[float, dict[int, int]]] = {}
        self._loading: set[str] = set()

    def get(self, kb, what: str) -> list[dict[str, Any]]:
        key = (kb.id, what)
        fp = kb.file.read_sync(lambda c: tuple(c.execute(_FINGERPRINT[what]).fetchone()))
        with self._lock:
            hit = self._data.get(key)
        if hit is None or hit[0] != fp:
            rows = _load_people(kb) if what == "people" else _load_chats(kb)
            with self._lock:
                self._data[key] = (fp, rows)
        else:
            rows = hit[1]
        if what == "people":
            counts = self._sender_counts(kb)
            for r in rows:
                r["messages"] = counts.get(r["id"], 0)
        return rows

    def _sender_counts(self, kb) -> dict[int, int]:
        with self._lock:
            hit = self._counts.get(kb.id)
            fresh = hit is not None and time.monotonic() - hit[0] < self.TTL
            if hit is not None and not fresh and kb.id not in self._loading:
                self._loading.add(kb.id)
                threading.Thread(target=self._load_counts, args=(kb,), daemon=True,
                                 name=f"names-{kb.id}").start()
            if hit is not None:
                return hit[1]
        return self._load_counts(kb)

    def _load_counts(self, kb) -> dict[int, int]:
        try:
            counts = kb.file.read_sync(lambda c: {r[0]: r[1] for r in c.execute(
                "SELECT sender_id, COUNT(*) FROM messages WHERE sender_id IS NOT NULL "
                "GROUP BY sender_id")})
            with self._lock:
                self._counts[kb.id] = (time.monotonic(), counts)
            return counts
        finally:
            with self._lock:
                self._loading.discard(kb.id)

    def warm(self, kb) -> None:
        for what in ("people", "chats"):
            self.get(kb, what)


_FINGERPRINT = {
    "people": "SELECT COUNT(*), MAX(id), TOTAL(LENGTH(COALESCE(custom_name, '') || "
              "COALESCE(contact_name, '') || COALESCE(display_name, '') || "
              "COALESCE(push_name, '') || COALESCE(username, '') || COALESCE(phone, ''))) "
              "FROM people",
    "chats": "SELECT COUNT(*), MAX(id), TOTAL(LENGTH(COALESCE(custom_name, '') || "
             "COALESCE(name, '') || COALESCE(parent, ''))), SUM(message_count > 0), "
             "SUM(trashed), MAX(person_id) FROM chats",
}

_names = _NameCache()


def _load_people(kb) -> list[dict[str, Any]]:
    from . import names

    def run(conn):
        out = []
        for r in conn.execute("SELECT * FROM people WHERE is_me = 0"):
            forms = set()
            for col in ("custom_name", "contact_name", "display_name", "push_name",
                        "username"):
                v = r[col]
                if v and str(v).strip():
                    forms.add(" ".join(N.tokens(v)))
            phone = "".join(ch for ch in (r["phone"] or "") if ch.isdigit())
            name, source = names.resolve(r)
            out.append({"kb": kb.id, "id": r["id"], "name": name, "source": source,
                        "forms": [f for f in forms if f], "phone": phone, "messages": 0})
        return out
    return kb.file.read_sync(run)


def _load_chats(kb) -> list[dict[str, Any]]:
    res = kb.list_chats(limit=100_000)
    out = []
    for c in res["chats"]:
        forms = {" ".join(N.tokens(c["name"] or ""))}
        if c.get("parent"):
            forms.add(" ".join(N.tokens(f"{c['parent']} {c['name']}")))
        out.append({"kb": kb.id, "id": c["id"], "name": c["name"], "kind": c["kind"],
                    "parent": c.get("parent"), "forms": [f for f in forms if f],
                    "messages": c["messages"]})
    return out


def _match(rows: list[dict[str, Any]], name: str) -> tuple[str, list[dict[str, Any]]]:
    toks = N.tokens(name)
    if not toks:
        return "none", []
    q = " ".join(toks)
    sq = "".join(toks)
    digits = "".join(ch for ch in name if ch.isdigit())
    exact, prefix = [], []
    for r in rows:
        forms = r["forms"]
        if any(f == q or f.replace(" ", "") == sq for f in forms):
            exact.append(r)
        elif any(f.startswith(q) or f.replace(" ", "").startswith(sq)
                 or any(part.startswith(q) for part in f.split()) or
                 (" " in q and q in f) for f in forms):
            prefix.append(r)
        elif len(digits) >= 5 and digits in (r.get("phone") or ""):
            prefix.append(r)
    if exact:
        return "exact", exact
    if prefix:
        return "prefix", prefix
    if len(sq) >= 4:
        from rapidfuzz.distance import OSA
        fuzzy = [r for r in rows if any(
            OSA.distance(sq, part, score_cutoff=1) <= 1
            for f in r["forms"] for part in f.split() + [f.replace(" ", "")])]
        if fuzzy:
            return "fuzzy", fuzzy
    return "none", []


def _resolve(kbs, name: str, what: str, kb_filter: list[str] | None) -> Resolution:
    from .chat import ChatKB
    res = Resolution(name=name)
    per_kb: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for kb in kbs:
        if not isinstance(kb, ChatKB) or (kb_filter and kb.id not in kb_filter):
            continue
        level, found = _match(_names.get(kb, what), name)
        if found:
            per_kb[kb.id] = (level, found)
    if not per_kb:
        return res
    order = {"exact": 0, "prefix": 1, "fuzzy": 2}
    best = min((level for level, _ in per_kb.values()), key=lambda x: order[x])
    # keep only kbs that matched at the best level, so an exact "Alex" in one kb is
    # not diluted by prefix matches ("Alexander") in another
    kept = {kid: found for kid, (level, found) in per_kb.items() if level == best}
    cands = [c for found in kept.values() for c in found]
    cands.sort(key=lambda c: -(c.get("messages") or 0))
    res.match = best
    res.candidates = [{k: v for k, v in c.items() if k not in ("forms", "phone")}
                      | {"ref": f"{c['kb']}:{c['id']}"} for c in cands[:20]]
    res.ambiguous = best == "fuzzy" or any(len(f) > 1 for f in kept.values())
    res.refs = [f"{c['kb']}:{c['id']}" for c in cands[:50]]
    return res


def resolve_people(kbs, name: str, *, kb: list[str] | None = None,
                   identities=None) -> Resolution:
    """A name, part of a name or phone digits -> people. Exact name matches win over
    prefixes, prefixes over one-typo matches (which are always ambiguous).

    With `identities` (kb/identity.py) a match is a person: an account marked as the
    same person as others brings them all, and the candidates are one per person. A
    same person's name and every alias match exactly, and beat a prefix or a typo."""
    from .chat import ChatKB
    kbs = list(kbs)
    res = _resolve(kbs, name, "people", kb)
    if identities is None or not identities.by_id:
        return res
    known = {k.id for k in kbs if isinstance(k, ChatKB) and (not kb or k.id in kb)}
    named = [i for i in identities.named(name)
             if any(_kb_of(m) in known for m in i.members)]
    if named and res.match != "exact":
        res = Resolution(name=name, match="exact")
    named_ids = {i.id for i in named}
    matched = res.refs + [m for i in named for m in sorted(i.members) if _kb_of(m) in known]
    if not matched:
        return res

    def person_of(ref: str):
        rec = identities.identity_of(ref)
        return rec if rec is not None and (rec.same_person or rec.id in named_ids) else None

    rows = {k.id: {r["id"]: r for r in _names.get(k, "people")}
            for k in kbs if k.id in known}
    refs: list[str] = []
    per_kb: dict[str, set[str]] = {}
    found: dict[str, list[dict[str, Any]]] = {}
    for ref in matched:
        rec = person_of(ref)
        key = f"person:{rec.id}" if rec else ref
        per_kb.setdefault(_kb_of(ref), set()).add(key)
        if key in found:
            continue
        found[key] = []
        for r in sorted(rec.members if rec else {ref}):
            k, _, pid = r.partition(":")
            row = rows.get(k, {}).get(int(pid))
            if row is not None:
                refs.append(r)
                found[key].append(row)
    cands = []
    for key, accts in found.items():
        if not accts:
            continue
        first = accts[0]
        ref = f"{first['kb']}:{first['id']}"
        rec = identities.same_person(ref)
        c = {"kb": first["kb"], "id": first["id"], "ref": ref,
             "messages": sum(a["messages"] for a in accts)}
        cands.append({**c, "name": rec.name} if rec is not None
                     else {**c, "name": first["name"], "source": first["source"]})
    res.refs = refs
    res.candidates = sorted(cands, key=lambda c: -c["messages"])[:20]
    res.ambiguous = res.match == "fuzzy" or any(len(v) > 1 for v in per_kb.values())
    return res


def _kb_of(ref: str) -> str:
    return ref.partition(":")[0]


def resolve_chats(kbs, name: str, *, kb: list[str] | None = None) -> Resolution:
    """A chat name or part of it (Discord: `server channel` works) -> chats."""
    return _resolve(kbs, name, "chats", kb)


def resolve(query: Query, kbs, identities=None) -> Query:
    """Fill refs and candidates for from:/in:, validate kb:. `kbs` is an iterable of
    knowledge bases (ChatKB and DocumentsKB); `identities` the person records."""
    kbs = list(kbs)
    known = {k.id for k in kbs}
    if query.kbs:
        # kb:documents means the Documents folder, whatever its block is called
        nk = next((k.id for k in kbs if getattr(k, "kind", "") == "documents"), None)
        if nk:
            query.kbs = [nk if k == "documents" else k for k in query.kbs]
        bad = [k for k in query.kbs if k not in known]
        for chip in query.chips:
            if chip.kind == "kb" and chip.label in bad:
                chip.error = f"no source called {chip.label!r}"
    for i, r in enumerate(query.people):
        query.people[i] = resolve_people(kbs, r.name, kb=query.kbs, identities=identities)
    for i, r in enumerate(query.chats):
        query.chats[i] = resolve_chats(kbs, r.name, kb=query.kbs)
    people = iter(query.people)
    chats = iter(query.chats)
    for chip in query.chips:
        if chip.error:
            continue
        if chip.kind == "from" and chip.label != "me":
            _fill_chip(chip, next(people))
        elif chip.kind == "in":
            _fill_chip(chip, next(chats))
    return query


def _fill_chip(chip: Chip, r: Resolution) -> None:
    chip.refs = r.refs
    chip.candidates = r.candidates
    chip.ambiguous = r.ambiguous
    if not r.refs:
        chip.error = f"no match for {r.name!r}"
    elif not r.ambiguous and r.candidates:
        names = sorted({c["name"] for c in r.candidates})
        chip.label = names[0] if len(names) == 1 else ", ".join(names[:3])


def understand(q: str, kbs, *, now: dt.datetime | None = None, identities=None) -> Query:
    return resolve(parse(q, now=now), kbs, identities)


def warm(kbs) -> None:
    """Load what the first operator or typo query would otherwise wait for: the name
    caches, the typo vocabulary and dateparser. Called from a background thread at startup."""
    from .chat import ChatKB
    for kb in kbs:
        if isinstance(kb, ChatKB):
            try:
                _names.warm(kb)
                if kb.search_ready:
                    kb.ix_vocab()          # the typo vocabulary (~150 ms to load)
            except Exception:
                log.debug("kb warm failed", exc_info=True, extra={"event": "kb.warm_failed"})
    _dateparser("12. März 2021", dt.datetime.now())
