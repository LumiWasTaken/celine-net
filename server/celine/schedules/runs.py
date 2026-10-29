"""Runs: one schedule at one slot, durable.

When a run exists. At the next slot, every TICK seconds at most, on every `models.state`
change, at the end of every turn and after every change to the schedules, the scheduler
makes the run of every slot of a schedule that is on and came due since its last look
(kv `schedules.checked`; the first look makes those since the start of the current
night). A run's night is `night_of` its slot. Slots missed while the app was off are
made too, oldest first: those of the last CATCH_UP_DAYS wait like any other, older ones
are written `skipped` with the reason. A slot is never dropped silently.

When a run starts. A run is `waiting` until all of these hold; its `reason` says the
first that does not, in words:
1. every run it follows in the same night is finished, done or failed or skipped
   ("Runs after World news");
2. the model of its role can answer now, the Router says ("Waiting for <model>"); it
   starts by itself when the model comes back, the same night or days later;
3. no turn a person waits on is live: a chat (not a trigger's), a turn the user
   started in a mission ("Waiting until your chat is done"); a mission's
   turns that go on by themselves hold no run unless Routines wait for missions is on
   (Settings, Advanced);
4. no other run is running ("While Self-review runs"): one at a time, one model server.
While it is frozen (guard/freeze.py) no run starts ("Frozen"); the run whose turn a
freeze cut is `waiting` ("Frozen") and goes on in that turn after the unfreeze.
A schedule with `idle_only` starts only when, besides, nobody used the app for
IDLE_MINUTES (no user message, no turn a person waits on); otherwise, or when it could
not start within IDLE_MINUTES of its slot, that run is `skipped` and never caught up.
The queue is ordered by night, then by how deep the schedule follows, then by slot, so
missed nights run night by night, each about its own date.

A run. One background conversation (`run:<id>`, hidden from the chat list) with the
filled prompt as its first message, from `system:schedules`; its persona, notes and
tools as in any conversation, on the schedule's role. The agent's turns and the partner's calls
wait behind every interactive request (Priority BACKGROUND, `binding`), named by the
schedule's label, on the role's own model only: no other model (turns/dispatcher.py
binds them so). What the result is it reads with the prompt and after the partner's
talk, from final.md beside this file (note.md names the note): the whole result, in
full, nothing asked again. With a partner, the partner prompt (on the same role, with the
read-only tools of READS) and it alternate up to `rounds` lines, the partner may end
sooner with END alone on its last line; then it is asked for its final answer. The agent's
final answer is the result. With `note` the app writes it to that notebook path; a note
that is there already gets it appended under "## <date> <label>" (run_date). A note it
wrote itself (a notebook__write or notebook__edit of that path that ended ok in the run)
is the result instead, and the app writes nothing over it (only what it
added, when it added to its end). The agent's words beside the write are a lead-in, not the
result (a turn ends with that step, skills.ToolSpec needs_answer). The run is over
when its conversation and its sub-agents are quiet and every sub-agent has reported
(looked at after every turn or job end, the dispatcher's `on_end`). Every run first keeps the
night's backups (schedules/backups.py: once a night, the later runs find them made).

One try. A run is never re-asked, rewritten or retried. When its turn fails and the
Router says the role's model cannot answer (it is unreachable, or none is set), the run
is `waiting` for it ("Waiting for <model>"), its conversation kept; when the Router sees
the model answer, the run starts again and continues that failed turn in the same
conversation (turns/store.continue_turn: the stored context, no new prompt). Any other
failure fails the run with the error, once: a model error, a tool error that ends its
turn, its turn stopped, or its ceiling: the Routine time limit (Settings, Advanced, 0
none; a schedule's own `wall_minutes` makes it shorter) counts from the run's start; a
run that waited for its model starts with a new clock. A run that was running when the app stopped is running still at
the next start: the dispatcher's sweep resumes its cut turn, the scheduler takes its
answer once the conversation is quiet (a partner's talk that was cut ends there). `end`
ends a waiting or running run with a reason: Stop in the run's drawer (`stopped`), its
schedule deleted (`skipped`), its ceiling (`failed`). A run keeps its schedule's
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
from ..backends.base import Done, TextDelta, ToolCallDelta
from ..conversations import store as conversations, tree
from ..core.ids import iso, new_id, now
from ..core.states import LIVE_SQL
from ..skills import ToolContext
from ..clock import zone
from ..sub_agents.report import asked, last_answer, quiet
from . import NONE, depth, fill, night_of, night_start, run_date, shown
from .variables import FROM_USER, extras
from ..core.event_types import RUN_STATE

log = logging.getLogger("celine.schedules")

TICK = 60.0                  # seconds between two looks without a wake
CATCH_UP_DAYS = 7
MAX_SLOTS = 2000             # slots made per schedule in one look
CHECKED = "schedules.checked"
PARTNER_STEPS = 12           # the partner's tool steps before one line
READS = ("notebook", "past_conversations", "chat_archive")   # the partner's read tools
END = re.compile(r"\s*\bEND\s*$")

MISSED = f"Missed while the app was off for more than {CATCH_UP_DAYS} days"
DELETED = "Its schedule was deleted"
STOPPED = "Stopped by {{username}}"
IDLE_MINUTES = 30
IN_USE = "The app was in use"
FROZEN = "Frozen"            # a freeze cut its turn, or it may not start while frozen
PASSED = f"More than {IDLE_MINUTES} minutes late"
WAITED_ON = ("((c.kind = 'mission' AND {mission}) OR (c.kind = 'chat' AND "
             "c.trigger_key IS NULL))")

RULE = ("This runs in the background: {{username}} is not here and reads nothing of it "
        "live. {final} If something needs {{username}} to act or to know, start a chat "
        "for them with intercom__start_chat: a title and what they read first, short and "
        "complete.")
PARTNER_LINE = ("In this run you talk with {who} first. Their lines come to you in this "
                "conversation: answer here, in your own words. intercom__send reaches "
                "{{username}}, not {who}.")
FINAL = "The talk with {who} is over. Now give your final answer to what this run asks of you. {final}"
ASKED = "What {{name}} was asked for this run:"
LAST = "This is your last line: give your closing line now."
LINE = "This is line {n} of at most {rounds}."


def _repeated(slot: datetime, tz) -> bool:
    """The second 02:00 of the night the clocks go back: that wall time already ran."""
    local = slot.astimezone(tz)
    return local.utcoffset() != local.replace(fold=0).utcoffset()


def final_words(note: str) -> str:
    """final.md (with note.md's clause for `note`, the notebook path, when there is one):
    what the result of a run is, said to it with the prompt and after a partner's talk."""
    clause = prompts.text(__file__, "note.md").strip().replace("{{path}}", note) if note else ""
    return prompts.text(__file__, "final.md").strip().replace("{{note}}", clause)


class RunFailed(Exception):
    """The agent's conversation gave no answer; `stopped` True when someone stopped it: the run
    fails, it never waits for its model then; `frozen` True when a freeze cut its turn: the
    run waits for the unfreeze."""

    def __init__(self, sentence: str, stopped: bool = False, frozen: bool = False):
        super().__init__(sentence)
        self.stopped = stopped
        self.frozen = frozen


# ---- rows ---------------------------------------------------------------------------------

def night_words(night: str) -> str:
    """A night or a date in a row's words: "Sat 10 Oct"."""
    d = date.fromisoformat(night)
    return f"{d:%a} {d.day} {d:%b}"


def view(r: sqlite3.Row | dict, cfg) -> dict:
    """A run as the API and the feed give it: `at` its slot's time of day in the app's
    zone, `label` its schedule's (the one it kept once the schedule is gone)."""
    s = (getattr(cfg, "schedules", None) or {}).get(r["schedule"])
    return {"id": r["id"], "schedule": r["schedule"],
            "label": shown(s) if s else r["label"], "slot": r["slot"],
            "at": datetime.fromisoformat(r["slot"]).astimezone(zone(cfg)).strftime("%H:%M"),
            "night": r["night"], "state": r["state"], "reason": r["reason"],
            "conversation_id": r["conversation_id"], "result": r["result"],
            "error": r["error"], "created_at": r["created_at"],
            "started_at": r["started_at"], "finished_at": r["finished_at"],
            "started_by": r["started_by"]}


def _set(c: sqlite3.Connection, store, cfg, rid: str, **fields) -> dict:
    """Inside a write: change run `rid` and tell the feed. -> its view."""
    if fields:
        c.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                  (*fields.values(), rid))
    out = view(c.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone(), cfg)
    store.global_event(c, RUN_STATE, {"run": out})
    if fields.get("state") == "failed":
        from ..events import notice
        notice(store, c, "run", out["label"], out["error"] or "Failed",
               homes.run_link(out["night"], rid), f"run:{rid}")
    if fields.get("state") in ("done", "failed", "skipped", "stopped"):
        from ..events import added
        added(store, c, "run", out["conversation_id"])
        metrics.ROUTINE_RUNS.labels(out["schedule"], out["state"]).inc()
        if out["started_at"] and out["finished_at"]:
            metrics.ROUTINE_SECONDS.labels(out["schedule"]).observe(max(0.0, (
                datetime.fromisoformat(out["finished_at"])
                - datetime.fromisoformat(out["started_at"])).total_seconds()))
    return out


def _insert(c: sqlite3.Connection, store, cfg, name: str, slot: datetime,
            tz, state: str = "waiting", reason: str = "", by: str = "clock") -> dict | None:
    """The run of `name` at `slot`, None when it is there already; `by` who started it:
    the clock, the user (Run now) or it (routines__run)."""
    rid = new_id()
    s = (getattr(cfg, "schedules", None) or {}).get(name)
    made = c.execute(
        "INSERT INTO runs(id, schedule, label, slot, night, state, reason, "
        "created_at, started_by) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(schedule, slot) "
        "DO NOTHING",
        (rid, name, shown(s) if s else name, iso(slot), night_of(slot, tz).isoformat(),
         state, reason, now(), by)).rowcount
    if not made:
        return None
    # a slot written skipped at once is finished, counted and in Events (_set)
    return _set(c, store, cfg, rid, **({} if state == "waiting" else
                                       {"state": state, "finished_at": now()}))


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
    """How a turn of conversation `cid` binds its model when it is a run's (a sub-agent
    of one too): {priority: BACKGROUND, label: the schedule's}, on the role's own model
    only; None for any other conversation. turns/dispatcher.py binds with it
    (`run_binding`, set at start)."""
    from ..backends.queue import Priority
    # one hop: a sub-agent starts no sub-agents (sub_agents/api.py)
    run = c.execute("SELECT r.schedule, r.label FROM conversations c JOIN runs r ON "
                    "r.conversation_id = coalesce(c.agent_of, c.id) WHERE c.id = ?",
                    (cid,)).fetchone()
    if run is None:
        return None
    s = (getattr(cfg, "schedules", None) or {}).get(run["schedule"])
    return {"priority": Priority.BACKGROUND, "label": shown(s) if s else run["label"]}


def finished(c: sqlite3.Connection, cid: str) -> bool:
    """The agent's conversation is quiet, and so is every sub-agent of it, each reported."""
    if not quiet(c, cid):
        return False
    return all(quiet(c, s) and asked(c, s, cid) is None for (s,) in c.execute(
        "SELECT id FROM conversations WHERE agent_of = ?", (cid,)).fetchall())


def outcome(c: sqlite3.Connection, cid: str) -> tuple[str, str]:
    """(state, reason) of the conversation's last turn."""
    r = c.execute("SELECT state, reason FROM turns WHERE conversation_id = ? "
                  "ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
    return (r["state"], r["reason"] or "") if r else ("done", "")


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
        self._began: datetime | None = None   # when it started: its clock
        self._scope: anyio.CancelScope | None = None   # its work, for `end`
        self._ceiling: tuple[timedelta, str] | None = None   # its own, for tick
        self._turn_ended = anyio.Event()      # set and replaced at every turn or job end
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
            self.app.dispatcher.run_binding = lambda c, cid: binding(c, cid, self.app.cfg)
        for r in resume:
            self._start(r)
        tg.start_soon(self._loop)

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
        if self._began is not None and self._ceiling:    # the running run's ceiling
            wait = min(wait, max(0.0, (self._began + self._ceiling[0] - at).total_seconds()))
        return wait

    # -- making runs

    async def make_due(self) -> None:
        """The runs of every slot that came due since the last look (module docstring)."""
        at, tz, schedules, store = self.now(), zone(self.app.cfg), self.schedules, \
            self.app.store
        since = await store.kv_get(CHECKED)

        start = datetime.fromisoformat(since) if since else night_start(night_of(at, tz), tz)

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
            "SELECT * FROM runs WHERE schedule = ? AND slot = ?",
            (name, iso(slot))).fetchone(), self.app.cfg))

    async def end(self, rid: str, state: str, why: str,
                  from_states: tuple[str, ...] = ("waiting", "running")) -> dict | None:
        """Run `rid`, while in one of `from_states`, ends as `state` with `why` (its
        reason, its error when failed); a running one's work is cancelled and its turns
        aborted as a chat's Stop does. None: no such run, or it is in no such state."""
        store, cfg = self.app.store, self.app.cfg
        if rid == self.current and self._scope is not None:
            self._scope.cancel()

        def fn(c: sqlite3.Connection):
            r = c.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
            if r is None or r["state"] not in from_states:
                return None, None
            out = _set(c, store, cfg, rid, state=state, finished_at=now(),
                       **{"error" if state == "failed" else "reason": why})
            return out, r["conversation_id"] if r["state"] == "running" else None
        out, cid = await store.write(fn)
        abort = getattr(self.app.dispatcher, "abort_conversation", None)
        if cid and abort is not None:
            await abort(cid)
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
                await store.write(lambda c, r=r, why=why: _set(
                    c, store, self.app.cfg, r["id"], reason=why))

    def _start(self, r: dict) -> None:
        """Run `r` runs now, in the task group."""
        s = self.schedules.get(r["schedule"])
        self.current, self._began = r["id"], self.now()
        cap = [m for m in (s and s.wall_minutes, self.app.cfg.r("routine_max_minutes")) if m]
        self._ceiling = (timedelta(minutes=min(cap)), f"Ran over {min(cap)} minutes") \
            if cap else None
        self._tg.start_soon(self._execute, r["id"])

    async def _watch(self) -> None:
        """The running run's ceiling (module docstring) fails it."""
        if self._began is not None and self._ceiling and \
                self.now() - self._began > self._ceiling[0]:
            await self.end(self.current, "failed", self._ceiling[1], ("running",))

    def _idle_skip(self, r: dict, s, unused: bool) -> str | None:
        """Why an `idle_only` run is skipped now, None when it may wait on: one that
        began already (it waits for its model) waits like any run."""
        if not s.idle_only or r["conversation_id"]:
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

    async def _model_why(self, role: str) -> str | None:
        """"Waiting for <model>" while the Router says no model of `role` answers now
        (its first model named; the Router's sentence when none is set), else None."""
        if getattr(self.app.dispatcher, "backend_factory", None) is not None:
            return None                          # one fixed backend answers every role
        router = getattr(self.app.llm, "router", None)
        why = None if router is None else await router.can_answer(role)
        if why is None:
            return None
        refs = router.chain(role) if getattr(router, "chain", None) else []
        if refs:
            from ..settings.her_models import label
            return f"Waiting for {label(router.s, refs[0])}"
        return f"Waiting for a model: {why}"

    # -- one run

    async def _execute(self, rid: str) -> None:
        try:
            with anyio.CancelScope() as self._scope:
                r = await self.app.store.read(lambda c: dict(c.execute(
                    "SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()))
                await self._run_as(rid, r)
        finally:
            self.current = self._began = self._scope = None
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
        day = run_date(s, datetime.fromisoformat(r["slot"]), zone(cfg))
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
                answer = await self._resume(cid)
            elif s.partner:
                prompt = fill(cfg, s.prompt, day, res, await extras(self.app, s.prompt, night))
                answer = await self._dialogue(rid, s, cid, prompt, day, res, where)
            else:
                prompt = fill(cfg, s.prompt, day, res, await extras(self.app, s.prompt, night))
                answer = await self._turn(cid, "system:schedules", "\n\n".join(
                    [prompt, cfg.substitute(RULE.replace("{final}", where))]), "trigger",
                    {"run": rid})
            answer, hers = self._take(note, start, answer, await self._wrote(cid, note))
            failed_note = await self._write_note(cid, note, answer, s, day.isoformat()) \
                if note and not hers else None
        except Exception as e:                   # noqa: BLE001 - every failure ends it
            error = str(e) or type(e).__name__
            why = FROZEN if getattr(e, "frozen", False) else \
                None if getattr(e, "stopped", False) else await self._model_why(s.role)
            if why:              # its model is gone, or a freeze: the run waits for it
                await store.write(lambda c: _set(c, store, cfg, rid, state="waiting",
                                                 reason=why))
            else:
                await store.write(lambda c: _set(c, store, cfg, rid, state="failed",
                                                 error=error, finished_at=now()))
            log.info("run failed", extra={"event": "schedules.failed", "run": rid,
                                          "waits": bool(why), "error": error})
            return
        await store.write(lambda c: _set(c, store, cfg, rid, state="done", result=answer,
                                         error=failed_note, finished_at=now()))
        log.info("run done", extra={"event": "schedules.done", "run": rid})

    async def _resume(self, cid: str) -> str:
        """The run goes on in `cid`: its last turn, when it ended without an answer and
        can be continued, is continued (turns/store.continue_turn); then its answer
        once everything there is quiet. Raises RunFailed as `_turn`."""
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
        await self._settle(cid)
        answer = await self.app.store.read(lambda c: last_answer(c, cid))
        if not answer:
            raise RunFailed("No answer")
        return answer

    async def _turn(self, cid: str, sender: str, text: str, kind: str = "message",
                    data: dict | None = None) -> str:
        """`text` to its conversation, then its answer once everything there is quiet.
        Raises RunFailed when its turn failed or gave no answer."""
        await self.app.intercom.send(sender, f"agent:{cid}", text, kind=kind, data=data)
        await self._settle(cid)
        answer = await self.app.store.read(lambda c: last_answer(c, cid))
        if not answer:
            raise RunFailed("No answer")
        return answer

    async def _backups(self, night: date) -> None:
        """The night's backups, when this is its first run (schedules/backups.py)."""
        from .backups import nightly
        cfg = self.app.cfg
        data = getattr(cfg, "data_dir", None)
        if data is None:
            return
        root = cfg.her.notebook if getattr(cfg, "her", None) else None
        try:
            await anyio.to_thread.run_sync(nightly, data, cfg.db_path, root, night,
                                           cfg.r("backup_keep_days"))
        except Exception:                      # noqa: BLE001 - the run goes on
            log.exception("backup failed", extra={"event": "backup.failed"})

    def _note_path(self, note: str):
        return self.app.cfg.her.notebook / note if note and getattr(
            self.app.cfg, "her", None) else None

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
        want = posixpath.normpath(note)
        for (args,) in rows:
            try:
                path = json.loads(args).get("path")
            except (ValueError, AttributeError):
                continue
            if isinstance(path, str) and posixpath.normpath(path) == want:
                return True
        return False

    async def _write_note(self, cid: str, path: str, answer: str, s, day: str) -> str | None:
        """The agent's answer into the notebook at `path`: a note there already gets it appended
        under "## <day> <label>", `day` the run's date (run_date) (its own renames and
        edits stay). -> the sentence why not, or None."""
        spec = getattr(self.app.registry, "tools", {}).get("notebook__write")
        if spec is None:
            return f"The note {path} was not written: the notebook is off."
        there = (self.app.cfg.her.notebook / path).is_file()
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

    # -- a partner

    async def _dialogue(self, rid: str, s, cid: str, prompt: str, day: date,
                        res: dict, where: str) -> str:
        """The agent and the partner alternate (module docstring); -> its final answer."""
        cfg = self.app.cfg
        who = s.partner.removesuffix(".md")
        sender = f"system:{who}"
        rule = cfg.substitute(RULE.replace("{final}", where))
        line = cfg.substitute(PARTNER_LINE.replace("{who}", who))
        history = [{"role": "user", "content": await self._turn(
            cid, "system:schedules", "\n\n".join([prompt, line, rule]), "trigger",
            {"run": rid})}]
        base = "\n\n".join([fill(cfg, s.partner_prompt, day, res),
                            cfg.substitute(ASKED), prompt])
        backend, tools = self._partner_backend(cid, s), self._partner_tools()
        for n in range(1, s.rounds + 1):
            last = n == s.rounds
            system = f"{base}\n\n{LAST if last else LINE.format(n=n, rounds=s.rounds)}"
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
        return await self._turn(cid, "system:schedules", FINAL.replace(
            "{who}", who).replace("{final}", where), "trigger", {"run": rid})

    async def _settle(self, cid: str) -> None:
        """Until its conversation is `finished`, looked at after every turn or job end;
        raises RunFailed when its last turn was stopped or failed."""
        store = self.app.store
        while True:
            ended = self._turn_ended         # taken before the look: no end is missed
            if await store.read(lambda c: finished(c, cid)):
                break
            await ended.wait()
        state, reason = await store.read(lambda c: outcome(c, cid))
        if state == "aborted" and reason == "frozen":
            raise RunFailed(FROZEN, frozen=True)
        if state == "aborted":
            raise RunFailed("Stopped", stopped=True)
        if state == "failed":
            raise RunFailed(reason or "The turn failed")

    def _partner_backend(self, cid: str, s):
        """The partner's model: behind every interactive request, named by the
        schedule's label, on the role's own model only; never its Banned words, the
        partner is not it."""
        d = self.app.dispatcher
        fixed = d.backend_factory() if getattr(d, "backend_factory", None) else None
        if self.app.llm is None:
            return fixed
        from ..backends.queue import Priority
        return self.app.llm.bind(fixed, role=s.role, priority=Priority.BACKGROUND,
                                 conversation_id=cid, label=shown(s), fallback=False,
                                 voice=False)

    def _partner_tools(self) -> dict:
        reg = self.app.registry
        if reg is None:
            return {}
        return {k: s for k, s in reg.tools.items() if s.skill in READS
                and s.annotations.read_only and reg.down(s.skill) is None}

    async def _line(self, backend, cid: str, system: str, history: list[dict],
                    tools: dict) -> str:
        """One line of the partner: its tool steps first (kept in `history`, so it
        remembers what it read), then its words."""
        wire = [t.as_wire() for t in tools.values()]
        for step in range(PARTNER_STEPS + 1):
            text, calls = await _ask(backend, [{"role": "system", "content": system}]
                                     + history, wire if step < PARTNER_STEPS else [])
            if not calls or step == PARTNER_STEPS:
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
    """One read of the partner's (READS, read-only: nothing leaves, so not through its
    chokepoint); none while it is frozen."""
    from ..guard.freeze import frozen
    if await frozen(ctx.store) is not None:
        return "error: frozen, nothing runs now"
    try:
        return (await spec.fn(ctx, **json.loads(arguments or "{}"))).content
    except Exception as e:                       # noqa: BLE001 - it reads the error
        return f"error: {type(e).__name__}: {e}"


__all__ = ["NONE", "Scheduler", "night_words", "results", "view"]
