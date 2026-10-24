"""The terminal sync, routes and socket against a fake daemon: what the app sends it,
what the rows and its terminal history say afterwards, and what a page hears."""
from __future__ import annotations

import asyncio
import base64
import gzip
import json
import sqlite3
from chat_fixtures import until_sync
from datetime import datetime, timezone
from types import SimpleNamespace

import anyio
import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from celine.core.store import attach
from celine.errors import install_error_handler
from celine.core.services import Services
from celine.api import ws as ws_mod
from celine.boot import load
from celine.core.bus import EventBus
from celine.terminals import api as api_mod
from celine.terminals import store as terminals
from celine.terminals.sync import TerminalSync

pytestmark = pytest.mark.anyio

HEADERS = {"origin": "http://127.0.0.1:8766", "host": "127.0.0.1:8766"}
OPENED = int(datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc).timestamp())
OPENED_ISO = "2026-10-02T01:00:00.000+00:00"
STATE = [{"type": "terminal.state"}]


class Chunks(httpx.AsyncByteStream):
    """GET /view: the scripted chunks, then the end of the body, or a view that stays."""

    def __init__(self, chunks: list[bytes], hang: bool):
        self.chunks, self.hang = chunks, hang

    async def __aiter__(self):
        for c in self.chunks:
            yield c
        if self.hang:
            await anyio.sleep_forever()


def term(name: str, id: str, owner: str = "celine", **more) -> dict:
    return {"name": name, "id": id, "owner": owner, "opened_at": OPENED, "shell": "bash",
            "cwd": "/home/celine", "program": None, "pid": None, "busy": False,
            "paused": False, "fullscreen": False, "waiting": True, "last_exit": 0,
            "running_for": None, "quiet_for": 12, "dead": False, "exit": None, **more}


class Daemon:
    """Stands in for shellboxd: records requests, answers its listing, logs and queue."""

    def __init__(self, *listing: dict):
        self.sent: list[tuple[str, dict, dict]] = []
        self.terminals, self.boot_id = list(listing), "2026-10-02T04:00:31+a1b2"
        self.answers: dict[str, list] = {}
        self.chunks, self.hang = [b"celine@shellbox:~$ "], False
        self.down = self.log_down = False
        self.logs: dict[str, bytes] = {}
        self.per_answer = 1_000_000

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.down or (self.log_down and path == "/log"):
            raise httpx.ConnectError("down")
        body = json.loads(request.content or b"{}") if request.method == "POST" \
            else dict(request.url.params)
        self.sent.append((path, body, dict(request.headers)))
        if path == "/view":
            return httpx.Response(200, stream=Chunks(self.chunks, self.hang))
        if path == "/log":
            if body["id"] not in self.logs:
                return httpx.Response(404, json={"error": "No log of that terminal."})
            log, start = self.logs[body["id"]], int(body["from"])
            return httpx.Response(200, content=log[start:start + self.per_answer],
                                  headers={"X-Log-Size": str(len(log))})
        if path == "/read":
            return httpx.Response(200, json={
                "terminals": self.terminals, "boot_id": self.boot_id, "fresh": False,
                "boot": None, "notes": []})
        return httpx.Response(200, json=(self.answers.get(path) or [{"ok": True}]).pop(0))

    def calls(self, path: str) -> list[dict]:
        return [b for p, b, _ in self.sent if p == path]


def _app(store, daemon: Daemon) -> FastAPI:
    app = FastAPI()
    app.state.services = Services()
    install_error_handler(app)
    app.include_router(api_mod.router)
    app.include_router(api_mod.ws_router)
    app.include_router(ws_mod.router)
    cfg = load()
    cfg.user, cfg.data_dir = "Joe", store.db_path.parent
    app.state.services.store, app.state.services.cfg, app.state.services.bus = store, cfg, EventBus()
    store.bus = app.state.services.bus
    app.state.allowed_origins = {"http://127.0.0.1:8766"}
    cfg.settings.services = {"shellbox": {"url": "http://shellbox:8770"}}
    app.state.services.terminals = TerminalSync(cfg, store,
                                                transport=httpx.MockTransport(daemon.handle))
    app.state.services.dispatcher = SimpleNamespace(notify=lambda: None)
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers=HEADERS)


async def _events(store, rx) -> list[dict]:
    """What pages heard so far (the global feed's events are written in order: one
    write after them has them published), without their ids."""
    await store.write(lambda c: None)
    return [{k: v for k, v in rx.receive_nowait().items() if k != "gid"}
            for _ in range(rx.statistics().current_buffer_used)]


async def _rows(store) -> dict[str, dict]:
    return {t["id"]: t for t in await terminals.list_terminals(store)}


async def test_the_sync_keeps_rows_with_the_key_and_tells_pages_once_per_change(
        store, stack_key):
    stack_key("shell", "k" * 24)
    daemon = Daemon(term("build", "ab12cd", busy=True, program="cmake", running_for=10),
                    term("sam", "lu0001", owner="sam"))
    app = _app(store, daemon)
    sync = app.state.services.terminals
    _, rx = app.state.services.bus.subscribe("global")
    await sync.refresh()
    assert daemon.sent[0][:2] == ("/read", {})
    assert daemon.sent[0][2]["authorization"] == "Bearer " + "k" * 24
    rows = await _rows(store)
    assert {(r["id"], r["name"], r["owner"]) for r in rows.values()} == \
        {("ab12cd", "build", "celine"), ("lu0001", "sam", "sam")}
    assert rows["ab12cd"]["opened_at"] == OPENED_ISO and sync.live["ab12cd"]["program"] == "cmake"
    assert await _events(store, rx) == STATE
    daemon.terminals[0].update(running_for=15, quiet_for=0)
    await sync.refresh()
    assert await _events(store, rx) == [], "ticking seconds are no change"
    daemon.terminals[0].update(busy=False, program=None)
    await sync.refresh()
    assert await _events(store, rx) == STATE
    # a daemon that does not answer keeps the rows and drops live after three misses
    daemon.down = True
    for _ in range(3):
        assert sync.live["ab12cd"]
        await sync.refresh()
    assert sync.live == {} and (await _rows(store))["ab12cd"]["ended_at"] is None


async def test_her_terminal_history_is_appended_packed_at_the_end_and_never_lost(store):
    daemon = Daemon(term("build", "ab12cd"), term("main", "zz9999"))
    daemon.logs = {"ab12cd": b"$ make\r\n"}
    daemon.per_answer = 4
    app = _app(store, daemon)
    sync = app.state.services.terminals
    folder = store.db_path.parent / "terminals"
    await sync.refresh()
    assert (folder / "ab12cd.raw").read_bytes() == b"$ make\r\n", "answer after answer"
    daemon.logs["ab12cd"] += b"cc -O2\r\n"
    await sync.refresh()
    assert (folder / "ab12cd.raw").read_bytes() == b"$ make\r\ncc -O2\r\n", "only what is new"

    # it exits build: the rest is fetched, then the log is packed for good
    daemon.logs["ab12cd"] += b"done\r\n"
    daemon.terminals[0].update(dead=True, exit=130)
    await sync.refresh()
    row = (await _rows(store))["ab12cd"]
    assert (row["ended_by"], row["exit"]) == ("exit", 130)
    assert not (folder / "ab12cd.raw").exists()
    whole = b"$ make\r\ncc -O2\r\ndone\r\n"
    assert gzip.decompress((folder / "ab12cd.raw.gz").read_bytes()) == whole

    # a reset: main's log is gone from the box, what the app stored stays and is packed
    daemon.logs["zz9999"] = b"top\r\n"
    await sync.refresh()
    daemon.boot_id, daemon.terminals, daemon.logs = "2026-10-03T04:00:40+c3d4", [], {}
    await sync.refresh()
    rows = await _rows(store)
    assert rows["zz9999"]["ended_by"] == "reset" and rows["ab12cd"]["ended_by"] == "exit"
    assert gzip.decompress((folder / "zz9999.raw.gz").read_bytes()) == b"top\r\n"

    # one that opened and ended while the box did not answer gets its log once it does
    daemon.terminals, daemon.logs = [term("quick", "qq0001", dead=True)], {"qq0001": b"exit"}
    daemon.log_down = True
    await sync.refresh()
    daemon.log_down = False
    await sync.refresh()
    assert gzip.decompress((folder / "qq0001.raw.gz").read_bytes()) == b"exit"

    async with _client(app) as c:
        r = await c.get("/api/terminals/ab12cd/log")
        assert r.status_code == 200 and r.content == whole
    assert terminals.log_path(store.db_path.parent, "../celine") is None, "no plain name"


class SyncStore:
    """The data functions over one connection: the test client's app runs on its own
    thread, where the real Store's writer task is not."""

    def __init__(self, path):
        self.db_path = path
        self.c = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA foreign_keys=ON")
        attach(self.c, path)

    async def read(self, fn):
        return fn(self.c)

    write = read


def test_the_socket_relays_the_view_and_takes_keys_only_for_the_users_own(db_path):
    daemon = Daemon()
    daemon.chunks = [b"\x1b[2J\x1b[H", b"celine@shellbox:~$ "]
    daemon.answers["/keys"] = [{"ok": True, "line": "echo hi"}]
    store = SyncStore(db_path)
    anyio.run(terminals.upsert, store, "ab12cd", "build", "celine", OPENED_ISO)
    anyio.run(terminals.upsert, store, "lu0001", "sam", "sam", OPENED_ISO)
    with TestClient(_app(store, daemon)) as client:
        with client.websocket_connect("/ws/terminals/ab12cd", headers=HEADERS) as sock:
            assert [sock.receive_bytes() for _ in range(2)] == daemon.chunks
            assert sock.receive_json() == {"type": "end"}
        assert daemon.sent[-1][:2] == ("/view", {"terminal": "build"})
        with pytest.raises(WebSocketDisconnect) as e:            # another site's page
            with client.websocket_connect("/ws/terminals/lu0001",
                                          headers={"origin": "http://evil", "host": "evil"}):
                pass
        assert e.value.code == 1008

        daemon.chunks, daemon.hang = [b"sam@shellbox:~$ "], True
        with client.websocket_connect("/ws/terminals/lu0001", headers=HEADERS) as sock:
            sock.receive_bytes()
            sock.send_bytes(b"echo hi\r")
            until_sync(lambda: store.c.execute("SELECT 1 FROM terminal_commands").fetchone(), 3)
            assert [tuple(r) for r in store.c.execute(
                "SELECT terminal_id, kind, text FROM terminal_commands")] == \
                [("lu0001", "line", "echo hi")]
            assert daemon.calls("/keys") == [{"terminal": "sam",
                                              "data": base64.b64encode(b"echo hi\r").decode()}]

        # its own: a binary frame closes the socket
        with client.websocket_connect("/ws/terminals/ab12cd", headers=HEADERS) as sock:
            sock.receive_bytes()
            sock.send_bytes(b"x")
            with pytest.raises(WebSocketDisconnect) as e:
                sock.receive_bytes()
            assert e.value.code == 1008
        assert len(daemon.calls("/keys")) == 1


async def test_the_message_targets_are_names_never_an_app_title(store):
    from celine.conversations import store as conversations
    await terminals.upsert(store, "ab12cd", "build", "celine", OPENED_ISO)
    auto = await conversations.create(store)
    await conversations.update(store, auto, title="Shell tasks completed", auto=True)
    mine = await conversations.create(store)
    await conversations.update(store, mine, title="Lighthouse trip")
    await conversations.create(store, agent_of=mine, agent_name="scout")
    from celine.missions import store as missions
    goal = "Research slowly, one short web search at a time"
    unnamed = await conversations.create(store, title=goal[:40], auto=True)
    await missions.create(store, unnamed, {"name": goal[:40], "terminal": "mission",
                                            "brief": goal})
    named = await conversations.create(store, title="Tides")
    await missions.create(store, named, {"name": "Tides", "terminal": "tides", "brief": goal})
    async with _client(_app(store, Daemon())) as client:
        r = await client.get("/api/terminals/ab12cd/targets")
    names = {t["id"]: t["name"] for t in r.json()["targets"]}
    assert len(names) == 4 and names[mine] == "Lighthouse trip" and names[named] == "Tides"
    assert names[unnamed].startswith("Celine ")
    assert names[auto].startswith("Celine ") and "Shell" not in names[auto]


async def test_a_refused_control_is_one_sentence_as_200(store):
    await terminals.upsert(store, "ab12cd", "build", "celine", OPENED_ISO)
    daemon = Daemon()
    plain = daemon.handle
    daemon.handle = lambda r: httpx.Response(400, json={"error": "Nothing runs."}) \
        if r.url.path == "/control" else plain(r)
    async with _client(_app(store, daemon)) as client:
        r = await client.post("/api/terminals/ab12cd/control", json={"action": "stop"})
        assert (r.status_code, r.json()) == (200, {"refused": "Nothing runs."})
        await terminals.end(store, "ab12cd", "user", None)
        r = await client.post("/api/terminals/ab12cd/control", json={"action": "stop"})
        assert (r.status_code, r.json()) == (200, {"refused": "build has ended."})


async def test_a_known_secret_is_blanked_in_her_stored_history_also_split_in_two(store):
    """`cat ~/.vault/example-shop/password` in its terminal: the stored copy has stars for it,
    byte for byte, when it comes in one answer, split across two answers of a round, or
    split across two rounds. The stored size stays the daemon's offset. After a start,
    nothing is stored before the vault's first unlock is over."""
    from celine import errors
    pw = "test-Pass-0123456789"
    errors.set_secrets([pw], "vault")
    try:
        daemon = Daemon(term("main", "ab12cd"))
        head = b"$ cat ~/.vault/example-shop/password\r\n"
        daemon.logs = {"ab12cd": head + pw.encode() + b"$ " + head + pw[:9].encode()}
        daemon.per_answer = len(head) + 5           # the first one splits in two answers
        app = _app(store, daemon)
        raw = store.db_path.parent / "terminals" / "ab12cd.raw"
        # after a start, nothing is stored before the vault's first unlock is over
        sync, ready = app.state.services.terminals, asyncio.Event()
        sync.ready = ready
        async with anyio.create_task_group() as tg:
            tg.start_soon(sync.refresh)
            await anyio.wait_all_tasks_blocked()     # the refresh waits for the unlock
            assert not raw.exists()
            ready.set()
        daemon.logs["ab12cd"] += pw[9:].encode() + b"$ ok\r\n"
        await app.state.services.terminals.refresh()
        stars = b"*" * len(pw)
        whole = head + stars + b"$ " + head + stars + b"$ ok\r\n"
        assert raw.read_bytes() == whole and len(whole) == len(daemon.logs["ab12cd"])
        daemon.logs["ab12cd"] += b"done\r\n"
        await app.state.services.terminals.refresh()
        assert raw.read_bytes() == whole + b"done\r\n", "only what is new, nothing lost"
    finally:
        errors.set_secrets([], "vault")
