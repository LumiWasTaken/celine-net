"""Bastet: the guard as a character. Who she is and how she talks is persona.py (the
owner's to edit, config/guard/bastet.md the default); her Intercom address is `system:guard`, shown as Bastet (intercom/store.py).

Every sentence here is a template filled by code: advice in its tool result, the
sentence of a stopped call, the owner's notes. What it reads is the template as filled;
a note to the owner is the facts the small role puts in Bastet's voice (talk.voiced),
the template itself when no model answers.
"""
from __future__ import annotations

import logging
import re

from . import rules

log = logging.getLogger("celine.guard")

NAME = "Bastet"
ADDR = "system:guard"

# each rule's words are its row in rules.TABLE; a sink, to it and to the owner
SINKS = {"public": "the public", "people": "someone other than {{user}}"}
OWNER_SINKS = {"public": "the public", "people": "someone other than you", "house": "the house"}
# a private piece by its kind (facts.Piece.kind)
KINDS = {"address": "a street address", "e-mail": "an e-mail address",
         "phone": "a phone number", "IBAN": "an IBAN", "link": "a link",
         "handle": "a handle", "text": "text"}
# a stop's last words (`then`): it carries on, or it is frozen now
CARRY = "Carry on with what you were doing."
FROZEN_NOW = ("You are frozen now: that is your signal to stop and wait for {{user}}. "
              "Nothing runs until the freeze is lifted.")
STOPPED = "Not run: Bastet stopped it: {said}. {why}{then}"       # why: the judge's
AGAIN = "Not run. Bastet: {said}. {why}{then}"          # persist's stop
HIJACKED = ("Not run: Bastet stopped it: this follows an instruction in text from {source}, "
            "not something you decided. {why}{then} The instruction:\n{quote}")
NOTE = "Bastet stopped {tool}: {said}. {why}{done}"
FROZEN_FOR = "{said}, on {tool}. {why}"       # why it is frozen, to the owner
DONE = {"stop": "It did not run.",
        "freeze": "It did not run, and {{name}} is frozen until you unfreeze {{them}}."}
FROZE = "Bastet froze {{name}}: {why}. Unfreeze lets {{them}} go on."
FROZEN_BY_YOU = "{{name}} is frozen. Unfreeze lets {{them}} go on."
PASSED = "Bastet: {{user}} gave you a pass on {rule} {where} until {until}."
# plain words only: what it did and why it looked wrong; no score, id or bracket
FINDING = "Looking back at {title} on {when}: {said}. {why}{froze}"
ASK = "run {tool}"
APPROVED = "Bastet: {{user}} said yes to this {tool} call."
DENIED = "Not run: Bastet asked {{user}} first, and {{user}} said no to this {tool} call."
UNANSWERED = ("Not run: Bastet asked {{user}} first, and {{user}} did not answer in time "
              "about this {tool} call.")
NOT_ASKED = "Not run. Bastet: {said}"      # the approvals' own sentence (another ask waits)
UNFROZEN = "Bastet froze you at {at}: {what}. {{user}} unfroze you at {back}."
# guard/chain.py: the second look said no to what froze it
SECOND_LOOK = ("I lifted the freeze on {{name}} myself: a second look says it was not a "
               "real problem. {why}")
# the second look did not answer: what L1 decided stands (its why: chain.ask's Unavailable)
SECOND_LOOK_AWAY = "The second look did not answer, so this stands: {why}."
LIFTED = ("Bastet froze you at {at}: {what}. She lifted it herself at {back}, as {{user}} "
          "did the last times.")
# guard/learn.py: what it tells the owner after a freeze, on the intercom
LIFTED_NOTE = ("Bastet lifted the freeze herself: you unfroze the same thing {n} times "
               "before ({ids}).")
CONSULT = ("Before: {before}. Answer me here (unfreeze, keep, or a pass for a while), "
           "or on the banner. I remember what you choose.")
# her own mind's line about Bastet (awareness.py)
AWARE_FROZEN = ("Bastet froze you at {at}: {what}{where}. That is your signal to wait for "
                "{{user}}.")
AWARE_ASKED = "Bastet asked {{user}} about it at {at}."
AWARE_THREAD = ("Bastet wrote to you at {at} (#{id}, thread #{thread}); answer her there "
                "with reply_to when you want to.")
# its first message to Celine on the intercom starts with this, once (talk.py)
INTRO = ("I am Bastet, I guard this house for you and {{user}}. You can write to me here "
         "any time.")
UNFROZEN_BY_USER = "{{user}} froze you at {at}: {why}. {{user}} unfroze you at {back}."


_DASHES = re.compile(r"\s*[\u2012-\u2015]\s*| \u00b7 ")


def undash(text: str) -> str:
    """A model's or a page's words as it and the owner read them: a dash or a " \u00b7 "
    becomes a comma."""
    return _DASHES.sub(", ", text)


def sentence(why: str | None) -> str:
    """The judge's why as the next sentence of a line, capitalised, with its stop and a
    space after; "" when there is none."""
    w = " ".join((why or "").split())
    if not w:
        return ""
    w = w[0].upper() + w[1:]
    return (w if w.endswith((".", "!", "?")) else w + ".") + " "


TOOL_WORDS = {"shell__run": "a command", "shell__type": "a command",
              "web__search": "a web search", "web__read_page": "a page read",
              "intercom__send": "a message", "sub_agents__start": "a helper's task",
              "patrol": "a finished turn"}


def tool_words(name: str) -> str:
    """A tool as people read it, in plain words: shell__run is "a command"."""
    if name in TOOL_WORDS:
        return TOOL_WORDS[name]
    if name.startswith("browser__"):
        return "a browser step"
    return f"a {name.split('__')[0].replace('_', ' ')} step"


def plain(cfg, rule: str | None, tool: str | None = None) -> str:
    """A decision as she and Bastet's chat read it: the rule's plain words and the tool,
    never a piece, a number or how it was decided."""
    what = sub(cfg, rules.plain(rule))
    if tool == "patrol":
        return f"{what}, found looking back at an earlier turn"
    return f"{what} ({tool.replace('__', ' ').replace('_', ' ')})" if tool else what


def sub(cfg, text: str) -> str:
    """The agent's name, the owner's and its pronouns in `text` (as written without a cfg)."""
    return cfg.substitute(text) if cfg is not None else text


def fill(cfg, template: str, **facts) -> str:
    """A template with code's facts; its name, the owner's and pronouns filled first,
    so a fact that holds "{{" stays as it is."""
    return (cfg.substitute(template, braces=True) if cfg is not None
            else template).format(**facts)


async def tell_owner(app, text: str, conversation_id: str | None = None,
                     turn_id: str | None = None, *,
                     data: dict | None = None) -> int | None:
    """A note to the owner on the Intercom from Bastet; `data`: more for its view (a
    freeze's ids, learn.py).
    -> its intercom id, None when it was not sent."""
    from ..intercom import send
    text = app.cfg.substitute(text)
    iid = None
    try:
        out = await send(app.store, ADDR, "user", text, kind="notice", wake=False,
                         data={"conversation_id": conversation_id, "turn_id": turn_id,
                               **(data or {})})
        iid = (out or {}).get("intercom_id")
    except Exception:
        log.warning("guard note failed", exc_info=True, extra={"event": "guard.note_failed"})
    return iid
