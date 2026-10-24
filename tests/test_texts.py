"""History and Ask Celine of every long text (server/celine/texts.py, api/texts.py)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from celine import texts
from celine.api import texts as texts_api
from celine.backends.base import BackendError, Done, TextDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.core import ids
from celine.core.services import Services
from celine.errors import install_error_handler


pytestmark = pytest.mark.anyio
T0 = "2026-10-16T20:00:00.000+00:00"


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(ids, "now", lambda: T0)


@pytest.fixture
async def app(store, clock):
    app = FastAPI()
    app.state.services = s = Services()
    install_error_handler(app)
    app.include_router(texts_api.router)
    s.store, s.cfg = store, load()
    s.events = []
    store.emit_global = s.events.append
    return app


@pytest.fixture
async def c(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        yield client


async def ok(r: httpx.Response) -> dict:
    assert r.status_code == 200, r.text
    return r.json()


async def write(store, of: str, text: str, by: str = "user") -> None:
    await store.write(lambda c: texts.record(c, of, text, by))


async def test_record_keeps_each_version_with_a_one_line_change_and_skips_the_same_text(store, clock):
    await write(store, "lore:1", "Miso is a cat. She sleeps a lot.")
    await write(store, "lore:1", "Miso is a cat. She sleeps a lot.")
    await write(store, "lore:1", "Miso is a cat. She hates the rain.", "celine")
    await write(store, "lore:1", "Miso is a cat. She hates the rain. She steals butter. "
                                 "She purrs.")
    await write(store, "lore:1", "Miso is a cat.")
    rows = await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT change, by, size, created_at FROM text_versions WHERE of='lore:1' ORDER BY id")])
    assert [r["change"] for r in rows] == [
        'First version: "Miso is a cat."',
        'Changed the wording of "She hates the rain."',
        'Added 2 sentences: "She steals butter."',
        'Removed 3 sentences: "She hates the rain."']
    assert rows[1]["by"] == "celine" and rows[0]["size"] == 32 and rows[0]["created_at"] == T0


def test_a_change_line_quotes_80_characters_with_no_dash():
    line = texts.change("A.", "A. " + "Word \u2014 word " * 20 + ".")
    assert line.startswith('Added: "Word, word') and line.endswith('\u2026"')
    assert "\u2014" not in line and len(line.split(": ", 1)[1]) == 82


async def test_history_pages_400_versions_newest_first(c, app):
    store = app.state.services.store
    await store.write(lambda conn: [texts.record(conn, "note:4", f"Version {n}.", "user")
                                    for n in range(1, 401)])
    await write(store, "note:5", "Another text.")
    seen, before = [], None
    while True:
        page = await ok(await c.get("/api/text-versions",
                                    params={"of": "note:4", **({"before": before} if before else {})}))
        assert page["total"] == 400 and len(page["versions"]) <= 50
        seen += page["versions"]
        if (before := page["next"]) is None:
            break
    assert len(seen) == 400 and [v["id"] for v in seen] == sorted((v["id"] for v in seen), reverse=True)
    newest = seen[0]
    assert newest["change"] == 'Changed the wording of "Version 400."' and newest["at"] == T0
    assert newest["by"] == app.state.services.cfg.user
    full = await ok(await c.get(f"/api/text-versions/{seen[-1]['id']}"))
    assert full["text"] == "Version 1." and full["change"] == 'First version: "Version 1."'
    assert (await c.get("/api/text-versions/999999")).status_code == 404


async def test_history_search_finds_the_change_or_who_wrote_it(c, app):
    store, cfg = app.state.services.store, app.state.services.cfg
    await write(store, "lore:k", "Miso is a cat.")
    await write(store, "lore:k", "Miso is a grey cat.", "her")
    await write(store, "lore:k", "Miso is a grey cat. She hates rain.")
    hits = await ok(await c.get("/api/text-versions", params={"of": "lore:k", "q": "GREY"}))
    assert [v["change"] for v in hits["versions"]] == ['Changed the wording of "Miso is a grey cat."']
    assert hits["total"] == 1 and hits["next"] is None
    name = cfg.name
    mine = await ok(await c.get("/api/text-versions", params={"of": "lore:k", "q": name[:3]}))
    assert [v["by"] for v in mine["versions"]] == [name]


def lines(r: httpx.Response) -> list[dict]:
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in r.text.splitlines() if line]


async def test_ask_streams_her_draft_until_done(c, app):
    """The draft comes back on the request that asked, nowhere else: what it adds as it
    writes, then the whole draft with done. Nothing goes on the global feed."""
    s = app.state.services
    fake = FakeBackend([TextDelta("Miso is "), TextDelta("the bakery cat \u2014 grey."),
                        Done(reason="stop")])
    s.dispatcher = SimpleNamespace(backend_factory=lambda: fake)
    got = lines(await c.post("/api/text-drafts", json={
        "of": "lore:none", "prompt": "create a cute kitty", "text": "Old text here."}))
    assert got[-1] == {"done": True, "error": None,
                       "text": "Miso is the bakery cat \u2014 grey."}
    grew = [g["text"] for g in got[:-1]]
    assert grew and got[-1]["text"].startswith("".join(grew)), "what it added, in order"
    assert all(set(g) == {"text"} for g in got[:-1])
    assert s.events == [], "a draft is not a feed row"
    assert await s.store.read(lambda c: c.execute(
        "SELECT count(*) FROM global_events").fetchone()[0]) == 0
    system, user = fake.calls[0]
    assert user == {"role": "user", "content": "create a cute kitty"}
    assert "write only that field" in system["content"] and "Old text here." in system["content"]


async def test_ask_fails_with_its_reason(c, app):
    s = app.state.services

    class Broken(FakeBackend):
        async def stream(self, messages, tools, body=None):
            yield TextDelta("Half a")
            raise BackendError("The model server is unreachable.")

    s.dispatcher = SimpleNamespace(backend_factory=lambda: Broken())
    last = lines(await c.post("/api/text-drafts", json={"of": "x", "prompt": "p"}))[-1]
    assert last == {"done": True, "error": "The model server is unreachable.",
                    "text": "Half a"}


async def test_ask_without_a_chat_model_is_refused(c, app):
    app.state.services.dispatcher = SimpleNamespace(backend_factory=None)
    r = await c.post("/api/text-drafts", json={"of": "x", "prompt": "p"})
    assert r.status_code == 503 and r.json()["error"]["message"] == "No chat model is set up."

