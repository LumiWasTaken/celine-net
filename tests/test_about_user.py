"""About you (General settings): what the user wrote about themselves, placeholders
filled, in the system prompt of every turn; empty adds nothing. And the Context tab's
Load automatically switch: GET .../context carries auto_context, PUT .../auto-context
turns it."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from celine import boot
from celine.api import context as context_api
from celine.conversations import store as conversations
from celine.turns import loads
from celine.turns.prompt import system_blocks
from compaction_fixtures import Model
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


async def test_about_you_reaches_the_prompt_and_empty_adds_nothing(tmp_path, monkeypatch,
                                                                   store):
    scratch(tmp_path, monkeypatch, general={"user_name": "Joe"})
    cfg = boot.load()
    cid = await conversations.create(store)
    app = make_app(cfg, store)
    app.state.services.dispatcher = SimpleNamespace(
        notify=lambda: None, resize=lambda: None, backend_factory=lambda: Model(window=65_536),
        bound=lambda cid, **_: Model(window=65_536), llm=None, _tg=None)
    async with client(app) as api:
        r = await api.put("/api/settings", json={"general": {
            "user_name": "Joe", "user_bio": "{{user}} likes short answers."}})
        assert r.status_code == 200 and r.json()["general"]["user_bio"].startswith("{{user}}")
        cfg = app.state.services.cfg
        carried = await loads.select(store, cfg, None, cid, None)
        got = dict(await system_blocks(cfg, None, store, cid, "", carried))
        assert got["about"] == "About Joe:\nJoe likes short answers."
        ctx = (await api.get(f"/api/conversations/{cid}/context")).json()
        assert {b["name"]: b["tokens"] for b in ctx["blocks"]}["about"] > 0

        r = await api.put("/api/settings", json={"general": {
            "user_name": "Joe", "user_bio": ""}})
        assert r.status_code == 200
        got = dict(await system_blocks(cfg, None, store, cid, "", carried))
        assert "about" not in got and "About Joe" not in str(got)
        ctx = (await api.get(f"/api/conversations/{cid}/context")).json()
        assert {b["name"]: b["tokens"] for b in ctx["blocks"]}["about"] == 0


async def test_the_context_payload_carries_the_switch(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cid = await conversations.create(store)
    app = make_app(boot.load(), store)
    app.include_router(context_api.router)
    app.state.services.dispatcher = SimpleNamespace(
        notify=lambda: None, backend_factory=lambda: Model(window=65_536),
        bound=lambda cid, **_: Model(window=65_536), llm=None, _tg=None)
    async with client(app) as api:
        assert (await api.get(f"/api/conversations/{cid}/context")).json()["auto_context"] is True
        r = await api.put(f"/api/conversations/{cid}/auto-context", json={"on": False})
        assert r.json() == {"on": False}
        assert (await api.get(f"/api/conversations/{cid}/context")).json()["auto_context"] is False
