"""The work queue: background model jobs as durable items (table `work`).

Turns keep their own table and claim (turns/store.py). Everything else that asks a
model in the background and that nobody waits on goes here: a conversation's title
(`title`, turns/dispatcher.py), its mood appraisal after a turn (`appraisal`,
skills/mind.py), the next run of the document descriptions when no model answered
(`documents.describe`, kb/documents.py). Image captions and the topic search index have
their own durable state and schedulers (kb/media.py, kb/semantic.py).

The five calls a worker makes, the same shape as turns.claim:
- `claim(worker, kinds)`: the first due item of those kinds (queued, or deferred with
  its `not_before` passed), highest priority first, oldest first; it runs under a lease
  of LEASE_SECONDS. An item whose lease ran out (its worker died) goes back first.
  Nothing while it is frozen (guard/freeze.py): the house leases nothing new then
  (an item already leased runs to its end).
- `heartbeat(id, worker)`: the lease again from now; a worker beats while it runs.
- `finish(id, worker)`: done.
- `defer(id, worker, why, until)`: deferred, no try counted: no host of its models
  answers. It runs at `until`, or as soon as a host comes back (`wake`).
- `fail(id, worker, error)`: one try; queued again after RETRY_SECONDS, failed with its
  reason after MAX_TRIES.
and `put_down(workers)` for a worker that stops (its running items go back to queued).

The pool: WORKERS loops. Before an item runs, its kind's roles are looked at: when every
model of them is on a host the gateway sees down (backends/router.py), the item is
deferred without a call. A handler that raises NoModel, an unreachable error or a queue
timeout is deferred too; any other error is a try. Times are ISO UTC text, as `runs`;
every call takes `now` so a test fixes the clock.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import anyio

from . import metrics
from .core.ids import iso, new_id
from .events import added

log = logging.getLogger("celine.work")

LEASE_SECONDS = 120.0
DEFER_SECONDS = 60.0         # a deferred item looks again after this, or when a host is back
RETRY_SECONDS = 60.0         # a failed try runs again after this
MAX_TRIES = 3
WORKERS = 2
IDLE_MAX = 60.0              # an idle worker looks again at least this often
KEEP_DAYS = 7                # done and failed items are kept this long
OPEN = ("queued", "running", "deferred")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _after(now: datetime | None, seconds: float) -> str:
    return iso(_now(now) + timedelta(seconds=seconds))


def _row(r: sqlite3.Row) -> dict[str, Any]:
    out = dict(r)
    out["payload"] = json.loads(out.get("payload") or "{}")
    return out


# ---- the store calls ----------------------------------------------------------------

async def put(store, kind: str, payload: dict[str, Any], *, priority: int = 0,
              conversation_id: str | None = None, unique: bool = False,
              after: float | None = None, now: datetime | None = None) -> int | None:
    """A new item. `unique`: not when an open item of the same kind and payload exists
    (None then). `after`: deferred for that many seconds (a run that found no model).
    None too when `conversation_id` is gone: purged while the turn that asks ended."""
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    t = _now(now)

    def fn(c: sqlite3.Connection) -> int | None:
        if conversation_id is not None and not c.execute(
                "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
            return None
        if unique and c.execute(
                "SELECT 1 FROM work WHERE kind=? AND payload=? AND state IN (?,?,?)",
                (kind, body, *OPEN)).fetchone():
            return None
        wid = c.execute(
            "INSERT INTO work(kind, priority, state, payload, not_before, conversation_id, "
            "created_at) VALUES(?,?,?,?,?,?,?) RETURNING id",
            (kind, priority, "queued" if after is None else "deferred", body,
             None if after is None else _after(t, after), conversation_id,
             iso(t))).fetchone()[0]
        added(store, c, "work", conversation_id)
        return wid
    return await store.write(fn)


async def claim(store, worker: str, kinds: list[str], *,
                now: datetime | None = None) -> dict[str, Any] | None:
    if not kinds:
        return None
    t = iso(_now(now))
    until = _after(now, LEASE_SECONDS)

    def fn(c: sqlite3.Connection) -> dict[str, Any] | None:
        if c.execute("SELECT 1 FROM kv WHERE k='frozen'").fetchone():
            return None                  # frozen: nothing is leased (guard/freeze.py)
        c.execute("UPDATE work SET state='queued', lease_worker=NULL, lease_until=NULL "
                  "WHERE state='running' AND lease_until < ?", (t,))
        marks = ",".join("?" * len(kinds))
        row = c.execute(
            f"SELECT id FROM work WHERE state IN ('queued','deferred') AND "
            f"(not_before IS NULL OR not_before <= ?) AND kind IN ({marks}) "
            f"ORDER BY priority DESC, created_at, id LIMIT 1", (t, *kinds)).fetchone()
        if row is None:
            return None
        return _row(c.execute(
            "UPDATE work SET state='running', lease_worker=?, lease_until=?, started_at=? "
            "WHERE id=? RETURNING id, kind, priority, payload, tries, conversation_id",
            (worker, until, t, row[0])).fetchone())
    return await store.write(fn)


async def heartbeat(store, wid: int, worker: str, *, now: datetime | None = None) -> bool:
    """False when the item is no longer this worker's (its lease ran out)."""
    until = _after(now, LEASE_SECONDS)
    return await store.write(lambda c: c.execute(
        "UPDATE work SET lease_until=? WHERE id=? AND lease_worker=? AND state='running'",
        (until, wid, worker)).rowcount > 0)


def _end(state_sql: str):
    """An UPDATE of this worker's running item, its lease let go."""
    return (f"UPDATE work SET {state_sql}, lease_worker=NULL, lease_until=NULL "
            f"WHERE id=? AND lease_worker=? AND state='running'")


async def finish(store, wid: int, worker: str, *, now: datetime | None = None) -> None:
    t = iso(_now(now))

    def fn(c: sqlite3.Connection) -> None:
        if c.execute(_end("state='done', error=NULL, finished_at=?"), (t, wid, worker)).rowcount:
            added(store, c, "work")
    await store.write(fn)


async def defer(store, wid: int, worker: str, why: str, *, until: datetime | None = None,
                now: datetime | None = None) -> None:
    nb = iso(until) if until is not None else _after(now, DEFER_SECONDS)
    await store.write(lambda c: c.execute(
        _end("state='deferred', not_before=?, error=?"), (nb, why[:300], wid, worker)))


async def fail(store, wid: int, worker: str, error: str, *,
               now: datetime | None = None) -> str:
    """One try spent. -> the item's state now: queued (tried again) or failed."""
    t = iso(_now(now))
    again = _after(now, RETRY_SECONDS)

    def fn(c: sqlite3.Connection) -> str:
        r = c.execute(_end(
            "tries=tries+1, error=?, "
            "state=CASE WHEN tries+1 >= ? THEN 'failed' ELSE 'queued' END, "
            "not_before=CASE WHEN tries+1 >= ? THEN not_before ELSE ? END, "
            "finished_at=CASE WHEN tries+1 >= ? THEN ? ELSE NULL END") + " RETURNING state",
            (error[:300], MAX_TRIES, MAX_TRIES, again, MAX_TRIES, t, wid, worker)).fetchone()
        if r and r[0] == "failed":
            added(store, c, "work")
        return r[0] if r else "gone"
    return await store.write(fn)


async def put_down(store, workers: list[str] | None = None) -> int:
    """Running items of `workers` back to queued; None: every running item (at startup,
    when every worker lives in this process)."""
    def fn(c: sqlite3.Connection) -> int:
        sql = ("UPDATE work SET state='queued', lease_worker=NULL, lease_until=NULL "
               "WHERE state='running'")
        if workers is None:
            return c.execute(sql).rowcount
        marks = ",".join("?" * len(workers))
        return c.execute(f"{sql} AND lease_worker IN ({marks})", workers).rowcount
    return await store.write(fn)


async def wake(store, *, now: datetime | None = None) -> int:
    """A host is back: every deferred item is due now."""
    t = iso(_now(now))
    return await store.write(lambda c: c.execute(
        "UPDATE work SET not_before=? WHERE state='deferred'", (t,)).rowcount)


async def prune(store, *, now: datetime | None = None) -> int:
    t = iso(_now(now) - timedelta(days=KEEP_DAYS))
    return await store.write(lambda c: c.execute(
        "DELETE FROM work WHERE state IN ('done','failed') AND finished_at < ?",
        (t,)).rowcount)


async def next_due(store, kinds: list[str]) -> datetime | None:
    """The soonest a waiting item of `kinds` comes due or a lease runs out; None: nothing
    waits. Other kinds (no handler here) are not this worker's to wake for."""
    if not kinds:
        return None
    marks = ",".join("?" * len(kinds))
    t = await store.read(lambda c: c.execute(
        f"SELECT min(t) FROM (SELECT not_before t FROM work WHERE state IN "
        f"('queued','deferred') AND not_before IS NOT NULL AND kind IN ({marks}) "
        f"UNION ALL SELECT lease_until FROM work WHERE state='running' "
        f"AND kind IN ({marks}))", (*kinds, *kinds)).fetchone()[0])
    return datetime.fromisoformat(t) if t else None


# ---- the pool -----------------------------------------------------------------------

Handler = Callable[[dict[str, Any]], Awaitable[None]]


def deferrable(e: BaseException) -> bool:
    """No model can answer now (it may later): no try is spent."""
    from .backends.queue import QueueTimeout
    from .backends.router import NoModel
    return isinstance(e, (NoModel, QueueTimeout)) or bool(getattr(e, "unreachable", False))


class Pool:
    """The app's workers. `handle(kind, roles, fn)` before or after `start`; an item
    runs `await fn(payload)`. `roles`: the model roles it asks (its host check)."""

    def __init__(self, store, llm=None, *, workers: int = WORKERS,
                 clock: Callable[[], datetime] | None = None):
        self.store = store
        self.llm = llm
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.handlers: dict[str, tuple[tuple[str, ...], Handler]] = {}
        self.ids = [f"app-{new_id()[:8]}" for _ in range(workers)]
        self._wake = anyio.Event()
        self._back = False
        self._stopping = False

    def handle(self, kind: str, roles: tuple[str, ...], fn: Handler) -> None:
        self.handlers[kind] = (tuple(roles), fn)

    async def put(self, kind: str, payload: dict[str, Any], **kw: Any) -> int | None:
        wid = await put(self.store, kind, payload, now=self.clock(), **kw)
        self.notify()
        return wid

    def notify(self) -> None:
        self._wake.set()

    def host_back(self, _pid: str = "") -> None:
        """The gateway saw a host come back (Router.on_reachable)."""
        self._back = True
        self.notify()

    async def start(self, tg) -> None:
        await put_down(self.store)       # what a previous process left running
        await prune(self.store, now=self.clock())
        if self.llm is not None:
            self.llm.router.on_reachable(self.host_back)
        for w in self.ids:
            tg.start_soon(self._loop, w)

    async def stop(self) -> None:
        self._stopping = True
        self.notify()
        try:
            await put_down(self.store, self.ids)
        except Exception:
            log.exception("work put down failed", extra={"event": "work.put_down_failed"})

    async def _loop(self, worker: str) -> None:
        from .guard.freeze import frozen
        while not self._stopping:
            try:
                if await frozen(self.store) is not None:
                    await self._idle(IDLE_MAX)   # the unfreeze wakes them (notify)
                    continue
                if self._back:
                    self._back = False
                    await wake(self.store, now=self.clock())
                item = await claim(self.store, worker, list(self.handlers), now=self.clock())
                if item is None:
                    await self._idle()
                    continue
                self.notify()            # another idle worker looks too
                await self.run_one(item, worker)
            except Exception:
                if self._stopping:
                    return
                log.exception("work loop failed", extra={"event": "work.loop_failed"})
                await self._idle()

    async def _idle(self, secs: float | None = None) -> None:
        if secs is None:
            due = await next_due(self.store, list(self.handlers))
            secs = IDLE_MAX if due is None else \
                min(IDLE_MAX, max(0.05, (due - self.clock()).total_seconds()))
        with anyio.move_on_after(secs):
            await self._wake.wait()
        self._wake = anyio.Event()

    def _down(self, roles: tuple[str, ...]) -> str | None:
        """The sentence why no host of `roles` answers, else None."""
        if self.llm is None or not roles or not self.llm.router.all_down(roles):
            return None
        return "No host of its models answers."

    async def run_one(self, item: dict[str, Any], worker: str) -> str:
        """One claimed item. -> done, deferred, queued (a try failed) or failed."""
        roles, fn = self.handlers[item["kind"]]
        kind, wid = item["kind"], item["id"]
        if (why := self._down(roles)) is not None:
            await defer(self.store, wid, worker, why, now=self.clock())
            return self._count(kind, "deferred", why)
        err: Exception | None = None
        async with anyio.create_task_group() as tg:
            async def beat() -> None:
                while True:
                    await anyio.sleep(LEASE_SECONDS / 3)
                    await heartbeat(self.store, wid, worker, now=self.clock())
            tg.start_soon(beat)
            try:
                await fn(item["payload"])
            except Exception as e:      # noqa: BLE001 - its state says how it ended
                err = e
            tg.cancel_scope.cancel()
        if err is None:
            await finish(self.store, wid, worker, now=self.clock())
            return self._count(kind, "done")
        why = f"{type(err).__name__}: {err}"[:300]
        if deferrable(err):
            await defer(self.store, wid, worker, str(err)[:300], now=self.clock())
            return self._count(kind, "deferred", why)
        state = await fail(self.store, wid, worker, why, now=self.clock())
        return self._count(kind, state, why)

    def _count(self, kind: str, outcome: str, why: str | None = None) -> str:
        metrics.WORK.labels(kind, outcome).inc()
        if outcome != "done":
            log.info("work item " + outcome, extra={"event": f"work.{outcome}",
                                                    "kind": kind, "why": why})
        return outcome
