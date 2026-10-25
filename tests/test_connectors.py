"""The app's side of the connectors: deleting a chat, and what reaches the connector's
own paths. A fake connector only."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.kb.chat import ChatKB
from celine.core.services import Services
from celine.kb.connectors import Connector
from celine.kb.tasks import Tasks

KEY = "app-key-0123456789"


class FakeLink:
    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.ticks: dict[str, bool] = {}
        self.head = 0
        self.polls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, p := request.url.path, body))
        if p == "/admin/status":
            return httpx.Response(200, json={"connection": {"state": "connected"},
                                             "account": {"id": "105", "name": "sam"},
                                             "journal": {"head": self.head}})
        if p == "/admin/sync":
            return httpx.Response(200, json={"job": "j9"})
        if p == "/admin/jobs/j9":
            self.polls += 1
            return httpx.Response(200, json={"state": "done" if self.polls > 1 else "running"})
        if p == "/admin/targets" and request.method == "PUT":
            self.ticks.update(body["set"])
            return httpx.Response(200, json={"changed": len(body["set"]), "job": "j1"})
        return httpx.Response(404, json={"error": "No such thing."})


@pytest.fixture
def world(tmp_path, monkeypatch):
    from celine.api import connectors as api_mod
    from celine.errors import install_error_handler
    link = FakeLink()
    kb = ChatKB(id="discord", title="Discord", platform="discord",
                path=tmp_path / "kb" / "discord.db")
    monkeypatch.setattr(Connector, "follow", lambda self, job, ctx, **kw: {"job": job})
    app = FastAPI()
    app.state.services = Services()
    install_error_handler(app)
    app.include_router(api_mod.router)
    conn = Connector("discord", "http://127.0.0.1:8768/mcp", {"Authorization": f"Bearer {KEY}"},
                     kb, transport=httpx.MockTransport(link.handle),
                     sync_transport=httpx.MockTransport(link.handle))
    app.state.services.connectors = SimpleNamespace(get={"discord": conn}.get)
    app.state.services.kbs = SimpleNamespace(tasks=Tasks(lambda ev: None), get={"discord": kb}.get)
    app.state.services.bus = SimpleNamespace(publish=lambda *a: None)
    app.state.services.store = SimpleNamespace(emit_global=lambda ev: None)
    return TestClient(app), link, kb


def test_the_connector_state_is_its_status(world):
    client, link, _ = world
    got = client.get("/api/sources/discord/connector").json()
    assert got == {"platform": "discord", "connection": {"state": "connected"},
                   "account": {"id": "105", "name": "sam"}, "login_lost_at": None,
                   "login": None, "qr_at": None}
    assert [p for _, p, _ in link.calls] == ["/admin/status"]


def test_a_deleted_chat_is_left_out_first_and_comes_back_when_included(world):
    client, link, kb = world
    sink = kb.sink()
    ids = {}
    for key in ("D1", "D2"):
        ids[key] = sink.chat(key, kind="dm", name=key)
        for i in range(12):
            sink.message(ids[key], f"{key}-{i}", ts=1_758_700_000_000 + i * 60_000, text=f"hi {i}")
    sink.close()
    assert client.delete(f"/api/sources/discord/chats/{ids['D2']}").json() == {"deleted": 12}
    assert link.ticks == {"D2": False}                   # the connector leaves it out first
    assert kb.get_chat(ids["D2"]) is None and kb.get_chat(ids["D1"])["messages"] == 12
    assert [c["key"] for c in kb.ignored_chats()] == ["D2"]
    # included again from the list: the connector reads it from the start
    r = client.post("/api/sources/discord/ignored-chats/include", json={"key": "D2"}).json()
    assert link.ticks["D2"] is True and r["task"]
    assert client.get("/api/sources/discord/ignored-chats").json()["chats"] == []


def test_ids_in_a_connector_path_are_checked_first(world):
    """Ids go into the connector's own /admin paths: anything else is refused first."""
    client, link, _ = world
    before = len(link.calls)
    # dots encoded, so the client cannot collapse them before the route's check
    for method, path, code in (
            ("GET", "/api/sources/discord/discord/guilds/%2e%2e/channels", 400),
            ("PUT", "/api/sources/discord/discord/subscriptions/12ab", 400),
            ("DELETE", "/api/sources/discord/discord/subscriptions/%2e%2e", 400),
            ("DELETE", "/api/sources/discord/discord/login-sessions/%2e%2e", 404),
            ("DELETE", "/api/sources/discord/discord/login-sessions/a.b", 404)):
        r = client.request(method, path, json={} if method == "PUT" else None)
        assert r.status_code == code, (method, path, r.status_code)
    assert len(link.calls) == before


def test_the_first_import_counts_the_archive_and_ends_at_the_journal_head(world):
    """The onboarding's Import on a linked Discord: its sync first, then the feed up to the
    journal's head; the progress is the archive's message count, the end says Done."""
    from celine.kb.importers import ImportContext
    client, link, kb = world
    link.head = 7
    conn = client.app.state.services.connectors.get("discord")
    seen: list[int] = []
    sink = kb.sink()
    chat = sink.chat("D1", kind="dm", name="D1")
    sink.close()

    def fed(_seconds: float) -> None:          # one tick: the feed applied a little more
        s = kb.sink()
        s.message(chat, f"m{len(seen)}", ts=1_758_700_000_000 + len(seen), text="hi")
        s.close()
        kb.file.set_json("feed_seq", 3 * len(seen))

    ctx = ImportContext(progress=lambda done, total, unit: seen.append(done),
                        cancelled=lambda: False)
    out = conn.first_import("j9", ctx, sleep=fed)
    assert seen == [0, 1, 2, 3] and out["messages"] == 3     # head 7 reached at seq 9
    assert out["sentence"] == "Done. 3 messages in the archive."
