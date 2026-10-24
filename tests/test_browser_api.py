"""POST /api/browser/control front and close: forwarded to the box with the tab, its
refusals as 200 {refused}, browser.state for the pages."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from celine.browser import api, sync
from celine.core.services import Services
from celine.browser.sync import BrowserRefused
from celine.errors import install_error_handler


def test_front_and_close_forward_and_refuse_in_a_sentence(monkeypatch):
    sent, events = [], []

    async def control(cfg, action, tab=None):
        sent.append((action, tab))
        if tab == "busy":
            raise BrowserRefused(409, {"error": "busy", "tab": "a"})
        if tab == "gone":
            raise BrowserRefused(404, {"error": "no_tab", "tabs": []})
        return {"front": tab}

    monkeypatch.setattr(sync, "control", control)
    app = FastAPI()
    app.state.services = Services()
    app.state.services.store = SimpleNamespace(emit_global=events.append)
    install_error_handler(app)
    app.include_router(api.router)
    with TestClient(app) as client:
        def post(body):
            return client.post("/api/browser/control", json=body)

        assert post({"action": "front", "tab": "b"}).json() == {"front": "b"}
        assert events == [{"type": "browser.state"}]
        r = post({"action": "close", "tab": "busy"})
        assert r.status_code == 200 and r.json()["refused"].startswith("Tab a is in the middle")
        assert post({"action": "close", "tab": "gone"}).json() == \
            {"refused": "Tab gone is closed already."}
        assert post({"action": "close"}).status_code == 400
    assert sent == [("front", "b"), ("close", "busy"), ("close", "gone")]


def test_the_console_runs_her_tool_and_answers_her_text(monkeypatch):
    """POST /api/browser/calls: its tool by name, its arguments, its text back; the tab
    in front when none is named."""
    from celine.skills.browser import Browser
    sent = []

    async def post(cfg, body):
        sent.append(body)
        return {"tab": body["tab"], "profile": "main", "url": "https://x/", "title": "x",
                "view": {"y": 0, "h": 900, "page": 900}, "lines": ['- button "Go" e1'],
                "notes": [], "done": 1, "effects": [{"effect": "none"}]}

    async def status(cfg):
        return {"front": "c-2c87ce"}

    monkeypatch.setattr(sync, "post", post)
    monkeypatch.setattr(sync, "status", status)
    app = FastAPI()
    app.state.services = Services()
    app.state.services.registry = SimpleNamespace(skills={"browser": Browser(SimpleNamespace(
        user="Joe", settings=None))})
    app.state.services.store = app.state.services.cfg = None
    install_error_handler(app)
    app.include_router(api.router)
    with TestClient(app) as client:
        r = client.post("/api/browser/calls", json={"tool": "act", "args": {
            "steps": [{"do": "click", "target": "e1"}]}})
        assert r.status_code == 200 and not r.json()["is_error"]
        assert r.json()["content"].startswith('tab c-2c87ce  https://x/  "x"')
        assert r.json()["display"]["steps"] == [{"step": "click e1",
                                                 "effect": "nothing changed on the page"}]
        assert client.post("/api/browser/calls", json={"tool": "restart"}).status_code == 400
        assert client.post("/api/browser/calls", json={"tool": "look", "args": {
            "colour": "red"}}).status_code == 400
    assert sent == [{"tab": "c-2c87ce", "steps": [{"do": "click", "target": "e1"}]}]


@pytest.mark.anyio
async def test_her_browser_calls_list_newest_first_with_their_line_and_state(store):
    """GET /api/browser/calls: every browser call of its own with its line as the tool
    row says it, a state word, the shots it has and its conversation; `failed` and `q`
    narrow it, `before` pages it."""
    import httpx
    from celine.conversations import store as conversations, tree
    cid = await conversations.create(store, title="find a job")
    calls = [
        ("a", "browser__open", '{"url": "https://jobs.example.org/x"}', None, "ok", "2026-10-01T10:00:00+00:00"),
        ("b", "browser__act", '{"steps": [{"do": "click", "target": "e14"}, {"do": "type", "target": "e2", "text": "python\\nmore"}]}',
         '{"tab": "t1", "steps": [{"step": "click e14"}, {"step": "type e2"}]}', "ok", "2026-10-01T10:01:00+00:00"),
        ("c", "browser__look", '{"what": "screen", "question": "Where is Play?"}', '{"refused": true}', "ok", "2026-10-01T10:02:00+00:00"),
        ("d", "browser__open", '{"url": "http://x.org"}', None, "error", "2026-10-01T10:03:00+00:00"),
        ("e", "shell__run", '{}', None, "ok", "2026-10-01T10:04:00+00:00"),
    ]

    def add(c):
        m = tree.append(c, store, cid, None, "assistant", "on it", "model")
        for id_, name, args, display, state, at in calls:
            c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, display, state, started_at) "
                      "VALUES(?, ?, ?, ?, ?, ?, ?)", (id_, m["id"], name, args, display, state, at))
    await store.write(add)
    app = FastAPI()
    app.state.services = Services()
    app.state.services.store = store
    install_error_handler(app)
    app.include_router(api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        got = (await c.get("/api/browser/calls")).json()
        assert [(x["id"], x["line"], x["state"], x["shots"]) for x in got["calls"]] == [
            ("d", "open x.org", "failed", 0), ("c", "look screen: Where is Play?", "failed", 0),
            ("b", "click e14, type e2: python", "done", 2), ("a", "open jobs.example.org/x", "done", 0)]
        assert got["total"] == 4 and got["failed"] == 2 and got["next"] is None
        assert got["calls"][2]["tab"] == "t1" and got["calls"][2]["conversation"] == {"id": cid, "title": "find a job",
                                                                                     "link": f"/chat/{cid}"}
        assert [x["id"] for x in (await c.get("/api/browser/calls", params={"failed": 1})).json()["calls"]] == ["d", "c"]
        assert [x["id"] for x in (await c.get("/api/browser/calls", params={"q": "JOBS"})).json()["calls"]] == ["a"]
        page = (await c.get("/api/browser/calls", params={"limit": 2})).json()
        assert [x["id"] for x in page["calls"]] == ["d", "c"] and page["next"] == "2026-10-01T10:02:00+00:00"
        rest = (await c.get("/api/browser/calls", params={"before": page["next"]})).json()
        assert [x["id"] for x in rest["calls"]] == ["b", "a"]
