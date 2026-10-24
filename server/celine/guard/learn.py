"""Bastet learns from past decisions. After she freezes
Celine it reads how the owner ended the freezes of the same kind before, and either
lifts this one itself or asks them on the intercom, in one message.

- The kind (`signature`): the deciding rule, the tool, where it sent (sink) and the
  class of the piece, from the decision row. Plain columns, no model.
- The precedents: earlier freezes by Bastet of the same kind, newest first, each with
  its outcome: unfrozen (by the owner, and how fast), kept (they answered keep on its
  banner) or lifted (by Bastet herself: never proof).
- Lift: only with guard.toml [learn] lift = "self", when the owner themselves unfroze at
  least `lift_after` freezes of this kind and kept none. The unfreeze is done as their
  precedent (owner.precedent reads those rows back), recorded as unfrozen_by 'bastet'
  with the ids, and the owner hears it at once.
- Ask: anything else. The freeze note to the owner carries
  the precedents in plain words; `asked_at` and `asked_id` on the freeze say it asked. Celine reads in its
  context that Bastet asked the owner (awareness.py).

Code decides; no model is asked here. A model never lifts a freeze.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from ..core.ids import now
from . import bastet, rules as rule_set, store as decisions
from .owner import Owner, precedent

NAMED = 5          # precedents named in the message to the owner


def signature(c: sqlite3.Connection, decision_id: int | None) -> dict | None:
    """The kind of a decision: {rule, tool, sink, cls}; None without a row."""
    r = decisions.row(c, decision_id)
    return {"rule": r["rule"], "tool": r["tool"], "sink": r["sink"], "cls": r["class"]} \
        if r else None


def precedents(c: sqlite3.Connection, freeze: dict) -> list[dict]:
    """Earlier freezes by Bastet of the same kind as `freeze`, newest first:
    {id, at, outcome (unfrozen, kept, lifted, open), minutes (to the unfreeze)}."""
    sig = signature(c, freeze.get("decision_id"))
    if sig is None:
        return []
    out = []
    for r in decisions.like(c, freeze["id"], sig):
        outcome = "open" if r["unfrozen_at"] is None else \
            "lifted" if r["unfrozen_by"] == "bastet" else "unfrozen"
        if decisions.kept(c, r["decision_id"]):
            outcome = "kept"
        minutes = None
        if r["unfrozen_at"]:
            minutes = round((datetime.fromisoformat(r["unfrozen_at"]) -
                             datetime.fromisoformat(r["at"])).total_seconds() / 60)
        out.append({"id": r["id"], "at": r["at"], "outcome": outcome, "minutes": minutes})
    return out


def decide(rows: list[dict], cfg) -> tuple[str, list[int]]:
    """("lift", the owner's own unfreezes as proof) or ("ask", [])."""
    learn = rule_set.current(cfg)["learn"]
    if learn["lift"] != "self" or any(r["outcome"] == "kept" for r in rows):
        return "ask", []
    mine = [r["id"] for r in rows if r["outcome"] == "unfrozen"]
    return ("lift", mine) if len(mine) >= learn["lift_after"] else ("ask", [])


def before(rows: list[dict]) -> str:
    """The precedents in plain words for the owner."""
    if not rows:
        return "nothing like it"
    words = {"unfrozen": "you unfroze it", "kept": "you kept it",
             "lifted": "I lifted it", "open": "still open"}
    parts = []
    for r in rows[:NAMED]:
        took = f" after {r['minutes']} min" if r["outcome"] == "unfrozen" and \
            r["minutes"] is not None else ""
        parts.append(f"#{r['id']} {words[r['outcome']]}{took}")
    more = f", {len(rows) - NAMED} more" if len(rows) > NAMED else ""
    return "; ".join(parts) + more


async def after_freeze(app, row: dict) -> Owner | None:
    """Bastet's freeze `row` was just made and every turn stopped: tell the owner, then
    -> their own past unfreezes as the Owner that lifts it now (freeze.py unfreezes, then
    `lifted`), or None when it asked them instead."""
    store, cfg = app.store, app.cfg
    cid, tid = row.get("conversation_id"), row.get("turn_id")
    rows = await store.read(lambda c: precedents(c, row))
    what, ids = decide(rows, cfg)
    note = bastet.fill(cfg, bastet.FROZE, why=row["why"])
    if what == "lift":
        await bastet.tell_owner(app, note, cid, tid)
        return await store.read(lambda c: precedent(c, ids, len(ids)))
    iid = await bastet.tell_owner(app, note + " " + bastet.fill(
        cfg, bastet.CONSULT, before=before(rows)), cid, tid,
        data={"freeze_id": row["id"], "decision_id": row.get("decision_id")})
    await store.write(lambda c: c.execute(
        "UPDATE freezes SET asked_at=?, asked_id=? WHERE id=?", (now(), iid, row["id"])))
    return None


async def lifted(app, row: dict, by: Owner) -> None:
    """It lifted freeze `row` on the owner's past unfreezes `by`: they hear it at once."""
    await bastet.tell_owner(app, bastet.fill(
        app.cfg, bastet.LIFTED_NOTE, n=len(by.ids), ids=", ".join(f"#{i}" for i in by.ids)),
        row.get("conversation_id"), row.get("turn_id"))
