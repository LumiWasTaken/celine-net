"""The LLM request queue, for the UI: what runs and what waits, per model."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..settings.kinds import KINDS
from ..core.event_types import LLM_QUEUE

router = APIRouter(prefix="/api")


@router.get("/llm-queue")
async def queue(request: Request) -> dict:
    """Same shape as the `llm.queue` event on the global feed (WS /ws/events)."""
    llm = request.app.state.services.llm
    if llm is None:
        return {"type": LLM_QUEUE, "running": [], "waiting": []}
    return llm.scheduler.snapshot()


@router.get("/loaded-models")
async def loaded(request: Request) -> dict:
    """What the model servers have loaded now, asked fresh enough (the probe's few
    seconds): {models: [{ref, loaded, load_seconds}]} (backends/router.py loaded_view).
    The `model.loaded` event says the same when it changes."""
    llm = request.app.state.services.llm
    if llm is None:
        return {"models": []}
    s = llm.router.s
    for pid, p in (s.providers.items() if s is not None else ()):
        if KINDS[p["kind"]]["loaded"] is not None and not s.key_missing(pid):
            await llm.router.probe(pid)
    return llm.router.loaded_view()
