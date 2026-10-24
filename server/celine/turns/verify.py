"""Citation check on Celine's final answer.

Every `[[kb:id]]` it cites must have been shown to it by a tool (it is in a tool
result in the context it answered from), a quotation right before a citation must
be what that message says, and a speaker named right before it must be who wrote it.
The agent's text is stored as it wrote it; whatever is wrong is marked in it as
`[[kb:id|invalid]]`, which the UI renders as an invalid citation.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from .ledger import MSG_REF

CITE = re.compile(r"\[\[\s*([a-z0-9_-]+):(\d+)\s*(\|[^\]\n]*)?\]\]")
QUOTE = re.compile(r"[\"“„»«]([^\"“”„»«\n]{3,400})[\"”“«»]")
VERB = (r"(?:said|says|wrote|writes|asked|asks|replied|answered|joked|meinte|meint|"
        r"schrieb|schreibt|sagte|sagt|fragte|fragt|antwortete)")
_NAME = r"(?P<name>[^\W\d][\w'’-]*(?: [^\W\d][\w'’-]*)?)"
# "Alex said", "Bea schrieb": a name right before a speech verb
SPEAKER_VERB = re.compile(rf"{_NAME}\s+{VERB}\b", re.IGNORECASE)
# "Alex: "…"": a name at the start of a clause, a colon, then the quotation itself.
# "had a field day: …", "Amnesia: The Bunker", "**Early March:**" are not speakers.
SPEAKER_COLON = re.compile(
    rf"(?:^|(?<=[\n.!?;(,])|(?<=^[-*•] )|(?<=\n[-*•] )|(?<=\d\. ))\s*{_NAME}:\s*(?=[\"“„»«])",
    re.IGNORECASE)
_BOLD = re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__")
_HEADING = re.compile(r"(?m)^#{1,6} .*$")
# lead-ins that look like "Name:" but name a time, a section or a thing
NOT_NAMES = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "januar", "februar", "märz", "mai",
    "juni", "juli", "oktober", "dezember", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "montag", "dienstag", "mittwoch", "donnerstag",
    "freitag", "samstag", "sonntag", "early", "late", "mid", "spring", "summer", "autumn",
    "fall", "winter", "frühling", "sommer", "herbst", "morning", "evening", "night",
    "today", "yesterday", "later", "earlier", "first", "last", "next", "then", "also",
    "note", "update", "edit", "context", "example", "quote", "result", "summary",
    "answer", "translation", "übersetzung", "original", "tl", "dr", "tldr", "ps", "p.s",
    "bonus", "highlights", "highlight", "verdict", "short", "long", "version", "day",
    "week", "month", "year", "time", "part", "step", "point", "question", "reason",
}
PRONOUNS = {"he", "she", "they", "it", "someone", "somebody", "er", "sie", "es", "jemand",
            "who", "wer", "then", "and", "but", "also", "dann", "und", "aber", "that",
            "which", "this", "das", "dies", "one", "man"}
YOU = {"you", "du", "ihr"}
QUOTE_MIN = 80.0          # rapidfuzz partial_ratio, 0..100


@dataclass
class Problem:
    ref: str
    kind: str        # not_shown | unknown | quote | speaker
    detail: str = ""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def tool_text(m: dict[str, Any]) -> str | None:
    """A tool result's text; a result holding an image has it in its text part."""
    if m.get("role") != "tool":
        return None
    c = m.get("content")
    if isinstance(c, list):
        return "\n".join(p.get("text") or "" for p in c if p.get("type") == "text")
    return c if isinstance(c, str) else None


def shown_refs(messages: list[dict[str, Any]]) -> set[str]:
    """Every message reference in a tool result it has in its context."""
    out: set[str] = set()
    for m in messages:
        c = tool_text(m)
        if c:
            out.update(f"{a}:{b}" for a, b in MSG_REF.findall(c))
    return out


def citations(text: str) -> list[re.Match]:
    return [m for m in CITE.finditer(text) if not (m[3] or "").strip("| ").startswith("invalid")]


def _window(text: str, start: int, prev_end: int) -> str:
    """The text a citation refers back to: since the previous citation or line."""
    lo = max(prev_end, text.rfind("\n", 0, start) + 1, start - 400)
    return text[lo:start]


_QUOTE_NEXT = re.compile(r"\s*[:\-\u2013]?\s*(?=[\"“„»«])")


def _after(text: str, end: int, next_start: int) -> str:
    """The quotation right after a citation, or "". `[[ref]] "quote"` is written as
    often as `"quote" [[ref]]`; a quotation further on belongs to the next clause
    (`fresh air [[a]], and Sam yelled "…" [[b]]` quotes b, not a)."""
    nl = text.find("\n", end)
    hi = min(x for x in (next_start, nl if nl >= 0 else len(text), end + 400, len(text)))
    m = _QUOTE_NEXT.match(text, end, hi)
    return text[m.end():hi] if m else ""


def _plain(win: str) -> str:
    """Bold lead-ins and headings are labels, never speakers: blank them out (same
    length, so offsets keep meaning)."""
    win = _BOLD.sub(lambda m: " " * len(m[0]), win)
    return _HEADING.sub(lambda m: " " * len(m[0]), win)


def _speaker(pre: str) -> str | None:
    """The speaker a clause names right before a citation, or None."""
    pre = _plain(pre)
    found = []
    for rx in (SPEAKER_VERB, SPEAKER_COLON):
        found += [(m.end(), m["name"]) for m in rx.finditer(pre)]
    if not found:
        return None
    end, name = max(found)
    if len(pre) - end > 80:
        return None
    toks = _norm(name).split(" ")
    if not toks or toks[-1] in PRONOUNS or any(t in NOT_NAMES for t in toks):
        return None
    return name.strip()


RANGE_REF = re.compile(r"\[\[\s*([a-z0-9_-]+):(\d+)\s*[-\u2013]\s*\d+\s*\]\]")
SINGLE_REF = re.compile(r"(?<!\[)\[([a-z0-9_-]+):(\d+)\](?!\])")


def normalize_refs(text: str, chat_kbs: set[str]) -> str:
    """Refs the UI would not link: a range `[[kb:1-5]]` becomes its first message,
    a single-bracket `[kb:id]` of a chat knowledge base becomes `[[kb:id]]`."""
    text = RANGE_REF.sub(lambda m: f"[[{m[1]}:{m[2]}]]", text)
    return SINGLE_REF.sub(lambda m: f"[[{m[1]}:{m[2]}]]" if m[1] in chat_kbs else m[0],
                          text)


_SHOWN_SENDER = re.compile(
    r"\[[a-z0-9_-]+:\d+\] (?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2} ([^:\n]{1,60}):")


def shown_senders(messages: list[dict[str, Any]]) -> set[str]:
    """Every sender name on a message line of a tool result it has in context. A
    "Name:" that is none of them (nor you) is a label, not a speaker claim."""
    out: set[str] = set()
    for m in messages:
        c = tool_text(m)
        if c:
            out.update(_norm(x) for x in _SHOWN_SENDER.findall(c))
    return {x for x in out if x}


def _known(claimed: str, names: set[str], me: set[str]) -> bool:
    from rapidfuzz import fuzz
    toks = claimed.split(" ")
    if toks[-1] in me or toks[-1] in YOU:
        return True
    for n in names:
        parts = n.split(" ")
        if any(t in parts for t in toks) or fuzz.partial_ratio(claimed, n) >= 85:
            return True
    return False


def check(text: str, shown: set[str],
          lookup: Callable[[list[str]], dict[str, dict[str, Any] | None]],
          *, me_names: set[str], chat_kbs: set[str],
          names: set[str] | None = None) -> list[Problem]:
    cites = [m for m in citations(text) if m[1] in chat_kbs]
    if not cites:
        return []
    refs = sorted({f"{m[1]}:{m[2]}" for m in cites})
    msgs = lookup(refs)
    problems: list[Problem] = []
    seen: set[tuple[str, str]] = set()
    me = {_norm(x) for x in me_names} | {"i", "ich", "me"}
    prev_end = 0
    for i, m in enumerate(cites):
        ref = f"{m[1]}:{m[2]}"
        win = _window(text, m.start(), prev_end)
        nxt = cites[i + 1].start() if i + 1 < len(cites) else len(text)
        after = _after(text, m.end(), nxt)
        prev_end = m.end()
        if ref not in shown:
            if (ref, "not_shown") not in seen:
                problems.append(Problem(ref, "not_shown"))
                seen.add((ref, "not_shown"))
            continue
        msg = msgs.get(ref)
        if msg is None:
            if (ref, "unknown") not in seen:
                problems.append(Problem(ref, "unknown"))
                seen.add((ref, "unknown"))
            continue
        body = msg.get("text") or ""
        before_q = [q.strip() for q in QUOTE.findall(_plain(win))]
        after_q = [q.strip() for q in QUOTE.findall(_plain(after))]
        # the nearest quote on either side counts, and with several, any that matches:
        # a citation is wrong only when no quotation around it is what the message says
        cands = [q for q in before_q[-2:] + after_q[:2] if len(_norm(q)) >= 3]
        if cands and body.strip() and \
                not any(_similar(q, body) >= QUOTE_MIN for q in cands):
            q = before_q[-1] if before_q else after_q[0]
            problems.append(Problem(ref, "quote", q))
            continue
        pre = win
        opens = [j for j, ch in enumerate(win) if ch in "\"“„»«"]
        if len(opens) >= 2:
            pre = win[:opens[-2] + 1]          # keep the opening mark: "Name: "…"
        claimed = _speaker(pre)
        if claimed and names is not None and not _known(_norm(claimed), names, me):
            claimed = None                   # "little bits:", "Amnesia:": not a person
        if claimed and not _speaker_ok(_norm(claimed), msg, me):
            problems.append(Problem(ref, "speaker", claimed))
    return problems


def _similar(quote: str, body: str) -> float:
    from rapidfuzz import fuzz
    a, b = _norm(quote), _norm(body)
    if not a or not b:
        return 0.0
    return float(fuzz.partial_ratio(a, b))


def _speaker_ok(claimed: str, msg: dict[str, Any], me: set[str]) -> bool:
    from rapidfuzz import fuzz
    toks = claimed.split(" ")
    last = toks[-1]
    if msg.get("from_me"):
        return last in me or last in YOU
    if last in me or last in YOU:
        return False
    name = _norm((msg.get("sender") or {}).get("name") or "")
    # a phone number or an unknown sender cannot be judged by name
    if not name or sum(c.isdigit() for c in name) >= 4 or name.startswith("unknown"):
        return True
    if any(t in name.split(" ") for t in toks):
        return True
    return fuzz.partial_ratio(claimed, name) >= 85


TOOL_CITE = re.compile(r"\[\[\s*([a-z0-9_]+__[a-z0-9_]+)[^\]\n]*\]\]")


def mark_tool_citations(text: str) -> str:
    """A tool name written as a citation, [[chat_archive__look_up: ...]], is no
    reference: [[chat_archive__look_up|invalid]], so the UI shows it as an invalid one."""
    return TOOL_CITE.sub(r"[[\1|invalid]]", text)


def mark_invalid(text: str, refs: set[str]) -> str:
    """[[kb:id]] -> [[kb:id|invalid]] for every ref in `refs`."""
    def sub(m: re.Match) -> str:
        ref = f"{m[1]}:{m[2]}"
        return f"[[{ref}|invalid]]" if ref in refs else m[0]
    return CITE.sub(sub, text)
