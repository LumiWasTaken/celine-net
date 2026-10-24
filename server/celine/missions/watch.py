"""Missions keep going (docs/ARCHITECTURE.md "Missions").

When a turn of a mission ends while its status is still active and nothing will wake it
again (no live or queued turn, no running job, no message waiting to land, there or in
its sub-agents: sub_agents/report.quiet; the time limit's notice does not count) and it
is not locked (conversations/store.locked), its plan (plans/) decides:
- a step is open, or it has no plan yet: one `notice` starts its next turn
  (KEEP_GOING). When the turn that line started called no tool, or the turn ended
  another way than done or capped (a Stop, a failure), no line follows;
- past the time limit: one line OVER, whatever the plan; after it, Needs you, the note
  PAST;
- it asked at a checkpoint (the plan waits): Needs you, the note WAITING;
- every step is done, or no line follows: Needs you, the note NOTE.
A move to Needs you is reported as its own status change is (missions/skill.record).
A turn the user started ends a Needs you: the mission is active again, and the rule above
goes on from there.

The ceiling: the Mission time limit (Settings, Advanced; 0 none) counts from its start;
past it an active mission is stopped, its note why (`ceilings`, looked at every LOOK
seconds), as a routine past its own.

Check-ins: every `check_in` minutes while a mission is active, a short line on where it
stands (the step of its plan, its note) goes where it reports, else to the user
(check_ins). It is the app's, not its own: it costs its no turn.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import anyio

from ..core import ids
from . import store as missions
from ..plans import flow, store as plans
from .skill import record, stop

log = logging.getLogger("celine.missions")
LOOK = 60.0         # seconds between two looks for a check-in that is due
NOTE = "The mission stopped without saying whether it is done."
PAST = "The time limit passed before the mission was closed."
WAITING = "A question for you at step {n}: {title}."
KEEP_GOING = ("The mission is still active and nothing is set to wake you. Do the next "
              "step, missions__wait until a time when you wait on something, or close it "
              "with missions__update(status=\"Done\").")
OVER = ("The time limit has passed. Deliver what you have, report, and close the mission "
        "with missions__update(status=\"Done\") and a note on what is still open.")


def stuck(c: sqlite3.Connection, cid: str) -> bool:
    """Nothing will start another turn of mission `cid`: it and its sub-agents are
    quiet. The time limit's notice is not a wake: it may be hours away."""
    from ..conversations.store import family
    from ..sub_agents.report import quiet
    r = c.execute("SELECT wake_id FROM missions WHERE conversation_id=?", (cid,)).fetchone()
    limit = int(r[0]) if r and r[0] else None
    return all(quiet(c, one, limit if one == cid else None) for one in family(c, cid))


def _may_go_on(c: sqlite3.Connection, turn_id: str) -> bool:
    """Turn `turn_id` ended done or capped, and unless only KEEP_GOING started it, it
    called a tool."""
    r = c.execute("SELECT state FROM turns WHERE id=?", (turn_id,)).fetchone()
    if r is None or r["state"] not in ("done", "capped"):
        return False
    started = [json.loads(d or "{}").get("keep_going") for (d,) in c.execute(
        "SELECT i.data FROM intercom_deliveries d JOIN intercom i ON i.id = d.intercom_id "
        "WHERE d.turn_id=?", (turn_id,))]
    if not started or not all(started):
        return True
    return c.execute("SELECT 1 FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
                     "WHERE m.turn_id=? LIMIT 1", (turn_id,)).fetchone() is not None


async def check(app, cid: str, turn_id: str | None) -> str | None:
    """After turn `turn_id` of `cid` ended: the rule above. -> "line" (KEEP_GOING sent),
    "needs_you" (moved), or None (left as it stands)."""
    from ..conversations.store import locked
    if turn_id is None or await app.store.read(lambda c: locked(c, cid)):
        return None                  # a locked mission is left as it stands
    row = await missions.get(app.store, cid)
    if row is not None and row["status"] == "needs_you" and await app.store.read(
            lambda c: c.execute("SELECT origin FROM turns WHERE id=?", (turn_id,))
            .fetchone()[0] == "user"):
        row = await record(app, app.store, cid, "active", None)   # the user answered
    if row is None or row["status"] != "active" or \
            not await app.store.read(lambda c: stuck(c, cid)):
        return None
    plan = await plans.get(app.store, cid)
    if plan is not None and plan["waiting"]:
        n = max((i for i, s in enumerate(plan["steps"], 1) if s["state"] == "done"
                 and s["checkpoint"] == "ask"), default=0)
        await record(app, app.store, cid, "needs_you", WAITING.format(
            n=n, title=plan["steps"][n - 1]["title"] if n else ""))
        return "needs_you"
    past = bool(row["until"]) and row["until"] <= ids.now()
    if past and await app.store.read(lambda c: _told_over(c, cid)):
        await record(app, app.store, cid, "needs_you", PAST)
        return "needs_you"
    if (past or plan is None or flow.keeps_going(plan)) and \
            await app.store.read(lambda c: _may_go_on(c, turn_id)):
        await app.intercom.send("system:app", f"agent:{cid}", OVER if past else KEEP_GOING,
                                kind="notice", data={"mission": cid, "keep_going": True,
                                                     **({"over": True} if past else {})})
        return "line"
    await record(app, app.store, cid, "needs_you", NOTE)
    return "needs_you"


def _told_over(c: sqlite3.Connection, cid: str) -> bool:
    return c.execute("SELECT 1 FROM intercom WHERE to_addr=? AND from_addr='system:app' "
                     "AND json_extract(data, '$.over') LIMIT 1",
                     (f"agent:{cid}",)).fetchone() is not None


def progress(row: dict) -> str:
    """A check-in: the mission, the step of its plan it is on, its note."""
    n, steps = row["step"], row["steps"]
    where = f"step {n} of {len(steps)}, {steps[n - 1]['title']}" if n else \
        "every step done" if steps else "no plan yet"
    note = f" {row['note']}" if row.get("note") else ""
    return f"Mission {row['name']}: {where}.{note}"


async def check_ins(app, at: datetime) -> list[str]:
    """Every active mission whose check-in is due at `at` (its cadence since the last
    one, else since it started) sends it. -> the missions that did."""
    from ..intercom.addresses import Refused
    out = []
    for row in await missions.list(app.store, "active"):
        if not row.get("check_in"):
            continue
        cid = row["conversation_id"]
        last = await app.store.read(lambda c: c.execute(
            "SELECT max(created_at) FROM intercom WHERE from_addr=? AND "
            "json_extract(data, '$.check_in')", (f"agent:{cid}",)).fetchone()[0])
        since = datetime.fromisoformat(last or row["created_at"])
        if at - since < timedelta(minutes=row["check_in"]):
            continue
        to = f"agent:{row['report_to']}" if row["report_to"] else "user"
        try:
            await app.intercom.send(f"agent:{cid}", to, progress(row), kind="report",
                                    wake=False, created_at=ids.iso(at),
                                    data={"mission": cid, "check_in": True})
        except Refused:
            continue                   # that chat is locked: it takes no check-in
        out.append(cid)
    return out


def _over(hours: int) -> str:
    return f"Ran over {hours} hour{'' if hours == 1 else 's'}"


async def ceilings(app, at: datetime) -> list[str]:
    """Every active mission that started more than the Mission time limit before `at`
    stops, its note why. -> the missions stopped."""
    hours = app.cfg.r("mission_max_hours")
    if not hours:
        return []
    out = []
    for row in await missions.list(app.store, "active"):
        if at - datetime.fromisoformat(row["created_at"]) > timedelta(hours=hours):
            await stop(app, row["conversation_id"], _over(hours))
            log.info("mission over its ceiling", extra={
                "event": "missions.ceiling", "conversation": row["conversation_id"]})
            out.append(row["conversation_id"])
    return out


async def run(app) -> None:
    """The app's loop: every LOOK seconds a look for due check-ins and for missions past
    their ceiling."""
    while True:
        at = datetime.now(timezone.utc)
        for look, what in ((check_ins, "check_in"), (ceilings, "ceiling")):
            try:
                await look(app, at)
            except Exception:
                log.exception(f"mission {what} failed",
                              extra={"event": f"missions.{what}_failed"})
        await anyio.sleep(LOOK)


def start(app) -> None:
    """Hooks the rule onto the dispatcher's turn ends (app.dispatcher.on_end). Called
    once at startup, before the workers start."""
    async def on_end(cid: str, turn_id: str | None) -> None:
        # a sub-agent's end can be what leaves its mission quiet: look at the mission,
        # after its own latest turn
        parent = await app.store.read(lambda c: (c.execute(
            "WITH RECURSIVE up(id, p) AS (SELECT id, agent_of FROM conversations WHERE id=? "
            "UNION ALL SELECT s.id, s.agent_of FROM conversations s JOIN up ON s.id = up.p) "
            "SELECT id FROM up WHERE p IS NULL", (cid,)).fetchone() or [cid])[0])
        if parent != cid:
            cid, turn_id = parent, await app.store.read(lambda c: (c.execute(
                "SELECT id FROM turns WHERE conversation_id=? ORDER BY created_at DESC "
                "LIMIT 1", (parent,)).fetchone() or [None])[0])
        await check(app, cid, turn_id)
    app.dispatcher.on_end(on_end)
