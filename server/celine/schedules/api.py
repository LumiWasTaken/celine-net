"""Schedules and their runs. A schedule write is
schedules/edit.py's, the same as its own (schedules/skill.py)."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Request

from ..core.ids import iso
from ..core.states import RUN, table
from ..errors import CelineError, NotFound
from ..settings import files, seed
from ..settings.files import Invalid
from ..settings.registry import ROLES
from ..clock import zone
from . import VARIABLES, fill, night_of, run_date, run_roles, shown, words
from .runs import IDLE_MINUTES, MODEL_WAIT, ROW, STOPPED, first_slot, night_words, \
    results, view as run_view

router = APIRouter(prefix="/api")
NIGHTS = 14                     # the past nights the page shows
# a field's "?" on the schedule page (ui Help): at most two sentences, filled by the server;
# what runs.py does (_fail, _idle_skip), the period in the Routines page's word (day)
HELP = {
    "on_fail": "Retry runs a failed run again after 15 minutes, 1 hour and 4 hours for the "
               "same day, never past the next run, unless it was stopped or over the time "
               "limit. Skip ends it with no notice.",
    "idle_only": f"A run starts only after nobody used the app for {IDLE_MINUTES} minutes, "
                 f"else it is skipped; a run still waiting {IDLE_MINUTES} minutes after its "
                 "time is skipped too. Run now and Retry start a run regardless.",
    "reviewer": "A second prompt on the same role talks with the run first, up to Turns "
                "lines. The run gives its final answer after the talk.",
    "wall_minutes": "The most minutes one run may work; waiting for a model does not count. "
                    "Empty uses the Routine time limit in Settings, Advanced.",
}
CHOICES = {"on_fail": [{"value": "retry", "label": "Retry"}, {"value": "skip", "label": "Skip"}]}


def _folder(request: Request):
    return seed.schedules_dir(files.settings_dir(request.app.state.services.cfg))


def _schedules(request: Request) -> dict:
    return request.app.state.services.cfg.schedules


def _tonight(cfg) -> date:
    """The night the page opens on: the night of this moment."""
    return night_of(datetime.now(timezone.utc), zone(cfg))


def _view(s, cfg) -> dict:
    """`label` as written (its {{username}} and {{name}} kept, so a save keeps them),
    `title` the label filled; `every` its cron in words ("Daily at 02:00", schedules.words);
    `next_night` the night of the next slot and `next_at` its time
    of day."""
    from croniter import croniter
    tz = zone(cfg)
    nxt = croniter(s.cron, datetime.now(tz)).get_next(datetime) if s.on else None
    return {"name": s.name, "label": s.label, "title": shown(s), "summary": s.summary, "cron": s.cron, "time": s.time,
            "every": words(s.cron),
            "prompt": s.prompt, "after": list(s.after),
            "reviewer": s.reviewer, "reviewer_prompt": s.reviewer_prompt, "turns": s.turns,
            "note": s.note, "role": s.role,
            "on": s.on, "idle_only": s.idle_only,
            "daytime": s.daytime,
            "wall_minutes": s.wall_minutes, "on_fail": s.on_fail,
            "next": iso(nxt) if nxt else None,
            "next_night": night_of(nxt, tz).isoformat() if nxt else None,
            "next_at": f"{nxt:%H:%M}" if nxt else None}


def _scheduler(request: Request):
    """The running Scheduler: set while Routines is on."""
    if (s := request.app.state.services.schedules) is None:
        raise CelineError("unavailable", "Routines are off.", 409)
    return s


@router.get("/schedules")
async def list_schedules(request: Request) -> dict:
    """{schedules: [{name, label, title, summary, cron, time, every, prompt, after, reviewer,
    reviewer_prompt, turns, note, role, on, idle_only, wall_minutes, on_fail, next,
    next_night, next_at}],
    roles, variables, tonight, choices, help}: by time of day; `time` "02:00" for a daily one (else
    null), `every` in words ("Daily at 02:00", "Sundays at 07:00"), `next` the next
    slot (UTC, null while off), `next_night` its night and `next_at` its time of day,
    `roles` the chat roles a schedule can run on,
    `variables` the words a prompt can hold, `tonight` the night the page opens on,
    `choices` a field's choices ({on_fail: [{value, label}]}), `help` a field's "?" text
    (on_fail, idle_only, wall_minutes)."""
    cfg = request.app.state.services.cfg
    rows = sorted(_schedules(request).values(), key=lambda s: (s.time or "99", s.name))
    return {"schedules": [_view(s, cfg) for s in rows],
            "roles": [{"name": r, "label": ROLES[r]["label"]} for r in run_roles()],
            "variables": [*VARIABLES, *(f"result:{s.name}" for s in rows)],
            "tonight": _tonight(cfg).isoformat(), "choices": CHOICES,
            "help": {k: cfg.substitute(v) for k, v in HELP.items()}}


@router.put("/schedules/{name}")
async def put_schedule(request: Request, name: str) -> dict:
    """{label, summary, cron or time ("02:00": every day then), prompt, after, reviewer,
    reviewer_prompt, turns, note, role, on, idle_only
    (start only when nobody used the app for 30 minutes, else skip that run),
    wall_minutes (minutes it may run, null: the Routine time limit),
    daytime (true: its dates are the calendar date of its slot, not the night's),
    on_fail (retry or skip; daytime and on_fail left out, the stored ones stay)}: the
    whole schedule, made or replaced.
    `reviewer_prompt` is the reviewer's text, written to the file `reviewer` names
    (`<name>-reviewer.md` when none is named); empty: no reviewer. -> the schedule as in
    the list, and `applied`."""
    from ..api.deps import json_body
    from .edit import FIELDS, save
    body = await json_body(request, allow=FIELDS)
    s, applied = await save(request.app.state.services, name, body, "user")
    return {**_view(s, request.app.state.services.cfg), "applied": applied}


@router.delete("/schedules/{name}")
async def delete_schedule(request: Request, name: str) -> dict:
    """The schedule goes, its reviewer prompt with it when no other schedule talks with
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
        if s.reviewer and not any(o.reviewer == s.reviewer for o in every_.values()
                                  if o.name != name):
            (folder / s.reviewer).unlink(missing_ok=True)
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
    """{night, text}: `text` (a prompt, a reviewer prompt or a note path) filled as a run
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
    """{runs, nights, tonight, states}: the runs of `night` (default: tonight) or of one
    `schedule` (its newest 50), oldest slot first; `nights` the NIGHTS nights up to
    `until` (default and at most tonight), newest first, each {night, failed: the newest
    try of one of its schedules failed, or was skipped on an error, waiting: a run of it
    waits for its model}; `states` every state of a run as {words, tone}, `planned` a
    slot with no run yet. A run's `asked`: an approval waits in it or in a
    sub-agent of it (approvals.asked)."""
    from ..approvals import asked
    services = request.app.state.services
    cfg, store = services.cfg, services.store
    waits = asked(services)
    held = getattr(services.schedules, "held", None)
    tonight = _tonight(cfg)
    which = (_night(night) if night else tonight).isoformat()
    last = min(_night(until), tonight) if until else tonight
    first = (last - timedelta(days=NIGHTS - 1)).isoformat()

    def fn(c):
        if schedule is not None:
            rows = c.execute(f"{ROW} WHERE r.schedule = ? ORDER BY r.slot DESC LIMIT 50",
                             (schedule,)).fetchall()[::-1]
        else:
            rows = c.execute(f"{ROW} WHERE r.night = ? ORDER BY r.slot", (which,)).fetchall()
        # each schedule's newest try of each night (a later slot replaces an earlier one)
        newest: dict[tuple[str, str], sqlite3.Row] = {}
        for d in c.execute("SELECT night, schedule, state, error, reason FROM runs WHERE "
                           "night >= ? AND night <= ? ORDER BY slot",
                           (first, last.isoformat())):
            newest[(d["night"], d["schedule"])] = d
        return [run_view(r, cfg, r["id"] == held) for r in rows], newest
    runs, newest = await store.read(fn)
    failed = {n for (n, _), d in newest.items()
              if d["state"] == "failed" or d["state"] == "skipped" and d["error"]}
    waiting = {n for (n, _), d in newest.items()
               if d["state"] == "waiting" and d["reason"].startswith(MODEL_WAIT)}
    nights = [{"night": n, "failed": n in failed, "waiting": n in waiting} for n in
              ((last - timedelta(days=i)).isoformat() for i in range(NIGHTS))]
    runs = [{**r, "asked": r["conversation_id"] in waits} for r in runs]
    return {"runs": runs, "nights": nights, "tonight": tonight.isoformat(),
            "states": table(RUN)}


def asked(c: sqlite3.Connection, cid: str | None) -> str | None:
    """What run conversation `cid` was asked: the first message system:schedules sent
    it, as the agent got it; None before it began."""
    r = cid and c.execute(
        "SELECT m.content FROM messages m JOIN intercom i ON i.id = m.intercom_id "
        "WHERE m.conversation_id = ? AND i.from_addr = 'system:schedules' "
        "ORDER BY m.created_at, m.rowid LIMIT 1", (cid,)).fetchone()
    return r[0] if r else None


def facts(c: sqlite3.Connection, r, cfg) -> list[dict]:
    """A run's facts as its page shows them, each {label, value}: Started and Ended
    (Skipped for a skipped run; "Sun 11 Oct 01:13" in the app's zone), Duration, Started by (a run the clock did not
    start), Model (the models that answered in its conversation, else the one it was sent
    to, each its label as the sub-agents tree says it)."""
    from ..attachments import label
    from ..events import _took
    from ..sub_agents.report import work
    from ..texts import who
    tz = zone(cfg)

    def when(at: str) -> str:
        t = datetime.fromisoformat(at).astimezone(tz)
        return f"{night_words(t.date().isoformat())} {t:%H:%M}"
    ended = "Skipped" if r["state"] == "skipped" else "Ended"
    out = [{"label": k, "value": when(r[f])} for k, f in
           (("Started", "started_at"), (ended, "finished_at")) if r[f]]
    if took := _took(r["started_at"], r["finished_at"]):
        out.append({"label": "Duration", "value": took})
    if r["started_by"] != "clock":
        out.append({"label": "Started by", "value": who(cfg, r["started_by"])})
    cid = r["conversation_id"]
    models = cid and (work(c, cid, whole=True)["models"] or [c.execute(
        "SELECT model_id FROM conversations WHERE id = ?", (cid,)).fetchone()[0]])
    if names := [label(getattr(cfg, "settings", None), m) for m in dict.fromkeys(models or ()) if m]:
        out.append({"label": "Model", "value": ", ".join(dict.fromkeys(names))})
    return out


@router.get("/runs/{rid}")
async def get_run(request: Request, rid: str) -> dict:
    """{run, prompt, reviewer_prompt, note, facts}: the run; `prompt` what it was asked
    (asked), before it began its schedule's prompt filled for its date (run_date of its
    first try, first_slot); `reviewer_prompt` and `note` its schedule's filled so; each
    null for a deleted schedule; `facts` as `facts` words them."""
    services = request.app.state.services
    cfg, store = services.cfg, services.store
    def one(c):
        r = c.execute(f"{ROW} WHERE r.id = ?", (rid,)).fetchone()
        return (r, first_slot(c, r), asked(c, r["conversation_id"]), facts(c, r, cfg)) \
            if r else (None, None, None, [])
    r, slot, got, said = await store.read(one)
    if r is None:
        raise NotFound("run")
    s = _schedules(request).get(r["schedule"])
    res = await store.read(lambda c: results(c, r["night"]))

    def filled(text: str) -> str | None:
        return fill(cfg, text, run_date(s, datetime.fromisoformat(slot), zone(cfg)),
                    res) if s and text else None
    held = getattr(services.schedules, "held", None) == rid
    return {"run": run_view(r, cfg, held),
            "prompt": got if got is not None else filled(s.prompt if s else ""),
            "reviewer_prompt": filled(s.reviewer_prompt if s else ""),
            "note": filled(s.note if s else ""), "facts": said}


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


@router.post("/runs/{rid}/retry")
async def retry_run(request: Request, rid: str) -> dict:
    """A failed, skipped or stopped run runs again for its night, now: a new waiting run
    `again_of` it (Scheduler.retry). -> {run}. 404: no such run; 400: it has not ended,
    it is done, it ran again already, or its schedule was deleted."""
    return {"run": await _scheduler(request).retry(rid)}
