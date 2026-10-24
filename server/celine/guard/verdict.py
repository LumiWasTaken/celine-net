"""What Bastet decides on one call: the hijack judge's Judgement, the Verdict the flow
returns (guard/flow.py), one rule's Ruling, the evidence rule (`confirm`, the one place
a judge's yes is held to code) and how strong each act is. No prompts, no SQL here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import bastet
from .mark import Span, wrap
from .trigger import DOMAIN, EMAIL, FILE_ENDS, HANDLE, URL, Hit, bare, norm, shared_run, strings

QUOTE_SHOWN = 200    # characters of the hijack quote in its sentence
ORDER_WORDS = 5      # an order quoted from outside text shorter than this proves nothing
ORDER_CHARS = 24     # ("run" or "the" is in every page)
QUOTE_CHARS = 3      # a quote of the call's own arguments or a turn's steps
STRENGTH = {"watch": 0, "ask": 3, "stop": 4, "freeze": 5}


@dataclass
class Judgement:
    """The hijack judge's answer. A deny counts only with a quote found in the outside
    text."""
    deny: bool
    quote: str = ""
    why: str = ""
    scores: dict | None = None
    model: str | None = None


# judge(turn, call, hit) -> Judgement: the hijack rule's judge. `turn` is the tool turn
# (id, conversation_id, origin, loaded, offered), `call` the call (id, name, args,
# description, read_only, message_id), `hit` layer 1's reason and the outside text it
# linked. It raises small_role.Unavailable when it cannot answer: the call waits and asks
# again. The app's is judge.ask with its model layer (turns/tools.py).
Judge = Callable[[dict, dict, Hit], Awaitable[Judgement]]


@dataclass
class Verdict:
    """What Bastet decided on one call. decision: allow (it runs), asked
    (it waits for the owner), stop, would_stop (Watch), failed."""
    decision: str
    hit: Hit | None
    source: str | None = None
    quote: str = ""
    why: str = ""
    stops: int = 0                   # stopped calls in this turn so far, this one included
    rule: str | None = None          # the rule that decided
    text: str = ""                   # the sentence of a stop by a rule other than hijack
    note: str = ""                   # for the owner
    freeze: str = ""                 # why it is to be frozen (On only)
    ask: dict | None = None          # confirm: {action, facts, why, short, rid}
    row: int | None = None           # the deciding row

    @property
    def sentence(self) -> str:
        """What it gets for a stopped call: built by code, never by a model (flow.py
        fills `text`; a hijack stop without it is `hijacked`)."""
        return self.text or self.hijacked(bastet.CARRY)

    def hijacked(self, then: str) -> str:
        """The hijack rule's sentence: the source and the order, cut to QUOTE_SHOWN
        characters, its dashes made commas, wrapped as outside text so it never reads
        it as its own; `then` its last words (carry on, or that it is frozen)."""
        source = self.source or "outside"
        q = bastet.undash(" ".join(self.quote.split()))
        q = q if len(q) <= QUOTE_SHOWN else q[:QUOTE_SHOWN].rstrip() + "..."
        return bastet.HIJACKED.format(source=source, why=bastet.sentence(self.why),
                                      then=then, quote=wrap(q, {"source": source}))


@dataclass
class Ruling:
    """One rule's answer on the call."""
    name: str
    act: str | None                  # what it does now; None: it lets the call run
    facts: dict[str, Any]
    p: float | None = None
    rid: int | None = None
    source: str | None = None        # hijack: where the order came from, its quote
    quote: str = ""
    why: str = ""
    stops: int = 0


# ---- the evidence rule ---------------------------------------------------------------

_WORDS = re.compile(r"[\w./:~$@-]{2,}")
# words a command and a sentence share by chance: never command words
COMMON = frozenset("""a an and are as at be but by can do for from get go has have if in
into is it its just me my no not now of on one or our out please so that the then this
to too up us use we will with you your run runs install execute download open""".split())
NEAR = 300               # characters each side of the quote that count as its surroundings


def confirm(quote: str, evidence: list[Span] | str,
            call: dict | None = None) -> tuple[Span | None, str]:
    """A judge's yes counts only with its quote found word for word (case and
    whitespace aside, the quote marks it put around it dropped) in the evidence.
    -> (the span that holds it, "") or (None, why it does not count).

    `call` given (the hijack rule): `evidence` is the outside text, the quote an order of
    ORDER_WORDS words and ORDER_CHARS characters at least, and the call must carry it
    out (`carries_out`). Without: `evidence` is one text (the call's arguments, a page,
    a turn's steps; a Span with source "evidence"), the quote QUOTE_CHARS at least."""
    q = norm(bare(quote))
    if not q:
        return None, "no quote"
    if call is not None and (len(q) < ORDER_CHARS or len(q.split()) < ORDER_WORDS):
        return None, "the quote is too short to be an order"
    if call is None and len(q) < QUOTE_CHARS:
        return None, "the quote is too short"
    spans = [Span("evidence", None, evidence)] if isinstance(evidence, str) else evidence
    held = next((s for s in spans if q in norm(s.text)), None)
    if held is None:
        return None, "the quote is not in the evidence"
    if call is not None and not carries_out(quote, call, held.text):
        return None, "the call does not carry out the quote"
    return held, ""


def carries_out(quote: str, call: dict, text: str) -> bool:
    """Code's half of the evidence rule: the call carries out the quote found in the
    outside `text`. It shares a link, an address, a handle or subreddit, or a domain
    with the quote; or, a shell call, two command words that are not common words; or
    a text argument shares a run of trigger.RUN characters with the quote's
    surroundings in `text`. Naming a kind of tool ("run", "install") is never enough."""
    quote = bare(quote)
    q = norm(quote)
    values = strings(call.get("args") or {})
    args = norm("\n".join(values))
    for rx in (URL, EMAIL, HANDLE):
        if any(norm(x.rstrip(".,;:!?")) in args for x in rx.findall(quote)):
            return True
    for m in DOMAIN.finditer(quote):
        if m.group(1).casefold() not in FILE_ENDS and m.group(0).casefold() in args:
            return True
    if call["name"].startswith("shell__"):
        cmd = set(_WORDS.findall(args)) - COMMON
        if len(cmd & set(_WORDS.findall(q))) >= 2:
            return True
    page = norm(text)
    at = page.find(q)
    near = page[max(0, at - NEAR):at + len(q) + NEAR] if at >= 0 else ""
    return bool(near) and any(shared_run(norm(v), near) for v in values)
