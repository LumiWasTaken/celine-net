"""The single writer, the reads around it, and the event logs: each conversation's and
the global feed. One connection sees var/celine.db, var/log.db (attached as `log`) and
the notebook's index (as `notebook`)."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import queue
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import anyio

from ..db.migrate import BUSY_MS, log_path, notebook_db
from .bus import for_pages, lands
from .ids import now
from .event_types import EVENTS_ADDED

log = logging.getLogger("celine.store")
T = TypeVar("T")
# a conversation event's copy on the global feed carries what pages need to refetch,
# not the payload (a tool's result, its arguments)
_HEAVY = ("result", "display", "arguments", "content", "text")

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    f"PRAGMA busy_timeout={BUSY_MS}",
    "PRAGMA synchronous=NORMAL",
)


def attach(conn: sqlite3.Connection, path: Path) -> None:
    """log.db beside `path` as `log` (events, global_events, raw_responses, requests,
    terminal_commands) and the notebook's index as `notebook` (notebook_index,
    notebook_fts): their tables read and write by their bare names, in the same
    transaction as the main file's."""
    conn.execute("ATTACH DATABASE ? AS log", (str(log_path(path)),))
    conn.execute("ATTACH DATABASE ? AS notebook", (str(notebook_db(path)),))
    conn.execute("PRAGMA log.synchronous=NORMAL")
    conn.execute("PRAGMA notebook.synchronous=NORMAL")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for p in _PRAGMAS:
        conn.execute(p)
    attach(conn, path)
    return conn


@dataclass
class _Job:
    fn: Callable[[sqlite3.Connection], Any]
    result: Any = None
    error: BaseException | None = None
    done: anyio.Event | None = None
    ctx: contextvars.Context | None = None    # the caller's context
    # (topic, event) the write appended (Store.event, Store.global_event): published
    # on the loop after its commit
    events: list[tuple[str, dict]] = field(default_factory=list)


# One write connection, one writer thread taking jobs in arrival order: that is what
# makes events.seq gapless without a lock. Never open a second write connection. The
# loop only queues a write and awaits it; a write that takes long (a lock held by
# another process, a large prune) holds up the writes behind it, never the loop.
class Store:
    def __init__(self, db_path: Path, bus=None):
        self.db_path = db_path
        self.bus = bus
        self.alive = False          # the writer thread runs
        self._pending: list[tuple[str, dict]] = []     # the running write's events
        self._conn: sqlite3.Connection | None = None
        self._jobs: queue.SimpleQueue[_Job | None] | None = None   # None: stop
        self._ended: anyio.Event | None = None          # the writer thread is done
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None   # start() sets the writer's
        # one read connection per worker thread, opened once (pragmas, ATTACHes) and
        # kept: a read is a transaction on it, not a new connection
        # idle reader connections: a read takes one and gives it back, so there are never
        # more than reads at once (a thread-local one was left open when its thread ended)
        self._idle: queue.SimpleQueue[sqlite3.Connection] = queue.SimpleQueue()

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._conn = connect(self.db_path)
        self._loop = asyncio.get_running_loop()
        # unbounded: an awaited write waits for its own result anyway, and emit_global
        # must never block or fail a sync caller
        self._jobs, self._ended = queue.SimpleQueue(), anyio.Event()
        self.alive = True
        threading.Thread(target=self._serve, name="store-writer", daemon=True).start()
        task_group.start_soon(self._writer, name="store-writer")

    def _stop(self) -> None:
        """No write is taken after this; the writer ends after the ones queued."""
        if not self._closed and self._jobs is not None:
            self._jobs.put(None)
        self._closed = True

    async def aclose(self) -> None:
        """Every write queued so far lands, then the writer's connection closes."""
        self._stop()
        if self._ended is not None:
            await self._ended.wait()
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                break

    async def _writer(self) -> None:
        """The writer thread's life in the task group: a cancelled group stops it."""
        try:
            await self._ended.wait()
        finally:
            self._stop()
            self.alive = False      # the health check says so: nothing can be written now

    def _serve(self) -> None:
        """The single writer, on its own thread. Serialises every write in arrival
        order; each one's result and events go back to the loop in commit order."""
        conn = self._conn
        try:
            while (job := self._jobs.get()) is not None:
                self._pending = job.events
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        job.result = job.ctx.run(job.fn, conn) if job.ctx else job.fn(conn)
                        conn.execute("COMMIT")
                    except BaseException:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                        raise
                except BaseException as e:      # noqa: BLE001 - reported to caller
                    job.error = e
                    job.events.clear()          # nothing of it landed
                self._pending = []
                self._to_loop(self._finish, job)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
            self._conn = None
            self._to_loop(self._ended.set)

    def _to_loop(self, fn, *args) -> None:
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:                    # the loop has closed: nobody waits
            pass

    def _finish(self, job: _Job) -> None:
        """On the loop, after the commit: the write's events go out, its caller wakes."""
        try:
            if job.error is None and self.bus is not None:
                for topic, event in job.events:
                    self.bus.publish(topic, event)
            elif job.error is not None and job.done is None:   # write_soon: nobody waits
                log.error("queued write failed", exc_info=job.error,
                          extra={"event": "store.write_soon_failed"})
        finally:
            if job.done is not None:
                job.done.set()

    # Callables so a caller cannot await mid-transaction.
    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run fn on the writer thread inside one transaction. fn must be sync."""
        if self._jobs is None or self._closed:
            raise RuntimeError("store is not running")
        job = _Job(fn=fn, done=anyio.Event(), ctx=contextvars.copy_context())
        self._jobs.put(job)
        await job.done.wait()
        if job.error is not None:
            raise job.error
        return job.result            # type: ignore[return-value]

    # One deferred read transaction. resume() reads trim_below and the events in
    # the same snapshot; two transactions would let a prune race in between.
    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run fn in a worker thread inside ONE deferred read transaction."""
        def run() -> T:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                conn = connect(self.db_path)
            conn.execute("BEGIN DEFERRED")
            try:
                out = fn(conn)
                conn.execute("COMMIT")
                return out
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                self._idle.put(conn)

        return await anyio.to_thread.run_sync(run)

    def global_event(self, conn: sqlite3.Connection, type_: str, data: dict) -> int:
        """Inside a write: one event of the global feed, written to global_events and
        published after the commit as {gid, type, ...data}. -> its gid."""
        gid = conn.execute(
            "INSERT INTO global_events(type, data, created_at) VALUES(?,?,?) RETURNING id",
            (type_, json.dumps(data, ensure_ascii=False), now())).fetchone()[0]
        # gid: the feed's own number; an event's own `id` (a conversation) stays
        self._pending.append(("global", {"gid": gid, "type": type_, **data}))
        return gid

    def write_soon(self, fn: Callable[[sqlite3.Connection], Any]) -> None:
        """A write queued for the writer and never awaited, so sync callers (the model
        queue, the archive's tasks, a settings reload) make one too, from any thread; a
        write after it sees it. Dropped once the store has closed."""
        if self._jobs is None or self._closed:
            return
        self._jobs.put(_Job(fn=fn))

    def emit_global(self, event: dict) -> None:
        """One event of the global feed ({"type": ..., ...}) from outside a write
        (write_soon)."""
        data = {k: v for k, v in event.items() if k != "type"}
        self.write_soon(lambda c: self.global_event(c, event["type"], data))

    def event(self, conn: sqlite3.Connection, conversation_id: str, type_: str,
              data: dict, touch: bool = True) -> int:
        """Append one event inside a write; it is published after the commit. `touch`
        False: news that is no activity there (its updated_at stays)."""
        # seq comes from the row counter inside the same transaction as the insert
        row = conn.execute(
            "UPDATE conversations SET next_seq = next_seq + 1, updated_at = "
            "CASE WHEN ? THEN ? ELSE updated_at END WHERE id = ? RETURNING next_seq - 1",
            (touch, now(), conversation_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"no conversation {conversation_id}")
        seq = int(row[0])
        ts = now()
        conn.execute(
            "INSERT INTO events(conversation_id, seq, type, data, created_at) "
            "VALUES(?,?,?,?,?)",
            (conversation_id, seq, type_, json.dumps(data, ensure_ascii=False), ts),
        )
        event = {"seq": seq, "type": type_, "ts": ts, **data}
        self._pending.append((conversation_id, event))
        # what every page needs (turn and job states, titles, its shell and browser
        # calls) goes on the global feed too; deltas and the like stay off it
        if for_pages(event):
            self.global_event(conn, type_, {
                **{k: v for k, v in data.items() if k not in _HEAVY},
                "conversation": conversation_id, "seq": seq})
        if kind := lands(event):
            self.global_event(conn, EVENTS_ADDED, {"kind": kind, "conversation": conversation_id})
        return seq

    async def append_event(self, conversation_id: str, type_: str, data: dict) -> int:
        """Append one event in a write of its own and return the seq it landed at."""
        return await self.write(lambda c: self.event(c, conversation_id, type_, data))

    async def kv_get(self, key: str) -> str | None:
        def fn(c: sqlite3.Connection) -> str | None:
            r = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return r["v"] if r else None

        return await self.read(fn)

    async def kv_set(self, key: str, value: str) -> None:
        await self.write(lambda c: c.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value)))
