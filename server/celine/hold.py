"""Why none of its turns may start now: the one question the dispatcher asks before it
claims a turn (turns/dispatcher.py). "frozen": the user or Bastet froze her
(guard/freeze.py); the unfreeze wakes the workers. "paused": the gateway sees no host of
the chat and tasks models answer (backends/router.py); it resumes by itself when one
is back. Frozen wins over paused. Routines and messages still queue their turns; they
wait here."""
from __future__ import annotations

from .guard.freeze import frozen


async def her_hold(store, llm) -> str | None:
    """"frozen", "paused", or None when its turns may run. `llm`: the LLM service, or
    None (no gateway, never paused)."""
    if await frozen(store) is not None:
        return "frozen"
    if llm is not None and llm.router.paused_since is not None:
        return "paused"
    return None
