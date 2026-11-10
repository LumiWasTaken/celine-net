"""The LLM request queue, for the UI: what runs and what waits, per model."""
from __future__ import annotations

from fastapi import APIRouter, Request

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
    """What the model servers had loaded when the app last asked them (a model's first
    answer, a load, Check); nothing is asked here: {models: [{ref, loaded,
    loading, load_seconds}]} (backends/router.py loaded_view). `model.loaded` says the same when
    it changes."""
    llm = request.app.state.services.llm
    if llm is None:
        return {"models": []}
    return llm.router.loaded_view()
