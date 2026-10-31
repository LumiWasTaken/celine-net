"""What runs for a feature while it is on, started and stopped while the app runs.

The lifespan registers one Runner per feature that runs something on the services bag
(`runners`): shellbox the TerminalSync loop, browser the browser watch, vault the login,
discord and whatsapp their MCP server with its archive and feed, documents its index and
watcher, routines the Scheduler and the triggers, missions the check-ins, guard the
patrol and Bastet's answers, and `media` (images or music) the batches. A features PUT or
a services PUT calls `apply(on, changed)`: what became on starts, what became off stops,
what changed its address stops and starts again. Each runner is one task in the
lifespan's task group under its own CancelScope; stop cancels it and waits for it to end.
A runner lives while its feature is on: a function that returns (the vault's login) keeps
it running, so the next apply does not run that function again; one that raises ends it,
and the next apply starts it again.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import anyio

log = logging.getLogger("celine.runners")


class Runner:
    def __init__(self, tg: anyio.abc.TaskGroup, fn: Callable[[], Awaitable[Any]],
                 features: tuple[str, ...] = (),
                 on_stop: Callable[[], Awaitable[Any]] | None = None):
        self.tg = tg
        self.what = fn
        self.features = features    # on while any of them is on; () its own id
        self.on_stop = on_stop      # after it stops because its feature was switched off
        self.scope: anyio.CancelScope | None = None
        self.done: anyio.Event | None = None

    @property
    def running(self) -> bool:
        return self.scope is not None

    async def start(self) -> None:
        if self.running:
            return
        self.scope, self.done = anyio.CancelScope(), anyio.Event()
        self.tg.start_soon(self._run)
        await anyio.lowlevel.checkpoint()   # its first step (opening what it runs) before the PUT answers

    async def _run(self) -> None:
        try:
            with self.scope:
                await self.what()
                await anyio.sleep_forever()
        except Exception:
            log.exception("runner failed", extra={"event": "runner.failed"})
        finally:
            self.scope = None
            self.done.set()

    async def stop(self) -> None:
        if not self.running:
            return
        self.scope.cancel()
        await self.done.wait()


class Runners(dict[str, Runner]):
    async def apply(self, on: dict[str, bool], changed: set[str] = frozenset()) -> None:
        """`on`: every feature's switch (features.effective); `changed`: the services
        whose address changed, started again when on."""
        for fid, r in self.items():
            if not any(on.get(f, True) for f in r.features or (fid,)):
                was = r.running
                await r.stop()
                if was and r.on_stop is not None:
                    await r.on_stop()
            elif fid in changed:
                await r.stop()
                await r.start()
            else:
                await r.start()
