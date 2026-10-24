"""A decision row as the owner reads it: its kind, Bastet's line (what she did, then
why), the answers it takes, and the banner it shows as. Plain functions of a row, no
SQL (store.py reads the rows, banners.py takes the owner's answers)."""
from __future__ import annotations

import sqlite3

from .bastet import OWNER_SINKS, sentence, tool_words
from .rules import TABLE, label

# only what acted, in On: a stop, an ask and how it ended, a confirmed patrol finding.
# Watch's "would have" rows and a rule set to watch are rows for Events, never banners
BANNER_DECISIONS = ("found", "stop", "asked", "approved", "denied", "closed")

# what it did, then why: the owner reads everything (rows, facts), Celine none of it
DID = {"stop": "Stopped {tool}.", "freeze": "Stopped {tool} and froze everything.",
       "would_stop": "Would have stopped {tool}.",
       "would_freeze": "Would have stopped {tool} and frozen everything.",
       "ask": "Asks you before {tool}.", "approved": "You approved {tool}.",
       "denied": "You denied {tool}.", "closed": "No answer on {tool}, it did not run.",
       "patrol": "Looked back at this turn.", "gate": "Stopped {tool}.",
       "allow": "Allowed {tool}.", "passed": "Allowed {tool} on your pass.",
       "failed": "Could not check {tool}.", "waiting": "Checking {tool}.",
       "unreachable": "No judge answered on {tool}. It ran."}


def kind(r: sqlite3.Row) -> str:
    """What a row is to the owner: patrol, ask, freeze, would_freeze, else its decision."""
    d = r["decision"]
    if r["does"] == "patrol" or d == "found":
        return "patrol"
    if d == "asked":
        return "ask"
    if d == "stop" and r["froze"]:
        return "freeze"
    if d == "would_stop" and r["froze"]:
        return "would_freeze"
    return d


def shown(r: sqlite3.Row | None) -> bool:
    """A row that is a banner: it acted in On (a closed gate holds in Watch too)."""
    return r is not None and r["decision"] in BANNER_DECISIONS and (
        r["mode"] == "on" or r["rule"] == "gates")


def actions(r: sqlite3.Row) -> list[str]:
    """What the owner can answer on a row from outside its thread (the Events page): "once"
    (allow this call once) and "pass" (a pass on its rule in its conversation). Only a
    row that is a banner, not a closed gate (only guard.toml opens it), not the patrol
    (no call to allow)."""
    if not shown(r) or r["rule"] == "gates" or r["does"] == "patrol":
        return []
    return ["once", "pass"]


def line(r: sqlite3.Row) -> str:
    """What Bastet says to the owner about one row: what she did, then why."""
    k = kind(r)
    facts = {"piece": r["piece"] or "", "source": r["source"] or "outside",
             "quote": r["quote"] or "", "cls": r["class"] or "unclassed",
             "sink": OWNER_SINKS.get(r["sink"] or "", r["sink"] or ""),
             "quoted": f": \"{r['quote']}\"" if r["quote"] else ""}
    # the judge's one sentence, when it confirmed a finding
    judged = "" if k in ("failed", "unreachable") else sentence(r["why"]).strip()
    if k == "patrol":                   # after the fact: no call, no piece to name
        return f"{DID[k]} Found: {label(r['rule'])}. {judged}".strip()
    # it could not decide: the reason is why it could not, not what the rule looks for
    rule = TABLE.get(r["rule"])
    why = (r["why"] or "") if k in ("failed", "unreachable") else \
        (rule.banner.format(**facts) if rule else "")
    did = DID["gate" if r["rule"] == "gates" else k]
    return f"{did.format(tool=tool_words(r['tool']))} {why} {judged}".strip()


def banner(r: sqlite3.Row, thread: list[dict], frozen_turn: str | None) -> dict:
    """The banner of a shown row; `frozen_turn`: the turn of the open freeze."""
    k = kind(r)
    return {"id": r["id"], "conversation_id": r["conversation_id"],
            "message_id": r["message_id"], "tool_call_id": r["tool_call_id"],
            "tool": r["tool"], "rule": r["rule"], "kind": k, "line": line(r),
            "frozen": bool(frozen_turn and k == "freeze" and frozen_turn == r["turn_id"]),
            "thread": thread, "created_at": r["created_at"]}


def repeats(a: dict, b: dict) -> bool:
    """A stop or patrol banner that repeats another: the same rule, kind and words (an
    ask never collapses: it waits for their answer)."""
    return a["kind"] in ("stop", "patrol") and \
        (a["rule"], a["kind"], a["line"]) == (b["rule"], b["kind"], b["line"])
