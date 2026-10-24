"""Bastet in the chat: every decision of hers that is not a silent allow shows in the
conversation where it happened, as a banner after the message that made the call. A
banner is one guard_decisions row (a stop, a confirm ask and how it ended, a patrol
finding; a stop that froze it is a freeze); view.py says what it shows, store.py reads
the banners (`store.banners`, the snapshot's `guard`) and emits every change as the
conversation event `guard.banner` {banner}: a row written or settled, a freeze or
unfreeze of its turn, a line in its thread.

Each banner is a thread with Bastet: the owner answers in it (only from a logged-in
session, guard/api.py) with a button or their own words. Their words go to the small
role (`read`) that gives one grant; `answer` turns it into what it does: allow once (an
ask waiting for them is approved, else the next identical call passes), a pass, an
unfreeze, keep (an ask waiting is denied). Bastet answers in the thread with one line
from a template.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

from ..clock import zone
from ..core.ids import now
from . import freeze as freezes, passes as passes_mod, prompts, store as decisions
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
BUTTONS = {"approve": ("Approve", "once", ALL), "deny": ("Deny", "keep", ALL),
           "unfreeze": ("Unfreeze", "unfreeze", ALL), "once": ("Allow once", "once", ALL),
           "pass": ("Pass", "pass", None)}

# Bastet's answers in a thread; {{name}}, {{them}} filled by cfg.substitute
SAID = {
    "approved": "Allowed this once. It runs now.",
    "once": "Allowed once: the next step like this one runs.",
    "pass": "Pass on {rule} {where} until {until}.",
    "pass_runs": "Pass on {rule} {where} until {until}. It runs now.",
    "gone": "Nothing waits for you here.",
    "unfreeze": "Unfrozen. {{name}} goes on.",
    "not_frozen": "{{name}} is not frozen.",
    "denied": "Denied. It does not run.",
    "keep": "Kept as it is.",
    "gate": "That gate is closed in guard.toml. Only the file opens it.",
}
STILL_FROZEN = " {{name}} stays frozen until you unfreeze {{them}}."
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
    """Their words read by the small role: one JSON object, thinking off, temperature 0,
    {"grant", "rule", "minutes", "where"}. Raises Unavailable."""
    cfg = app.cfg
    prompt = cfg.substitute(prompts.text("reader"), braces=True).format(
        line=banner["line"], rule=banner["rule"], text=text)
    said, _, _ = await complete(
        getattr(app, "llm", None), prompt,
        {"temperature": 0, "max_tokens": 80, "response_format": {"type": "json_object"}},
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
    """The owner's answer on banner `rid`: their words (`text`, read by the small role),
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
    elif decided is None:
        text = " ".join((text or "").split())[:LONGEST]
        decided = grant(await read(app, text, banner), banner)
    reply = await _apply(app, banner, decided, by)
    if (await freezes.frozen(store)) is not None and decided["grant"] != "unfreeze" \
            and banner["kind"] == "freeze":
        reply += STILL_FROZEN
    log.info("guard answered", extra={"event": "guard.answered", "grant": decided["grant"]})
    return await decisions.add_to_thread(store, rid, [
        {"by": "user", "text": said or text, "at": now()},
        {"by": "guard", "text": cfg.substitute(reply), "grant": decided, "at": now()}])


async def _apply(app, banner: dict, g: dict, by) -> str:
    """The grant done; -> Bastet's template for the thread. On an ask: once and a pass
    approve the request that waits, keep denies it; nothing waits any more (answered,
    expired, a restart): the ask is closed, once and keep do nothing."""
    store, cfg = app.store, app.cfg
    approvals = getattr(app, "approvals", None)
    ask = banner["kind"] == "ask"
    waiting = approvals.by_ref(banner["id"]) if approvals is not None and ask else None
    if banner["rule"] == "gates" and g["grant"] in ("once", "pass"):
        return SAID["gate"]                # a closed gate: no answer opens it
    if ask and waiting is None:            # nothing waits: closed, no Approve or Deny
        await decisions.close_ask(store, banner["id"])
    if g["grant"] in ("once", "keep") and ask:
        if waiting is not None and approvals.decide(waiting, g["grant"] == "once", by):
            return SAID["approved" if g["grant"] == "once" else "denied"]
        return SAID["gone"]
    if g["grant"] == "once":
        args = await store.read(lambda c: decisions.call_args(c, banner["tool_call_id"]))
        await decisions.allow_once(store, banner["conversation_id"],
                                   same(banner["tool"], args), banner["id"])
        return SAID["once"].format(tool=tool_words(banner["tool"]))
    if g["grant"] == "pass":
        p = await passes_mod.make(store, by=by, rule=g["rule"], minutes=g["minutes"],
                                  conversation_id=banner["conversation_id"]
                                  if g["where"] == "here" else None,
                                  why=f"answer on Bastet's banner {banner['id']}")
        until = datetime.fromisoformat(p["until"]).astimezone(zone(cfg))
        runs = waiting is not None and approvals.decide(waiting, True, by)
        return SAID["pass_runs" if runs else "pass"].format(
            rule=label(g["rule"]), until=f"{until:%H:%M}",
            where="in this chat" if g["where"] == "here" else "everywhere")
    if g["grant"] == "unfreeze":
        return SAID["unfreeze"] if await freezes.unfreeze(app, by=by) \
            else SAID["not_frozen"]
    return SAID["keep"]
