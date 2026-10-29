"""Queue and conversation leases."""
from __future__ import annotations

import logging
import time

import anyio

from .. import metrics, work
from ..conversations import store as conversations
from ..core.ids import new_id
from ..core.store import Store
from ..hold import her_hold
from ..intercom import deliver
from ..settings import roles
from . import cancel as cancel_mod, store as turns
from .runner import run_turn
from .title import make as make_title
from ..core.event_types import LLM_QUEUE

log = logging.getLogger("celine.dispatcher")

READY_SECONDS = 15.0              # the most the first turn waits for `ready`
HOLD_RECHECK = 5.0                # a held worker looks again at least this often


class Dispatcher:
    def __init__(self, store: Store, cfg, bus, backend_factory=None, registry=None,
                 llm=None):
        self.store = store
        self.llm = llm                  # backends/llm.py LLM: queue + routing; None = direct
        self._tg = None
        self.cfg = cfg
        self.bus = bus
        # None: every turn follows its conversation's chat models from Settings (the
        # app). A factory (tests): that backend for every call.
        self.backend_factory = backend_factory
        self.registry = registry
        self._wake = anyio.Event()
        self._running = True
        self._stopping = False
        self.tokens = cancel_mod.Registry()
        self.jobs = None
        self.intercom = None            # the Intercom service, for a Stop's cascade
        # set once the vault's first unlock is over (its passwords are in the redaction
        # set): no turn starts before it, or before READY_SECONDS
        self.ready = None
        self._workers: list[str] = []
        self._active: dict[str, str] = {}        # turn id -> conversation id, while held
        # how long a restart waits for a step with a side effect to finish before it
        # puts the turn down anyway (read-only steps are not waited for: they run again)
        self.shutdown_grace_seconds = 10.0
        self._on_end: list = []                 # async fn(cid, turn_id), see on_end
        # fn(c, cid) -> {priority, label} | None: how a Routine's run binds its turns
        # (schedules/runs.py sets it at start, from its `binding`)
        self.run_binding = None
        # the work queue's workers (work.py): titles here, more kinds from elsewhere
        self.work = work.Pool(store, llm)
        self.work.handle("title", ("small", "chat"),
                         lambda p: self._title(p["conversation_id"]))

    def notify(self) -> None:
        """Called after anything is enqueued. Never awaits."""
        self._wake.set()

    def on_end(self, fn) -> None:
        """`await fn(cid, turn_id)` after turn `turn_id` of conversation `cid` ended, and
        with `turn_id` None after one of its jobs ended (a sub-agent's report,
        sub_agents/report.py)."""
        self._on_end.append(fn)

    async def ended(self, cid: str, turn_id: str | None = None) -> None:
        for fn in self._on_end:
            try:
                await fn(cid, turn_id)
            except Exception:
                log.exception("end listener failed", extra={"event": "turn.end_failed"})
        if turn_id is not None:      # a shutdown that cuts this off leaves it for replay_ended
            await self.store.write(lambda c: c.execute(
                "UPDATE turns SET ended_hooks=1 WHERE id=?", (turn_id,)))

    async def replay_ended(self) -> None:
        """At start: the end hooks of turns that ended without running them (the shutdown
        came first, or the startup sweep ended them): what lands in the conversation, then
        the listeners, as at any turn's end."""
        rows = await self.store.read(lambda c: [(r[0], r[1]) for r in c.execute(
            "SELECT id, conversation_id FROM turns WHERE ended_hooks=0 AND state IN "
            "('done','aborted','failed','interrupted','capped') ORDER BY finished_at, "
            "created_at")])
        for tid, cid in rows:
            await self.deliver_idle(cid)
            await self.ended(cid, tid)

    async def start(self, tg: anyio.abc.TaskGroup) -> None:
        from ..jobs.jobs import Jobs
        self.jobs = Jobs(self.store, self.cfg, self.notify, tg, on_end=self.ended,
                         registry=self.registry)
        self._tg = tg
        self.resize()
        await self.work.start(tg)
        tg.start_soon(self.replay_ended)

    def resize(self) -> None:
        """Workers up to max_concurrent_turns (Settings, Advanced; reload.py calls it on a
        change). A worker past the number claims nothing until it is raised again."""
        if self._tg is None:                # not started
            return
        for _ in range(self._cap() - len(self._workers)):
            wid = new_id()[:8]
            self._workers.append(wid)
            self._tg.start_soon(self._worker, wid)
        self.notify()

    def _cap(self) -> int:
        return max(1, int(self.cfg.r("max_concurrent_turns")))

    async def stop(self, grace: float | None = None) -> None:
        """Graceful shutdown, before the store closes. Every running turn is cancelled
        with reason "shutdown" (not a Stop): streams end and keep their partial text,
        steps not yet started stay pending. Waits up to `grace` for steps with a side
        effect to land; read-only steps are not waited for. Then every turn still held
        ends `interrupted` with the reason (`interrupt_held`); nothing of it runs again
        on its own."""
        self._running = False
        self._stopping = True
        self._wake.set()
        held = dict(self._active)
        log.info("shutdown", extra={"event": "shutdown.begin", "turns": len(held)})
        for token in self.tokens.live():
            token.cancel("shutdown")
        limit = time.monotonic() + (self.shutdown_grace_seconds if grace is None
                                    else grace)
        while self._active and time.monotonic() < limit:
            if not await self._waiting_on_side_effect():
                break
            await anyio.sleep(0.05)
        await self.work.stop()
        # a turn that returned during the wait wrote nothing terminal: it ends here too
        try:
            down = await turns.interrupt_held(self.store, self._workers, self.registry)
        except Exception:
            log.exception("interrupt failed", extra={"event": "shutdown.interrupt_failed"})
            down = []
        log.info("shutdown done", extra={"event": "shutdown.done", "turns": len(held),
                                         "interrupted": len(down)})

    async def _waiting_on_side_effect(self) -> bool:
        """True while a held turn is inside a step that is not safe to run again, or
        is still between steps (streaming stops at once on cancel, so that is short)."""
        from .journal import safe_to_rerun
        ids = list(self._active)
        if not ids:
            return False

        def fn(c):
            marks = ",".join("?" * len(ids))
            states = {r["id"]: r["state"] for r in c.execute(
                f"SELECT id, state FROM turns WHERE id IN ({marks})", ids)}
            calls = c.execute(
                f"SELECT m.turn_id, tc.name, tc.state, tc.mutating, tc.mode "
                f"FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
                f"WHERE m.turn_id IN ({marks}) AND tc.state IN ('pending','running')",
                ids).fetchall()
            return states, calls
        try:
            states, calls = await self.store.read(fn)
        except Exception:
            log.warning("tool call read failed", exc_info=True,
                        extra={"event": "turn.rerun_check_failed"})
            return False
        flags = turns.tool_flags(self.registry)
        if any(not safe_to_rerun(r["state"], r["mutating"], r["mode"], flags(r["name"]))
               for r in calls):
            return True
        busy_in_tools = {r["turn_id"] for r in calls}
        # a turn not in a tool is streaming or preparing: it returns on its own soon
        return any(t not in busy_in_tools and states.get(t) in
                   ("preparing", "streaming", "tools") for t in ids)

    async def abort_conversation(self, cid: str) -> str | None:
        """The user's Stop for a whole conversation: its live turn, every job it
        runs, and its sub-agents' conversations the same way. Returns the stopped
        turn's id, or None when no turn was live."""
        tid = await turns.request_abort(self.store, cid)
        if tid:
            self.tokens.cancel(tid)
        if self.jobs is not None:
            await self.jobs.cancel_for_conversation(cid)
        subs = await self.store.read(lambda c: [r[0] for r in c.execute(
            "SELECT id FROM conversations WHERE agent_of=?", (cid,))])
        for sub in subs:
            if self.intercom is not None:    # nothing it scheduled wakes it again
                await self.intercom.cancel_scheduled(sub)
            await self.abort_conversation(sub)
        # a Stop stops the turn, not what waits for the conversation: a turn that ended
        # here (it was still queued) lets it land; a running one does at its end
        await self.deliver_idle(cid)
        if tid and tid not in self._active:  # it ended here: no worker tells its end
            await self.ended(cid, tid)
        return tid

    async def deliver_idle(self, cid: str) -> None:
        """Rule 1 of the delivery rule (intercom/deliver.py) for this conversation."""
        out = await deliver.deliver_idle(self.store, cid)
        if out and out["turn_id"]:
            self.notify()

    async def _title(self, cid: str) -> None:
        """Once, after the first reply (a work item, work.py): turns/title.make."""
        from ..backends.queue import Priority
        from ..settings.thinking import OFF
        await make_title(self.store, cid, lambda role: self.bound(
            cid, role=role, priority=Priority.BACKGROUND, thinking=OFF, voice=False))

    def bound(self, cid: str, *, role: str, priority, **kw):
        """A backend for conversation `cid` in model role `role`: the app's model queue
        (llm.bind) over the configured backend, or that backend alone without a queue
        (tests). For /compact, /usage and the title; a turn has its own
        (_backend_for)."""
        fixed = self.backend_factory() if self.backend_factory is not None else None
        if self.llm is None:
            return fixed
        return self.llm.bind(fixed, role=role, priority=priority, conversation_id=cid,
                             **kw)

    def _backend_for(self, turn: dict, token, clock=None, label: str | None = None,
                     run: dict | None = None):
        """The backend one turn uses: the conversation's chat models, queued and routed
        when the app has an LLM queue. Priority by origin: the user's turn first, trigger
        and job turns after, a Routine's run (`run`, schedules/runs.binding) last. The
        user's turn and a run's run on exactly the first model of their role (no
        fallback: when that model fails, the turn fails); other trigger and job turns
        walk the list. Time spent waiting for a model is given back to the turn's wall
        `clock`. `label`: who the calls are for in the model queue (the conversation's
        title; a run's schedule)."""
        fixed = self.backend_factory() if self.backend_factory is not None else None
        if self.llm is None:
            return fixed
        from ..backends.queue import Priority
        user = (turn.get("origin") or "user") == "user"
        store, cid = self.store, turn["conversation_id"]
        priority = Priority.INTERACTIVE if user else Priority.AGENT
        if run is not None:
            priority, label = run["priority"], run["label"]


        async def emit(data: dict) -> None:
            await store.append_event(cid, LLM_QUEUE, data)
            if data.get("state") == "running" and data.get("backend"):
                await turns.set_backend(store, turn["id"], data["backend"], data["model"])

        from ..attachments import Images
        return self.llm.bind(fixed, role="chat", priority=priority,
                             fallback=not user and run is None,
                             keep_waiting=turn.get("reason") == "keep_waiting",
                             conversation_id=cid, turn_id=turn["id"], token=token,
                             emit=emit, clock=clock,
                             images=Images(self.cfg, store, self.llm, lane=cid),
                             label=label)

    async def _cli_backend(self, cid: str, clock):
        """A sub-agent started with an agent CLI runs its turns there
        (sub_agents/cli.py); None for every other conversation."""
        app = getattr(self.registry, "app", None)
        cli = await self.store.read(lambda c: (c.execute(
            "SELECT agent_cli FROM conversations WHERE id=?", (cid,)).fetchone() or [None])[0])
        if not cli or app is None:
            return None
        from ..sub_agents.cli import CliBackend
        return CliBackend(app, cid, cli, clock)

    async def _held(self) -> None:
        """While its turns are held (hold.py): until the gateway's state changes."""
        if self.llm is None:
            await anyio.sleep(HOLD_RECHECK)
        else:
            await self.llm.router.wait_change(HOLD_RECHECK)

    async def _wait_for_work(self) -> None:
        """Until notify(). Every queued turn is announced: the startup sweep runs
        before the workers, and each worker claims once before it waits."""
        await self._wake.wait()
        self._wake = anyio.Event()

    async def _worker(self, worker_id: str) -> None:
        """One worker for as long as the app runs. An error outside a turn (a store
        error in the hold check or the claim) is logged and the worker looks again
        later, as work.py's do: it never reaches the app's task group. A worker that
        ends anyway outside a shutdown leaves its place, so resize starts another."""
        if self.ready is not None:
            with anyio.move_on_after(READY_SECONDS):
                await self.ready.wait()
        try:
            while self._running:
                try:
                    if not await self._next(worker_id):
                        return
                except Exception:
                    if self._stopping:
                        return
                    log.exception("worker loop failed",
                                  extra={"event": "turn.worker_failed", "worker": worker_id})
                    with anyio.move_on_after(HOLD_RECHECK):
                        await self._wait_for_work()
        finally:
            if not self._stopping and worker_id in self._workers:
                self._workers.remove(worker_id)

    async def _next(self, worker_id: str) -> bool:
        """One look for a turn, and the turn when there is one. -> False when the
        worker ends (a shutdown)."""
        from ..guard.freeze import frozen
        hold = await her_hold(self.store, self.llm)
        if hold == "frozen":            # the unfreeze wakes them (notify)
            await self._wait_for_work()
            return True
        if hold is not None:
            await self._held()
            return True
        if self._workers.index(worker_id) >= self._cap():
            await self._wait_for_work()
            return True
        turn = await turns.claim(self.store, worker_id)
        if turn is None:
            await self._wait_for_work()
            return True
        # the token opens before the second look: a freeze after that look finds it
        # live and stops it (freeze._stop_all); the DB column seeds it, covering the
        # lease-to-start window
        token = self.tokens.open(turn["id"], bool(turn.get("abort_requested")))
        try:
            again = await frozen(self.store) is not None
        except Exception:
            # claimed, not run: back in the queue for the next look
            self.tokens.close(turn["id"])
            await turns.put_down(self.store, [worker_id])
            raise
        if again:
            # frozen while it was claimed: back in the queue
            self.tokens.close(turn["id"])
            await turns.put_down(self.store, [worker_id], reason="frozen", code="frozen")
            await self._wait_for_work()
            return True
        # another idle worker looks too: one notify may have queued several turns
        self.notify()
        if not self._running:          # claimed as the shutdown began
            self.tokens.close(turn["id"])
            await turns.put_down(self.store, [worker_id])
            return False
        try:
            await self._run_one(turn, worker_id, token)
        except Exception:
            # the store closed under a turn during shutdown: it ended interrupted already
            if not self._stopping:
                raise
            log.info("turn left to the next start", extra={
                "event": "shutdown.turn_left", "turn": turn["id"]})
            return False
        return True

    async def _count(self, tid: str, origin: str, seconds: float) -> None:
        """A turn that ended: its outcome and its steps (the requests it sent)."""
        state, steps = await self.store.read(lambda c: tuple(c.execute(
            "SELECT state, (SELECT count(*) FROM requests WHERE turn_id=turns.id) "
            "FROM turns WHERE id=?", (tid,)).fetchone() or ("gone", 0)))
        metrics.TURNS.labels(origin, state).inc()
        metrics.TURN_SECONDS.labels(origin).observe(seconds)
        metrics.TURN_STEPS.labels(origin).observe(steps)

    async def _run_one(self, turn: dict, worker_id: str, token) -> None:
        self._active[turn["id"]] = turn["conversation_id"]
        cid = turn["conversation_id"]
        # its work time, then the room for the last step (runner.run_turn); past it a
        # turn that still runs (a model that hangs) is ended here
        wall = float(self.cfg.r("turn_wall_clock_seconds")) + \
            float(self.cfg.r("last_step_seconds"))
        origin = metrics.origin_of(turn.get("origin"), None)
        began = time.monotonic()
        try:
            # a store error here fails this turn like one in the run: it never leaves
            # the worker
            await roles.resolve(self.store, self.cfg, cid)
            label = await conversations.title(self.store, cid) or None
            kind = await self.store.read(lambda c: (c.execute(
                "SELECT kind FROM conversations WHERE id=?", (cid,)).fetchone() or [None])[0])
            origin = metrics.origin_of(turn.get("origin"), kind)
            metrics.TURNS_STARTED.labels(origin).inc()
            with anyio.move_on_after(wall) as clock:
                run = await self.store.read(lambda c: self.run_binding(c, cid)) \
                    if self.run_binding is not None else None
                backend = await self._cli_backend(cid, clock) or \
                    self._backend_for(turn, token, clock, label, run)
                await run_turn(turn, self.store, self.cfg, backend, self.bus,
                               token, self.registry, self.jobs, clock)
            if clock.cancelled_caught:
                token.cancel()
                await turns.fail_if_active(
                    self.store, turn["id"], turn["conversation_id"],
                    f"exceeded turn_wall_clock_seconds and last_step_seconds ({wall:.0f}s)",
                    "wall_clock")
                log.warning("turn hit the wall clock",
                            extra={"event": "turn.wall_clock", "turn": turn["id"]})
        except Exception as e:
            if self._stopping:
                raise
            log.exception("turn crashed", extra={"event": "turn.crashed",
                                                "turn": turn["id"]})
            await turns.fail_if_active(self.store, turn["id"], turn["conversation_id"],
                                       f"internal error ({type(e).__name__})", "crash")
        finally:
            self._active.pop(turn["id"], None)
            self.tokens.close(turn["id"])
            if self.llm is not None:
                self.llm.forget_turn(turn["id"])
            if self.jobs is not None:        # a call the turn's end cut off is stopped
                await self.jobs.cancel_for_turn(turn["id"])
        if self._stopping:
            return
        try:
            await self._count(turn["id"], origin, time.monotonic() - began)
            # what reached the conversation after the turn's last step lands now
            await self.deliver_idle(cid)
            await self.ended(cid, turn["id"])
            # a work item: a title waits behind every turn in the model queue
            if not await conversations.title(self.store, cid):
                await self.work.put("title", {"conversation_id": cid}, conversation_id=cid,
                                    unique=True)
        except Exception:
            if self._stopping:
                raise
            log.exception("turn end failed", extra={"event": "turn.after_failed",
                                                    "turn": turn["id"]})
