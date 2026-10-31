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
A turn a freeze cut (guard/freeze.py) is no Stop: with Continue missions after Unfreeze
(Settings, Advanced, on by default) the mission stays active, and after the unfreeze
that turn goes on (`go_on`, looked at every LOOK seconds; a turn that cannot be
continued gets KEEP_GOING). Switched off, it is a Stop as above. A turn a restart cut
(turns/store.py, reason `restart`) goes on the same way, with no switch.
A move to Needs you is reported as its own status change is (missions/skill.record).
A turn the user started ends a Needs you: the mission is active again, and the rule above
goes on from there.

The ceiling: the Mission time limit (Settings, Advanced; 0 none) counts from its start;
past it an active mission is stopped, its note why (`ceilings`, looked at every LOOK
seconds), as a routine past its own.

The house's hold (hold.py: frozen, or paused with no model answering) is no time of a
mission's: when a hold ends, every mission still open gets its length (`credit`): its
ceiling counts it off, and its time limit and minimum time move later by it.

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
              "step; when you wait on something, missions__wait until the time to look "
              "again; or close it with missions__update(status=\"Done\").")
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


def _cut(c: sqlite3.Connection, turn_id: str | None) -> str | None:
    """"frozen" or "restart" when turn `turn_id` ended cut by a freeze or a restart."""
    r = c.execute("SELECT state, reason_code FROM turns WHERE id=?", (turn_id,)).fetchone()
    if r is None:
        return None
    return {("aborted", "frozen"): "frozen", ("interrupted", "restart"): "restart"}.get(
        (r["state"], r["reason_code"]))


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
    cut = await app.store.read(lambda c: _cut(c, turn_id))
    if cut == "restart" or (cut == "frozen" and app.cfg.r("missions_resume_after_unfreeze")):
        return None                  # go_on takes it on
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
    """Every active mission that started more than the Mission time limit before `at`,
    the time the house held it aside, stops, its note why. -> the missions stopped."""
    hours = app.cfg.r("mission_max_hours")
    if not hours:
        return []
    out = []
    for row in await missions.list(app.store, "active"):
        ran = at - datetime.fromisoformat(row["created_at"]) - \
            timedelta(seconds=row["held_seconds"])
        if ran > timedelta(hours=hours):
            await stop(app, row["conversation_id"], _over(hours))
            log.info("mission over its ceiling", extra={
                "event": "missions.ceiling", "conversation": row["conversation_id"]})
            out.append(row["conversation_id"])
    return out


async def credit(app, seconds: int) -> list[str]:
    """The house held every mission `seconds` (hold.py): each one active or needing the
    user gets them; its time limit and minimum time move later by them, the time
    limit's notice with it. -> the missions credited."""
    out = []
    for status in ("active", "needs_you"):
        for row in await missions.list(app.store, status):
            cid, later = row["conversation_id"], timedelta(seconds=seconds)
            changes = {"held_seconds": row["held_seconds"] + seconds}
            for key in ("until", "min_until"):
                if row[key]:
                    changes[key] = ids.iso(datetime.fromisoformat(row[key]) + later)
            if row["until"]:
                # the old notice goes when it has not landed; the new limit gets its own
                if row["wake_id"]:
                    await app.intercom.cancel(int(row["wake_id"]))
                changes["wake_id"] = await missions.book_time_limit(
                    app.intercom, cid, datetime.fromisoformat(changes["until"]))
            await missions.update(app.store, cid, changes)
            out.append(cid)
    return out


async def held(app, at: datetime, since: datetime | None) -> datetime | None:
    """A look at the house's hold at `at`: -> when the hold standing now began, None
    when none stands. A hold that ended (`since` given, none now) is credited."""
    from ..hold import her_hold
    if await her_hold(app.store, getattr(app, "llm", None)):
        return since or at
    if since is not None:
        await credit(app, round((at - since).total_seconds()))
    return None


async def go_on(app) -> list[str]:
    """Every active mission that is stuck with its latest turn cut by a restart, or by a
    freeze after the unfreeze (Continue missions after Unfreeze on), goes on in that
    turn, or with KEEP_GOING when it cannot be continued. Nothing while frozen. -> the
    missions that did."""
    from ..conversations import tree
    from ..guard.freeze import frozen
    from ..turns import store as turns
    if await frozen(app.store):
        return []
    cuts = {"restart"} | ({"frozen"} if app.cfg.r("missions_resume_after_unfreeze") else set())
    out = []
    for row in await missions.list(app.store, "active"):
        cid = row["conversation_id"]
        last = await app.store.read(lambda c: (c.execute(
            "SELECT id FROM turns WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
            (cid,)).fetchone() or [None])[0])
        if not await app.store.read(lambda c: _cut(c, last) in cuts and stuck(c, cid)):
            continue
        try:
            await turns.continue_turn(app.store, cid, last, app.registry)
        except (KeyError, ValueError, tree.Busy):
            await app.intercom.send("system:app", f"agent:{cid}", KEEP_GOING, kind="notice",
                                    data={"mission": cid, "keep_going": True})
        else:
            app.dispatcher.notify()
        log.info("mission goes on after its cut",
                 extra={"event": "missions.go_on", "conversation": cid})
        out.append(cid)
    return out


async def run(app) -> None:
    """The app's loop: every LOOK seconds a look for due check-ins, for the house's hold
    (no ceiling counts while it stands), for missions past their ceiling and for
    missions a restart or a freeze cut."""
    since = None

    async def hold(app, at) -> None:
        nonlocal since
        # the hold's start is kept in memory, a restart during one credits none
        since = await held(app, at, since)

    async def ceiling(app, at) -> None:
        if since is None:
            await ceilings(app, at)

    async def cut(app, _at) -> None:
        await go_on(app)
    while True:
        at = datetime.now(timezone.utc)
        for look, what in ((check_ins, "check_in"), (hold, "hold"), (ceiling, "ceiling"),
                           (cut, "go_on")):
            try:
                await look(app, at)
            except Exception:
                log.exception(f"mission {what} failed",
                              extra={"event": f"missions.{what}_failed"})
        await anyio.sleep(LOOK)


def start(app) -> None:
    """Hooks the rule onto the dispatcher's turn ends (app.dispatcher.on_end). Called
    once at startup, before the workers start."""
    from ..settings import features

    async def on_end(cid: str, turn_id: str | None) -> None:
        if not features.on(app.cfg, "missions"):
            return
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
