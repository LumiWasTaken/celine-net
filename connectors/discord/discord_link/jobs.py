"""Work that outlives a request (a sync, a server's history), one thread each.

A job ends done, cancelled, failed or unreachable. Unreachable is a normal end: the
work continues from its cursors next time. The app follows a job it started through
`/admin/jobs/{id}` and shows its progress as one of its own tasks.

The schedule: a full sync every `sync_every_minutes`, every 5 minutes while the last
one found Discord unreachable (never later than the interval), and at once when the
gateway is back after a drop.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import Cancelled, Ctx, Failed, Unreachable
from .state import now_ms

log = logging.getLogger("discord_link.jobs")

UNREACHABLE_RETRY_MS = 300_000
KEEP = 50


def due_at(every_ms: float, *, last_ok: int | None, last_attempt: int | None,
           unreachable: bool) -> int:
    """When the scheduled sync runs next: one interval after the last run, or every
    UNREACHABLE_RETRY_MS while the last run found Discord unreachable."""
    if unreachable:
        return int((last_attempt or 0) + min(UNREACHABLE_RETRY_MS, every_ms))
    return int(max(last_ok or 0, last_attempt or 0) + every_ms)


def sentence(e: BaseException) -> str:
    if isinstance(e, (Failed, Unreachable)):
        return str(e)
    return f"The run stopped with an error ({type(e).__name__})."


@dataclass
class Job:
    id: str
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

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "state": self.state,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "progress": dict(self.progress), "result": self.result, "error": self.error}


class Jobs:
    def __init__(self) -> None:
        self.all: dict[str, Job] = {}
        self._mu = threading.Lock()

    def get(self, jid: str) -> Job | None:
        return self.all.get(jid)

    def start(self, kind: str, label: str, fn: Callable[[Ctx], dict[str, Any]]) -> Job:
        """The running job of this kind, or a new one."""
        with self._mu:
            live = next((j for j in self.all.values()
                         if j.kind == kind and j.state == "running"), None)
            if live is not None:
                return live
            job = Job(id=f"j_{uuid.uuid4().hex[:10]}", kind=kind, label=label)
            done = [j for j in self.all.values() if j.state != "running"]
            for j in sorted(done, key=lambda j: j.started_at)[:max(0, len(done) - KEEP)]:
                self.all.pop(j.id, None)
            self.all[job.id] = job

        def progress(done: int, total: int | None, unit: str) -> None:
            job.progress = {"done": done, "total": total, "unit": unit}

        ctx = Ctx(progress=progress, cancelled=job.cancel.is_set)

        def run() -> None:
            try:
                job.result = fn(ctx)
                job.state = "done"
            except Cancelled:
                job.state = "cancelled"
            except Unreachable as e:
                job.state, job.error = "unreachable", sentence(e)
                log.info("job unreachable", extra={"event": "job.unreachable", "kind": kind})
            except Exception as e:
                job.state, job.error = "failed", sentence(e)
                if not isinstance(e, Failed):
                    log.exception("job failed", extra={"event": "job.failed", "kind": kind})
            finally:
                job.finished_at = now_ms()
        threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
        return job

    def cancel(self, jid: str) -> bool:
        job = self.all.get(jid)
        if job is None or job.state != "running":
            return False
        job.cancel.set()
        return True
