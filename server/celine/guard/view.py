"""A decision row as the owner reads it: its kind, Bastet's line (what she did, then
why), the answers it takes, and the banner it shows as. Plain functions of a row, no
SQL (store.py reads the rows, banners.py takes the owner's answers)."""
from __future__ import annotations

import sqlite3

from .bastet import OWNER_SINKS, sentence, tool_words
from .rules import TABLE, label

# only what acted, in On: a stop, a confirmed patrol finding, and the asks of the rule
# that moved to Settings, Tools with how they ended. Watch's "would have" rows and a rule
# set to watch are rows for Events, never banners
BANNER_DECISIONS = ("found", "stop", "approved", "denied", "closed")

# what it did, then why: the owner reads everything (rows, facts), the agent none of it
DID = {"stop": "Stopped {tool}.", "freeze": "Stopped {tool} and froze everything.",
       "would_stop": "Would have stopped {tool}.",
       "would_freeze": "Would have stopped {tool} and frozen everything.",
       "approved": "You approved {tool}.",
       "denied": "You denied {tool}.", "closed": "No answer on {tool}, it did not run.",
       "patrol": "Looked back at this turn.", "gate": "Stopped {tool}.",
       "allow": "Allowed {tool}.", "passed": "Allowed {tool} by your exception.",
       "failed": "Bastet could not decide on {tool}.", "waiting": "Checking {tool}.",
       "unreachable": "Bastet could not decide on {tool}. It ran."}


def kind(r: sqlite3.Row) -> str:
    """What a row is to the owner: patrol, freeze, would_freeze, else its decision."""
    d = r["decision"]
    if r["does"] == "patrol" or d == "found":
        return "patrol"
    if d == "stop" and r["froze"]:
        return "freeze"
    if d == "would_stop" and r["froze"]:
        return "would_freeze"
    return d


def shown(r: sqlite3.Row | None) -> bool:
    """A row that is a banner: it acted in On (a closed gate holds in Watch too)."""
    return r is not None and r["decision"] in BANNER_DECISIONS and (
        r["mode"] == "on" or r["rule"] == "gates")


# the "?" texts of Settings, Guard ({{name}} and the pronouns filled by the route)
PAGE_HELP = {
    "mode": "Warn lets every step run and writes down what Bastet would have done. Enforce "
            "checks a risky step first and may stop it, freeze {{name}} or ask you.",
    "decision": "Decides whether a risky step may run. Without it, Warn lets every step "
                "run and Enforce makes risky steps wait until it answers.",
    "second_look": "A stronger model looks again at what the decision model stopped. When "
                   "it disagrees, you get no message and Bastet unfreezes {{name}} if "
                   "{{name}} is frozen.",
    "rules": "What Bastet does when a rule finds something in a step. Warn only writes "
             "it down in Events.",
    "passes": "Tool calls you allowed with Allow for 7 days or a reply to Bastet. End ends "
              "one now.",
}

# the "?" on a stop or freeze banner: what each of its answers does, for the ones it has
HELP = {"once": "Allow once runs this same step one time.",
        "pass": "Allow for 7 days lets steps with the same finding run in this chat."}


def actions(r: sqlite3.Row) -> list[str]:
    """What the owner can answer on a stop or a freeze, on its banner and from the Events
    page: "once" (allow this call once) and "pass" (allow for 7 days: its rule and its piece
    in its conversation, only for a row with a piece). Not a closed gate (only
    guard.toml opens it), not the patrol (no call to allow)."""
    if not shown(r) or r["rule"] == "gates" or kind(r) not in ("stop", "freeze"):
        return []
    return ["once", "pass"] if r["piece"] else ["once"]


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
            "actions": (got := actions(r)), "help": " ".join(HELP[a] for a in got) or None,
            "thread": thread, "created_at": r["created_at"]}


def repeats(a: dict, b: dict) -> bool:
    """A stop or patrol banner that repeats another: the same rule, kind and words."""
    return a["kind"] in ("stop", "patrol") and \
        (a["rule"], a["kind"], a["line"]) == (b["rule"], b["kind"], b["line"])
