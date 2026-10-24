"""Live fan-out. The durable copies are the events table (a conversation's) and
global_events (the global feed, written by core/store.py); this is only the tail."""
from __future__ import annotations

from collections import defaultdict

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from .states import TERMINAL
from .event_types import (
    ARTIFACT_UPDATED,
    CONVERSATION_TITLE,
    IMAGES_BATCH,
    JOB_STATE,
    LLM_QUEUE,
    MESSAGE_PURGED,
    MUSIC_BATCH,
    TOOL_DETACHED,
    TOOL_DONE,
    TOOL_GATED,
    TOOL_STARTED,
    TURN_STATE,
    VERSION_CHANGED,
)


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[MemoryObjectSendStream[dict]]] = defaultdict(set)

    def subscribe(self, topic: str, buffer: int = 512
                  ) -> tuple[MemoryObjectSendStream[dict],
                             MemoryObjectReceiveStream[dict]]:
        tx, rx = anyio.create_memory_object_stream[dict](buffer)
        self._subs[topic].add(tx)
        return tx, rx

    def unsubscribe(self, topic: str, tx: MemoryObjectSendStream[dict]) -> None:
        self._subs[topic].discard(tx)

    def publish(self, topic: str, event: dict) -> None:
        """Never awaits. A slow reader is dropped rather than blocking the writer: its
        stream is closed, so it ends and the reader reconnects and replays from the log."""
        for tx in list(self._subs.get(topic, ())):
            try:
                tx.send_nowait(event)
            except (anyio.WouldBlock, anyio.BrokenResourceError, anyio.ClosedResourceError):
                self._subs[topic].discard(tx)
                tx.close()


# The conversation events (Store.event) that also go on the global feed, because pages
# other than the conversation need them: turn and job states, a title, the model queue,
# an artifact, a version switched or a message deleted (a row's last line
# changes without a turn), image and music batches. A conversation's own stream (token
# deltas and the like) stays off it. Global-only events (Store.global_event,
# Store.emit_global) take any type.
MIRRORED = frozenset({
    TURN_STATE, JOB_STATE, CONVERSATION_TITLE, LLM_QUEUE, ARTIFACT_UPDATED,
    VERSION_CHANGED, MESSAGE_PURGED, IMAGES_BATCH, MUSIC_BATCH,
})
# and its shell and browser calls, so the terminal pages refetch their commands and the
# browser page its busy tabs as they happen
TOOL_EVENTS = frozenset({TOOL_STARTED, TOOL_DONE, TOOL_DETACHED})


def for_pages(ev: dict) -> bool:
    t = ev.get("type")
    return t in MIRRORED or (
        t in TOOL_EVENTS and str(ev.get("name", "")).startswith(("shell__", "browser__")))


# The conversation events that mean a row of Events (server/celine/events.py) landed or
# settled: a turn or a job ended, a tool call finished. Store.event sends `events.added`
# for each in the same write; the other sources call events.added themselves.
NOTEBOOK_TOOLS = ("notebook__write", "notebook__edit", "notebook__delete", "notebook__move")


def lands(ev: dict) -> str | None:
    """The kind of event `ev` adds to Events, or None."""
    t = ev.get("type")
    if t == TURN_STATE:
        return "turn" if ev.get("state") in TERMINAL else None
    if t == JOB_STATE:
        return "job" if ev.get("state") != "running" else None
    if t in (TOOL_DONE, TOOL_GATED):
        return "notebook" if ev.get("name") in NOTEBOOK_TOOLS else "tool"
    return None
