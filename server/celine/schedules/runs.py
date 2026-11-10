"""Runs: one schedule at one slot, durable.

When a run exists. At the next slot, every TICK seconds at most, on every `models.state`
change, at the end of every turn and after every change to the schedules, the scheduler
makes the run of every slot of a schedule that is on and came due since its last look
(kv `schedules.checked`). A slot counts only once its routine is on: the first look, a
look before the onboarding is done (features `done`) and the first look after Routines
was switched off (`forget`) make nothing that passed, they only start the count. While
the onboarding is not done no run starts. A run's night is `night_of` its slot. Slots
missed while the app was off with Routines on are made too, oldest first: those of the
last CATCH_UP_DAYS wait like any other, older ones are written `skipped` with the
reason. A slot is never dropped silently. A Skip routine (`on_fail` skip) waits for its
newest slot only: its new waiting run ends the older waiting ones `skipped` (NEXT_DUE).
A run the user started (`started_by` user: Run now, Retry) neither ends others so nor
is ended so.

When a run starts. A run is `waiting` until all of these hold; its `reason` says the
first that does not, in words:
0. its slot has come: a run again waits for its time ("Retry of the 02:00 run");
1. every run it follows in the same night is finished, done or failed or skipped
   ("Runs after World news");
2. the first model of its role can answer now, the Router says ("Waiting for <model>.
   <why>", the why in the server's words); a server known to be failing is asked again
   at most every RETRY_DOWN (backends/router.py), and the run starts by itself when the
   model comes back, the same night or days later;
3. no turn a person waits on is live: a chat (not a trigger's), a turn the user
   started in a mission ("Waiting until your chat is done"); a mission's
   turns that go on by themselves hold no run unless Routines wait for missions is on
   (Settings, Advanced);
4. no other run is running ("While Self-review runs"): one at a time, one model server.
While it is frozen (guard/freeze.py) no run starts ("Frozen"); the run whose turn a
freeze cut is `waiting` ("Frozen") and goes on in that turn after the unfreeze.
A schedule with `idle_only` starts only when, besides, nobody used the app for
IDLE_MINUTES (no user message, no turn a person waits on): a run that finds the app in
use is `skipped` at that look (IN_USE), one still waiting for another reason IDLE_MINUTES
after its slot is `skipped` (PASSED), and neither is caught up. A run the user started
is held to none of this.
The queue is ordered by night, then by how deep the schedule follows, then by slot, so
missed nights run night by night, each about its own date.

A run. One background conversation (`run:<id>`, hidden from the chat list) with the
filled prompt as its first message, from `system:schedules`; its persona, notes and
tools as in any conversation, on the schedule's role. The agent's turns and the reviewer's calls
wait behind every interactive request (Priority BACKGROUND, `binding`), named by the
schedule's label, on the role's own model only: no other model (turns/dispatcher.py
binds them so, `binding`, at every depth of sub-agents under the run). Its lines are
.md files beside this file (`_say`): background.md with the prompt (what the result is,
from final.md, note.md naming the note: the whole result, in full, nothing asked
again), reviewer_first.md, asked.md, line.md, last_line.md and talk_over.md for a
reviewer. With a reviewer, the reviewer prompt (on the same role, with the
read-only tools of READS) and it alternate up to `turns` lines, the reviewer may end
sooner with END alone on its last line; then it is asked for its final answer. The agent's
final answer is the result. With `note` the app writes it to that notebook path; a note
that is there already gets it appended under "## <date> <label>" (run_date). A note it
wrote itself (a notebook__write or notebook__edit of that path that ended ok in the run)
is the result instead, and the app writes nothing over it (only what it
added, when it added to its end). The agent's words beside the write are a lead-in, not the
result (a turn ends with that step, skills.ToolSpec needs_answer). A turn that ends with
a tool call that is not the note's write has no answer: the run is done with the lead-in
as its result, the note is not written, and the run's `error` says so. A note it wrote
and then went on past (ok tool calls after its last write of it, the note older than the
last of them) is the result still, and the run's `error` says how many calls came after
it. The run is over
when its conversation and its sub-agents are quiet and every sub-agent has reported
(looked at after every turn or job end, the dispatcher's `on_end`). Every run first keeps the
night's backups (schedules/backups.py: once a night, the later runs find them made).

When a run fails. A run is never re-asked or rewritten. When its turn fails and the
Router says the role's model cannot answer (it is unreachable, or none is set), the run
is `waiting` for it ("Waiting for <model>"), its conversation kept (after a cut stream
the server is asked first, `_failed_why`); the first run of a night that waits so sends
one notice; when the Router sees
the model answer, the run starts again and continues that failed turn in the same
conversation (turns/store.continue_turn: the stored context, no new prompt). Any other
failure ends it, in one place (`_fail`), as its schedule's When a run fails (`on_fail`)
says: a model error, a tool error that ends its turn, its turn stopped, or its ceiling.
Skip: the run is `skipped` with the error as its reason and its `error`, with no
notice; its day shows it. Retry:
the run is `failed` with the error; a run that failed on an error (not its ceiling, not
a stop) runs again as a fresh run of its prompt, up to three times (RUN_AGAIN: 15
minutes, 1 hour, then 4 hours after the try before): a waiting run `again_of` it, for the
same night (its dates are the first try's, `first_slot`), only when that is before the
schedule's next slot; the runs after it wait for it. A failure with no run again sends
the notice. Retry on the run's page runs
a failed, skipped or stopped run again now (`retry`). The ceiling is the Routine time
limit (Settings, Advanced, 0 none; a schedule's own `wall_minutes` makes it shorter): it
counts the run's work from its start, not its waits for a model: while any call of the
run, its sub-agents or its reviewer waits for a slot, a busy server or a GPU lease, its
clock stands still (RunClock, a backends/llm.HeldClock; overlapping waits count once), and the run reads `waiting`, its reason HELD (view `held`). A request that
waits for the user's answer runs on the clock: it waits as long as the run's limit lets
it. A run that waited for its model starts with a new clock. A run that was running when the app stopped is running
still at the next start: the scheduler continues its cut turn (`_resume`) and takes its
answer once the conversation is quiet (a reviewer's talk that was cut ends there). `end`
ends a waiting or running run with a reason: Stop on the run's page (`stopped`), its
schedule deleted (`skipped`), its ceiling (`_fail`). A run keeps its schedule's
label. Every change is `run.state {run}` on the global feed.
"""
from __future__ import annotations

import json
import logging
import posixpath
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

import anyio
from croniter import croniter

from .. import homes, metrics, prompts
from ..errors import NotFound
from ..backends.base import Done, TextDelta, ToolCallDelta
from ..backends.llm import HeldClock
from ..conversations import store as conversations, tree
from ..core.ids import iso, new_id, now
from ..core.states import LIVE_SQL
from ..skills import ToolContext
from ..clock import zone
from ..sub_agents.report import asked, last_answer, quiet
from . import NONE, depth, fill, night_of, run_date, shown
from ..settings.files import Invalid
from .variables import FROM_USER, extras
from ..core.event_types import RUN_STATE

log = logging.getLogger("celine.schedules")

TICK = 60.0                  # seconds between two looks without a wake
# a Retry routine's failed run runs again this long after it, the first, second and third time
RUN_AGAIN = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=4))
CATCH_UP_DAYS = 7
MAX_SLOTS = 2000             # slots made per schedule in one look
CHECKED = "schedules.checked"
REVIEWER_STEPS = 12          # the reviewer's tool steps before one line
READS = ("notebook", "past_conversations", "chat_archive")   # the reviewer's read tools
END = re.compile(r"\s*\bEND\s*$")

MISSED = f"Missed while the app was off for more than {CATCH_UP_DAYS} days"
DELETED = "Its schedule was deleted"
STOPPED = "Stopped by {{username}}"
IDLE_MINUTES = 30
IN_USE = "The app was in use"
FROZEN = "Frozen"            # a freeze cut its turn, or it may not start while frozen
PASSED = f"More than {IDLE_MINUTES} minutes late"
NEXT_DUE = "The next run was due"     # a Skip routine's older waiting run, at a newer slot
RETRY_OF = "Retry of the {at} run"    # a Retry routine's run again, until its time
HELD = "Waiting for the model"         # a running run's reason while its work waits (view)
MODEL_WAIT = "Waiting for "            # how a waiting run's reason starts while its model cannot answer
# a run's row for `view`, with the slot of the run it runs again and the id and slot of
# the run that runs it again (`retried`)
ROW = ("SELECT r.*, a.slot AS again_slot, x.id AS retried, x.slot AS retried_slot "
       "FROM runs r LEFT JOIN runs a ON a.id = r.again_of LEFT JOIN runs x ON x.id = "
       "(SELECT y.id FROM runs y WHERE y.again_of = r.id ORDER BY y.slot DESC LIMIT 1)")
WAITED_ON = ("((c.kind = 'mission' AND {mission}) OR (c.kind = 'chat' AND "
             "c.trigger_key IS NULL))")

def _repeated(slot: datetime, tz) -> bool:
    """The second 02:00 of the night the clocks go back: that wall time already ran."""
    local = slot.astimezone(tz)
    return local.utcoffset() != local.replace(fold=0).utcoffset()


def _say(cfg, name: str, **values) -> str:
    """A run's line from its .md beside this file (background.md, reviewer_first.md,
    talk_over.md, asked.md, line.md, last_line.md), filled (prompts.fill)."""
    return prompts.fill(cfg, __file__, name, **values).strip()


def final_words(note: str) -> str:
    """final.md (with note.md's clause for `note`, the notebook path, when there is one):
    what the result of a run is, said to it with the prompt and after a reviewer's talk."""
    clause = prompts.text(__file__, "note.md").strip().replace("{{path}}", note) if note else ""
    return prompts.text(__file__, "final.md").strip().replace("{{note}}", clause)


class RunFailed(Exception):
    """The agent's conversation gave no answer; `stopped` True when someone stopped it: the run
    fails, it never waits for its model then; `frozen` True when a freeze cut its turn: the
    run waits for the unfreeze."""

    def __init__(self, sentence: str, stopped: bool = False, frozen: bool = False,
                 code: str = ""):
        super().__init__(sentence)
        self.stopped = stopped
        self.frozen = frozen
        self.code = code            # the failed turn's reason_code


class RunClock(HeldClock):
    """The running run's HeldClock (backends/llm.py): its time limit (None: no limit,
    `left` is None), and `changed()` when it starts or stops waiting for a model."""

    def __init__(self, limit: timedelta | None, now, changed):
        super().__init__(limit or timedelta(0), now)
        self.limited = limit is not None
        self.changed = changed
        self.told = False        # the held state the feed was last told (Scheduler._tell)

    @property
    def held(self) -> bool:
        return self._held > 0

    def hold(self) -> None:
        super().hold()
        if self._held == 1:
            self.changed()

    def release(self) -> None:
        super().release()
        if self._held == 0:
            self.changed()

    def left(self):
        return super().left() if self.limited else None


# ---- rows ---------------------------------------------------------------------------------

def night_words(night: str) -> str:
    """A night or a date in a row's words: "Sat 10 Oct"."""
    d = date.fromisoformat(night)
    return f"{d:%a} {d.day} {d:%b}"


def why_alone(reason: str) -> str:
    """A run's reason without the "Waiting" its state word says already, for the day's
    list: "Waiting for Big. Main returned 503: ..." is "Main returned 503: ...", "Waiting
    until your chat is done" is "Until your chat is done"; any other reason as it is."""
    if reason.startswith("Waiting for ") and ". " in reason:
        return reason.split(". ", 1)[1]
    if reason.startswith("Waiting "):
        rest = reason.removeprefix("Waiting ")
        return rest[:1].upper() + rest[1:]
    return reason


def view(r: sqlite3.Row | dict, cfg, held: bool = False) -> dict:
    """A run as the API and the feed give it (a ROW): `at` its slot's time of day in the
    app's zone, `label` its schedule's (the one it kept once the schedule is gone);
    `state` waiting and `reason` HELD while it runs and its work waits for a model
    (`held`, its RunClock), `reason_why` its reason without the "Waiting" (why_alone);
    `again_of` the run it runs again and `retried` the run that runs it again, each
    {id, at} or None."""
    s = (getattr(cfg, "schedules", None) or {}).get(r["schedule"])
    waits = held and r["state"] == "running"
    reason = HELD if waits else r["reason"]

    def at(slot: str) -> str:
        return datetime.fromisoformat(slot).astimezone(zone(cfg)).strftime("%H:%M")
    return {"id": r["id"], "schedule": r["schedule"],
            "label": shown(s) if s else r["label"], "slot": r["slot"], "at": at(r["slot"]),
            "night": r["night"], "state": "waiting" if waits else r["state"],
            "reason": reason, "reason_why": why_alone(reason),
            "conversation_id": r["conversation_id"], "result": r["result"],
            "error": r["error"], "created_at": r["created_at"],
            "started_at": r["started_at"], "finished_at": r["finished_at"],
            "started_by": r["started_by"],
            "again_of": {"id": r["again_of"], "at": at(r["again_slot"])}
            if r["again_of"] else None,
            "retried": {"id": r["retried"], "at": at(r["retried_slot"])}
            if r["retried"] else None}


def _set(c: sqlite3.Connection, store, cfg, rid: str, held: bool = False,
         **fields) -> dict:
    """Inside a write: change run `rid` and tell the feed. -> its view."""
    if fields:
        c.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                  (*fields.values(), rid))
    out = view(c.execute(f"{ROW} WHERE r.id = ?", (rid,)).fetchone(), cfg, held)
    store.global_event(c, RUN_STATE, {"run": out})
    if fields.get("state") in ("done", "failed", "skipped", "stopped"):
        from ..events import added
        added(store, c, "run", out["conversation_id"])
        if out["conversation_id"]:
            from ..skills.mind import run_ended
            run_ended(c, store, out["conversation_id"])
        metrics.ROUTINE_RUNS.labels(out["schedule"], out["state"]).inc()
        if out["started_at"] and out["finished_at"]:
            metrics.ROUTINE_SECONDS.labels(out["schedule"]).observe(max(0.0, (
                datetime.fromisoformat(out["finished_at"])
                - datetime.fromisoformat(out["started_at"])).total_seconds()))
    return out


def _insert(c: sqlite3.Connection, store, cfg, name: str, slot: datetime,
            tz, state: str = "waiting", reason: str = "", by: str = "clock",
            night: str | None = None, again_of: str | None = None) -> dict | None:
    """The run of `name` at `slot`, None when it is there already; `by` who started it:
    the clock, the user (Run now, Retry) or it (routines__run). A run again (`again_of`)
    keeps the `night` of the run it runs again. A Skip routine's new waiting run ends its
    older waiting ones skipped (NEXT_DUE): after days off it runs once, for its newest
    slot. A run the user started neither ends others so nor is ended so."""
    rid = new_id()
    s = (getattr(cfg, "schedules", None) or {}).get(name)
    made = c.execute(
        "INSERT INTO runs(id, schedule, label, slot, night, state, reason, "
        "created_at, started_by, again_of) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(schedule, slot) DO NOTHING",
        (rid, name, shown(s) if s else name, iso(slot),
         night or night_of(slot, tz).isoformat(), state, reason, now(), by,
         again_of)).rowcount
    if not made:
        return None
    if state == "waiting" and s is not None and s.on_fail == "skip" and by != "user":
        for (old,) in c.execute("SELECT id FROM runs WHERE schedule = ? AND state = "
                                "'waiting' AND slot < ? AND started_by != 'user'",
                                (name, iso(slot))).fetchall():
            _set(c, store, cfg, old, state="skipped", reason=NEXT_DUE, finished_at=now())
    # a slot written skipped at once is finished, counted and in Events (_set)
    return _set(c, store, cfg, rid, **({} if state == "waiting" else
                                       {"state": state, "finished_at": now()}))


def _fail(c: sqlite3.Connection, store, cfg, rid: str, s, error: str, again: bool,
          at: datetime) -> dict:
    """Inside a write: the one place a run ends unfinished, as its schedule's When a run
    fails says. Skip: `skipped`, `error` its reason and its error, no notice. Retry: `failed` with
    `error`; a run that failed on an error (`again`: not its time limit, not a stop) with
    fewer than len(RUN_AGAIN) tries before it runs again, RUN_AGAIN[tries] from `at` for
    the same night, when that is before the schedule's next slot: a waiting run `again_of`
    it, its notice left to how that ends. Otherwise the notice. -> the run's view."""
    if s is not None and s.on_fail == "skip":
        return _set(c, store, cfg, rid, state="skipped", reason=error, error=error,
                    finished_at=now())
    r = c.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
    tz = zone(cfg)
    tries = len(_chain(c, r)) - 1
    slot = (at + RUN_AGAIN[tries]).replace(microsecond=0) if tries < len(RUN_AGAIN) else None
    retry = again and s is not None and slot is not None and croniter(
        s.cron, datetime.fromisoformat(r["slot"]).astimezone(tz)).get_next(datetime) > slot
    out = _set(c, store, cfg, rid, state="failed", error=error, finished_at=now())
    if retry:
        _insert(c, store, cfg, s.name, slot, tz, reason=RETRY_OF.format(at=out["at"]),
                night=r["night"], again_of=rid)
    else:
        from ..events import notice
        notice(store, c, "run", out["label"], error or "Failed",
               homes.run_link(out["night"], rid), f"run:{rid}")
    return out


def _chain(c: sqlite3.Connection, r) -> list:
    """Run `r`, then the run it runs again (`again_of`), and so on to the first try."""
    out = [r]
    while out[-1]["again_of"]:
        up = c.execute("SELECT slot, again_of FROM runs WHERE id = ?",
                       (out[-1]["again_of"],)).fetchone()
        if up is None:
            break
        out.append(up)
    return out


def first_slot(c: sqlite3.Connection, r) -> str:
    """The slot of the first try of a run again, else its own: a run again fills the
    dates of the first try."""
    return _chain(c, r)[-1]["slot"]


def results(c: sqlite3.Connection, night: str) -> dict[str, str]:
    """{{result:<schedule>}} of every schedule with a finished run that night: its
    answer, or "(no result: <why>)"; the latest run of a schedule counts."""
    out: dict[str, str] = {}
    for r in c.execute("SELECT schedule, state, result, error, reason FROM runs WHERE "
                       "night = ? AND state IN ('done', 'failed', 'skipped', 'stopped') "
                       "ORDER BY slot",
                       (night,)):
        out[r["schedule"]] = r["result"] if r["state"] == "done" and r["result"] else \
            f"(no result: {r['error'] or r['reason'] or 'no answer'})"
    return out


def waited_on(c: sqlite3.Connection, missions: bool = False) -> bool:
    """A turn a person waits on is live (WAITED_ON): in a mission the turns the user
    started, every one of its turns with `missions` (Routines wait for missions)."""
    mission = "1" if missions else "t.origin = 'user'"
    return c.execute(
        f"SELECT 1 FROM turns t JOIN conversations c ON c.id = t.conversation_id "
        f"WHERE {WAITED_ON.format(mission=mission)} AND t.state IN {LIVE_SQL} LIMIT 1"
    ).fetchone() is not None


def idle(c: sqlite3.Connection, at: datetime, missions: bool = False) -> bool:
    """Nobody used the app for IDLE_MINUTES before `at`: no turn a person waits on is
    live (waited_on, `missions` as there), and no user message came."""
    since = iso(at - timedelta(minutes=IDLE_MINUTES))
    return not waited_on(c, missions) and c.execute(
        f"SELECT 1 FROM messages m WHERE {FROM_USER} AND m.created_at >= ? LIMIT 1",
        (since,)).fetchone() is None


def binding(c: sqlite3.Connection, cid: str, cfg) -> dict | None:
    """How a turn of conversation `cid` binds its model when it is a run's, at any depth
    (its root, homes.root, is the run's conversation): {priority: BACKGROUND, label: the
    schedule's, run: the run's id}, on the role's own model only; None for any other
    conversation. turns/dispatcher.py binds with it (`run_binding`, set at start, which
    adds the running run's clock)."""
    from ..backends.queue import Priority
    top = homes.root(c, cid)
    run = top and c.execute("SELECT id, schedule, label FROM runs WHERE conversation_id = ?",
                            (top["id"],)).fetchone()
    if not run:
        return None
    s = (getattr(cfg, "schedules", None) or {}).get(run["schedule"])
    return {"priority": Priority.BACKGROUND, "label": shown(s) if s else run["label"],
            "run": run["id"]}


def finished(c: sqlite3.Connection, cid: str) -> bool:
    """The agent's conversation is quiet, and so is every sub-agent of it, each reported."""
    if not quiet(c, cid):
        return False
    return all(quiet(c, s) and asked(c, s, cid) is None for (s,) in c.execute(
        "SELECT id FROM conversations WHERE agent_of = ?", (cid,)).fetchall())


def outcome(c: sqlite3.Connection, cid: str) -> tuple[str, str, str]:
    """(state, reason, reason_code) of the conversation's last turn."""
    r = c.execute("SELECT state, reason, reason_code FROM turns WHERE conversation_id = ? "
                  "ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
    return (r["state"], r["reason"] or "", r["reason_code"] or "") if r else ("done", "", "")


def _said(c: sqlite3.Connection, cid: str) -> tuple[str, bool]:
    """(the run's last answer, it is the final words of its latest turn): the final words
    (conversations/tree.final_words), else the lead-in beside the tool call its turn
    ended with (sub_agents/report.last_answer)."""
    t = c.execute("SELECT id FROM turns WHERE conversation_id=? ORDER BY created_at DESC "
                  "LIMIT 1", (cid,)).fetchone()
    words = tree.final_words(c, t["id"]) if t else ""
    return (words, True) if words else (last_answer(c, cid), False)


def _of_note(args: str, note: str) -> bool:
    """`args`, a stored tool call's arguments, name the notebook path `note`."""
    try:
        path = json.loads(args).get("path")
    except (ValueError, AttributeError):
        return False
    return isinstance(path, str) and posixpath.normpath(path) == posixpath.normpath(note)


def _answer(c: sqlite3.Connection, cid: str, iid: int) -> str:
    """The agent's words after intercom message `iid`."""
    chain = tree.path(c, cid)
    at = next((i for i, m in enumerate(chain)
               if (m["intercom"] or {}).get("id") == iid), len(chain))
    return "\n\n".join(m["content"] for m in chain[at + 1:]
                       if m["role"] == "assistant" and (m["content"] or "").strip())


# ---- the scheduler ------------------------------------------------------------------------

class Scheduler:
    """The app's one scheduler: `run(tg)` at start, `wake()` after a change."""

    def __init__(self, app, clock=None):
        self.app = app
        self.now = clock or (lambda: datetime.now(timezone.utc))
        self._wake = anyio.Event()
        self._tg = None
        self.current: str | None = None      # the run that runs, by id
        self._clock: RunClock | None = None   # its time limit, held while it waits
        self._over = ""                       # the reason it fails with past the limit
        self._scope: anyio.CancelScope | None = None   # its work, for `end`
        self._turn_ended = anyio.Event()      # set and replaced at every turn or job end
        self._told: set[str] = set()          # the nights whose wait for a model was told
        on_end = getattr(app.dispatcher, "on_end", None)
        if on_end is not None:               # a turn ended: a run may start or be over
            on_end(self._ended)

    @property
    def schedules(self) -> dict:
        return getattr(self.app.cfg, "schedules", None) or {}

    def wake(self) -> None:
        self._wake.set()

    async def run(self, tg) -> None:
        self._tg = tg
        from ..settings import files, seed
        root, cfg = files.settings_dir(self.app.cfg), self.app.cfg
        if seed.schedules(root):              # the first start with routines on
            from . import load
            cfg.schedules = load(seed.schedules_dir(root), cfg.problems, cfg.substitute)
        resume = await self.app.store.write(self._interrupted)
        if getattr(self.app.dispatcher, "on_end", None) is not None:
            # its turns in a run: behind every interactive request, on its own model
            self.app.dispatcher.run_binding = self._binding
        for r in resume:
            self._start(r)
        tg.start_soon(self._loop)

    def _binding(self, c: sqlite3.Connection, cid: str) -> dict | None:
        """`binding`, with the run's clock while that run runs."""
        b = binding(c, cid, self.app.cfg)
        if b is not None:
            b["clock"] = self._clock if b["run"] == self.current else None
        return b

    async def _ended(self, _cid: str, _turn: str | None) -> None:
        ended, self._turn_ended = self._turn_ended, anyio.Event()
        ended.set()
        self.wake()

    def _interrupted(self, c: sqlite3.Connection) -> list[dict]:
        """The run that was running when the app stopped runs on (module docstring):
        -> its row (one at most, runs_one_running), for `run` to pick up."""
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE state = 'running'")]

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed", extra={"event": "schedules.tick_failed"})
            await self._sleep()

    async def _sleep(self) -> None:
        router = getattr(self.app.llm, "router", None)
        wait = self.until_next()
        async with anyio.create_task_group() as tg:
            async def first(fn) -> None:
                await fn()
                tg.cancel_scope.cancel()
            tg.start_soon(first, self._wake.wait)
            if router is not None:
                tg.start_soon(first, lambda: router.wait_change(wait))
            else:
                tg.start_soon(first, lambda: anyio.sleep(wait))
        self._wake = anyio.Event()

    def until_next(self) -> float:
        """Seconds to the next slot of a schedule that is on, or to the running run's
        ceiling, TICK at most."""
        at = self.now().astimezone(zone(self.app.cfg))
        nxt = min((croniter(s.cron, at).get_next(datetime)
                   for s in self.schedules.values() if s.on), default=None)
        wait = TICK if nxt is None else min(TICK, max(0.0, (nxt - at).total_seconds()))
        left = self._clock.left() if self._clock is not None else None
        if left is not None:                  # the running run's ceiling, while not held
            wait = min(wait, max(0.0, left.total_seconds()))
        return wait

    # -- making runs

    async def make_due(self) -> None:
        """The runs of every slot that came due since the last look (module docstring)."""
        at, tz, schedules, store = self.now(), zone(self.app.cfg), self.schedules, \
            self.app.store
        since = await store.kv_get(CHECKED)
        start = datetime.fromisoformat(since) if since and self.ready() else at

        def fn(c: sqlite3.Connection) -> None:
            horizon, checked = at - timedelta(days=CATCH_UP_DAYS), at
            for s in schedules.values():
                if not s.on:
                    continue
                it = croniter(s.cron, start.astimezone(tz))
                # note: a cron line that fires every minute after months off makes
                # at most MAX_SLOTS rows per look; the look is checked up to its last
                # slot, so the next look makes the rest
                for _ in range(MAX_SLOTS):
                    slot = it.get_next(datetime)
                    if slot > at:
                        break
                    if _repeated(slot, tz):
                        continue
                    old = slot < horizon
                    _insert(c, store, self.app.cfg, s.name, slot, tz,
                            "skipped" if old else "waiting", MISSED if old else "")
                else:
                    checked = min(checked, slot)
            if iso(checked) != since:
                c.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET "
                          "v = excluded.v", (CHECKED, iso(checked)))
        await store.write(fn)

    def ready(self) -> bool:
        """The onboarding is done: routines may run."""
        from ..settings import features
        return features.done(getattr(getattr(self.app.cfg, "settings", None), "features", None))

    async def forget(self) -> None:
        """Routines was switched off: the next start counts from its first look."""
        await self.app.store.write(lambda c: c.execute("DELETE FROM kv WHERE k = ?",
                                                       (CHECKED,)))

    async def run_now(self, name: str, by: str = "user") -> dict:
        """A run of `name` at this second, waiting like any other, started `by` the user
        or it."""
        tz = zone(self.app.cfg)
        at = self.now().replace(microsecond=0)
        out = await self.app.store.write(lambda c: _insert(
            c, self.app.store, self.app.cfg, name, at, tz, by=by))
        self.wake()
        return out or await self.get_by(name, at)

    async def get_by(self, name: str, slot: datetime) -> dict:
        return await self.app.store.read(lambda c: view(c.execute(
            f"{ROW} WHERE r.schedule = ? AND r.slot = ?",
            (name, iso(slot))).fetchone(), self.app.cfg))

    async def retry(self, rid: str) -> dict:
        """Run `rid` (failed, skipped or stopped, not run again yet) runs again for its
        night, now: a waiting run `again_of` it, started by the user. Raises NotFound,
        or Invalid when it may not."""
        store, cfg, at = self.app.store, self.app.cfg, self.now().replace(microsecond=0)

        def fn(c: sqlite3.Connection) -> dict:
            r = c.execute(f"{ROW} WHERE r.id = ?", (rid,)).fetchone()
            if r is None:
                raise NotFound("run")
            if r["state"] not in ("failed", "skipped", "stopped"):
                raise Invalid("run", "Only a failed, skipped or stopped run can be retried.")
            if r["retried"]:
                raise Invalid("run", "This run was retried already.")
            if r["schedule"] not in self.schedules:
                raise Invalid("run", f"{DELETED}.")
            out = _insert(c, store, cfg, r["schedule"], at, zone(cfg), by="user",
                          night=r["night"], again_of=rid)
            if out is None:
                raise Invalid("run", "Another run of this schedule starts at this second.")
            return out
        out = await store.write(fn)
        self.wake()
        return out

    @property
    def held(self) -> str | None:
        """The running run while its work waits for a model (its RunClock is held)."""
        return self.current if self._clock is not None and self._clock.held else None

    async def end(self, rid: str, state: str, why: str,
                  from_states: tuple[str, ...] = ("waiting", "running")) -> dict | None:
        """Run `rid`, while in one of `from_states`, ends as `state` with `why` (its
        reason; failed goes through `_fail`, its time limit, never run again); a running
        one's work is cancelled, its turns aborted as a chat's Stop does and the
        missions it started stopped. None: no
        such run, or it is in no such state."""
        store, cfg = self.app.store, self.app.cfg
        if rid == self.current and self._scope is not None:
            self._scope.cancel()

        def fn(c: sqlite3.Connection):
            r = c.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
            if r is None or r["state"] not in from_states:
                return None, None
            out = _fail(c, store, cfg, rid, self.schedules.get(r["schedule"]), why, False,
                        self.now()) if state == "failed" else \
                _set(c, store, cfg, rid, state=state, finished_at=now(), reason=why)
            return out, r["conversation_id"] if r["state"] == "running" else None
        out, cid = await store.write(fn)
        abort = getattr(self.app.dispatcher, "abort_conversation", None)
        if cid and abort is not None:
            await abort(cid)
            from ..missions.skill import stop_started
            await stop_started(self.app, cid)       # the missions it started stop too
        log.info("run ended", extra={"event": "schedules.ended", "run": rid,
                                     "state": state, "why": why})
        self.wake()
        return out

    async def stop_schedule(self, name: str) -> None:
        """Every waiting or running run of `name` stops: its schedule is deleted."""
        ids = await self.app.store.read(lambda c: [r[0] for r in c.execute(
            "SELECT id FROM runs WHERE schedule = ? AND state IN ('waiting', 'running')",
            (name,))])
        for rid in ids:
            await self.end(rid, "skipped", DELETED)

    # -- starting runs

    async def tick(self) -> None:
        if self.current:
            await self._watch()
        await self.make_due()
        if not self.ready():
            return
        store, schedules = self.app.store, self.schedules
        waiting = await store.read(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM runs WHERE state = 'waiting'")])
        if not waiting:
            return
        waiting.sort(key=lambda r: (r["night"], depth(schedules, r["schedule"]), r["slot"]))
        missions = bool(self.app.cfg.r("routines_wait_for_missions"))
        busy = await store.read(lambda c: waited_on(c, missions))
        from ..guard.freeze import frozen
        cold = await frozen(store) is not None
        unused = await store.read(lambda c: idle(c, self.now(), missions))
        open_ = await store.read(lambda c: {(r["night"], r["schedule"]) for r in c.execute(
            "SELECT night, schedule FROM runs WHERE state IN ('waiting', 'running')")})
        models: dict[str, str | None] = {}
        for r in waiting:
            if datetime.fromisoformat(r["slot"]) > self.now():
                continue                     # a run again waits for its time (RETRY_OF)
            s = schedules.get(r["schedule"])
            if s is None:
                await store.write(lambda c, r=r: _set(
                    c, store, self.app.cfg, r["id"], state="skipped", reason=DELETED,
                    finished_at=now()))
                continue
            skip = self._idle_skip(r, s, unused)
            if skip:
                await store.write(lambda c, r=r, skip=skip: _set(
                    c, store, self.app.cfg, r["id"], state="skipped", reason=skip,
                    finished_at=now()))
                continue
            why = FROZEN if cold else await self._why(r, s, open_, busy, models)
            if why is None:
                self._start(r)
                continue
            if why != r["reason"]:
                # the first run of a night that waits for its model tells the user
                tell = why.startswith(MODEL_WAIT) and r["night"] not in self._told
                if tell:
                    self._told.add(r["night"])

                def fn(c: sqlite3.Connection, r=r, why=why, tell=tell) -> None:
                    out = _set(c, store, self.app.cfg, r["id"], reason=why)
                    if tell:
                        from ..events import notice
                        notice(store, c, "run", out["label"], why,
                               homes.run_link(out["night"], r["id"]), f"run:{r['id']}:waits")
                await store.write(fn)

    def _start(self, r: dict) -> None:
        """Run `r` runs now, in the task group, with a new clock for its ceiling."""
        s, rid = self.schedules.get(r["schedule"]), r["id"]
        self.current = rid
        cap = [m for m in (s and s.wall_minutes, self.app.cfg.r("routine_max_minutes")) if m]
        self._clock = RunClock(timedelta(minutes=min(cap)) if cap else None, self.now,
                               lambda: self._tell(rid))
        n = min(cap) if cap else 0
        self._over = f"Stopped at its time limit of {n} minute{'' if n == 1 else 's'}." \
            if cap else ""
        self._tg.start_soon(self._execute, rid)

    def _tell(self, rid: str) -> None:
        """The running run's work started or stopped waiting for a model: its `run.state`
        says so (view `held`) once the writer gets to it, only when that changed then,
        so a wait that ended at once tells nothing."""
        clock, store, cfg = self._clock, self.app.store, self.app.cfg

        def fn(c: sqlite3.Connection) -> None:
            if clock is None or clock is not self._clock or clock.held == clock.told:
                return
            clock.told = clock.held
            _set(c, store, cfg, rid, held=clock.held)
        store.write_soon(fn)

    async def _watch(self) -> None:
        """The running run's ceiling (module docstring) fails it. The ceiling counts the
        run's work: while a call of its conversation or of a sub-agent under it waits for
        a model, or a turn of theirs waits for the pause (`_paused`), its clock stands
        still (backends/llm.HeldClock). A request of the agent's that
        waits for the user runs on the clock; when the limit ends the run meanwhile, the
        reason names it (approvals.lapsed)."""
        rid, clock = self.current, self._clock
        row = await self.app.store.read(lambda c: c.execute(
            "SELECT conversation_id FROM runs WHERE id = ?", (rid,)).fetchone())
        if clock is not None and row and row[0]:
            # ponytail: looked at once a tick, so up to TICK seconds of a pause count as work
            idle = await self._paused(row[0])
            if idle != getattr(clock, "idle", False):
                clock.idle = idle
                clock.hold() if idle else clock.release()
        left = clock.left() if clock is not None else None
        if left is not None and left < timedelta(0):
            approvals = getattr(self.app, "approvals", None)
            why = approvals.lapsed(row[0]) if approvals is not None and row and row[0] else None
            await self.end(rid, "failed", why or self._over, ("running",))

    async def _paused(self, cid: str) -> bool:
        """A turn of the run's conversation `cid`, or of a sub-agent under it, is queued
        and waits for the pause (hold.py): the run's clock stands still meanwhile."""
        from .. import hold
        models = hold.answering(getattr(self.app, "llm", None))
        if models is None:
            return False

        def fn(c: sqlite3.Connection) -> bool:
            family = conversations.family(c, cid)
            return c.execute(
                "SELECT 1 FROM turns t JOIN conversations c ON c.id = t.conversation_id "
                f"WHERE t.state = 'queued' AND {hold.held_sql(models)} AND c.id IN "
                f"({','.join('?' * len(family))}) LIMIT 1", family).fetchone() is not None
        return await self.app.store.read(fn)

    def _idle_skip(self, r: dict, s, unused: bool) -> str | None:
        """Why an `idle_only` run is skipped now, None when it may wait on: one that
        began already (it waits for its model) and one the user started (Run now, Retry)
        wait like any run."""
        if not s.idle_only or r["conversation_id"] or r["started_by"] == "user":
            return None
        if self.now() - datetime.fromisoformat(r["slot"]) > timedelta(minutes=IDLE_MINUTES):
            return PASSED
        return None if unused else IN_USE

    async def _why(self, r: dict, s, open_: set, busy: bool, models: dict) -> str | None:
        """None when run `r` may start now, else the first reason it may not."""
        for a in s.after:
            if (r["night"], a) in open_:
                other = self.schedules.get(a)
                return f"Runs after {shown(other) if other else a}"
        if s.role not in models:
            models[s.role] = await self._model_why(s.role)
        if models[s.role]:
            return models[s.role]
        if busy:
            return "Waiting until your chat is done"
        if self.current is not None:
            label = await self.app.store.read(lambda c: c.execute(
                "SELECT schedule FROM runs WHERE id = ?", (self.current,)).fetchone())
            other = self.schedules.get(label["schedule"]) if label else None
            return f"While {shown(other) if other else 'another run'} runs"
        return None

    async def _model_why(self, role: str, probe: bool = True) -> str | None:
        """"Waiting for <model>. <why>" while the Router says the first model of `role`
        (the one a run uses) cannot answer now, else None. `probe` False: the Router's
        state as it stands, no new question to the server."""
        if getattr(self.app.dispatcher, "backend_factory", None) is not None:
            return None                          # one fixed backend answers every role
        router = getattr(self.app.llm, "router", None)
        why = None if router is None else await router.can_answer(role, probe=probe)
        return None if why is None else self._waiting(router, role, why)

    @staticmethod
    def _waiting(router, role: str, why: str) -> str:
        refs = router.chain(role) if getattr(router, "chain", None) else []
        if refs:
            from ..settings.agent_models import label
            return f"{MODEL_WAIT}{label(router.s, refs[0])}. {why}"
        return f"{MODEL_WAIT}a model. {why}"

    async def _failed_why(self, role: str, code: str, error: str) -> str | None:
        """Why a run whose turn failed waits instead of failing, else None: its turn got
        no model (`no_model`), or the Router's state, as the failure left it (after a
        cut stream: as its server answers now), says the run's model cannot answer. A model that answered with an error it may answer
        again fails the run, as its schedule's When a run fails says (`_fail`)."""
        router = getattr(self.app.llm, "router", None)
        if code == "backend_retryable" and getattr(router, "probe", None) is not None \
                and (refs := router.chain(role)):
            # the stream was cut: the server is asked now, so one that went away is known
            await router.probe(router.s.split(refs[0])[0], max_age=0, strict=True)
        if (why := await self._model_why(role, probe=False)) is not None:
            return why
        if code == "no_model":
            return self._waiting(router, role, error) if router is not None else None
        return None

    # -- one run

    async def _execute(self, rid: str) -> None:
        try:
            with anyio.CancelScope() as self._scope:
                r = await self.app.store.read(lambda c: dict(c.execute(
                    "SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()))
                await self._run_as(rid, r)
        finally:
            self.current = self._clock = self._scope = None
            self.wake()

    async def _run_as(self, rid: str, r: dict) -> None:
        """The run: its conversation, its name in the prompt, its notebook for the note.
        With a conversation already (it waited for its model, or the app restarted) the
        run goes on in it."""
        cfg, store, schedules = self.app.cfg, self.app.store, self.schedules
        s = schedules.get(r["schedule"])
        if s is None:
            await store.write(lambda c: _set(c, store, cfg, rid, state="skipped",
                                             reason=DELETED, finished_at=now()))
            return
        first = await store.read(lambda c: first_slot(c, r))
        day = run_date(s, datetime.fromisoformat(first), zone(cfg))
        fresh = not r["conversation_id"]
        started = r["started_at"] or now()

        def begin(c: sqlite3.Connection, cid: str) -> dict | None:
            # only a run still waiting or running: a Stop may have ended it meanwhile
            if not c.execute(
                    "UPDATE runs SET state = 'running', reason = '', conversation_id = ?, "
                    "started_at = ? WHERE id = ? AND state IN ('waiting', 'running')",
                    (cid, started, rid)).rowcount:
                return None
            return _set(c, store, cfg, rid)
        try:
            cid = r["conversation_id"] or await conversations.for_trigger(
                store, f"run:{rid}", f"{shown(s)}, {night_words(day.isoformat())}")
            if fresh:
                from ..settings import roles
                await roles.resolve(store, cfg, cid)
            if await store.write(lambda c: begin(c, cid)) is None:
                log.info("run ended before it began", extra={"event": "schedules.gone",
                                                              "run": rid})
                return
            log.info("run begins", extra={"event": "schedules.run", "run": rid,
                                          "schedule": s.name, "conversation": cid,
                                          "fresh": fresh})
            night = date.fromisoformat(r["night"])
            await self._backups(night)
            res = await store.read(lambda c: results(c, r["night"]))
            note = fill(cfg, s.note, day, res) if s.note else ""
            where = final_words(note)
            # what the note is its own against: what was there before its turns; a note
            # it touched since the run began is its own whole
            start = self._note_text(note)
            if not fresh and start is not None and self._note_touched(note, started):
                start = None
            if not fresh:
                answer, final = await self._resume(cid)
            elif s.reviewer:
                prompt = fill(cfg, s.prompt, day, res, await extras(self.app, s.prompt, night))
                answer, final = await self._dialogue(rid, s, cid, prompt, day, res, where)
            else:
                prompt = fill(cfg, s.prompt, day, res, await extras(self.app, s.prompt, night))
                answer, final = await self._turn(cid, "system:schedules", "\n\n".join(
                    [prompt, _say(cfg, "background.md", final=where)]), "trigger",
                    {"run": rid})
            answer, own = self._take(note, start, answer, await self._wrote(cid, note))
            if not note:
                error = None
            elif own:
                error = await self._stale(cid, note)
            elif not final:
                error = (f"The note {note} was not written: the run ended with a tool call "
                         "and no answer after it.")
            else:
                error = await self._write_note(cid, note, answer, s, day.isoformat())
        except Exception as e:                   # noqa: BLE001 - every failure ends it
            error = str(e) or type(e).__name__
            why = FROZEN if getattr(e, "frozen", False) else \
                None if getattr(e, "stopped", False) else \
                await self._failed_why(s.role, getattr(e, "code", ""), error)
            if why:              # its model is gone, or a freeze: the run waits for it
                await store.write(lambda c: _set(c, store, cfg, rid, state="waiting",
                                                 reason=why))
            else:
                again = not getattr(e, "stopped", False)
                await store.write(lambda c: _fail(c, store, cfg, rid, s, error, again,
                                                  self.now()))
            log.info("run failed", extra={"event": "schedules.failed", "run": rid,
                                          "waits": bool(why), "error": error})
            return
        await store.write(lambda c: _set(c, store, cfg, rid, state="done", result=answer,
                                         error=error, finished_at=now()))
        log.info("run done", extra={"event": "schedules.done", "run": rid})

    async def _resume(self, cid: str) -> tuple[str, bool]:
        """The run goes on in `cid`: its last turn, when it ended without an answer and
        can be continued, is continued (turns/store.continue_turn); then its answer as
        `_said`. Raises RunFailed as `_turn`."""
        from ..turns import store as turns
        last = await self.app.store.read(lambda c: c.execute(
            "SELECT id, state, reason_code FROM turns WHERE conversation_id = ? ORDER BY "
            "created_at DESC LIMIT 1", (cid,)).fetchone())
        if last is not None and (last["state"] in ("failed", "interrupted") or (
                last["state"] == "aborted" and last["reason_code"] == "frozen")):
            try:
                await turns.continue_turn(self.app.store, cid, last["id"],
                                          self.app.registry)
            except (KeyError, ValueError, tree.Busy) as e:
                log.info("run turn not continued", extra={"event": "schedules.not_continued",
                                                           "conversation": cid, "why": str(e)})
            else:
                notify = getattr(self.app.dispatcher, "notify", None)
                if notify is not None:
                    notify()
        return await self._said(cid)

    async def _turn(self, cid: str, sender: str, text: str, kind: str = "message",
                    data: dict | None = None) -> tuple[str, bool]:
        """`text` to its conversation, then its answer as `_said`. Raises RunFailed when
        its turn failed or gave no answer."""
        await self.app.intercom.send(sender, f"agent:{cid}", text, kind=kind, data=data)
        return await self._said(cid)

    async def _said(self, cid: str) -> tuple[str, bool]:
        """Once everything in `cid` is quiet: (its answer, it is the final words of its
        last turn). Not final: the turn ended with a tool call and the answer is the
        lead-in beside it (sub_agents/report.last_answer). Raises RunFailed when it said
        nothing."""
        await self._settle(cid)
        answer, final = await self.app.store.read(lambda c: _said(c, cid))
        if not answer:
            raise RunFailed("No answer")
        return answer, final

    async def _backups(self, night: date) -> None:
        """The night's backups, when this is its first run (schedules/backups.py)."""
        from .backups import nightly
        cfg = self.app.cfg
        data = getattr(cfg, "data_dir", None)
        if data is None:
            return
        root = cfg.agent.notebook if getattr(cfg, "agent", None) else None
        try:
            await anyio.to_thread.run_sync(nightly, data, cfg.db_path, root, night,
                                           cfg.r("backup_keep_days"))
        except Exception:                      # noqa: BLE001 - the run goes on
            log.exception("backup failed", extra={"event": "backup.failed"})

    def _note_path(self, note: str):
        return self.app.cfg.agent.notebook / note if note and getattr(
            self.app.cfg, "agent", None) else None

    def _note_text(self, note: str) -> str | None:
        """What `note` holds now, None when it is not there (or there is no note)."""
        p = self._note_path(note)
        return p.read_text(errors="replace") if p is not None and p.is_file() else None

    def _note_touched(self, note: str, since: str) -> bool:
        """`note` was written after `since` (the run's start)."""
        p = self._note_path(note)
        return p is not None and p.is_file() and datetime.fromtimestamp(
            p.stat().st_mtime, timezone.utc) >= datetime.fromisoformat(since)

    def _take(self, note: str, start: str | None, answer: str,
              wrote: bool) -> tuple[str, bool]:
        """(the result, it wrote it): when it wrote `note` itself (`wrote`), the note, and
        the app writes nothing over it; only what it added when it added to `start` (what
        was there before its turns). Else its answer."""
        from ..skills.notebook import unstamped
        text = self._note_text(note)
        if not note or not wrote or text is None:
            return answer, False
        now_, was = (unstamped(t or "").strip() for t in (text, start))
        return (now_[len(was):].strip() if was and now_.startswith(was) and now_ != was
                else now_), True

    async def _wrote(self, cid: str, note: str) -> bool:
        """The run wrote `note` itself: a notebook__write or notebook__edit of that path
        that ended ok in its conversation."""
        if not note:
            return False
        rows = await self.app.store.read(lambda c: c.execute(
            "SELECT tc.arguments FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
            "WHERE m.conversation_id=? AND tc.state='ok' AND tc.name IN "
            "('notebook__write', 'notebook__edit')", (cid,)).fetchall())
        return any(_of_note(args, note) for (args,) in rows)

    async def _stale(self, cid: str, note: str) -> str | None:
        """`note`, which the run wrote: the sentence when ok tool calls ran after its last
        write of it and the note is older than the last of them; else None."""
        rows = await self.app.store.read(lambda c: c.execute(
            "SELECT tc.name, tc.arguments, tc.finished_at FROM tool_calls tc JOIN messages m "
            "ON m.id = tc.message_id WHERE m.conversation_id=? AND tc.state='ok' ORDER BY "
            "tc.finished_at", (cid,)).fetchall())
        later: list[str] = []
        for name, args, at in rows:
            later = [] if name in ("notebook__write", "notebook__edit") and \
                _of_note(args, note) else later + [at]
        if not later or self._note_touched(note, later[-1]):
            return None
        n = len(later)
        calls = "last tool call" if n == 1 else f"last {n} tool calls"
        return f"The note {note} was last written before the run's {calls}."

    async def _write_note(self, cid: str, path: str, answer: str, s, day: str) -> str | None:
        """The agent's answer into the notebook at `path`: a note there already gets it appended
        under "## <day> <label>", `day` the run's date (run_date) (its own renames and
        edits stay). -> the sentence why not, or None."""
        spec = getattr(self.app.registry, "tools", {}).get("notebook__write")
        if spec is None:
            return f"The note {path} was not written: the notebook is off."
        there = (self.app.cfg.agent.notebook / path).is_file()
        out = await spec.fn(self._ctx(cid), path=path, summary=f"{shown(s)}, "
                            f"{night_words(day)}",
                            **({"content": f"\n\n## {day} {shown(s)}\n\n{answer}\n",
                                "mode": "append"} if there else {"content": answer}))
        return f"The note {path} was not written: {out.content}" if out.is_error else None

    def _ctx(self, cid: str) -> ToolContext:
        async def quiet_emit(*_a, **_k) -> None:
            return None
        return ToolContext(conversation_id=cid, turn_id="", tool_call_id="",
                           store=self.app.store, config=self.app.cfg, emit=quiet_emit,
                           max_output_tokens=8000)

    # -- a reviewer

    async def _dialogue(self, rid: str, s, cid: str, prompt: str, day: date,
                        res: dict, where: str) -> tuple[str, bool]:
        """The agent and the reviewer alternate (module docstring); -> its final answer
        as `_said`."""
        cfg = self.app.cfg
        who = s.reviewer.removesuffix(".md")
        sender = f"system:{who}"
        rule = _say(cfg, "background.md", final=where)
        line = _say(cfg, "reviewer_first.md", who=who)
        history = [{"role": "user", "content": (await self._turn(
            cid, "system:schedules", "\n\n".join([prompt, line, rule]), "trigger",
            {"run": rid}))[0]}]
        base = "\n\n".join([fill(cfg, s.reviewer_prompt, day, res),
                            _say(cfg, "asked.md"), prompt])
        backend, tools = self._reviewer_backend(cid, s), self._reviewer_tools()
        for n in range(1, s.turns + 1):
            last = n == s.turns
            system = f"{base}\n\n" + (_say(cfg, "last_line.md") if last else
                                         _say(cfg, "line.md", n=n, turns=s.turns))
            said = await self._line(backend, cid, system, history, tools)
            ends = last or bool(END.search(said))
            said = END.sub("", said).strip()
            if not said:
                break
            history.append({"role": "assistant", "content": said})
            sent = await self.app.intercom.send(sender, f"agent:{cid}", said)
            await self._settle(cid)
            history.append({"role": "user", "content": await self.app.store.read(
                lambda c: _answer(c, cid, sent["intercom_id"])) or "(no answer)"})
            if ends:
                break
        return await self._turn(cid, "system:schedules", _say(
            cfg, "talk_over.md", who=who, final=where), "trigger", {"run": rid})

    async def _settle(self, cid: str) -> None:
        """Until its conversation is `finished`, looked at after every turn or job end;
        raises RunFailed when its last turn was stopped or failed."""
        store = self.app.store
        while True:
            ended = self._turn_ended         # taken before the look: no end is missed
            if await store.read(lambda c: finished(c, cid)):
                break
            await ended.wait()
        state, reason, code = await store.read(lambda c: outcome(c, cid))
        if state == "aborted" and reason == "frozen":
            raise RunFailed(FROZEN, frozen=True)
        if state == "aborted":
            raise RunFailed("Stopped", stopped=True)
        if state == "failed":
            raise RunFailed(reason or "The turn failed", code=code)

    def _reviewer_backend(self, cid: str, s):
        """The reviewer's model: behind every interactive request, named by the
        schedule's label, on the role's own model only; never its Banned words, the
        reviewer is not it."""
        d = self.app.dispatcher
        fixed = d.backend_factory() if getattr(d, "backend_factory", None) else None
        if self.app.llm is None:
            return fixed
        from ..backends.queue import Priority
        return self.app.llm.bind(fixed, role=s.role, priority=Priority.BACKGROUND,
                                 conversation_id=cid, label=shown(s), fallback=False,
                                 voice=False, wait=0, run_clock=self._clock)

    def _reviewer_tools(self) -> dict:
        reg = self.app.registry
        if reg is None:
            return {}
        return {k: s for k, s in reg.tools.items() if s.skill in READS
                and s.annotations.read_only and reg.down(s.skill) is None}

    async def _line(self, backend, cid: str, system: str, history: list[dict],
                    tools: dict) -> str:
        """One line of the reviewer: its tool steps first (kept in `history`, so it
        remembers what it read), then its words."""
        wire = [t.as_wire() for t in tools.values()]
        for step in range(REVIEWER_STEPS + 1):
            text, calls = await _ask(backend, [{"role": "system", "content": system}]
                                     + history, wire if step < REVIEWER_STEPS else [])
            if not calls or step == REVIEWER_STEPS:
                return text
            history.append({"role": "assistant", "content": text, "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for c in calls]})
            for c in calls:
                spec = tools.get(c["name"])
                out = await _call(spec, self._ctx(cid), c["arguments"]) if spec else \
                    f"error: there is no tool {c['name']}"
                history.append({"role": "tool", "tool_call_id": c["id"], "content": out})
        return ""


async def _ask(backend, messages: list[dict], tools: list[dict]):
    text: list[str] = []
    calls: dict[int, dict] = {}
    async for ev in backend.stream(messages, tools):
        if isinstance(ev, TextDelta):
            text.append(ev.text)
        elif isinstance(ev, ToolCallDelta):
            slot = calls.setdefault(ev.index, {"id": f"call{ev.index}", "name": "",
                                                "arguments": ""})
            slot["id"] = ev.id or slot["id"]
            slot["name"] = ev.name or slot["name"]
            slot["arguments"] += ev.arguments
        elif isinstance(ev, Done):
            break
    return "".join(text).strip(), list(calls.values())


async def _call(spec, ctx: ToolContext, arguments: str) -> str:
    """One read of the reviewer's (READS, read-only: nothing leaves, so not through its
    chokepoint); none while it is frozen."""
    from ..guard.freeze import frozen
    if await frozen(ctx.store) is not None:
        return "error: frozen, nothing runs now"
    try:
        return (await spec.fn(ctx, **json.loads(arguments or "{}"))).content
    except Exception as e:                       # noqa: BLE001 - it reads the error
        return f"error: {type(e).__name__}: {e}"


__all__ = ["NONE", "Scheduler", "night_words", "results", "view"]
