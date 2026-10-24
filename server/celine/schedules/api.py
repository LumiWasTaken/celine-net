"""Schedules and their runs. A schedule write is
schedules/edit.py's, the same as its own (schedules/skill.py)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Request

from ..core.ids import iso
from ..errors import NotFound
from ..settings import files, seed
from ..settings.files import Invalid
from ..settings.registry import ROLES
from ..clock import zone
from . import VARIABLES, fill, night_of, run_date, run_roles, shown
from .runs import STOPPED, results, view as run_view

router = APIRouter(prefix="/api")
NIGHTS = 14                     # the past nights the page shows


def _folder(request: Request):
    return seed.schedules_dir(files.settings_dir(request.app.state.services.cfg))


def _schedules(request: Request) -> dict:
    return request.app.state.services.cfg.schedules


def _tonight(cfg) -> date:
    """The night the page opens on: the night of this moment."""
    return night_of(datetime.now(timezone.utc), zone(cfg))


def _view(s, cfg) -> dict:
    """`label` as written (its {{username}} and {{name}} kept, so a save keeps them),
    `title` the label filled; `every`: "Every day at 02:00", else the next slot ("Next:
    Mon 09:30") or "Off"; `next_night` the night of the next slot and `next_at` its time
    of day."""
    from croniter import croniter
    tz = zone(cfg)
    nxt = croniter(s.cron, datetime.now(tz)).get_next(datetime) if s.on else None
    return {"name": s.name, "label": s.label, "title": shown(s), "summary": s.summary, "cron": s.cron, "time": s.time,
            "every": f"Every day at {s.time}" if s.time else f"Next: {nxt:%a %H:%M}" if nxt else "Off",
            "prompt": s.prompt, "after": list(s.after),
            "partner": s.partner, "partner_prompt": s.partner_prompt, "rounds": s.rounds,
            "note": s.note, "role": s.role,
            "on": s.on, "idle_only": s.idle_only,
            "daytime": s.daytime,
            "wall_minutes": s.wall_minutes,
            "next": iso(nxt) if nxt else None,
            "next_night": night_of(nxt, tz).isoformat() if nxt else None,
            "next_at": f"{nxt:%H:%M}" if nxt else None}


def _scheduler(request: Request):
    return request.app.state.services.schedules


@router.get("/schedules")
async def list_schedules(request: Request) -> dict:
    """{schedules: [{name, label, title, summary, cron, time, every, prompt, after, partner,
    partner_prompt, rounds, note, role, on, idle_only, wall_minutes, next,
    next_night, next_at}],
    roles, variables, tonight}: by time of day; `time` "02:00" for a daily one (else
    null), `every` in words ("Every day at 02:00", "Next: Mon 09:30"), `next` the next
    slot (UTC, null while off), `next_night` its night and `next_at` its time of day,
    `roles` the chat roles a schedule can run on,
    `variables` the words a prompt can hold, `tonight` the night the page opens on."""
    cfg = request.app.state.services.cfg
    rows = sorted(_schedules(request).values(), key=lambda s: (s.time or "99", s.name))
    return {"schedules": [_view(s, cfg) for s in rows],
            "roles": [{"name": r, "label": ROLES[r]["label"]} for r in run_roles()],
            "variables": [*VARIABLES, *(f"result:{s.name}" for s in rows)],
            "tonight": _tonight(cfg).isoformat()}


@router.put("/schedules/{name}")
async def put_schedule(request: Request, name: str) -> dict:
    """{label, summary, cron or time ("02:00": every day then), prompt, after, partner,
    partner_prompt, rounds, note, role, on, idle_only
    (start only when nobody used the app for 30 minutes, else skip that run),
    wall_minutes (minutes it may run, null: 3 hours),
    daytime (true: its dates are the calendar date of its slot, not the night's; left
    out, the stored one stays)}: the whole schedule, made or replaced.
    `partner_prompt` is the reviewer's text, written to the file `partner` names
    (`<name>-partner.md` when none is named); empty: no partner. -> the schedule as in
    the list, and `applied`."""
    from ..api.deps import json_body
    from .edit import FIELDS, save
    body = await json_body(request, allow=FIELDS)
    s, applied = await save(request.app.state.services, name, body, "user")
    return {**_view(s, request.app.state.services.cfg), "applied": applied}


@router.delete("/schedules/{name}")
async def delete_schedule(request: Request, name: str) -> dict:
    """The schedule goes, its partner prompt with it when no other schedule talks with
    it. Its waiting and running runs stop, all its runs stay readable. Refused while
    another schedule follows it."""
    from .. import reload
    from ..settings import write
    every_ = _schedules(request)
    s = every_.get(name)
    if s is None:
        raise NotFound("schedule")
    after = [shown(o) for o in every_.values() if name in o.after]
    if after:
        raise Invalid("after", f"{after[0]} runs after {shown(s)}. Take it out of there first.")
    await _scheduler(request).stop_schedule(name)
    folder = _folder(request)
    async with write.lock():
        (folder / f"{name}.toml").unlink(missing_ok=True)
        if s.partner and not any(o.partner == s.partner for o in every_.values()
                                 if o.name != name):
            (folder / s.partner).unlink(missing_ok=True)
        applied = reload.apply(request.app.state.services)
    write.publish(request.app.state.services, ["schedules"])
    _scheduler(request).wake()
    return {"deleted": name, "applied": applied}


@router.post("/schedules/{name}/run")
async def run_schedule(request: Request, name: str) -> dict:
    """A run of the schedule at this second, waiting like any other. -> {run}."""
    if name not in _schedules(request):
        raise NotFound("schedule")
    return {"run": await _scheduler(request).run_now(name, by="user")}


def _night(raw) -> date:
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise Invalid("night", "The day is a date, such as 2026-01-10.") from None


@router.post("/schedules/preview")
async def preview(request: Request) -> dict:
    """{night, text}: `text` (a prompt, a partner prompt or a note path) filled as a run
    of that night gets it, with the results that night has so far and {{mood}} and the
    others as they are now (schedules/variables.py). -> {text}."""
    from ..api.deps import json_body
    body = await json_body(request, need=("night", "text"), allow=("night", "text"))
    night = _night(body["night"])
    if not isinstance(body["text"], str):
        raise Invalid("text", "The text is text.")
    res = await request.app.state.services.store.read(lambda c: results(c, night.isoformat()))
    from .variables import extras
    return {"text": fill(request.app.state.services.cfg, body["text"], night, res,
                         await extras(request.app.state.services, body["text"], night))}


@router.get("/runs")
async def list_runs(request: Request, night: str | None = None,
                    schedule: str | None = None, until: str | None = None) -> dict:
    """{runs, nights, tonight, attention}: the runs of `night` (default: tonight) or of
    one `schedule` (its newest 50), oldest slot first; `nights` the NIGHTS nights up to
    `until` (default and at most tonight), newest first, each {night, runs: [{schedule,
    state}] (a schedule's newest run), late (a run of a night gone by still waits)};
    `attention` running while a run runs, failed while a run of the last two nights
    failed, else null (the rail's dot)."""
    cfg, store = request.app.state.services.cfg, request.app.state.services.store
    tonight = _tonight(cfg)
    which = (_night(night) if night else tonight).isoformat()
    last = min(_night(until), tonight) if until else tonight
    first = (last - timedelta(days=NIGHTS - 1)).isoformat()
    two = (tonight - timedelta(days=1)).isoformat()

    def fn(c):
        if schedule is not None:
            rows = c.execute("SELECT * FROM runs WHERE schedule = ? ORDER BY slot DESC "
                             "LIMIT 50", (schedule,)).fetchall()[::-1]
        else:
            rows = c.execute("SELECT * FROM runs WHERE night = ? ORDER BY slot",
                             (which,)).fetchall()
        dots = c.execute("SELECT night, schedule, state FROM runs WHERE night >= ? AND "
                         "night <= ? ORDER BY slot", (first, last.isoformat())).fetchall()
        running = c.execute("SELECT 1 FROM runs WHERE state = 'running' LIMIT 1").fetchone()
        # a schedule's newest run of either of the last two nights
        recent = {(d["night"], d["schedule"]): d["state"] for d in c.execute(
            "SELECT night, schedule, state FROM runs WHERE night >= ? ORDER BY slot", (two,))}
        return [run_view(r, cfg) for r in rows], dots, \
            "running" if running else "failed" if "failed" in recent.values() else None
    runs, dots, attention = await store.read(fn)
    nights = []
    for i in range(NIGHTS):
        n = (last - timedelta(days=i)).isoformat()
        newest = {d["schedule"]: d["state"] for d in dots if d["night"] == n}
        mine = [{"schedule": k, "state": v} for k, v in newest.items()]
        nights.append({"night": n, "runs": mine, "late": n < tonight.isoformat() and any(
            d["state"] == "waiting" for d in mine)})
    return {"runs": runs, "nights": nights, "tonight": tonight.isoformat(),
            "attention": attention}


@router.get("/runs/{rid}")
async def get_run(request: Request, rid: str) -> dict:
    """{run, prompt, partner_prompt, note}: the run, and what its schedule asks filled
    for its date (run_date; null for a deleted schedule)."""
    cfg, store = request.app.state.services.cfg, request.app.state.services.store
    r = await store.read(lambda c: c.execute("SELECT * FROM runs WHERE id = ?",
                                             (rid,)).fetchone())
    if r is None:
        raise NotFound("run")
    s = _schedules(request).get(r["schedule"])
    res = await store.read(lambda c: results(c, r["night"]))

    def filled(text: str) -> str | None:
        return fill(cfg, text, run_date(s, datetime.fromisoformat(r["slot"]), zone(cfg)),
                    res) if s and text else None
    return {"run": run_view(r, cfg), "prompt": filled(s.prompt if s else ""),
            "partner_prompt": filled(s.partner_prompt if s else ""),
            "note": filled(s.note if s else "")}


@router.post("/runs/{rid}/stop")
async def stop_run(request: Request, rid: str) -> dict:
    """A waiting or running run stops, `stopped` "Stopped by <user>"; a running one's
    turn is aborted. -> {run}. 404: no such run; 400: it has finished."""
    exists = await request.app.state.services.store.read(lambda c: c.execute(
        "SELECT 1 FROM runs WHERE id = ?", (rid,)).fetchone())
    if exists is None:
        raise NotFound("run")
    out = await _scheduler(request).end(rid, "stopped",
                                        request.app.state.services.cfg.substitute(STOPPED))
    if out is None:
        raise Invalid("run", "The run has finished.")
    return {"run": out}
