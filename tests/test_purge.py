"""Delete permanently: a conversation in the Trash goes with its sub-agents and every row
that is part of them; a message in the Trash goes and what followed it stays."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from chat_fixtures import user_says

from celine import boot
from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.conversations import store as conversations, tree
from celine.core.bus import EventBus
from celine.intercom.store import insert as intercom_insert
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


class OneCall(FakeBackend):
    async def stream(self, messages, tools, body=None):
        self.calls.append(messages)
        if len(self.calls) == 1:
            yield ToolCallDelta(index=0, id="x", name="date_time__now", arguments="{}")
            yield Done(reason="tool_calls")
        else:
            yield TextDelta("It is late.")
            yield Done(reason="stop")


async def _everything_naming(store, ids: set[str]) -> list[tuple[str, str]]:
    """(table, column) of every row that holds one of `ids` as a value."""
    def fn(c):
        hits = []
        for t in conversations._tables(c):
            for col in conversations._columns(c, t):
                for v in ids:
                    if c.execute(f"SELECT 1 FROM {t} WHERE \"{col}\"=? LIMIT 1",
                                 (v,)).fetchone():
                        hits.append((t, col))
        kv = c.execute("SELECT k FROM kv").fetchall()
        hits += [("kv", r[0]) for r in kv if any(v in r[0] for v in ids)]
        return hits
    return await store.read(fn)


async def _app(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.state.services.dispatcher = SimpleNamespace(notify=lambda: None,
                                                    backend_factory=FakeBackend, llm=None)
    return cfg, app


async def test_a_trashed_chat_goes_for_good_with_its_sub_agent(tmp_path, monkeypatch, store):
    cfg, app = await _app(tmp_path, monkeypatch, store)
    reg = app.state.services.registry
    cid = await conversations.create(store, "Plans")
    sub = await conversations.create(store, "Helper", agent_of=cid, agent_name="helper")
    other = await conversations.create(store, "Elsewhere")
    await user_says(store, cid, "What time is it?")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, OneCall(), EventBus(), Token(turn["id"]), reg)
    await user_says(store, sub, "look it up")
    await store.write(lambda c: c.execute("UPDATE turns SET state='done'"))
    # a message from the chat landed in another chat: that message stays
    iid = await store.write(lambda c: intercom_insert(c, f"agent:{cid}", f"agent:{other}",
                                                      "message", "done"))
    await store.write(lambda c: tree.append(c, store, other, None, "user", "done",
                                            "intercom", intercom_id=iid))
    await store.write(lambda c: c.execute("INSERT INTO kv(k, v) VALUES(?, '1')",
                                          (f"{tree.KEEP_TIME}{turn['id']}",)))
    mids = set(await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT id FROM messages WHERE conversation_id IN (?, ?)", (cid, sub))]))
    assert mids
    async with client(app) as api:
        r = await api.delete(f"/api/conversations/{cid}?for_good=1")
        assert r.status_code == 409 and r.json()["error"]["message"] == "It is not in the Trash."
        assert (await api.delete(f"/api/conversations/{cid}")).status_code == 200
        assert (await api.delete(f"/api/conversations/{cid}?for_good=1")).status_code == 200
        assert (await api.delete(f"/api/conversations/{cid}?for_good=1")).status_code == 404
    assert await _everything_naming(store, {cid, sub, turn["id"], *mids}) == []
    kept = await store.read(lambda c: c.execute(
        "SELECT content, intercom_id FROM messages WHERE conversation_id=?",
        (other,)).fetchone())
    assert tuple(kept) == ("done", None)
    purged = await store.read(lambda c: [json.loads(r[0])["id"] for r in c.execute(
        "SELECT data FROM global_events WHERE type='conversation.purged'")])
    assert purged == [cid, sub]


async def test_a_deleted_message_goes_with_everything_after_it(tmp_path, monkeypatch,
                                                                store):
    cfg, app = await _app(tmp_path, monkeypatch, store)
    cid = await conversations.create(store)

    def chat(c):
        u1 = tree.append(c, store, cid, None, "user", "one", "user")["id"]
        a1 = tree.append(c, store, cid, u1, "assistant", "two", "model")["id"]
        u2 = tree.append(c, store, cid, a1, "user", "three", "user")["id"]
        a2 = tree.append(c, store, cid, u2, "assistant", "four", "model")["id"]
        return u1, a1, u2, a2
    u1, *after = await store.write(chat)
    async with client(app) as api:
        r = await api.delete(f"/api/conversations/{cid}/messages/{after[0]}")
        assert r.status_code == 200 and sorted(r.json()["message_ids"]) == sorted(after)
        assert r.json()["active_leaf_id"] == u1
        assert (await api.delete(f"/api/conversations/{cid}/messages/{after[0]}")
                ).status_code == 404
    path = await store.read(lambda c: [m["content"] for m in tree.path(c, cid)])
    assert path == ["one"]
    assert await _everything_naming(store, set(after)) == []
    ev = await store.read(lambda c: c.execute(
        "SELECT data FROM events WHERE conversation_id=? AND type='message.purged'",
        (cid,)).fetchone())
    assert sorted(json.loads(ev[0])["message_ids"]) == sorted(after)
