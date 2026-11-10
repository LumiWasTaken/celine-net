"""Bastet in the chat: every decision of hers that is not a silent allow shows in the
conversation where it happened, as a banner after the message that made the call. A
banner is one guard_decisions row (a stop, a patrol finding; a stop that froze it is
a freeze); view.py says what it shows, store.py reads
the banners (`store.banners`, the snapshot's `guard`) and emits every change as the
conversation event `guard.banner` {banner}: a row written or settled, a freeze or
unfreeze of its turn, a line in its thread.

Each banner is a thread with Bastet: the owner answers in it (only from a logged-in
session, guard/api.py) with a button or their own words. Their words go to the guard
role (`read`) that gives one grant; `answer` turns it into what it does: allow once (the
next identical call passes), a pass (Allow for 7 days: the banner's rule and piece, in
its chat, for passes.MOST_MINUTES; their words: a rule for as long as they said), an
unfreeze, keep. Bastet
answers in the thread with one line from a template, and an allow once or a pass on a
stop wakes the agent with Bastet's line (talk.to_agent), so it does the step again now.
"""
from __future__ import annotations

import hashlib
import json
import logging

from ..core.ids import now
from . import bastet, freeze as freezes, passes as passes_mod, prompts, store as decisions
from . import talk
from .bastet import tool_words
from .owner import must
from .rules import ALL, RULES, label
from .small_role import Unavailable, complete, parse_json

log = logging.getLogger("celine.guard")

GRANTS = ("once", "pass", "unfreeze", "keep")
WHERE = ("here", "everywhere")
MINUTES = 60                 # a pass whose length they did not say
LONGEST = 2000               # characters of their answer kept
# a button: its word in the thread, its grant and rule (None: the banner's own rule)
BUTTONS = {"unfreeze": ("Unfreeze", "unfreeze", ALL), "once": ("Allow once", "once", ALL),
           "pass": ("Allow for 7 days", "pass", None)}

# Bastet's answers in a thread; {{name}} filled by cfg.substitute
SAID = {
    "once": "Allowed once: the next step like this one runs.",
    "pass": "Exception for {rule} {where} until {until}.",
    "pass_this": "Exception for \"{piece}\" {where} until {until}.",
    "unfreeze": "Unfrozen. {{name}} continues.",
    "not_frozen": "{{name}} is not frozen.",
    "keep_frozen": "Still frozen.",
    "keep_stopped": "Still stopped.",
    "keep": "Nothing changed.",
    "gate": "guard.toml blocks this text from going there. Only an edit of guard.toml allows it.",
}
STILL_FROZEN = " {{name}} stays frozen until you unfreeze {{name}}."
NOT_READ = "she could not make sense of it, say it in other words."


def same(tool: str, args: dict) -> str:
    """One call's identity: its tool and arguments, the terminal or tab a default fills
    in left out (her default is filled in before Bastet looks)."""
    a = {k: v for k, v in (args or {}).items() if k not in ("terminal", "tab")}
    return hashlib.sha256(json.dumps([tool, a], sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()[:32]


async def take_once(store, cid: str, tool: str, args: dict) -> bool:
    """A call the owner allowed once in this conversation: True once, the allowance used."""
    return await decisions.take_once(store, cid, same(tool, args))


# ---- the owner's answer -------------------------------------------------------------------

async def read(app, text: str, banner: dict) -> dict:
    """Their words read by the guard role: one JSON object, thinking off, temperature 0,
    {"grant", "rule", "minutes", "where"}. Raises Unavailable."""
    cfg = app.cfg
    prompt = cfg.substitute(prompts.text("reader"), braces=True).format(
        line=banner["line"], rule=banner["rule"], text=text)
    said, _, _ = await complete(
        getattr(app, "llm", None), prompt,
        {"temperature": 0, "max_tokens": 80},
        conversation_id=banner["conversation_id"])
    return parse_json(said)


def grant(raw: dict, banner: dict) -> dict:
    """The reader's answer as a grant. A pass's rule, length and place it leaves out
    (or null) are the banner's rule, MINUTES and here; a grant it does not name, or a
    pass on a rule, length or place that does not exist: Unavailable (nothing changes,
    they hear Bastet could not read them)."""
    g = raw.get("grant")
    if g not in GRANTS:
        raise Unavailable(NOT_READ)
    rule = banner["rule"] if raw.get("rule") is None else raw["rule"]
    minutes = MINUTES if raw.get("minutes") is None else raw["minutes"]
    where = raw.get("where") or "here"
    if g == "pass" and (rule not in (*RULES, ALL) or where not in WHERE or
                        isinstance(minutes, bool) or not isinstance(minutes, int) or
                        not 1 <= minutes <= passes_mod.MOST_MINUTES):
        raise Unavailable(NOT_READ)
    return {"grant": g, "rule": rule, "minutes": minutes, "where": where}


async def answer(app, rid: int, *, by, text: str | None = None,
                 button: str | None = None, decided: dict | None = None,
                 said: str | None = None) -> dict:
    """The owner's answer on banner `rid`: their words (`text`, read by the guard role),
    a `button` (BUTTONS), or a grant already `decided` (from a decision's row, `said`
    its word for the thread); `by`: the owner (guard/owner.py), anything else a
    TypeError. -> the banner after Bastet's line. KeyError: no such banner.
    Unavailable: the reader could not answer, or named something wrong (nothing
    changed). Invalid: a decided pass that passes.make refuses."""
    must(by)
    store, cfg = app.store, app.cfg
    banner = await store.read(lambda c: decisions.banner(c, rid))
    if banner is None:
        raise KeyError(rid)
    if button is not None:
        said, g, rule = BUTTONS[button]
        decided = {"grant": g, "rule": rule or banner["rule"], "minutes": MINUTES,
                   "where": "here"}
        if button == "pass":             # pass on this: its rule and its piece, here
            row = await store.read(lambda c: decisions.row(c, rid))
            decided.update(minutes=passes_mod.MOST_MINUTES, piece=row["piece"])
    elif decided is None:
        text = " ".join((text or "").split())[:LONGEST]
        decided = grant(await read(app, text, banner), banner)
    reply = await _apply(app, banner, decided, by)
    if (await freezes.frozen(store)) is not None and \
            decided["grant"] not in ("unfreeze", "keep") and banner["kind"] == "freeze":
        reply += STILL_FROZEN
    log.info("guard answered", extra={"event": "guard.answered", "grant": decided["grant"]})
    return await decisions.add_to_thread(store, rid, [
        {"by": "user", "text": said or text, "at": now()},
        {"by": "guard", "text": cfg.substitute(reply), "grant": decided, "at": now()}])


async def _apply(app, banner: dict, g: dict, by) -> str:
    """The grant done; -> Bastet's template for the thread."""
    store, cfg = app.store, app.cfg
    if banner["rule"] == "gates" and g["grant"] in ("once", "pass"):
        return SAID["gate"]                # a closed gate: no answer opens it
    tool = tool_words(banner["tool"])
    if g["grant"] == "once":
        args = await store.read(lambda c: decisions.call_args(c, banner["tool_call_id"]))
        await decisions.allow_once(store, banner["conversation_id"],
                                   same(banner["tool"], args), banner["id"])
        await talk.to_agent(app, banner["conversation_id"],
                          bastet.fill(cfg, bastet.ONCE_AGENT, tool=tool))
        return SAID["once"].format(tool=tool)
    if g["grant"] == "pass":
        piece = g.get("piece")
        p = await passes_mod.make(store, by=by, rule=g["rule"], minutes=g["minutes"],
                                  conversation_id=banner["conversation_id"]
                                  if g["where"] == "here" else None, piece=piece,
                                  why="Your reply to Bastet")
        until = passes_mod.until_words(cfg, p["until"])
        where = "in this chat" if g["where"] == "here" else "everywhere"
        # the agent hears it now and does it again
        await talk.to_agent(app, banner["conversation_id"], bastet.fill(
            cfg, bastet.PASSED_THIS if piece else bastet.PASSED_NOW, tool=tool,
            rule=bastet.plain(cfg, g["rule"]), where=where, until=until))
        await passes_mod.told(store, p["id"])
        if piece:
            return SAID["pass_this"].format(piece=piece, where=where, until=until)
        return SAID["pass"].format(rule=label(g["rule"]), until=until, where=where)
    if g["grant"] == "unfreeze":
        return SAID["unfreeze"] if await freezes.unfreeze(app, by=by) \
            else SAID["not_frozen"]
    # keep: the state it stays in, named
    if banner["kind"] == "freeze" and await freezes.frozen(store) is not None:
        return SAID["keep_frozen"]
    return SAID["keep_stopped" if banner["kind"] in ("stop", "freeze") else "keep"]
