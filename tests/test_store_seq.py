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


async def test_orphans_end_interrupted_with_their_reason(store):
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
    assert out == {"total": 2, "interrupted": 2, "jobs": 0}
    state = lambda tid: store.read(lambda c: c.execute(  # noqa: E731
        "SELECT state, worker_id, reason FROM turns WHERE id=?", (tid,)).fetchone())
    row = await state(clean)
    assert row["state"] == "interrupted" and row["worker_id"] is None
    assert row["reason"] == "The server restarted during the answer."
    row = await state(busy)
    assert row["state"] == "interrupted" and "shell__run_command" in row["reason"]

async def test_reads_reuse_their_connections(store):
    for _ in range(3):
        await store.read(lambda c: c.execute("SELECT 1").fetchone())
    assert store._idle.qsize() == 1, "one read at a time needs one connection"


async def test_the_store_says_while_its_writer_runs():
    import anyio as _anyio
    import tempfile
    from pathlib import Path
    from celine.core.store import Store
    from celine.db.migrate import migrate_all
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "celine.db"
        migrate_all(path)
        s = Store(path)
        assert not s.alive
        async with _anyio.create_task_group() as tg:
            await s.start(tg)
            await s.write(lambda c: None)
            assert s.alive
            await s.aclose()
        assert not s.alive, "a writer that ended is no longer alive: the health check says 503"


async def test_a_slow_write_leaves_the_loop_free(store):
    """The writer runs on its own thread: while one write takes long, the loop still
    reads, and only the callers of later writes wait."""
    import threading
    gate = threading.Event()

    def slow(c):
        # the loop sets the gate: were this the loop's thread, it would wait in vain
        assert gate.wait(5), "the loop was blocked by the write"
        c.execute("INSERT INTO kv(k, v) VALUES('slow', '1')")

    async with anyio.create_task_group() as tg:
        tg.start_soon(store.write, slow)
        assert await store.kv_get("slow") is None
        gate.set()
    assert await store.kv_get("slow") == "1"


async def test_events_go_out_on_the_loop_after_their_commit(db_path):
    """The bus hears a write's events on the loop's thread, in commit order, each once
    its row can be read."""
    import threading
    from celine.core.bus import EventBus
    from celine.core.store import Store, connect
    bus = EventBus()
    look = connect(db_path)
    async with anyio.create_task_group() as tg:
        s = Store(db_path, bus=bus)
        await s.start(tg)
        cid = await conversations.create(s, "x")
        seen: list[tuple[int, int, int]] = []

        class Tap:
            def send_nowait(self, ev):
                landed = look.execute("SELECT count(*) FROM events WHERE "
                                      "conversation_id=? AND seq=?", (cid, ev["seq"]))
                seen.append((ev["seq"], threading.get_ident(), landed.fetchone()[0]))
        bus._subs[cid].add(Tap())
        for i in range(5):
            await s.append_event(cid, "message.delta", {"i": i})
        seqs = [x[0] for x in seen]
        assert len(seqs) == 5 and seqs == sorted(seqs)
        assert {x[1] for x in seen} == {threading.get_ident()}
        assert {x[2] for x in seen} == {1}, "published before its commit"
        await s.aclose()
    look.close()


async def test_write_soon_from_a_thread_lands(store):
    """A write queued from another thread (the archive's tasks, the model queue) lands
    before a write awaited after it."""
    await anyio.to_thread.run_sync(lambda: store.write_soon(lambda c: c.execute(
        "INSERT INTO kv(k, v) VALUES('soon', '1')")))
    await store.write(lambda c: None)
    assert await store.kv_get("soon") == "1"
