"""Missions over HTTP: start one, read or edit its brief, stop it
(docs/ARCHITECTURE.md). The brief and a changed brief are intercom messages
from the user (kind `brief`); the time limit is a `notice` from the app with a due time
before the limit, so it wraps up in time (book_time_limit), its id in `wake_id`."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from ..conversations import store as conversations
from ..core.ids import iso, parse_when
from ..errors import CelineError, NotFound
from . import store as missions
from .store import book_time_limit
from ..clock import zone
from ..terminals.store import slug
from . import skill
from .skill import brief_lines, head, report_title
from ..texts import record

router = APIRouter(prefix="/api")

class MissionIn(BaseModel):
    """The brief; with no name it has one from the brief; `model` its chat model (a
    ref), else the default."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    model: str | None = None
    brief: str = ""
    report_to: str | None = None
    time_limit: str | None = None
    min_time: str | None = None
    check_in: str | None = None


class StepIn(BaseModel):
    """A step of its plan as the page sends it back (plans/store.py)."""
    model_config = ConfigDict(extra="forbid")
    title: str
    state: str = "todo"
    checkpoint: str = "none"
    model: str | None = None


class MissionEdit(BaseModel):
    """Each given only to change it. `steps` (its plan's, a step's model changed) go to
    its plan (plans/store.py), not to it as a message."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    brief: str | None = None
    report_to: str | None = None
    time_limit: str | None = None
    min_time: str | None = None
    check_in: str | None = None
    steps: list[StepIn] | None = None


def _until(time_limit: str | None, tz, what: str = "Time limit") -> datetime | None:
    """A time limit or a minimum time as a time: "08:00" (at) or "2 h" (in). Empty:
    none."""
    text = (time_limit or "").strip()
    if not text:
        return None
    until = parse_when(text, text, tz, datetime.now(timezone.utc))
    if until is None:
        raise CelineError("bad_request", f'{what} is like "2 h" or "08:00".', 400)
    return until


def _minimum(text: str | None, tz, until: str | None) -> str | None:
    """The minimum time as stored; not after the time limit `until`."""
    least = _until(text, tz, "Minimum time")
    if least is None:
        return None
    if until and iso(least) > until:
        raise CelineError("bad_request", "Minimum time is after the time limit.", 400)
    return iso(least)


def _check_in(text: str | None) -> int | None:
    """A check-in cadence in minutes: "30 min", "2 h". Empty: none."""
    text = (text or "").strip()
    if not text:
        return None
    at = datetime.now(timezone.utc)
    then = parse_when(text, None, None, at)
    minutes = round((then - at).total_seconds() / 60) if then else 0
    if minutes < 1:
        raise CelineError("bad_request", 'Check-in is like "30 min" or "2 h".', 400)
    return minutes


async def _conversation_exists(store, cid: str | None) -> None:
    if cid and await store.read(lambda c: c.execute(
            "SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone()) is None:
        raise NotFound("conversation")


async def _mission(request: Request, cid: str) -> dict:
    row = await missions.get(request.app.state.services.store, cid)
    if row is None:
        raise NotFound("mission")
    return row


TITLE_MAX = 60
_SENTENCE = re.compile(r"(.+?[.!?])(\s|$)")


def title_of(brief: str) -> str:
    """A mission's name until the title role names it (as a chat's): the brief's first
    sentence, cut at a word with "..." when it is long."""
    text = " ".join(brief.split())
    m = _SENTENCE.match(text)
    words = m.group(1) if m else text
    if len(words) > TITLE_MAX:
        words = words[:TITLE_MAX + 1].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return words


@router.post("/missions")
async def start_mission(request: Request, body: MissionIn) -> dict:
    return {"conversation_id": await start(request.app.state.services, body)}


async def start(services, body: MissionIn, by: str | None = None) -> str:
    """A mission from its brief, the user's (the route) or hers (missions__start, `by`
    the conversation she started it from): its conversation (kind mission), the row,
    the time limit, the brief as its first message, from whoever started it. -> the
    conversation."""
    store, cfg = services.store, services.cfg
    tz = zone(cfg)
    brief = body.brief.strip()
    if not brief:
        raise CelineError("bad_request", "Brief is required.", 400)
    await _conversation_exists(store, body.report_to)
    from ..settings import conversations as conv_settings
    chosen = conv_settings.validate(cfg.settings, getattr(services, "registry", None),
                                    {"model": body.model}) if body.model else None
    until = _until(body.time_limit, tz)
    least = _minimum(body.min_time, tz, iso(until) if until else None)
    check_in = _check_in(body.check_in)
    name = " ".join((body.name or "").split())
    title = name or title_of(brief)
    # an unnamed one has no title yet: the title role names it after its first reply
    cid = await conversations.create(store, title=name, auto=not name, kind="mission")
    # the terminal carries the mission's name; a mission with no name is `mission`
    terminal = await missions.terminal_name(store, slug(name) or "mission")
    await missions.create(store, cid, {
        "name": name or title, "terminal": terminal, "brief": brief,
        "report_to": body.report_to or None, "until": iso(until) if until else None,
        "min_until": least, "check_in": check_in, "started_by": by})
    if chosen:                          # before the brief: its first turn runs on it
        await conv_settings.apply(store, cid, chosen)
    await store.write(lambda c: record(c, f"mission:{cid}:brief", brief,
                                       "her" if by else "user"))
    if until:
        await missions.update(store, cid, {
            "wake_id": await book_time_limit(services.intercom, cid, until)})
    row = await missions.get(store, cid)
    text = "\n".join([head(row), *brief_lines(row, await report_title(store, row), tz)])
    await services.intercom.send(f"agent:{by}" if by else "user", f"agent:{cid}",
                                 cfg.substitute(text), kind="brief")
    _announce(store, row)
    return cid


@router.get("/missions")
async def list_missions(request: Request, status: str | None = None,
                        trashed: bool = False, archived: bool = False) -> dict:
    """Every mission, newest first, with its conversation's title, its plan (`plan` the
    text, `steps`, `step` the number of the one doing) and its automatic context switch; with `trashed` only the ones in the
    trash, with `archived` only the archived ones (a mission is archived and deleted as
    its conversation is: POST /conversations/{cid}/archive, DELETE /conversations/{cid}).
    `counts`: the page's numbers (store.counts), whatever the filters."""
    store = request.app.state.services.store
    return {"missions": await missions.list(store, status, trashed, archived),
            "counts": await missions.counts(store)}


def _announce(store, row: dict | None) -> None:
    if row is not None:
        store.emit_global(missions.state_event(row))


@router.patch("/missions/{cid}")
async def edit_mission(request: Request, cid: str, body: MissionEdit) -> dict:
    """The given fields only; a changed one reaches it as a message. A stopped
    mission takes no change: nobody would read it."""
    store, cfg = request.app.state.services.store, request.app.state.services.cfg
    tz = zone(cfg)
    row = await _mission(request, cid)
    if row["status"] == "stopped":
        raise CelineError("stopped", "The mission is stopped. Start a new one.", 409)
    given = body.model_fields_set
    changes: dict = {}
    for key in ("name", "brief", "report_to"):
        if key not in given:
            continue
        value = getattr(body, key) or ""
        value = " ".join(value.split()) if key == "name" else value.strip()
        if key == "brief" and not value:
            raise CelineError("bad_request", "Brief is required.", 400)
        if key == "name" and not value:
            continue
        if key == "report_to":
            value = value or None
            await _conversation_exists(store, value)
        if value != row[key]:
            changes[key] = value
    if "steps" in given:
        from ..plans import flow, store as plans
        old = await plans.get(store, cid) or {"text": "", "waiting": False, "steps": []}
        try:
            steps = plans.clean([s.model_dump() for s in body.steps or []], old["steps"])
            flow.check_models(getattr(cfg, "settings", None), steps)
        except plans.Refused as e:
            raise CelineError("bad_request", str(e), 400) from None
        plan = await plans.save(store, cid, old["text"], steps, old["waiting"])
        await flow.follow_model(store, cfg, cid, plan)
        if not ({"name", "brief", "report_to", "time_limit", "min_time", "check_in"} & given):
            return {"ok": True}
    if "time_limit" in given:
        until = _until(body.time_limit, tz)
        new = iso(until) if until else None
        if new != row["until"]:
            changes["until"] = new
            if row["wake_id"]:
                await request.app.state.services.intercom.cancel(int(row["wake_id"]))
            changes["wake_id"] = await book_time_limit(
                request.app.state.services.intercom, cid, until) if until else None
    if "min_time" in given:
        least = _minimum(body.min_time, tz, changes.get("until", row["until"]))
        if least != row["min_until"]:
            changes["min_until"] = least
    if "check_in" in given and (minutes := _check_in(body.check_in)) != row["check_in"]:
        changes["check_in"] = minutes
    if not changes:
        return {"ok": True}
    await missions.update(store, cid, changes)
    if "brief" in changes:
        await store.write(lambda c: record(c, f"mission:{cid}:brief", changes["brief"], "user"))
    row = await missions.get(store, cid)
    changed = {c for c in changes if c not in ("wake_id", "check_in")}   # the app's own
    if not changed:
        _announce(store, row)
        return {"ok": True}
    lines = [head(row)] if "name" in changed else []
    lines += brief_lines(row, await report_title(store, row), tz, only=changed)
    await request.app.state.services.intercom.send("user", f"agent:{cid}",
                                          cfg.substitute("\n".join(lines)),
                                          kind="brief", data={"changed": True})
    _announce(store, row)
    return {"ok": True}


@router.post("/missions/{cid}/stop")
async def stop_mission(request: Request, cid: str) -> dict:
    await _mission(request, cid)
    await skill.stop(request.app.state.services, cid, None)
    return {"ok": True}
