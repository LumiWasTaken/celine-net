"""The one socket (api/ws.py): conversations and the global feed on one connection,
replay with merged deltas, the global feed durable with a resync below what is kept, a
reader the bus drops caught up from the log, progress thinned out live, and
GET /api/changes for a client coming back."""
from __future__ import annotations

import contextlib

import anyio
from fastapi import FastAPI
from starlette.testclient import TestClient

from celine.api import ws as ws_mod
from celine.core.services import Services
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.core.store import Store

HEADERS = {"origin": "http://127.0.0.1:8766", "host": "127.0.0.1:8766"}


def _app(db_path) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with anyio.create_task_group() as tg:
            app.state.services.bus = EventBus()
            app.state.services.store = Store(db_path, bus=app.state.services.bus)
            app.state.services.cfg = load()
            await app.state.services.store.start(tg)
            yield
            await app.state.services.store.aclose()

    app = FastAPI(lifespan=lifespan)
    app.state.services = Services()
    app.include_router(ws_mod.router)
    app.state.allowed_origins = {HEADERS["origin"]}
    return app


def _until(sock, pred) -> list[dict]:
    """Frames up to and with the first one `pred` takes."""
    got = []
    while True:
        got.append(sock.receive_json())
        if pred(got[-1]):
            return got


def test_one_socket_follows_conversations_and_the_global_feed(db_path):
    app = _app(db_path)
    with TestClient(app) as client:
        store = app.state.services.store
        a = client.portal.call(conversations.create, store, "a")
        b = client.portal.call(conversations.create, store, "b")
        with client.websocket_connect("/ws/events", headers=HEADERS) as sock:
            sock.send_json({"type": "global"})
            head = sock.receive_json()
            assert head["type"] == "global" and head["gid"] >= 2, "a and b were created"
            for cid in (a, b):
                sock.send_json({"type": "subscribe", "conversation": cid, "after": 0})
                snap = sock.receive_json()
                assert snap["type"] == "snapshot" and snap["conversation"]["id"] == cid
            client.portal.call(store.append_event, b, "message.delta",
                               {"message_id": "m", "text": "hi"})
            client.portal.call(store.append_event, a, "turn.state",
                               {"turn_id": "t", "state": "queued"})
            got = _until(sock, lambda f: f.get("conversation") == a and "gid" in f)
            assert [(f["type"], f["conversation"], "gid" in f) for f in got] == [
                ("message.delta", b, False), ("turn.state", a, False),
                ("turn.state", a, True)], "a's turn on its stream and on the feed"
            sock.send_json({"type": "unsubscribe", "conversation": b})
            sock.send_json({"type": "ping"})
            assert sock.receive_json() == {"type": "pong"}
            client.portal.call(store.append_event, b, "message.delta",
                               {"message_id": "m", "text": "x"})
            client.portal.call(store.emit_global, {"type": "terminal.state"})
            assert _until(sock, lambda f: f["type"] == "terminal.state")[0]["type"] == \
                "terminal.state", "b is no longer followed"


def test_replay_merges_deltas_and_a_dropped_reader_catches_up_from_the_log(db_path):
    app = _app(db_path)
    with TestClient(app) as client:
        store = app.state.services.store
        cid = client.portal.call(conversations.create, store, "c")
        put = lambda t, d: client.portal.call(store.append_event, cid, t, d)  # noqa: E731
        first = put("turn.state", {"turn_id": "t", "state": "streaming"})
        for w in ("The ", "red ", "car"):
            put("message.delta", {"message_id": "m", "text": w})
        put("message.reasoning", {"message_id": "m", "text": "hm"})
        last = put("message.delta", {"message_id": "m", "text": "."})
        with client.websocket_connect("/ws/events", headers=HEADERS) as sock:
            sock.send_json({"type": "subscribe", "conversation": cid, "after": first})
            got = _until(sock, lambda f: f["seq"] == last)
            assert [(f["type"], f["text"]) for f in got] == [
                ("message.delta", "The red car"), ("message.reasoning", "hm"),
                ("message.delta", ".")]
            # the bus drops a slow reader: the socket catches it up from the log
            (sub,) = app.state.services.bus._subs[cid]
            client.portal.call(sub.aclose)
            again = put("message.done", {"message_id": "m", "text": "The red car."})
            frame = sock.receive_json()
            assert (frame["type"], frame["seq"]) == ("message.done", again)


def test_progress_is_thinned_out_live_and_kept_in_the_log(db_path):
    app = _app(db_path)
    with TestClient(app) as client:
        store = app.state.services.store
        cid = client.portal.call(conversations.create, store, "c")
        with client.websocket_connect("/ws/events", headers=HEADERS) as sock:
            sock.send_json({"type": "subscribe", "conversation": cid, "after": 0})
            sock.receive_json()
            for n in range(5):
                client.portal.call(store.append_event, cid, "tool.progress",
                                   {"tool_call_id": "k", "note": str(n)})
            end = client.portal.call(store.append_event, cid, "turn.state",
                                     {"turn_id": "t", "state": "done"})
            got = _until(sock, lambda f: f["seq"] == end)
            assert [f.get("note") for f in got] == ["0", None]
    stored = anyio.run(lambda: _count(db_path, cid))
    assert stored == 5


async def _count(db_path, cid):
    async with anyio.create_task_group() as tg:
        s = Store(db_path)
        await s.start(tg)
        n = await s.read(lambda c: c.execute(
            "SELECT count(*) FROM events WHERE conversation_id=? AND type='tool.progress'",
            (cid,)).fetchone()[0])
        await s.aclose()
    return n


def test_the_global_feed_replays_after_an_id_and_resyncs_below_what_is_kept(db_path,
                                                                         monkeypatch):
    monkeypatch.setattr(conversations, "GLOBAL_KEEP", 3)
    app = _app(db_path)
    with TestClient(app) as client:
        store = app.state.services.store
        for n in range(6):
            client.portal.call(store.emit_global, {"type": "kb.changed", "kb": str(n)})
        client.portal.call(store.write, lambda c: None)
        with client.websocket_connect("/ws/events", headers=HEADERS) as sock:
            sock.send_json({"type": "global", "after": 4})
            assert [f["kb"] for f in _until(sock, lambda f: f["gid"] == 6)] == ["4", "5"]
        client.portal.call(conversations.prune_events, store, 500)
        with client.websocket_connect("/ws/events", headers=HEADERS) as sock:
            sock.send_json({"type": "global", "after": 1})
            assert sock.receive_json() == {"type": "resync", "feed": "global"}
            sock.send_json({"type": "global", "after": 3})        # 4 is kept: no gap
            assert sock.receive_json()["gid"] == 4


def test_changes_says_what_moved_while_away(db_path):
    app = _app(db_path)
    with TestClient(app) as client:
        store = app.state.services.store
        a = client.portal.call(conversations.create, store, "a")
        b = client.portal.call(conversations.create, store, "b")
        since = client.portal.call(store.read, lambda c: c.execute(
            "SELECT max(id) FROM global_events").fetchone()[0])
        client.portal.call(store.append_event, a, "conversation.title", {"title": "A"})
        client.portal.call(conversations.delete, store, b)
        out = client.get("/api/changes", params={
            "since": since, "conversations": f"{a}:0,{b}:0"}).json()
        assert [(r["id"], r["title"]) for r in out["conversations"]] == [(a, "a")]
        assert out["gone"] == [b] and out["global"] > since and not out["resync"]
        assert out["unread"] == 0 and out["jobs"] == [] and out["agents"] == []


def test_changes_names_an_archive_and_a_job_after_a_pruned_since(db_path):
    """An archive moves no seq; the feed says it. `since` just below the oldest kept row
    is no gap: no resync, and the jobs that finished after it come back."""
    app = _app(db_path)
    with TestClient(app) as client:
        store = app.state.services.store
        a = client.portal.call(conversations.create, store, "a")

        async def make_sub():
            return await conversations.create(store, "s", agent_of=a, agent_name="s")
        sub = client.portal.call(make_sub)
        since = client.portal.call(store.read, lambda c: c.execute(
            "SELECT max(id) FROM global_events").fetchone()[0])
        assert [r["id"] for r in client.get("/api/changes").json()["agents"]] == [sub]

        def away(c):
            c.execute("INSERT INTO jobs(id, conversation_id, tool_name, state, "
                      "created_at, finished_at) VALUES('j1', ?, 'x', 'done', 'z', 'z')",
                      (a,))
            store.event(c, a, "job.state", {"job_id": "j1", "state": "done"})
            c.execute("DELETE FROM global_events WHERE id <= ?", (since,))
        client.portal.call(store.write, away)
        client.portal.call(conversations.archive, store, a)
        seq = client.portal.call(store.read, lambda c: c.execute(
            "SELECT next_seq - 1 FROM conversations WHERE id=?", (sub,)).fetchone()[0])
        out = client.get("/api/changes", params={
            "since": since, "conversations": f"{sub}:{seq}"}).json()
        assert not out["resync"] and [j["id"] for j in out["jobs"]] == ["j1"]
        got = {r["id"]: r["archived_at"] for r in out["conversations"]}
        assert set(got) == {a, sub} and all(got.values())
        assert out["agents"] == [], "an archived sub-agent is history, not a change"
        assert client.get("/api/changes", params={"since": since - 1}).json()["resync"]
