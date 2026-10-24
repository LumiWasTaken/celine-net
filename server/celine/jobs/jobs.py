"""Steps that may outlive the turn that started them."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

import anyio

from ..core.ids import new_id
from . import store as rows
from ..skills import ToolResult

log = logging.getLogger("celine.jobs")


@dataclass
class Handle:
    id: str
    call: dict
    turn: dict
    scope: anyio.CancelScope
    done: anyio.Event = field(default_factory=anyio.Event)
    result: ToolResult | None = None
    state: str = "running"
    owner: str | None = None
    stopped_by: str | None = None       # 'user' | 'turn' once stopped
    finishing: anyio.CancelScope | None = None   # its finish as a job, for a Stop
    finished: bool = False              # its row has its end


STOPPED_BY = {"user": "{user} stopped it", "turn": "its reply was stopped"}


def _stopped(handle: "Handle", user: str = "the user") -> ToolResult:
    """The job promised a result; the job message says this instead. A shell
    call only watches a terminal: stopping it ends the watching, not the program."""
    who = STOPPED_BY.get(handle.stopped_by or "", "it was stopped").format(user=user)
    name = handle.call["name"]
    if name.startswith("shell__"):
        return ToolResult(content=f"{name} stopped watching ({who}). Whatever runs in that "
                                  f"terminal keeps running; shell__read shows it.")
    return ToolResult(content=f"{name} was stopped before it finished "
                              f"({who}). It is not running any more, there is no result, "
                              f"and nothing will report back.")


class Jobs:
    def __init__(self, store, cfg, notify, tg: anyio.abc.TaskGroup, on_end=None,
                 registry=None):
        self.store = store
        self.registry = registry        # its app: the page check's judge (guard/page.py)
        self.cfg = cfg
        self.notify = notify
        self.on_end = on_end            # async fn(cid) after a job ended (the dispatcher's)
        self.tg = tg
        self.handles: dict[str, Handle] = {}

    def begin(self, spec, ctx, args, call, turn) -> Handle:
        """Start the step in the job nursery. Runs exactly once, whoever ends up."""
        job_id = new_id()
        scope = anyio.CancelScope()
        handle = Handle(id=job_id, call=call, turn=turn, scope=scope)
        self.handles[job_id] = handle
        self.tg.start_soon(self._run, handle, spec, replace(ctx, turn_id=job_id), args)
        return handle

    async def _run(self, handle: Handle, spec, ctx, args) -> None:
        deadline = float(self.cfg.r("job_timeout_seconds"))
        result: ToolResult | None = None
        state = "done"
        with handle.scope:
            try:
                with anyio.fail_after(deadline):
                    out = await spec.fn(ctx, **args)
                result = out
                state = "failed" if out.is_error else "done"
            except TimeoutError:
                result = ToolResult(content=f"timed out after {deadline:.0f}s",
                                    is_error=True)
                state = "timeout"
            except Exception as e:
                log.warning("step raised", exc_info=True,
                            extra={"event": "job.raised", "job": handle.id,
                                   "error": type(e).__name__})
                result = ToolResult(content=f"failed: {type(e).__name__}: {e}",
                                    is_error=True)
                state = "failed"

        # an outside scope.cancel() unwinds at __exit__ instead of raising, so result stays unset
        if handle.scope.cancel_called or result is None:
            result, state = _stopped(handle, self.cfg.user), "cancelled"

        # no await between publishing the result and reading the owner
        handle.result, handle.state = result, state
        handle.done.set()
        owner = handle.owner
        if owner == "job":
            await self._finish(handle)   # in `handles` until its row has its end
            return
        self.handles.pop(handle.id, None)   # whoever owns it holds the handle itself
        if owner is None:
            handle.owner = "turn"        # the turn is still inside its window

    async def promote(self, handle: Handle) -> str:
        """The step outlived the wait window: give it a job row so it is visible."""
        await rows.create(
            self.store, handle.id, handle.turn["conversation_id"], handle.turn["id"],
            handle.call["id"], handle.call["name"])
        return handle.id

    async def _finish(self, handle: Handle) -> None:
        """_finish_as_job, and its row always gets its end: a finish that raises ends it
        failed with the error, a Stop while it is being finished ends it cancelled.
        Nothing raises up into the lifespan task group: that takes the app down."""
        try:
            with anyio.CancelScope() as handle.finishing:
                await self._finish_as_job(handle)
            if handle.finishing.cancel_called and not handle.finished:
                handle.result, handle.state = _stopped(handle, self.cfg.user), "cancelled"
                await self._post(handle, handle.result, "cancelled")
        except Exception as e:
            log.warning("job could not be finished",
                        extra={"event": "job.finish_failed", "job": handle.id},
                        exc_info=True)
            if not handle.finished:
                handle.result = ToolResult(content=f"failed: {type(e).__name__}: {e}",
                                           is_error=True)
                handle.state = "failed"
                try:
                    await self._post(handle, handle.result, "failed")
                except Exception:
                    log.warning("job could not be ended",
                                extra={"event": "job.end_failed", "job": handle.id},
                                exc_info=True)
        finally:
            self.handles.pop(handle.id, None)

    async def _finish_as_job(self, handle: Handle) -> None:
        """The agent's tool row keeps what it read; the result reaches it once, as a `job`
        message. It wakes it unless it is quiet: a Stop (it or the user knows of it),
        or a result that asked for quiet (a shell watch its own next call ended: it has
        that answer)."""
        from .. import guard                   # imported here, so no import cycle can form
        from ..turns.tools import checked, received
        result, state = handle.result, handle.state
        assert result is not None
        # as a result inside the turn: masked and capped, labelled and page-checked; it
        # is stored and posted both
        result = await checked(
            received(result, handle.turn, handle.call,
                      guard.enabled(self.cfg, handle.turn["conversation_id"])),
            handle.turn, handle.call, self.store, self.cfg,
            getattr(self.registry, "app", None))
        await self._post(handle, result, state)

    async def _post(self, handle: Handle, result: ToolResult, state: str) -> None:
        """Its row and its call get their end, its conversation the `job` message."""
        settled = "cancelled" if state == "cancelled" else \
            ("error" if result.is_error else "ok")
        quiet = state == "cancelled" or bool((result.display or {}).get("quiet"))
        sent = await rows.finish(self.store, handle.id, handle.call, result, settled, state,
                                 stopped_by=handle.stopped_by, wake=not quiet)
        handle.finished = True
        if not sent:
            log.info("job result has nowhere to go",
                     extra={"event": "job.orphaned", "job": handle.id})
        self.notify()
        if sent and self.on_end is not None:
            await self.on_end(handle.turn["conversation_id"])

    async def cancel(self, job_id: str) -> bool:
        """The user's Stop, from the row or the panel: the step, or its finish."""
        handle = self.handles.get(job_id)
        if handle is None or not _stoppable(handle):
            return False
        await self._stop(handle, "user")
        return True

    async def stop_handle(self, handle: Handle) -> None:
        """The turn's own stop: a call that ran out of the turn's time."""
        await self._stop(handle, "turn")

    async def cancel_for_turn(self, turn_id: str) -> int:
        """The turn ended: a step it still owns (not handed to a job) is stopped, so
        nothing runs on unseen after its turn is gone."""
        n = 0
        for handle in list(self.handles.values()):
            if handle.turn["id"] == turn_id and handle.owner != "job" \
                    and not handle.done.is_set():
                await self._stop(handle, "turn")
                n += 1
        return n

    async def cancel_for_conversation(self, cid: str) -> int:
        n = 0
        for handle in list(self.handles.values()):
            if handle.turn["conversation_id"] == cid and _stoppable(handle):
                await self._stop(handle, "user")
                n += 1
        return n

    async def _stop(self, handle: Handle, by: str) -> None:
        handle.stopped_by = by
        if not handle.done.is_set():
            handle.scope.cancel()
        elif handle.finishing is not None:
            handle.finishing.cancel()


def _stoppable(handle: Handle) -> bool:
    """Its step runs, or its finish as a job does (a page check may wait on a model)."""
    return not handle.done.is_set() or (handle.finishing is not None
                                        and not handle.finished)
