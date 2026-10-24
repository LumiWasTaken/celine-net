"""Long knowledge-base work (imports, syncs, embedding) that outlives a request.

In memory: a task is a thread plus a row here. A restart ends running tasks, and
because every writer commits in batches and every import is idempotent,
running it again picks up where it stopped.

Two kinds of work:
- A visible task is one the user started (Sync now, a fill, a restore). It is listed
  with its progress and outcome until it expires or is dismissed.
- A quiet task is a scheduled job's run (syncs, index refreshes, captions). Runs are
  never listed. Each job, one per (kb, kind), keeps only the outcome of its last run:
  one state, not a stream of failures (`jobs()`, event `kb.job` when it changes).
  `running` says a new run is checking again; a job whose last run did not end done
  announces that (`kb.job`), so a page stops showing the old reason as current.

A run ends in one of four states: done, cancelled, failed, or unreachable. Unreachable
(importers.Unreachable) is a normal end: the connection or server did not answer, the
work continues from its cursor next time, and the schedule is the retry (`due_at`).
Failed is for real errors and keeps its trace in the log.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.ids import now_ms
from .importers import Cancelled, Failed, ImportContext, Unreachable
from ..core.event_types import KB_CHANGED, KB_JOB, KB_TASK, KB_TASK_REMOVED

log = logging.getLogger("celine.kb.tasks")

KEEP_FINISHED = 50
DONE_TTL_MS = 3_600_000          # a finished visible task leaves the list after an hour
UNREACHABLE_RETRY_MS = 300_000   # a job that found its connection unreachable: every 5 min


def due_at(every_ms: float, *, last_ok: int | None, last_attempt: int | None,
           unreachable: bool) -> int:
    """When a scheduled job runs next. The schedule is the only retry policy: one
    interval after the last run, or every UNREACHABLE_RETRY_MS while the last run
    found its connection unreachable (never later than the interval)."""
    if unreachable:
        return int((last_attempt or 0) + min(UNREACHABLE_RETRY_MS, every_ms))
    return int(max(last_ok or 0, last_attempt or 0) + every_ms)


def sentence(e: BaseException) -> str:
    """What a run's end says to the user: Failed's and Unreachable's own sentence, or
    one plain line for anything unexpected (the trace goes to the log)."""
    if isinstance(e, (Failed, Unreachable)):
        return str(e)
    return f"The task stopped with an error ({type(e).__name__}). See the server log."


@dataclass
class Task:
    id: str
    kb: str
    kind: str
    label: str
    state: str = "running"
    started_at: int = field(default_factory=now_ms)
    finished_at: int | None = None
    progress: dict[str, Any] = field(default_factory=lambda: {"done": 0, "total": None,
                                                              "unit": ""})
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    retry: Callable[[], "Task"] | None = None
    quiet: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kb": self.kb, "kind": self.kind, "label": self.label,
                "state": self.state, "started_at": self.started_at,
                "finished_at": self.finished_at, "progress": dict(self.progress),
                "result": self.result, "error": self.error}


@dataclass
class Job:
    """The last outcome of a scheduled job. `since`: when this state began (the same
    state run after run keeps it)."""
    kb: str
    kind: str
    label: str
    state: str
    since: int
    finished_at: int
    error: str | None = None
    running: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"kb": self.kb, "kind": self.kind, "label": self.label, "state": self.state,
                "since": self.since, "finished_at": self.finished_at, "error": self.error,
                "running": self.running}


class Tasks:
    def __init__(self, publish: Callable[[dict[str, Any]], None]):
        self._publish = publish
        self._tasks: dict[str, Task] = {}
        self._jobs: dict[tuple[str, str], Job] = {}
        self._lock = threading.Lock()
        self.loop: Any = None

    def bind(self, loop: Any) -> None:
        """The server's event loop. The bus is not thread safe, so task threads hand
        their events to the loop instead of publishing directly."""
        self.loop = loop

    def emit(self, ev: dict[str, Any]) -> None:
        """Publish an event from any thread (on the server's loop when there is one)."""
        try:
            if self.loop is not None:
                self.loop.call_soon_threadsafe(self._publish, ev)
            else:
                self._publish(ev)
        except Exception:
            log.debug("publish failed", exc_info=True)

    def _prune(self) -> None:
        """Drop expired finished tasks. Caller holds the lock."""
        now = now_ms()
        for t in list(self._tasks.values()):
            if t.finished_at is not None and now - t.finished_at > DONE_TTL_MS:
                self._tasks.pop(t.id, None)
        finished = [t for t in self._tasks.values() if t.state != "running"]
        for old in sorted(finished, key=lambda t: t.started_at)[:-KEEP_FINISHED]:
            self._tasks.pop(old.id, None)

    def list(self) -> list[dict[str, Any]]:
        """Visible tasks, newest first. Quiet runs are never listed."""
        with self._lock:
            self._prune()
            return [t.as_dict() for t in sorted(self._tasks.values(),
                                                key=lambda t: -t.started_at) if not t.quiet]

    def jobs(self) -> list[dict[str, Any]]:
        """One entry per scheduled job that has run: its last outcome."""
        with self._lock:
            return [j.as_dict() for j in self._jobs.values()]

    def job(self, kb: str, kind: str) -> dict[str, Any] | None:
        with self._lock:
            j = self._jobs.get((kb, kind))
            return j.as_dict() if j else None

    def dismiss(self, tid: str) -> bool:
        """Remove a finished task from the list. Running tasks are cancelled instead."""
        with self._lock:
            t = self._tasks.get(tid)
            if t is None or t.state == "running":
                return False
            self._tasks.pop(tid, None)
        self.emit({"type": KB_TASK_REMOVED, "id": tid})
        return True

    def clear(self) -> int:
        """Remove every finished task."""
        with self._lock:
            gone = [t.id for t in self._tasks.values() if t.state != "running"]
            for tid in gone:
                self._tasks.pop(tid, None)
        for tid in gone:
            self.emit({"type": KB_TASK_REMOVED, "id": tid})
        return len(gone)

    def get(self, tid: str) -> Task | None:
        return self._tasks.get(tid)

    def running(self, kb: str, kind: str) -> Task | None:
        with self._lock:
            return next((t for t in self._tasks.values()
                         if t.kb == kb and t.kind == kind and t.state == "running"), None)

    def cancel(self, tid: str) -> bool:
        t = self._tasks.get(tid)
        if t is None or t.state != "running":
            return False
        t.cancel.set()
        return True

    def _record_job(self, task: Task, state: str) -> None:
        """A quiet run, or any run of a kind that has a job, sets that job's outcome.
        Called before the task's own state flips, so whoever sees the task finished
        also sees its job."""
        key = (task.kb, task.kind)
        with self._lock:
            old = self._jobs.get(key)
            if old is None and not task.quiet:
                return
            if task.quiet:
                self._tasks.pop(task.id, None)
            now = task.finished_at or now_ms()
            since = old.since if old is not None and old.state == state else now
            job = Job(kb=task.kb, kind=task.kind, label=task.label, state=state,
                      since=since, finished_at=now, error=task.error)
            self._jobs[key] = job
        if old is None or (old.state, old.error) != (job.state, job.error) or \
                old.state != "done":
            self.emit({"type": KB_JOB, "job": job.as_dict()})

    def start(self, kb: str, kind: str, label: str,
              work: Callable[[ImportContext], dict[str, Any] | None],
              on_done: Callable[[], None] | None = None, *, quiet: bool = False) -> Task:
        task = Task(id=f"t_{uuid.uuid4().hex[:12]}", kb=kb, kind=kind, label=label,
                    quiet=quiet)
        task.retry = lambda: self.start(kb, kind, label, work, on_done)   # a retry is yours
        with self._lock:
            self._tasks[task.id] = task
            self._prune()
            job = self._jobs.get((kb, kind))
            if job is not None:
                job.running = True
            recheck = job.as_dict() if job is not None and job.state != "done" else None
        if recheck is not None:
            self.emit({"type": KB_JOB, "job": recheck})
        last = [0.0]

        def progress(done: int, total: int | None, unit: str) -> None:
            task.progress = {"done": done, "total": total, "unit": unit}
            now = time.monotonic()
            if now - last[0] > 0.5 or (total is not None and done >= total):
                last[0] = now
                if not task.quiet:
                    self.emit({"type": KB_TASK, "task": task.as_dict()})

        ctx = ImportContext(progress=progress, cancelled=task.cancel.is_set)

        def body() -> None:
            log.info("task started", extra={"event": "kb.task.started", "task": task.id,
                                            "kb": kb, "kind": kind})
            what = {"task": task.id, "kb": kb, "kind": kind}
            state, error = "done", None
            try:
                task.result = work(ctx) or {}
                state = "cancelled" if task.cancel.is_set() else "done"
            except Cancelled:
                state = "cancelled"
            except Unreachable as e:
                state, error = "unreachable", sentence(e)
                log.info("task ended unreachable", extra={"event": "kb.task.unreachable",
                                                          **what, "error": error})
            except Failed as e:
                state, error = "failed", sentence(e)
                log.warning("task failed", extra={"event": "kb.task.failed", **what,
                                                  "error": error})
            except Exception as e:
                state, error = "failed", sentence(e)
                log.warning("task failed", extra={"event": "kb.task.failed", **what,
                                                  "error": f"{type(e).__name__}: {e}",
                                                  "trace": traceback.format_exc(limit=6)})
            task.error, task.finished_at = error, now_ms()
            self._record_job(task, state)
            task.state = state
            log.info("task finished", extra={"event": "kb.task.finished", "task": task.id,
                                             "state": state})
            if not task.quiet:
                self.emit({"type": KB_TASK, "task": task.as_dict()})
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    log.exception("task on_done failed")
            self.emit({"type": KB_CHANGED, "kb": kb})

        threading.Thread(target=body, name=f"kb-{kind}-{task.id}", daemon=True).start()
        if not quiet:
            self.emit({"type": KB_TASK, "task": task.as_dict()})
        return task
