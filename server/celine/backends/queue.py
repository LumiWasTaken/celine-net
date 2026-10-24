"""The LLM request scheduler. Every chat/completion call waits here for a slot.

A slot is one request in flight on one (endpoint, model). Limits come from a `Limits`
object: slots per model (llama-server --parallel), an optional cap per endpoint, and
whether the endpoint is exclusive (llama-swap group with swap: asking for a second
model unloads the first and kills its in-flight requests, so while one of our models
runs there, requests for another model on that endpoint wait).

Order: strict priority (interactive > agent > background), round-robin between lanes
(one lane per conversation) inside a priority, FIFO inside a lane. On an exclusive
endpoint a lane whose next ticket is for the model that ran there last goes first, at
most AFFINITY of that model in a row, then another model's: two conversations on two
models swap models half as often. A ticket that cannot start
blocks the resources it waits for against every ticket behind it, so a busy interactive
request is never overtaken by a background one on the same model.

A lease (an `exclusive` ticket) takes a whole endpoint, for a batch of images or music on the
GPU the endpoint's models run on (backends/llm.py `gpu_lease`). Text goes first: a
lease waits while any request runs or waits on its endpoint, and then `gpu_idle_seconds`
more, so a conversation that is still talking is not interrupted by a model swap. While
a lease runs, text waits (held, never timed out, the wait credited to its turn). After
`gpu_patience_seconds` of waiting a lease holds new text itself, so a stream of agent
work cannot starve it. Leases start in the order they were asked.

Nothing here does I/O. `acquire` waits, `release` frees. Waiting is cancellable by a
turn's Token and bounded by a timeout; an interactive request (the user waits on it) is
given none (backends/llm.py) and waits as long as it takes, saying who holds the model.
"""
from __future__ import annotations

import itertools
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Protocol

import anyio

from .base import BackendError
from ..core.event_types import LLM_QUEUE

log = logging.getLogger("celine.backends")


AFFINITY = 2        # tickets of the model an exclusive endpoint holds, in a row


class Priority(IntEnum):
    INTERACTIVE = 0     # the user's own turn
    AGENT = 1           # trigger / job / sub-agent turns
    BACKGROUND = 2      # titles, descriptions, captions


class QueueTimeout(BackendError):
    """Waited too long for a slot."""


class QueueCancelled(BackendError):
    """The turn was stopped while its request was still waiting."""

    def __init__(self, message: str = "stopped while waiting for the model"):
        super().__init__(message, retryable=False)


class Limits(Protocol):
    def slots(self, endpoint: str, model: str) -> int: ...          # 0 = unlimited
    def endpoint_max(self, endpoint: str) -> int: ...               # 0 = unlimited
    def exclusive(self, endpoint: str) -> bool: ...


_ids = itertools.count(1)


@dataclass(eq=False)
class Ticket:
    endpoint: str
    model: str
    priority: Priority
    lane: str
    role: str = ""                      # "chat", "small", ... for events and logs
    # who it is for, as the user reads it: the conversation's title, a schedule's name
    label: str = ""
    conversation_id: str | None = None
    turn_id: str | None = None
    id: int = field(default_factory=lambda: next(_ids))
    created: float = field(default_factory=time.monotonic)
    started: float | None = None
    granted: bool = False
    exclusive: bool = False             # a lease: the whole endpoint, nothing beside it
    changed: anyio.Event = field(default_factory=anyio.Event)

    @property
    def waited_ms(self) -> int:
        end = self.started if self.started is not None else time.monotonic()
        return int((end - self.created) * 1000)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "conversation_id": self.conversation_id,
                "turn_id": self.turn_id, "role": self.role, "label": self.label,
                "priority": self.priority.name.lower(), "model": self.model,
                "endpoint": self.endpoint, "waited_ms": self.waited_ms,
                "exclusive": self.exclusive}


@dataclass(frozen=True)
class Position:
    ahead: int          # waiting tickets that start before this one on the same endpoint
    running: int        # requests in flight on the same endpoint
    lease: bool = False  # another ticket's lease on the endpoint holds this one


class Scheduler:
    def __init__(self, limits: Limits, on_change: Callable[[dict], None] | None = None,
                 settings: Callable[[], dict] | None = None):
        self.limits = limits
        self.on_change = on_change
        self.settings = settings or (lambda: {})     # the [queue] table, read live
        self.last_text_end: dict[str, float] = {}    # endpoint -> when text last ended
        # exclusive endpoint -> (the model granted there last, how many in a row)
        self.streak: dict[str, tuple[str, int]] = {}
        # priority -> lane -> FIFO; lane order is the round-robin order
        self._waiting: dict[Priority, OrderedDict[str, deque[Ticket]]] = {
            p: OrderedDict() for p in Priority}
        self.running: list[Ticket] = []

    # ---- state ------------------------------------------------------------------

    def order(self) -> list[Ticket]:
        """Every waiting ticket in the order it would start if nothing were full."""
        out: list[Ticket] = []
        for p in Priority:
            lanes = list(self._waiting[p].values())
            # round-robin, one ticket of each lane a round, so a lane stays FIFO
            for rnd in range(max((len(q) for q in lanes), default=0)):
                out += sorted((q[rnd] for q in lanes if rnd < len(q)), key=self._rank)
        return out

    def _rank(self, t: Ticket) -> int:
        """Inside a round: 0 when `t` asks for the model its exclusive endpoint ran last,
        fewer than AFFINITY times in a row; 2 when that model ran AFFINITY times in a
        row there (another model goes first); else 1."""
        if t.exclusive or not self.limits.exclusive(t.endpoint):
            return 1
        model, n = self.streak.get(t.endpoint, ("", 0))
        return 1 if t.model != model else 0 if n < AFFINITY else 2

    @property
    def waiting(self) -> list[Ticket]:
        return self.order()

    def position(self, t: Ticket) -> Position:
        ahead = 0
        for other in self.order():
            if other is t:
                break
            if other.endpoint == t.endpoint:
                ahead += 1
        on_ep = [r for r in self.running if r.endpoint == t.endpoint]
        return Position(ahead, len(on_ep), self._held(t))

    def holder(self, endpoint: str) -> str | None:
        """The label of the oldest request running on `endpoint`; None when none runs."""
        on_ep = [r for r in self.running if r.endpoint == endpoint]
        return min(on_ep, key=lambda r: r.started or 0.0).label if on_ep else None

    def _patience(self) -> float:
        return float(self.settings().get("gpu_patience_seconds", 600))

    def _held(self, t: Ticket) -> bool:
        """A lease holds `t` back: one runs on its endpoint, or one has waited there
        past its patience. A waiting lease otherwise holds nothing: text goes first."""
        if t.exclusive:
            return False
        if any(r.exclusive and r.endpoint == t.endpoint for r in self.running):
            return True
        patience = self._patience()
        return any(x.exclusive and x.endpoint == t.endpoint and x.waited_ms / 1000 >= patience
                   for x in self.order())

    def snapshot(self) -> dict[str, Any]:
        return {"type": LLM_QUEUE,
                "running": [t.public() for t in self.running],
                "waiting": [{**t.public(), "ahead": i}
                            for i, t in enumerate(self.order())]}

    # ---- the rules --------------------------------------------------------------

    def _blocker(self, t: Ticket) -> str | None:
        """None when `t` may start now; else "model", "endpoint", or "text" for a lease
        that waits its turn behind text (running, waiting, or ended less than
        `gpu_idle_seconds` ago) and blocks nothing behind it."""
        on_ep = [r for r in self.running if r.endpoint == t.endpoint]
        if any(r.exclusive for r in on_ep):
            return "endpoint"
        if t.exclusive:
            if on_ep:
                return "text"
            if t.waited_ms / 1000 < self._patience():
                if any(not x.exclusive and x.endpoint == t.endpoint for x in self.order()):
                    return "text"
                idle = float(self.settings().get("gpu_idle_seconds", 10))
                if time.monotonic() - self.last_text_end.get(t.endpoint, 0.0) < idle:
                    return "text"
            return None
        if self.limits.exclusive(t.endpoint) and any(r.model != t.model for r in on_ep):
            return "endpoint"
        cap = self.limits.endpoint_max(t.endpoint)
        if cap and len(on_ep) >= cap:
            return "endpoint"
        slots = self.limits.slots(t.endpoint, t.model)
        if slots and sum(1 for r in on_ep if r.model == t.model) >= slots:
            return "model"
        return None

    def _dispatch(self) -> None:
        blocked_ep: set[str] = set()
        blocked_model: set[tuple[str, str]] = set()
        for t in self.order():
            key = (t.endpoint, t.model)
            if t.endpoint in blocked_ep or key in blocked_model:
                continue
            if self._held(t):
                continue
            why = self._blocker(t)
            if why == "text":
                continue
            if why == "model":
                blocked_model.add(key)
                continue
            if why == "endpoint":
                blocked_ep.add(t.endpoint)
                continue
            self._grant(t)
        for t in self.order():
            t.changed.set()          # positions may have moved; waiters recompute

    def _grant(self, t: Ticket) -> None:
        lanes = self._waiting[t.priority]
        q = lanes[t.lane]
        q.remove(t)
        if q:
            lanes.move_to_end(t.lane)        # this lane had its go; the next lane is up
        else:
            del lanes[t.lane]
        t.granted = True
        t.started = time.monotonic()
        self.running.append(t)
        if t.exclusive:
            self.streak.pop(t.endpoint, None)        # a lease unloads every model
        elif self.limits.exclusive(t.endpoint):
            model, n = self.streak.get(t.endpoint, ("", 0))
            self.streak[t.endpoint] = (t.model, n + 1 if model == t.model else 1)
        t.changed.set()

    def _remove_waiting(self, t: Ticket) -> None:
        lanes = self._waiting[t.priority]
        q = lanes.get(t.lane)
        if q is not None and t in q:
            q.remove(t)
            if not q:
                del lanes[t.lane]

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self.snapshot())
            except Exception:           # a listener never breaks scheduling
                log.warning("queue listener failed", exc_info=True,
                            extra={"event": "queue.listener_failed"})

    # ---- API --------------------------------------------------------------------

    async def acquire(self, t: Ticket, *, token=None, timeout: float | None = None,
                      on_wait: Callable[[Position], Awaitable[None]] | None = None) -> Ticket:
        """Wait until `t` holds a slot. Raises QueueCancelled when `token` is cancelled
        first, QueueTimeout after `timeout` seconds; the ticket is gone either way."""
        if token is not None and token.cancelled:
            raise QueueCancelled()
        self._waiting[t.priority].setdefault(t.lane, deque()).append(t)
        self._dispatch()
        self._changed()
        last: Position | None = None
        try:
            with anyio.fail_after(timeout if timeout and timeout > 0 else None):
                while not t.granted:
                    pos = self.position(t)
                    if on_wait is not None and pos != last:
                        last = pos
                        await on_wait(pos)
                        if t.granted:
                            break
                    # a lease also looks again every second: the idle gate opens by itself
                    with anyio.move_on_after(1.0 if t.exclusive else None) as look:
                        await _either(t.changed, token)
                    t.changed = anyio.Event()
                    if look.cancelled_caught and not t.granted:
                        self._dispatch()
                    if token is not None and token.cancelled and not t.granted:
                        raise QueueCancelled()
        except TimeoutError:
            self._abandon(t)
            raise QueueTimeout(
                f"Waited {timeout:.0f}s for {t.model}; it is busy with other requests.") from None
        except BaseException:
            self._abandon(t)
            raise
        return t

    def _abandon(self, t: Ticket) -> None:
        if t.granted:
            self.release(t)
        else:
            self._remove_waiting(t)
            self._dispatch()
            self._changed()

    def release(self, t: Ticket) -> None:
        if t in self.running:
            self.running.remove(t)
            if not t.exclusive:
                self.last_text_end[t.endpoint] = time.monotonic()
            self._dispatch()
            self._changed()


async def _either(ev: anyio.Event, token) -> None:
    """Return when `ev` is set or `token` is cancelled."""
    if token is None:
        await ev.wait()
        return
    if token.cancelled:
        return
    async with anyio.create_task_group() as tg:
        async def wait(fn):
            await fn()
            tg.cancel_scope.cancel()
        tg.start_soon(wait, ev.wait)
        tg.start_soon(wait, token.wait)

