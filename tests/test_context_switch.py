"""The automatic context switch (api/context.py)."""
from __future__ import annotations

import json

import pytest

from settings_fixtures import client, make_app, scratch

from celine import boot
from celine.api import context as context_api
from celine.conversations import store as conversations

pytestmark = pytest.mark.anyio


async def _events(store, kind: str) -> list[dict]:
    return await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT data FROM global_events WHERE type=? ORDER BY id", (kind,))])


async def test_the_switch_holds_for_her_sub_agents(
        tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(context_api.router)
    mission = await conversations.create(store, "Learn", kind="mission")
    sub = await conversations.create(store, "scout", agent_of=mission, agent_name="scout")
    async with client(app) as c:
        assert (await c.get(f"/api/conversations/{mission}/auto-context")).json() == \
            {"on": True}
        r = await c.put(f"/api/conversations/{mission}/auto-context", json={"on": False})
        assert r.json() == {"on": False}
        assert (await c.get(f"/api/conversations/{sub}/auto-context")).json() == \
            {"on": False}
        assert await _events(store, "conversation.auto_context") == [
            {"id": mission, "on": False}, {"id": sub, "on": False}]
        assert (await c.put("/api/conversations/nope/auto-context",
                            json={"on": True})).status_code == 404


async def test_a_reply_shows_her_own_block_for_the_message_it_answers(
        tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    app = make_app(boot.load(), store)
    app.include_router(context_api.router)
    chat = await conversations.create(store)
    report = {"mind": {"mood": "mood: soft, slight.", "gap": None}}

    def seed(c):
        for mid, parent, role in (("u1", None, "user"), ("a1", "u1", "assistant"),
                                  ("t1", "a1", "tool"), ("a2", "t1", "assistant")):
            c.execute("INSERT INTO messages(id, conversation_id, parent_id, role, content, "
                      "created_at) VALUES (?,?,?,?,'',datetime('now'))",
                      (mid, chat, parent, role))
        c.execute("INSERT INTO message_context(message_id, text, report) VALUES "
                  "('u1', 'mood: soft, slight.', ?)", (json.dumps(report),))
    await store.write(seed)
    async with client(app) as c:
        for mid in ("a1", "a2"):
            r = await c.get(f"/api/conversations/{chat}/messages/{mid}/context")
            assert r.json() == {"text": "mood: soft, slight.", "report": report}
        assert (await c.get(f"/api/conversations/{chat}/messages/nope/context")
                ).status_code == 404
