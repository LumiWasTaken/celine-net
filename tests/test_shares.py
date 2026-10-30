"""Shares (server/celine/shares.py): the real shellboxd's /forward and /file in a temp
home on a local port, its servers on loopback ports, the app built with its login on: a
private share's page opens only with the cookie its open route gives the owner, a public
one for anyone; no cookie reaches its box; it always comes sandboxed; it ends at its time
on a fixed clock, with nothing told."""
from __future__ import annotations

import getpass
import hashlib
import importlib.util
import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import serve

from celine import boot, shares
from celine.__main__ import build_app
from celine.api import origin as origin_mod
from celine.auth import COOKIE
from celine.clock import zone
from celine.core.ids import iso, when
from celine.skills import ToolContext
from celine.skills.shell import Shell

from test_terminals_api import SyncStore

ROOT = Path(__file__).parent.parent
KEY = "k" * 24
SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-pointer-lock allow-downloads"


class Store(SyncStore):
    def __init__(self, path):
        super().__init__(path)
        self.events: list[dict] = []

    def emit_global(self, event: dict) -> None:
        self.events.append(event)


def serve_in_thread(server) -> None:
    threading.Thread(target=server.serve_forever, daemon=True).start()


@pytest.fixture
def box(tmp_path, monkeypatch, stack_key):
    """shellboxd as in its box, minus tmux: its home is a temp folder."""
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("shellboxd_shares",
                                                  ROOT / "shellbox" / "shellboxd.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    h = tmp_path / "home"
    h.mkdir()
    d.HOME, d.USER, d.AS_HER = h, getpass.getuser(), {}
    d.KEY_HASH = hashlib.sha256(KEY.encode()).hexdigest()
    d.STATUS["reset"] = "2026-10-24T04:00:00"
    server = ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
    server.daemon_threads = True
    serve_in_thread(server)
    monkeypatch.setenv("CELINE_SHELL_URL", f"http://127.0.0.1:{server.server_port}")
    stack_key("shell", KEY)
    yield SimpleNamespace(d=d, home=h)
    server.shutdown()


class Hers(BaseHTTPRequestHandler):
    """The agent's web app: says what it got, sets a cookie, echoes a POST."""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.answer(json.dumps({"path": self.path, "cookie": self.headers.get("Cookie"),
                                "host": self.headers.get("Host"),
                                "origin": self.headers.get("Origin"),
                                "prefix": self.headers.get("X-Forwarded-Prefix")}).encode())

    def do_POST(self):
        self.answer(self.rfile.read(int(self.headers["Content-Length"])))

    def answer(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "celine_session=hers; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def web():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hers)
    server.daemon_threads = True
    serve_in_thread(server)
    yield server.server_port
    server.shutdown()


@pytest.fixture
def echo():
    def handler(ws):
        for msg in ws:
            ws.send(msg)
    server = serve(handler, "127.0.0.1", 0)
    serve_in_thread(server)
    yield server.socket.getsockname()[1]
    server.shutdown()


@pytest.fixture
def app(box, db_path):
    cfg = boot.load()
    cfg.server.update({"login": "on"})
    a = build_app(cfg, dev=False)
    a.state.services.store = Store(db_path)
    yield a
    origin_mod.configure({}, False)


@pytest.fixture
def clock(monkeypatch):
    c = SimpleNamespace(t=datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(shares, "clock", lambda: c.t)
    return c


def owner(app) -> TestClient:
    """The owner's browser, logged in."""
    c = TestClient(app, base_url="http://localhost:8766")
    app.state.auth.store.add_passkey(b"key", b"pub", 0)
    c.cookies.set(COOKIE, app.state.auth.store.new_session(b"key"))
    return c


def make(app, title="Snake", **what) -> dict:
    s = app.state.services
    return anyio.run(lambda: shares.create(s.cfg, s.store, title, conversation_id="c1", **what))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sandboxed(r) -> None:
    assert r.headers["content-security-policy"] == f"{SANDBOX}; frame-ancestors 'self'"
    assert r.headers["access-control-allow-origin"] == "*"
    assert "set-cookie" not in r.headers


async def _browser(cfg, client):
    return True


async def _approved(*_, **__):
    return None


def test_a_port_needs_no_session_and_never_sees_the_cookie(app, web, monkeypatch):
    t = make(app, port=web)["token"]
    monkeypatch.setattr(shares, "from_browser", _browser)    # its own browser box
    c = TestClient(app, base_url="http://celine:8766")
    c.cookies.set(COOKIE, "the-owners-session")
    r = c.get(f"/share/{t}/a/b.js?x=1", headers={"origin": "null"})
    assert r.status_code == 200, r.text
    sandboxed(r)
    assert r.json() == {"path": "/a/b.js?x=1", "cookie": None, "host": f"localhost:{web}",
                        "origin": f"http://localhost:{web}", "prefix": f"/share/{t}"}
    r = c.post(f"/share/{t}/save", content=b"x" * 300_000, headers={"origin": "null"})
    assert r.status_code == 200 and r.content == b"x" * 300_000
    r = c.options(f"/share/{t}/save", headers={"origin": "null",
                                                "access-control-request-method": "PUT"})
    assert r.status_code == 204 and r.headers["access-control-allow-methods"] == "*"
    r = c.get(f"/share/{t}", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == f"/share/{t}/"


def test_a_websocket_echoes_both_ways(app, echo):
    t = make(app, port=echo)["token"]
    with TestClient(app).websocket_connect(f"/share/{t}/live?id=1") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "hello"
        ws.send_bytes(b"\x00\x01")
        assert ws.receive_bytes() == b"\x00\x01"


def test_a_file_seeks_and_a_folder_serves_its_index(app, box):
    data = bytes(range(256)) * 4
    (box.home / "out").mkdir()
    (box.home / "out" / "clip.mp4").write_bytes(data)
    (box.home / "snake" / "js").mkdir(parents=True)
    (box.home / "snake" / "index.html").write_text("<h1>Snake</h1>")
    (box.home / "snake" / "js" / "game.js").write_text("let s = 1")
    (box.home / "secret.txt").write_text("not shared")
    c = TestClient(app)

    t = make(app, path="~/out/clip.mp4", public=True)["token"]
    r = c.get(f"/share/{t}/clip.mp4")
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == "video/mp4"
    sandboxed(r)
    r = c.get(f"/share/{t}/clip.mp4", headers={"range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == data[100:200]
    assert r.headers["content-range"] == "bytes 100-199/1024"
    assert c.get(f"/share/{t}/x", headers={"range": "bytes=2000-"}).status_code == 416

    t = make(app, path="~/snake", public=True)["token"]
    assert c.get(f"/share/{t}/").text == "<h1>Snake</h1>"
    r = c.get(f"/share/{t}/js/game.js")
    assert r.text == "let s = 1" and r.headers["content-type"].startswith("text/javascript")
    r = c.get(f"/share/{t}/js", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == f"/share/{t}/js/"
    # encoded, so the client cannot collapse the dots and the share's own check runs
    for rest in ("%2e%2e/secret.txt", "%2e%2e%2fsecret.txt", "js/%2e%2e/%2e%2e/secret.txt"):
        r = c.get(f"/share/{t}/{rest}")
        assert r.status_code == 404 and "not shared" not in r.text, rest

    with pytest.raises(RuntimeError, match="No file"):
        make(app, path="~/nothing.mp4")


def test_a_down_port_an_unknown_token_an_end_and_a_reset(app, box, web):
    c = TestClient(app)
    port = free_port()
    down = make(app, port=port, public=True)
    r = c.get(f"/share/{down['token']}/")
    assert r.status_code == 502
    assert f"Nothing is running on port {port} in the shellbox right now." in r.text
    sandboxed(r)
    s = app.state.services
    assert anyio.run(shares.state, s.cfg, s.store, down) == "down"

    assert c.get("/share/" + "a" * 24 + "/").status_code == 404
    up = make(app, port=web, public=True)
    assert anyio.run(shares.state, s.cfg, s.store, up) == "live"
    assert anyio.run(shares.end, s.store, up["token"])
    assert c.get(f"/share/{up['token']}/").status_code == 404

    again = make(app, port=web, public=True)
    box.d.STATUS["reset"] = "2026-10-25T04:00:00"
    assert c.get(f"/share/{again['token']}/").status_code == 404
    assert anyio.run(shares.get, s.store, again["token"])["ended_at"]
    assert {"type": "share.updated", "token": again["token"]} in s.store.events


def test_a_private_share_opens_only_for_the_owner_and_never_passes_a_cookie(app, web, clock):
    row = make(app, port=web)
    t = row["token"]
    assert row["ends_at"] == "2026-10-25T12:00:00.000+00:00"           # 24 hours
    anon = TestClient(app, base_url="http://localhost:8766")
    r = anon.get(f"/share/{t}/")
    assert r.status_code == 404 and "Not found" in r.text
    sandboxed(r)
    assert anon.get(f"/api/shares/{t}/open").status_code == 404        # the login gate
    anon.cookies.set(shares.COOKIE, "guess")
    assert anon.get(f"/share/{t}/").status_code == 404
    # its page's own requests (a module script, a fetch) carry no cookie: the token keys them
    assert anon.get(f"/share/{t}/app.js", headers={"sec-fetch-dest": "script"}).status_code == 200

    me = owner(app)
    r = me.get(f"/api/shares/{t}/open", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/share/{t}/"
    cookie = r.headers["set-cookie"]
    for part in (f"{shares.COOKIE}={row['secret']}", f"Path=/share/{t}/", "HttpOnly",
                 "SameSite=lax", "Max-Age=86400"):
        assert part in cookie, cookie
    r = me.get(f"/share/{t}/a", headers={"sec-fetch-dest": "document"})
    assert r.status_code == 200, r.text
    assert r.json()["cookie"] is None                    # neither its own nor the session
    sandboxed(r)


def test_a_public_share_opens_without_a_login(app, web, clock):
    row = make(app, port=web, public=True)
    assert row["ends_at"] == "2026-10-24T14:00:00.000+00:00"           # 2 hours
    r = TestClient(app, base_url="http://localhost:8766").get(f"/share/{row['token']}/x")
    assert r.status_code == 200 and r.json()["path"] == "/x"
    assert owner(app).get(f"/api/shares/{row['token']}").json()["public"] is True


def test_a_share_ends_at_its_time_and_nobody_is_told(app, web, clock):
    s = app.state.services
    row = make(app, port=web)
    t = row["token"]
    me = owner(app)
    assert me.get(f"/api/shares/{t}/open").status_code == 200
    assert me.get(f"/api/shares/{t}").json()["state"] == "live"
    told = list(s.store.events)

    clock.t += timedelta(hours=24)
    assert me.get(f"/share/{t}/").status_code == 404                   # the cookie too
    assert me.get(f"/api/shares/{t}/open").status_code == 404
    got = me.get(f"/api/shares/{t}").json()
    assert got["state"] == "ended" and got["ends_at"] == row["ends_at"]
    assert me.get("/api/shares").json() == {"shares": []}
    assert s.store.events == told                        # no event, so nothing wakes it
    assert anyio.run(shares.get, s.store, t)["ended_at"] is None       # nothing wrote

    shell = Shell(s.cfg)
    ctx = ToolContext("c1", "t", "call-1", s.store, s.cfg, None)  # type: ignore[arg-type]
    assert anyio.run(lambda: shell.list_shares(ctx)).content == \
        f"- {t}: \"Snake\", port {web}, ended {when(row['ends_at'], zone(s.cfg))}"
    assert anyio.run(lambda: shell.unshare(ctx, id=t)).is_error
    clock.t += timedelta(days=1)
    assert anyio.run(lambda: shell.list_shares(ctx)).content == "No shares."


def test_the_owner_stops_a_share_for_both_sides(app, web, clock):
    s = app.state.services
    t = make(app, port=web)["token"]
    me = owner(app)
    me.get(f"/api/shares/{t}/open")
    stop = {"origin": "http://localhost:8766"}
    r = me.post(f"/api/shares/{t}/stop", headers=stop)
    assert r.status_code == 200 and r.json()["state"] == "ended"
    assert {"type": "share.updated", "token": t} in s.store.events
    assert me.get(f"/share/{t}/").status_code == 404
    assert me.post(f"/api/shares/{t}/stop", headers=stop).json()["state"] == "ended"
    assert me.post("/api/shares/" + "a" * 24 + "/stop", headers=stop).status_code == 404
    shell = Shell(s.cfg)
    ctx = ToolContext("c1", "t", "call-1", s.store, s.cfg, None)  # type: ignore[arg-type]
    assert anyio.run(lambda: shell.list_shares(ctx)).content == \
        f"- {t}: \"Snake\", port {web}, ended {when(iso(clock.t), zone(s.cfg))}"


def test_her_tools_share_list_and_end(app, web, clock):
    s = app.state.services
    shell = Shell(s.cfg)
    shell.app = SimpleNamespace(approvals=SimpleNamespace(ask=_approved))   # a public one asks
    ctx = ToolContext("c1", "t", "call-1", s.store, s.cfg, None)  # type: ignore[arg-type]
    tz = zone(s.cfg)

    out = anyio.run(lambda: shell.share(ctx, title="Panel", port=web))
    assert not out.is_error, out.content
    t = out.display["share"]["token"]
    assert out.display == {"share": {"token": t, "title": "Panel"}}
    assert f"Only {s.cfg.user} opens it, logged in to the app: " \
           f"http://localhost:8766/api/shares/{t}/open" in out.content
    assert f"http://celine:8766/share/{t}/" in out.content
    ends = "2026-10-25T12:00:00.000+00:00"
    assert f"It ends {when(ends, tz)}" in out.content
    assert anyio.run(lambda: shell.list_shares(ctx)).content == \
        f"- {t}: \"Panel\", port {web}, answers, ends {when(ends, tz)}"
    assert anyio.run(lambda: shell.share(ctx, title="x")).is_error
    assert anyio.run(lambda: shell.unshare(ctx, id=t)).content == f"Ended share {t}."
    assert anyio.run(lambda: shell.unshare(ctx, id=t)).is_error
    assert anyio.run(lambda: shell.list_shares(ctx)).content == \
        f"- {t}: \"Panel\", port {web}, ended {when(shares.iso(clock.t), tz)}"

    out = anyio.run(lambda: shell.share(ctx, title="Open", port=web, public=True, hours=3))
    p = out.display["share"]["token"]
    assert f"Anyone with the link opens it: http://localhost:8766/share/{p}/" in out.content
    assert f"It ends {when('2026-10-24T15:00:00.000+00:00', tz)}" in out.content
    assert f"- {p}: \"Open\", port {web}, public, answers, ends " \
           f"{when('2026-10-24T15:00:00.000+00:00', tz)}" in \
           anyio.run(lambda: shell.list_shares(ctx)).content.splitlines()
    for bad in ({"hours": 25, "public": True}, {"hours": 169}, {"hours": 0}):
        r = anyio.run(lambda: shell.share(ctx, title="x", port=web, **bad))
        assert r.is_error and "at most" in r.content, bad


def test_shares_off_answer_404_for_every_share_until_on_again(app, web, echo, clock):
    s = app.state.services
    public, private = make(app, port=web, public=True), make(app, port=web)
    sock = make(app, port=echo, public=True)["token"]
    c, me = TestClient(app), owner(app)
    s.cfg.settings.features["shares"] = False             # Settings, Features
    assert c.get(f"/share/{public['token']}/").status_code == 404
    assert c.get(f"/share/{private['token']}/x", headers={"sec-fetch-dest": "script"}) \
        .status_code == 404
    assert me.get(f"/api/shares/{private['token']}/open").status_code == 404
    with pytest.raises(WebSocketDisconnect), c.websocket_connect(f"/share/{sock}/live"):
        pass
    assert me.get(f"/api/shares/{public['token']}").json()["state"] == "down"
    s.cfg.settings.features.update(shares=True, shellbox=False)
    assert c.get(f"/share/{public['token']}/").status_code == 404
    s.cfg.settings.features["shellbox"] = True
    assert c.get(f"/share/{public['token']}/").status_code == 200


def test_a_host_header_alone_does_not_open_a_private_share(app, web, monkeypatch):
    t = make(app, port=web)["token"]
    inside = TestClient(app, base_url="http://celine:8766")
    assert inside.get(f"/share/{t}/").status_code == 404, "anyone can send Host: celine"
    monkeypatch.setattr(shares, "from_browser", _browser)
    assert inside.get(f"/share/{t}/").status_code == 200, "its own browser box opens it"


def test_only_plain_headers_come_back_from_a_share(app, web, monkeypatch):
    from celine.shares import KEEP_OUT
    assert b"clear-site-data" not in KEEP_OUT and b"set-cookie" not in KEEP_OUT
    assert b"content-type" in KEEP_OUT


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """Her Mac's share server: share.js from setup.sh under node, its home a temp folder,
    as the device mac."""
    import re
    import shutil
    import subprocess
    from celine import devices
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    script = re.search(r"cat > \"\$LIB/share.js\" <<'EOF'\n(.*?)\nEOF\n",
                       (ROOT / "plugins/mac/setup.sh").read_text(), re.S)[1]
    home = tmp_path / "machome"
    home.mkdir()
    (tmp_path / "share.js").write_text(script)
    (tmp_path / "token").write_text("mac-token\n")
    port = free_port()
    proc = subprocess.Popen([node, str(tmp_path / "share.js"), str(port),
                             str(tmp_path / "token")], env={"HOME": str(home)})
    url = f"http://127.0.0.1:{port}"
    while True:                # until it answers
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            assert proc.poll() is None, "share.js ended"
    mine = {"mac": devices.Device("mac", "Her Mac", "", "mac-token", "", "", url, None)}
    monkeypatch.setattr(devices, "devices", lambda cfg: mine)
    yield SimpleNamespace(home=home, devices=mine)
    proc.kill()
    proc.wait()


def test_a_share_on_her_mac_goes_through_its_share_server(app, mac, web, clock):
    data = bytes(range(256)) * 4
    (mac.home / "clip.mp4").write_bytes(data)
    (mac.home / "site").mkdir()
    (mac.home / "site" / "index.html").write_text("<h1>Mac</h1>")
    c = TestClient(app)

    t = make(app, port=web, public=True, device="mac")["token"]
    r = c.get(f"/share/{t}/a.js?x=1")
    assert r.status_code == 200, r.text
    sandboxed(r)
    assert r.json()["path"] == "/a.js?x=1" and r.json()["host"] == f"localhost:{web}"
    t = make(app, path="~/clip.mp4", public=True, device="mac")["token"]
    r = c.get(f"/share/{t}/clip.mp4", headers={"range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == data[100:200]
    assert c.get(f"/share/{t}/clip.mp4").content == data
    t = make(app, path="site", public=True, device="mac")["token"]
    assert c.get(f"/share/{t}/").text == "<h1>Mac</h1>"
    port = free_port()
    t = make(app, port=port, public=True, device="mac")["token"]
    assert f"Nothing is running on port {port} on Her Mac right now." in \
        c.get(f"/share/{t}/").text
    with pytest.raises(RuntimeError, match="No file"):
        make(app, path="nothing.mp4", device="mac")

    s = app.state.services
    shell = Shell(s.cfg)
    ctx = ToolContext("c1", "t", "call-1", s.store, s.cfg, None)  # type: ignore[arg-type]
    out = anyio.run(lambda: shell.share(ctx, title="Mac panel", port=web, device="mac"))
    assert not out.is_error, out.content
    assert out.content.endswith("at shell__unshare.")
    assert f"\"Mac panel\", port {web} on Her Mac, answers" in \
        anyio.run(lambda: shell.list_shares(ctx)).content
    out = anyio.run(lambda: shell.share(ctx, title="x", port=web, device="pc"))
    assert out.is_error and out.content == "pc is not one of your devices that is on. Yours: mac."
    mac.devices.clear()
    assert c.get(f"/share/{t}/").status_code == 404, "the device is off"
