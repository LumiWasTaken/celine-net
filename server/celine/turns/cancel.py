"""One source of truth for "has this turn been stopped"."""
from __future__ import annotations

from dataclasses import dataclass, field

import anyio


@dataclass
class Token:
    turn_id: str
    _cancelled: bool = False
    _event: anyio.Event = field(default_factory=anyio.Event)
    # why: None (the user's Stop, or the wall clock) or "shutdown" (the server is
    # restarting: the turn is put back in the queue, not ended)
    reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def shutdown(self) -> bool:
        return self._cancelled and self.reason == "shutdown"

    def cancel(self, reason: str | None = None) -> None:
        if not self._cancelled:
            self.reason = reason
            self._cancelled = True
            self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class Registry:
    """Live tokens, one per turn in flight."""

    def __init__(self) -> None:
        self._tokens: dict[str, Token] = {}

    def open(self, turn_id: str, already_cancelled: bool = False) -> Token:
        token = Token(turn_id)
        if already_cancelled:
            token.cancel()
        self._tokens[turn_id] = token
        return token

    def close(self, turn_id: str) -> None:
        self._tokens.pop(turn_id, None)

    def cancel(self, turn_id: str) -> bool:
        """Whether a live turn was carrying this token."""
        token = self._tokens.get(turn_id)
        if token is None:
            return False
        token.cancel()
        return True

    def live(self) -> list[Token]:
        return list(self._tokens.values())


async def until_stop(fn, token: Token, *, shutdown_stops: bool = True):
    """Runs `fn()`; a Stop cancels it at once, a shutdown too unless `shutdown_stops` is
    False (work that finishes is kept). -> (stopped, what fn returned, None when it was
    cancelled). What fn raised is raised, unwrapped from the task group."""
    stopped, out, raised = False, None, None
    async with anyio.create_task_group() as tg:
        async def on_stop() -> None:
            nonlocal stopped
            await token.wait()
            if shutdown_stops or not token.shutdown:
                stopped = True
                tg.cancel_scope.cancel()
        tg.start_soon(on_stop)
        try:
            out = await fn()
        except Exception as e:
            raised = e
        tg.cancel_scope.cancel()
    if raised is not None:
        raise raised
    return stopped, out


async def until_stopped(events, token):
    """Yields from `events`; a Stop ends the wait for the next event at once (the
    pending step of `events` is cancelled) and raises QueueCancelled."""
    from ..backends.queue import QueueCancelled
    try:
        while True:
            try:
                stopped, ev = await until_stop(events.__anext__, token)
            except StopAsyncIteration:
                return
            if stopped:
                raise QueueCancelled("stopped")
            yield ev
    finally:
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()
