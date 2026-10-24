"""Slash commands (commands/, api/commands.py): /compact end to end, never during a live turn."""
from __future__ import annotations

from chat_fixtures import user_says
from compaction_fixtures import Model, arch_registry, archive_text, seed_exchange

from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest

from celine.conversations import store as conversations
from celine.core.services import Services
from celine.api import commands
from celine.boot import load
from celine.core.store import Store, attach


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from settings_fixtures import scratch
    scratch(tmp_path, monkeypatch, providers={"main": {
        "kind": "llama-swap", "label": "llama-swap", "base_url": "http://127.0.0.1:11434/v1",
        "models": {"my-model": {"caps": ["chat", "tools"]}}}},
        roles={"chat": {"chain": ["main/my-model"]}})
    c = load()
    return c


def _seed(db_path, exchanges, live=False):
    async def run():
        async with anyio.create_task_group() as tg:
            store = Store(db_path)
            await store.start(tg)
            cid = await conversations.create(store)
            for i in range(exchanges):
                await seed_exchange(store, cid, f"question {i}",
                                    [("arch__find", archive_text(40, 1000 * (i + 1)))])
            if live:
                await user_says(store, cid, "still running")
            await store.aclose()
            tg.cancel_scope.cancel()
        return cid
    return anyio.run(run)


def test_compact_now_and_busy(db_path, cfg):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from celine.errors import install_error_handler
    from celine.conversations import api as conversations_api
    cid = _seed(db_path, 3)
    busy = _seed(db_path, 1, live=True)

    @asynccontextmanager
    async def lifespan(app):
        async with anyio.create_task_group() as tg:
            store = Store(db_path)
            await store.start(tg)
            app.state.services.store, app.state.services.cfg, app.state.services.registry = store, cfg, arch_registry()
            app.state.services.dispatcher = SimpleNamespace(
                notify=lambda: None, backend_factory=lambda: Model(window=65_536),
        bound=lambda cid, **_: Model(window=65_536),
                llm=None, _tg=None)
            yield
            await store.aclose()
            tg.cancel_scope.cancel()

    app = FastAPI(lifespan=lifespan)
    app.state.services = Services()
    install_error_handler(app)
    app.include_router(conversations_api.router)
    app.include_router(commands.router)
    with TestClient(app) as c:
        r = c.post(f"/api/conversations/{cid}/slash-commands/compact/run?wait=true",
                   json={"text": "the beach trip"})
        assert r.status_code == 200 and r.json()["state"] == "done", r.text
        snap = c.get(f"/api/conversations/{cid}").json()
        assert [(x["trigger"], x["focus"]) for x in snap["compactions"]] == [
            ("manual", "the beach trip")]
        r = c.post(f"/api/conversations/{busy}/slash-commands/compact/run", json={})
        assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
        # /usage carries the last request as it went out, as it reads it; nothing
        # before one was kept
        usage = lambda: c.post(f"/api/conversations/{cid}/slash-commands/usage/run", json={}).json()
        assert usage()["request"] is None
        from celine.turns.prompt import keep_request
        anyio.run(keep_request, cfg, cid, ["persona"], [
            {"role": "system", "content": "persona\n\nEarlier in this conversation"},
            {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url"}]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x", "function": {"name": "arch__find", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "x", "content": "found"}],
            [{"type": "function", "function": {"name": "arch__find"}}])
        assert usage()["request"] == [
            {"role": "system", "name": None, "text": "persona"},
            {"role": "system", "name": None, "text": "Earlier in this conversation"},
            {"role": "tools", "name": None, "text": "arch__find"},
            {"role": "user", "name": None, "text": "look\n[image]"},
            {"role": "assistant", "name": None, "text": "arch__find({})"},
            {"role": "tool", "name": "arch__find", "text": "found"}]


def test_what_she_sees_is_measured_without_a_model_call(db_path, cfg, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from celine.errors import install_error_handler
    from celine.conversations import api as conversations_api
    import sqlite3
    cid = _seed(db_path, 2)
    nb = cfg.her.notebook
    (nb / "voice").mkdir(parents=True, exist_ok=True)
    (nb / "voice" / "style.md").write_text("load: always\n\nshort lines")
    (nb / "voice" / "old.md").write_text("load: off\n\nold voice")
    (nb / "voice" / "draft.md").write_text("by need")
    with sqlite3.connect(db_path) as db:
        attach(db, db_path)
        db.executemany("INSERT INTO notebook_index(path, summary, bytes, "
                       "touched_at, created_at) VALUES(?, 's', 1, 't', 't')",
                       [("voice/style.md",), ("voice/old.md",), ("voice/draft.md",)])
        db.execute("UPDATE conversations SET notes_off='[\"voice/draft.md\"]'")
        # a skill it loaded by need (the tests' skills are core otherwise)
        db.execute("INSERT INTO context_loaded(conversation_id, kind, name, touched) "
                   "VALUES(?, 'skill', 'arch', 0)", (cid,))
    from celine.turns import loads
    monkeypatch.setattr(loads, "CORE", loads.CORE - {"arch"})
    model = Model(window=65_536)

    @asynccontextmanager
    async def lifespan(app):
        async with anyio.create_task_group() as tg:
            store = Store(db_path)
            await store.start(tg)
            app.state.services.store, app.state.services.cfg, app.state.services.registry = store, cfg, arch_registry()
            app.state.services.dispatcher = SimpleNamespace(
                notify=lambda: None, backend_factory=lambda: model,
        bound=lambda cid, **_: model, llm=None, _tg=None)
            yield
            await store.aclose()
            tg.cancel_scope.cancel()

    app = FastAPI(lifespan=lifespan)
    app.state.services = Services()
    install_error_handler(app)
    app.include_router(conversations_api.router)
    with TestClient(app) as c:
        got = c.get(f"/api/conversations/{cid}/context").json()
        assert c.get("/api/conversations/nope/context").status_code == 404
        assert c.delete(f"/api/conversations/{cid}/context/skills/arch").json() == {"ok": True}
        after = c.get(f"/api/conversations/{cid}/context").json()
        assert c.delete(f"/api/conversations/{cid}/context/skills/notebook").status_code == 400
        assert c.delete("/api/conversations/nope/context/skills/arch").status_code == 404
    assert got["window"] == 65_536 and got["tools"] >= 1
    blocks = {b["name"]: b for b in got["blocks"]}
    assert list(blocks) == ["persona", "talk", "about", "notes", "tools", "history", "warning"]
    assert "bytes" not in blocks["persona"] and got["shares"] == {
        "notes": 6553, "tools": 10_000}
    assert blocks["persona"]["tokens"] > 0 and blocks["history"]["tokens"] > 0
    assert blocks["tools"]["tokens"] > 0 and blocks["warning"]["tokens"] == 0
    assert {n["path"]: (n["state"], n["why"]) for n in got["notes"]} == {
        "voice/draft.md": ("off", "left out in this conversation"),
        "voice/style.md": ("included", "")}
    assert got["loaded"]["notes"] == [{"path": "voice/style.md", "since": 0}]
    [arch] = [k for k in got["loaded"]["skills"] if k["name"] == "arch"]
    assert arch["since"] == 0 and arch["tokens"] > 0 and arch["core"] is False
    assert "arch" not in [k["name"] for k in after["loaded"]["skills"]], "unloaded here"

    assert model.calls == [], "no model call"
