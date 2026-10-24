"""The single writer, the reads around it, and the event logs: each conversation's and
the global feed. One connection sees var/celine.db, var/log.db (attached as `log`) and
the notebook's index (as `notebook`)."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

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


# One write connection, one writer task: that is what makes events.seq gapless
# without a lock. Never open a second write connection.
class Store:
    def __init__(self, db_path: Path, bus=None):
        self.db_path = db_path
        self.bus = bus
        self._pending: list[tuple[str, dict]] = []
        self._conn: sqlite3.Connection | None = None
        self._tx: MemoryObjectSendStream[_Job] | None = None
        self._rx: MemoryObjectReceiveStream[_Job] | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None   # start() sets the writer's
        self._thread: int | None = None
        # one read connection per worker thread, opened once (pragmas, ATTACHes) and
        # kept: a read is a transaction on it, not a new connection
        self._readers = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self._opened_lock = threading.Lock()

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._conn = connect(self.db_path)
        self._loop, self._thread = asyncio.get_running_loop(), threading.get_ident()
        # unbounded: an awaited write waits for its own result anyway, and emit_global
        # must never block or fail a sync caller
        self._tx, self._rx = anyio.create_memory_object_stream[_Job](math.inf)
        task_group.start_soon(self._writer, name="store-writer")

    async def aclose(self) -> None:
        self._closed = True
        if self._tx is not None:
            await self._tx.aclose()
        with self._opened_lock:
            for conn in self._opened:
                conn.close()
            self._opened.clear()
        self._readers = threading.local()

    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._readers, "conn", None)
        if conn is None:
            conn = self._readers.conn = connect(self.db_path)
            with self._opened_lock:
                self._opened.append(conn)
        return conn

    async def _writer(self) -> None:
        """The single writer. Serialises every write in arrival order."""
        assert self._rx is not None and self._conn is not None
        async with self._rx:
            async for job in self._rx:
                self._pending.clear()
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        job.result = job.ctx.run(job.fn, self._conn) if job.ctx \
                            else job.fn(self._conn)
                        self._conn.execute("COMMIT")
                    except BaseException:
                        if self._conn.in_transaction:
                            self._conn.execute("ROLLBACK")
                        raise
                    if self.bus is not None:
                        for topic, event in self._pending:
                            self.bus.publish(topic, event)
                except BaseException as e:      # noqa: BLE001 - reported to caller
                    job.error = e
                    if job.done is None:        # write_soon: nobody waits for it
                        log.error("queued write failed", exc_info=e,
                                  extra={"event": "store.write_soon_failed"})
                finally:
                    self._pending.clear()
                    if job.done is not None:
                        job.done.set()
        if self._conn is not None:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
            self._conn = None

    # Callables so a caller cannot await mid-transaction.
    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run fn on the writer task inside one transaction. fn must be sync."""
        if self._tx is None or self._closed:
            raise RuntimeError("store is not running")
        job = _Job(fn=fn, done=anyio.Event(), ctx=contextvars.copy_context())
        await self._tx.send(job)
        await job.done.wait()
        if job.error is not None:
            raise job.error
        return job.result            # type: ignore[return-value]

    # One deferred read transaction. resume() reads trim_below and the events in
    # the same snapshot; two transactions would let a prune race in between.
    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run fn in a worker thread inside ONE deferred read transaction."""
        def run() -> T:
            conn = self._reader()
            conn.execute("BEGIN DEFERRED")
            try:
                out = fn(conn)
                conn.execute("COMMIT")
                return out
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

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
        if self._tx is None or self._closed:
            return
        if threading.get_ident() != self._thread:      # the stream is the loop's
            self._loop.call_soon_threadsafe(self.write_soon, fn)
            return
        self._tx.send_nowait(_Job(fn=fn))

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
