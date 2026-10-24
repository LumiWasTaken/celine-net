"""events.seq is gapless per conversation under concurrency (a hole silently drops a
message from a transcript on a later reconnect), and a restart never re-runs a step
whose side effect may have landed."""
from __future__ import annotations

import json

import anyio
import pytest

from celine.conversations import store as conversations
from celine.core.ids import new_id, now
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


async def test_seq_is_gapless_under_load(store):
    cids = [await conversations.create(store, f"c{i}") for i in range(20)]

    async def hammer(cid: str) -> None:
        for i in range(100):
            await store.append_event(cid, "message.delta", {"i": i})

    async with anyio.create_task_group() as tg:
        for cid in cids:
            tg.start_soon(hammer, cid)
    for cid in cids:
        seqs = await store.read(lambda c: [r[0] for r in c.execute(
            "SELECT seq FROM events WHERE conversation_id=? ORDER BY seq", (cid,))])
        assert seqs == list(range(1, 101)), f"{cid} is not 1..n"


async def test_trigger_get_or_create_cannot_fork(store):
    ids: list[str] = []

    async def fire() -> None:
        ids.append(await conversations.for_trigger(store, "morning", "Morning"))

    async with anyio.create_task_group() as tg:
        for _ in range(10):
            tg.start_soon(fire)
    assert len(set(ids)) == 1
    created = await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT data FROM global_events WHERE type='conversation.created'")])
    assert [(e["id"], e["trigger_key"]) for e in created] == [(ids[0], "morning")]


async def test_orphans_resume_only_without_a_step_in_flight(store):
    cid = await conversations.create(store, "x")

    def make(c):
        clean, busy, mid = new_id(), new_id(), new_id()
        c.execute("INSERT INTO turns(id,conversation_id,state,created_at) "
                  "VALUES(?,?,'streaming',?), (?,?,'tools',?)",
                  (clean, cid, now(), busy, cid, now()))
        c.execute("INSERT INTO messages(id,conversation_id,role,turn_id,created_at) "
                  "VALUES(?,?,'assistant',?,?)", (mid, cid, busy, now()))
        c.execute("INSERT INTO tool_calls(id,message_id,name,arguments,state) "
                  "VALUES(?,?,'shell__run_command','{}','running')", (new_id(), mid))
        return clean, busy

    clean, busy = await store.write(make)
    out = await turns.sweep(store)
    assert out == {"total": 2, "resumable": 1, "interrupted": 1, "jobs": 0}
    state = lambda tid: store.read(lambda c: c.execute(  # noqa: E731
        "SELECT state, worker_id, reason FROM turns WHERE id=?", (tid,)).fetchone())
    row = await state(clean)
    assert row["state"] == "queued" and row["worker_id"] is None
    row = await state(busy)
    assert row["state"] == "interrupted" and "shell__run_command" in row["reason"]

async def test_reads_reuse_their_connections(store):
    for _ in range(3):
        await store.read(lambda c: c.execute("SELECT 1").fetchone())
    assert store._idle.qsize() == 1, "one read at a time needs one connection"
