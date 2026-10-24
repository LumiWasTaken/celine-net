"""One socket for everything a page follows, `WS /ws/events`: any number of conversations
(each its event log, conversations/view.py) and the global feed (global_events), and
`GET /api/changes`, what changed while a client was away.

The client says what it follows, at any time:
  {"type": "subscribe", "conversation": id, "after": seq}   0: a snapshot first
  {"type": "unsubscribe", "conversation": id}
  {"type": "global", "after": gid}                           no after: from now
  {"type": "ping"}                                           answered {"type": "pong"}
Every frame of a conversation carries `conversation` (its id; a snapshot carries the
row there, with `id`), every frame of the global feed its `gid`. A cursor below what is
kept gets {"type": "resync", "conversation": id} or {"type": "resync", "feed":
"global"} and that subscription ends: the client takes a snapshot (after 0) or refetches
and follows the feed from now. A reader the bus drops as too slow is caught up from the
log here, so seq and gid only ever grow; they may skip (deltas merged on replay,
progress lines thinned out live), never go back.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

import anyio
from fastapi import APIRouter, Request, WebSocket

from ..conversations import changes as changes_mod, view
from .deps import GONE
from .origin import ws_origin_ok
from ..core.event_types import MESSAGE_DELTA, MESSAGE_REASONING, TOOL_PROGRESS

log = logging.getLogger("celine.ws")
router = APIRouter()

PROGRESS_EVERY = 0.25           # seconds between two tool.progress frames of one call, live
MERGED = (MESSAGE_DELTA, MESSAGE_REASONING)    # merged on replay, per message


# a live view's other end went away: the page (Starlette) or the box's socket (anyio)
@router.websocket("/ws/events")
async def socket(ws: WebSocket) -> None:
    if not ws_origin_ok(ws.headers, ws.app.state.allowed_origins):
        await ws.close(code=1008)
        return
    await ws.accept()
    lock = anyio.Lock()

    async def send(frame: dict) -> None:
        async with lock:
            await ws.send_json(frame)

    follows: dict[str | None, anyio.CancelScope] = {}    # None: the global feed

    async with anyio.create_task_group() as tg:
        def follow(key: str | None, fn, *args) -> None:
            if key in follows:
                follows.pop(key).cancel()
            scope = follows[key] = anyio.CancelScope()

            async def run() -> None:
                with scope:
                    try:
                        await fn(ws.app.state.services, send, *args)
                    except GONE:                 # the client left mid-send
                        log.debug("socket closed while sending", exc_info=True)
                        tg.cancel_scope.cancel()
                    except Exception:            # a bug: seen, and the page reconnects
                        log.exception("socket feed failed", extra={"event": "ws.feed_failed"})
                        tg.cancel_scope.cancel()
                if follows.get(key) is scope:
                    del follows[key]
            tg.start_soon(run)

        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            try:
                data = json.loads(msg.get("text") or "")
            except ValueError:
                data = None
            if not isinstance(data, dict):       # a client's bug, visible on its side
                await send({"type": "error", "code": "bad_frame",
                            "message": "A frame is one JSON object."})
                continue
            kind, cid = data.get("type"), data.get("conversation")
            if kind == "ping":
                await send({"type": "pong"})
            elif kind == "subscribe" and isinstance(cid, str):
                follow(cid, _conversation, cid, _int(data.get("after")) or 0)
            elif kind == "unsubscribe" and isinstance(cid, str) and cid in follows:
                follows.pop(cid).cancel()
            elif kind == "global":
                follow(None, _global, _int(data.get("after")))
        tg.cancel_scope.cancel()
    try:
        await ws.close()
    except GONE:                                 # the client left first
        pass


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


async def _conversation(state, send, cid: str, after: int) -> None:
    """Snapshot or replay, then the live tail, for as long as it is followed. Subscribe
    before reading: the other order loses events, this one only duplicates."""
    store, bus = state.store, state.bus
    last = after
    while True:
        tx, rx = bus.subscribe(cid)
        try:
            try:
                got = await (view.snapshot(store, cid) if last == 0
                             else view.resume(store, cid, last))
            except KeyError:                     # no such conversation (any more)
                await send({"type": "error", "conversation": cid, "code": "not_found"})
                return
            if last == 0:
                await send({"type": "snapshot", **got})
            elif "resync" in got:
                await send({"type": "resync", "conversation": cid, "reason": got["resync"]})
                return
            else:
                for frame in replay(got["events"], cid):
                    await send(frame)
            last = got["seq"]
            last = await _tail(rx, send, cid, last)     # ends when the bus dropped us
        finally:
            bus.unsubscribe(cid, tx)
            await tx.aclose()


def replay(events: list[dict], cid: str) -> list[dict]:
    """Stored events as frames, each as it went out live (store.event) with its
    conversation; consecutive deltas (and reasoning) of one message as one frame."""
    out: list[dict] = []
    for ev in events:
        frame = {"seq": ev["seq"], "type": ev["type"], "ts": ev["ts"], **ev["data"],
                 "conversation": cid}
        prev = out[-1] if out else None
        if prev and frame["type"] in MERGED and prev["type"] == frame["type"] and \
                prev.get("message_id") == frame.get("message_id"):
            prev.update(seq=frame["seq"], ts=frame["ts"], text=prev["text"] + frame["text"])
        else:
            out.append(frame)
    return out


async def _tail(rx, send, cid: str, last: int) -> int:
    """Live events above `last`; a call's progress at most every PROGRESS_EVERY
    seconds (the log keeps them all). -> the last seq seen, once the bus dropped us."""
    shown: dict[str, float] = {}
    async with rx:
        async for ev in rx:
            if ev.get("seq", 0) <= last:
                continue
            last = ev["seq"]
            if ev["type"] == TOOL_PROGRESS:
                now, call = time.monotonic(), str(ev.get("tool_call_id"))
                if now - shown.get(call, -PROGRESS_EVERY) < PROGRESS_EVERY:
                    continue
                shown[call] = now
            await send({**ev, "conversation": cid})
    return last


async def _global(state, send, after: int | None) -> None:
    """The global feed: from now ({"type": "global", "gid": newest} first) or replayed
    after `after`, then live."""
    store, bus = state.store, state.bus
    while True:
        tx, rx = bus.subscribe("global")
        try:
            def read(c: sqlite3.Connection) -> dict:
                lo, hi = changes_mod.bounds(c)
                if after is None:
                    return {"newest": hi, "rows": []}
                if changes_mod.behind(lo, after):
                    return {"resync": True}
                return {"newest": hi, "rows": c.execute(
                    "SELECT id, type, data FROM global_events WHERE id > ? ORDER BY id",
                    (after,)).fetchall()}

            got = await store.read(read)
            if "resync" in got:
                await send({"type": "resync", "feed": "global"})
                return
            if after is None:
                await send({"type": "global", "gid": got["newest"]})
            for r in got["rows"]:
                await send({"gid": r["id"], "type": r["type"], **json.loads(r["data"])})
            after = got["newest"]
            async with rx:
                async for ev in rx:
                    if ev["gid"] <= after:
                        continue
                    after = ev["gid"]
                    await send(ev)
        finally:
            bus.unsubscribe("global", tx)
            await tx.aclose()


@router.get("/api/changes")
async def changes(request: Request, since: int | None = None,
                  conversations: str = "") -> dict:
    """One call for a client coming back: `since` its last global id, `conversations`
    "id:seq,id:seq" the ones it shows. -> {global (the newest id: follow from there),
    resync (since is below what the feed keeps, as on the socket: refetch everything),
    conversations (those whose seq moved, and those the feed names after `since`, such as
    an archive: id, seq, title, archived_at, active_state), gone (ids deleted or that no
    longer exist), jobs (finished since: id, conversation_id, tool, state, finished_at), unread
    (intercom messages waiting for the user), agents (every sub-agent not in the archive,
    as GET /sub-agents)}."""
    known: dict[str, int] = {}
    for part in conversations.split(","):
        cid, _, seq = part.strip().partition(":")
        if cid:
            known[cid] = int(seq) if seq.isdigit() else 0
    return await request.app.state.services.store.read(
        lambda c: changes_mod.catch_up(c, since, known))
