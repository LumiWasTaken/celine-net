"""The agent's boxes when they are missing or dead (the shellbox, the browser): each page
gets one sentence that names the box's address and where to fix it, each tool result one
that names the box and its address alone. Fake boxes, no network."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from celine import box_files
from celine.browser import api as browser_api, sync as browser_sync
from celine.browser.sync import BrowserRefused
from celine.boot import load
from celine.skills.browser import Browser
from celine.skills.shell import Shell
from test_shell import FakeBox, ctx
from test_terminals_api import Daemon, _app, _client, term

pytestmark = pytest.mark.anyio

NOT_SET_UP = "The shellbox has no address yet. Type one below, or leave it off."
REFUSED = "The shellbox did not answer at shellbox:8770. Start it, or fix the address."
BAD_KEY = ("The shellbox at shellbox:8770 does not accept this app's key. Put the key hash "
           "below into the box's key.hash file.")
# what she reads in a tool result: the fact, no field to type in
HER_NOT_SET_UP = "The shellbox is not set up."
HER_REFUSED = "The shellbox at shellbox:8770 does not answer."
HER_BAD_KEY = "The shellbox at shellbox:8770 does not take the app's key. Tell Joe."


@pytest.fixture
def box():
    return FakeBox()


@pytest.fixture
def shell(box, monkeypatch, stack_key):
    monkeypatch.setenv("CELINE_SHELL_URL", "http://shellbox:8770")
    stack_key("shell", "k" * 24)
    sh = Shell(load(), transport=httpx.MockTransport(box.handle))
    sh.cfg.user = "Joe"
    return sh


# ---- the shellbox -----------------------------------------------------------------------

async def test_shell_without_an_address_is_down_with_the_variable_named(monkeypatch):
    sh = Shell(load())
    assert (sh.status, sh.error) == ("down", HER_NOT_SET_UP)
    out = await sh.run(ctx(), terminal="main", command="ls")       # called anyway: no traceback, the reason
    assert out.is_error and out.content == HER_NOT_SET_UP


async def test_shell_refused_wrong_key_and_timeout_each_get_one_sentence(shell, box):
    box.answers = [httpx.ConnectError("refused")]
    out = await shell.run(ctx(), terminal="main", command="ls")
    assert out.is_error and out.content == HER_REFUSED and out.display["unreachable"]

    for status in (401, 403):
        box.answers = [(status, {"error": "unauthorized"})]
        out = await shell.run(ctx(), terminal="main", command="ls")
        assert out.is_error and out.content == HER_BAD_KEY, status
        assert shell.status == "up", "a key problem is said at the call, the tools stay"

    box.answers = [httpx.ReadTimeout("slow")]
    out = await shell.type(ctx(), text="ls", terminal="main")
    assert out.is_error and out.content == HER_REFUSED

    box.answers = [[{"progress": "x"}]]            # the stream ended without a result
    out = await shell.run(ctx(), terminal="main", command="ls")
    assert out.is_error and out.content.startswith(
        "The shellbox at shellbox:8770 dropped the connection or restarted.")
    assert "\n" not in out.content.split(". ")[0] and "Traceback" not in out.content


async def test_the_terminals_page_says_why_the_shellbox_is_down(store):
    daemon = Daemon(term("build", "ab12cd"))
    handle, refuse = daemon.handle, {"on": False}

    def answer(request):
        if refuse["on"] and request.url.path == "/read":
            return httpx.Response(401, json={"error": "unauthorized"})
        return handle(request)
    daemon.handle = answer
    app = _app(store, daemon)
    sync = app.state.services.terminals
    async with _client(app) as client:
        daemon.down = True
        for _ in range(3):
            await sync.refresh()
        assert (await client.get("/api/terminals")).json()["down"] == REFUSED
        r = await client.post("/api/terminals")
        assert r.status_code == 502 and r.json()["error"]["message"] == REFUSED
        daemon.down, refuse["on"] = False, True
        for _ in range(3):
            await sync.refresh()
        assert (await client.get("/api/terminals")).json()["down"] == BAD_KEY
        app.state.services.cfg.settings.services = {}
        assert (await client.get("/api/terminals")).json()["down"] == NOT_SET_UP
        assert (await client.post("/api/terminals")).json()["error"]["message"] == NOT_SET_UP


async def test_a_dead_box_never_fails_an_upload_copy(tmp_path, monkeypatch):
    cfg = load()
    cfg.settings.services = {"shellbox": {"url": "http://shellbox:8770"}}
    src = Path(tmp_path, "a.txt")
    src.write_text("hi")

    def dead(request):
        raise httpx.ConnectError("down")
    assert await box_files.copy_upload(cfg, src, "a.txt",
                                       transport=httpx.MockTransport(dead)) is None
    assert await box_files.copy_upload(cfg, src, "a.txt", transport=httpx.MockTransport(
        lambda r: httpx.Response(507, json={"error": "Her home is full."}))) is None
    box_files.copy_later(cfg, src, "a.txt")      # the task is made, the caller never waits
    cfg.settings.services = {}
    box_files.copy_later(cfg, src, "a.txt")      # no box: nothing to do, nothing raised


# ---- the browser --------------------------------------------------------------------------

B_NOT_SET_UP = "The browser has no address yet. Type one below, or leave it off."
B_REFUSED = "The browser did not answer at browser:8771. Start it, or fix the address."
B_HER_NOT_SET_UP = "The browser is not set up."
B_HER_REFUSED = "The browser at browser:8771 does not answer."


async def test_browser_without_an_address_is_down_with_the_variable_named(monkeypatch):
    b = Browser(SimpleNamespace(user="Joe", settings=SimpleNamespace(services={})))
    assert (b.status, b.error) == ("down", B_HER_NOT_SET_UP)
    with pytest.raises(OSError, match="has no address"):
        await browser_sync.screen(b.cfg)


async def test_browser_refused_wrong_key_and_timeout_each_get_one_sentence(monkeypatch,
                                                                          tmp_path):
    answers = []

    async def post(cfg, body):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    async def status(cfg):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(browser_sync, "post", post)
    monkeypatch.setattr(browser_sync, "status", status)
    b = Browser(SimpleNamespace(user="Joe", data_dir=tmp_path, settings=SimpleNamespace(
        services={"browser": {"url": "http://browser:8771"}})))
    c = SimpleNamespace(max_output_tokens=None, conversation_id="c", emit=None)

    answers.append(httpx.ConnectError("refused"))
    out = await b.open(c, url="https://example.com", tab="main")
    assert out.is_error and out.content == f"{B_HER_REFUSED} Tell Joe if it stays away."

    answers.append(httpx.ReadTimeout("slow"))
    out = await b.open(c, url="https://example.com", tab="main")
    assert out.content == f"{B_HER_REFUSED} Tell Joe if it stays away."

    for status in (401, 403):
        answers.append(BrowserRefused(status, {"error": "unauthorized"}))
        out = await b.open(c, url="https://example.com", tab="main")
        assert out.is_error and out.content == (
            "The browser at browser:8771 does not take the app's key. Tell Joe."), status


def test_the_browser_page_and_its_socket_say_why_the_box_is_down(monkeypatch):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from celine.core.services import Services
    from celine.errors import install_error_handler

    async def status(cfg):
        raise httpx.ConnectError("down")

    async def control(cfg, action, tab=None):
        raise httpx.ReadTimeout("slow")

    async def screen(cfg):
        raise ConnectionRefusedError("no Xvnc")
    monkeypatch.setattr(browser_sync, "status", status)
    monkeypatch.setattr(browser_sync, "screen", screen)
    monkeypatch.setattr(browser_sync, "control", control)
    app = FastAPI()
    app.state.services = Services(cfg=SimpleNamespace(settings=SimpleNamespace(services={"browser": {"url": "http://browser:8771"}})))
    app.state.services.store = SimpleNamespace(emit_global=lambda e: None,
                                               read=None)
    app.state.allowed_origins = {"http://127.0.0.1:8766"}
    install_error_handler(app)
    app.include_router(browser_api.router)
    app.include_router(browser_api.ws_router)
    with TestClient(app) as client:
        got = client.get("/api/browser").json()
        assert got["state"] == "unreachable" and got["reason"] == B_REFUSED
        r = client.post("/api/browser/control", json={"action": "take"})
        assert r.status_code == 502
        assert r.json()["error"]["message"] == B_REFUSED
        with pytest.raises(WebSocketDisconnect) as e:        # no Xvnc behind the address
            with client.websocket_connect("/ws/browser", headers={
                    "origin": "http://127.0.0.1:8766", "host": "127.0.0.1:8766"}) as ws:
                ws.receive_bytes()
        assert e.value.code == 1011
        app.state.services.cfg.settings.services = {}
        assert client.get("/api/browser").json() == {
            "state": "off", "reason": B_NOT_SET_UP, "taken": None, "tabs": []}
