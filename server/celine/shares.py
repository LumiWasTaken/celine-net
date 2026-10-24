"""Shares: something of its shellbox the user opens through the app. A share is a port (a
server it runs in its box: a game, a dev server, a control panel) or a path in its home
(a file, or a folder served as static files). /share/<token>/... reaches it.

A port goes through shellboxd's GET /forward, a raw tunnel to localhost:<port> inside its
box: only the app talks to its box, and a server bound to 127.0.0.1 works too. HTTP and
WebSocket pass through it, streamed both ways. A path goes through GET /file, with its
content type and byte ranges (a video seeks). The agent's server gets the path after the token
(/share/<token>/a/b.js asks for /a/b.js; X-Forwarded-Prefix names the rest), so its pages
link relatively. A share ends at its end time (ends_at, set when it shares it), when it
ends it (shell__unshare), when the user stops it (POST /api/shares/<token>/stop) or when its
box is reset: every answer of the box names its last reset (X-Shellbox-Reset). The end time
is passive: nothing runs at it and nobody is told; the proxy and the lists compare it with
the clock.

A share is private unless it made it public: its page opens only for the user, logged in.
/share/ passes the session gate and the origin check (the session cookie is SameSite=Strict
and its page's own requests carry none), so the card opens GET /api/shares/<token>/open,
behind the login, which sets the share's own cookie (`share`, HttpOnly, SameSite=Lax, the
share's path, until its end) and redirects there. The Cookie header never reaches its box
(nor a Set-Cookie of its own the browser). Why: its code runs on the app's origin, so every
answer carries a CSP sandbox: the page gets an opaque origin and can never act as the user.
That opaque origin makes its page's own requests cross-origin, hence
Access-Control-Allow-Origin: * on every answer and the preflights answered here.

Shares are a feature of their own (settings/features.py, off until the user turns it on):
off, its three share tools are not built and /share/ answers 404 for every share, even
one that still runs, until it is on again; so with the shellbox feature off.
"""
from __future__ import annotations

import asyncio
import mimetypes
import posixpath
import secrets
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import anyio
import httpcore
import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from starlette.requests import cookie_parser
from websockets.asyncio.client import connect as ws_connect

from . import keys, shellbox
from .artifacts.page import HEAD as PAGE_HEAD
from .core.ids import iso
from .errors import NotFound
from .core.event_types import SHARE_UPDATED
from .settings import features

router = APIRouter()

PREFIX = "/share/"
TOKEN_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
TOKEN_LEN = 24                      # about 124 bits: unguessable
TITLE_MAX = 100
CONNECT = shellbox.CONNECT          # seconds to reach the box
# where its own browser reaches the app in the stack (docker-compose.yml): its browser box
# sits on a network with the app, never with its shellbox
INSIDE = "http://celine:8766"
COOKIE = "share"                    # the user's key to a private share, on its path only
PRIVATE_HOURS, PRIVATE_MAX = 24, 7 * 24     # how long a share runs: the default and the most
PUBLIC_HOURS, PUBLIC_MAX = 2, 24
ENDED_SHOWN = timedelta(days=1)     # its list names the shares that ended this long ago

SANDBOX = "allow-scripts allow-forms allow-popups allow-pointer-lock allow-downloads"
HEADERS = [(b"content-security-policy", f"sandbox {SANDBOX}; frame-ancestors 'self'".encode()),
           (b"x-frame-options", b"SAMEORIGIN"), (b"access-control-allow-origin", b"*"),
           (b"referrer-policy", b"no-referrer")]
PREFLIGHT = [(b"access-control-allow-methods", b"*"), (b"access-control-allow-headers", b"*"),
             (b"access-control-max-age", b"600")]
# never passed to its box: hop by hop, the user's cookie, what we set ourselves
DROP_IN = {b"host", b"connection", b"keep-alive", b"upgrade", b"te", b"trailer",
           b"proxy-authorization", b"proxy-connection", b"cookie", b"origin", b"referer"}
# the only headers passed from its box: a share runs on the app's origin, so anything
# else (Set-Cookie, Clear-Site-Data, NEL, Service-Worker-Allowed, ...) could reach the app
KEEP_OUT = {b"content-type", b"content-length", b"content-encoding", b"content-range",
            b"content-disposition", b"content-language", b"accept-ranges", b"cache-control",
            b"etag", b"last-modified", b"expires", b"vary", b"location", b"date",
            b"access-control-allow-methods", b"access-control-allow-headers",
            b"access-control-expose-headers"}
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
NOT_FOUND = "Not found"
BOX_DOWN = "The shellbox is not answering right now."


def clock() -> datetime:
    return datetime.now(timezone.utc)


def _origin(cfg) -> str:
    return str(cfg.server.get("public_origin") or f"http://localhost:{cfg.port}").rstrip("/")


def link(cfg, row: dict) -> str:
    """Where the user opens it: the app's public origin; a private share through its open
    route behind the login."""
    if not row["public"]:
        return f"{_origin(cfg)}/api/shares/{row['token']}/open"
    return _origin(cfg) + page_path(row)


def page_path(row: dict) -> str:
    """The share's own address; a file share ends in its name."""
    return f"{PREFIX}{row['token']}/" + (posixpath.basename(row["path"])
                                         if row["path"] and not row["folder"] else "")


def end_of(row: dict) -> str:
    """When it ended or ends: the user's or its end, else its end time."""
    return row["ended_at"] or row["ends_at"]


def running(row: dict) -> bool:
    return end_of(row) > iso(clock())


def on(cfg) -> bool:
    """The shares feature is on, and the shellbox it goes through."""
    return features.on(cfg, "shellbox") and features.on(cfg, "shares")


def opens(cfg, row: dict | None) -> bool:
    """/share/ answers for it: it runs and shares are on."""
    return row is not None and running(row) and on(cfg)


async def probe(cfg, services) -> dict[str, str]:
    """Settings, Features: a share goes through its box, so the shellbox feature first."""
    if not features.on(cfg, "shellbox"):
        return {"state": "unset", "detail": "The shellbox is off."}
    return {"state": "ready", "detail": "The shellbox is on."}


def down_words(port: int) -> str:
    return f"Nothing is running on port {port} in the shellbox right now."


# ---- rows ------------------------------------------------------------------------------

async def create(cfg, store, title: str, *, port: int | None = None, path: str | None = None,
                 conversation_id: str | None = None, public: bool = False,
                 hours: float | None = None) -> dict:
    """A new share of a port or a path, for `hours` (its kind's default when None). RuntimeError
    with its sentence when the box says no or the path is not there; httpx.HTTPError when
    the box does not answer."""
    _, status = await shellbox.call(cfg, "GET", "/status")
    folder = False
    if path is not None:
        async with shellbox.client(cfg, CONNECT) as c:
            r = await c.get(shellbox.url(cfg) + "/file", params={"path": path},
                            headers={"Range": "bytes=0-0"})
        folder = r.status_code == 400 and _said(r).get("folder") is True
        if r.status_code not in (200, 206, 416) and not folder:
            raise RuntimeError(_said(r).get("error") or f"No file or folder {path} in your home.")
    row = {"token": "".join(secrets.choice(TOKEN_CHARS) for _ in range(TOKEN_LEN)),
           "title": title, "port": port, "path": path, "folder": int(folder),
           "reset": str(status.get("reset") or ""), "conversation_id": conversation_id,
           "public": int(public), "secret": secrets.token_urlsafe(24),
           "created_at": iso(clock()), "ends_at": iso(clock() + timedelta(
               hours=hours or (PUBLIC_HOURS if public else PRIVATE_HOURS))),
           "ended_at": None}
    cols = ("token", "title", "port", "path", "folder", "reset", "conversation_id", "public",
            "secret", "created_at", "ends_at")

    def fn(c: sqlite3.Connection) -> None:
        c.execute(f"INSERT INTO shares({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                  tuple(row[k] for k in cols))
    await store.write(fn)
    store.emit_global({"type": SHARE_UPDATED, "token": row["token"]})
    return row


async def get(store, token: str) -> dict | None:
    def fn(c: sqlite3.Connection) -> dict | None:
        r = c.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()
        return dict(r) if r else None
    return await store.read(fn)


async def listing(store, conversation_id: str | None = None,
                  since: datetime | None = None) -> list[dict]:
    """The shares that run, or ended after `since`, newest first: one conversation's, or all."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        return [dict(r) for r in c.execute(
            "SELECT * FROM shares WHERE COALESCE(ended_at, ends_at) > ? "
            "AND (? IS NULL OR conversation_id = ?) ORDER BY created_at DESC",
            (iso(since or clock()), conversation_id, conversation_id))]
    return await store.read(fn)


async def end(store, token: str) -> bool:
    """Ends a share; False when there is none by that token that runs."""
    def fn(c: sqlite3.Connection) -> bool:
        at = iso(clock())
        return c.execute("UPDATE shares SET ended_at = ? WHERE token = ? AND ended_at IS NULL "
                         "AND ends_at > ?", (at, token, at)).rowcount > 0
    ended = await store.write(fn)
    if ended:
        store.emit_global({"type": SHARE_UPDATED, "token": token})
    return ended


async def state(cfg, store, row: dict) -> str:
    """live, down (nothing on the port, the file gone, the box silent, the shares feature
    off) or ended; a box reset since it was shared ends it here."""
    if not running(row):
        return "ended"
    if not on(cfg):
        return "down"
    if row["port"] is not None:
        got = await tunnel(cfg, row["port"])
        if got is None:
            return "down"
        got[0].close()
        reset = got[1]
    else:
        try:
            async with shellbox.client(cfg, CONNECT) as c:
                r = await c.get(shellbox.url(cfg) + "/file", params={"path": row["path"]},
                                headers={"Range": "bytes=0-0"})
        except httpx.HTTPError:
            return "down"
        reset = r.headers.get("x-shellbox-reset", "")
        there = _said(r).get("folder") is True if row["folder"] else \
            r.status_code in (200, 206, 416)
        if not there:
            return "down" if reset == row["reset"] else await _ended(store, row)
    return "live" if reset == row["reset"] else await _ended(store, row)


async def _ended(store, row: dict) -> str:
    await end(store, row["token"])
    return "ended"


def shown(row: dict, st: str) -> dict:
    """A share as the UI gets it; `url` opens it in a tab of its own."""
    return {"token": row["token"], "title": row["title"], "port": row["port"],
            "path": row["path"], "conversation_id": row["conversation_id"],
            "public": bool(row["public"]), "created_at": row["created_at"],
            "ends_at": end_of(row), "state": st,
            "url": f"/api/shares/{row['token']}/open"}


async def from_browser(cfg, client: str | None) -> bool:
    """The request comes from its own browser box: the client is an address of the browser
    service's host (a Host header proves nothing, anyone can send one)."""
    from .settings import services
    host = urlsplit(services.url(cfg, "browser")).hostname
    if not host or not client:
        return False
    try:
        got = await anyio.getaddrinfo(host, None)
    except OSError:
        return False
    return client in {a[4][0] for a in got}


def allowed(row: dict, headers, page: bool, browser: bool = False) -> bool:
    """May this request reach a running share? A public one, anyone. A private one: the
    user with its cookie, or its own browser box (from_browser). `page`: a GET of a
    page (Sec-Fetch-Dest document, or no such header)."""
    if row["public"]:
        return True
    got = cookie_parser(headers.get("cookie", "")).get(COOKIE)
    if got and secrets.compare_digest(got, row["secret"]):
        return True
    if browser:
        return True
    # note: only a page needs the cookie. The agent's page's own requests come from its opaque
    # origin, which sends no cookie at all with a module script or a fetch, so the token is
    # their key: whoever has the token gets its scripts and its API with curl, never the
    # page in a browser. A share on an origin of its own would key every request.
    return not page


def _is_page(method: str, headers) -> bool:
    return method in ("GET", "HEAD") and headers.get("sec-fetch-dest", "document") == "document"


def _said(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ---- the tunnel ------------------------------------------------------------------------

async def tunnel(cfg, port: int) -> tuple[socket.socket, str] | None:
    """A raw connection to localhost:<port> in its box (shellboxd's /forward), and the
    box's last reset; None when nothing listens there or the box does not answer."""
    # note: plain http to the box only; a box behind TLS needs a TLS socket here
    base = urlsplit(shellbox.url(cfg))
    if not base.hostname:
        return None
    hello = (f"GET /forward?port={port} HTTP/1.1\r\nHost: {base.netloc}\r\n"
             f"Authorization: Bearer {keys.read(cfg.data_dir, 'shell')}\r\n"
             "Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n").encode()

    def dial() -> tuple[socket.socket, str] | None:
        sock = socket.create_connection((base.hostname, base.port or 80), timeout=CONNECT)
        try:
            sock.sendall(hello)
            got = b""
            while b"\r\n\r\n" not in got:
                chunk = sock.recv(4096)
                if not chunk or len(got) > 65536:
                    raise OSError("no answer")
                got += chunk
            status, *lines = got.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
            if status.split()[1:2] != ["101"]:
                sock.close()
                return None
            heads = {k.strip().lower(): v.strip() for k, _, v in
                     (line.partition(":") for line in lines)}
            sock.settimeout(None)
            sock.setblocking(False)
            return sock, heads.get("x-shellbox-reset", "")
        except BaseException:
            sock.close()
            raise

    try:
        return await anyio.to_thread.run_sync(dial)
    except OSError:
        return None


class _Stream(httpcore.AsyncNetworkStream):
    """The tunnel's socket for httpcore's HTTP/1.1 connection."""

    def __init__(self, sock: socket.socket):
        self.sock = sock

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await asyncio.get_running_loop().sock_recv(self.sock, max_bytes)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await asyncio.get_running_loop().sock_sendall(self.sock, buffer)

    async def aclose(self) -> None:
        self.sock.close()


# ---- the routes ------------------------------------------------------------------------

def _page(status: int, words: str) -> Response:
    r = Response(f"{PAGE_HEAD}<title>{words}</title><p>{words}</p>".encode(), status)
    r.raw_headers = [(b"content-type", b"text/html; charset=utf-8"), *HEADERS,
                     (b"content-length", str(len(r.body)).encode())]
    return r


def _services(app):
    s = app.state.services
    return s.cfg, s.store


@router.get("/share/{token}", include_in_schema=False)
async def bare(token: str) -> Response:
    """With the slash, so its relative links resolve under it."""
    return RedirectResponse(f"{PREFIX}{token}/", 307)


@router.api_route("/share/{token}/{rest:path}", methods=METHODS, include_in_schema=False)
async def share(request: Request, token: str, rest: str) -> Response:
    cfg, store = _services(request.app)
    row = await get(store, token)
    if not opens(cfg, row) or \
            not allowed(row, request.headers, _is_page(request.method, request.headers),
                        await from_browser(cfg, request.client and request.client.host)):
        return _page(404, NOT_FOUND)
    if request.method == "OPTIONS" and "access-control-request-method" in request.headers:
        r = Response(status_code=204)
        r.raw_headers = [*HEADERS, *PREFLIGHT]
        return r
    if row["port"] is None:
        return await _file(request, cfg, store, row, rest)
    return await _forward(request, cfg, store, row, rest)


async def _forward(request: Request, cfg, store, row: dict, rest: str) -> Response:
    port = row["port"]
    got = await tunnel(cfg, port)
    if got is None:
        return _page(502, down_words(port))
    sock, reset = got
    if reset != row["reset"]:
        sock.close()
        await end(store, row["token"])
        return _page(404, NOT_FOUND)
    target = "/" + rest + (f"?{request.url.query}" if request.url.query else "")
    head = [(k, v) for k, v in request.headers.raw if k.lower() not in DROP_IN]
    head += [(b"host", f"localhost:{port}".encode()),
             (b"x-forwarded-prefix", f"{PREFIX}{row['token']}".encode())]
    if "origin" in request.headers:      # its server sees its own origin, as from a tab
        head.append((b"origin", f"http://localhost:{port}".encode()))
    conn = httpcore.AsyncHTTP11Connection(httpcore.Origin(b"http", b"localhost", port),
                                          _Stream(sock))
    try:
        resp = await conn.handle_async_request(httpcore.Request(
            request.method, httpcore.URL(scheme=b"http", host=b"localhost", port=port,
                                         target=target.encode()),
            headers=head, content=request.stream()))
    except (httpcore.ProtocolError, httpcore.NetworkError, OSError):
        await conn.aclose()
        return _page(502, down_words(port))

    async def body():
        try:
            async for chunk in resp.stream:
                yield chunk
        finally:
            await resp.aclose()
            await conn.aclose()

    out = StreamingResponse(body(), resp.status)
    out.raw_headers = [(k, v) for k, v in resp.headers if k.lower() in KEEP_OUT] + HEADERS
    return out


async def _file(request: Request, cfg, store, row: dict, rest: str) -> Response:
    if request.method not in ("GET", "HEAD"):
        return _page(405, NOT_FOUND)
    rel = row["path"]
    if row["folder"]:
        sub = posixpath.normpath("/" + rest)            # never above the folder
        rel += "" if sub == "/" else sub
        if rest == "" or rest.endswith("/"):
            rel += "/index.html"
    client = shellbox.client(cfg, None)
    try:
        r = await client.send(client.build_request(
            "GET", shellbox.url(cfg) + "/file", params={"path": rel},
            headers={"Range": request.headers["range"]} if "range" in request.headers else {}),
            stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return _page(502, BOX_DOWN)
    if r.headers.get("x-shellbox-reset", "") != row["reset"] or \
            r.status_code not in (200, 206, 416):
        await r.aread()
        await client.aclose()
        if r.headers.get("x-shellbox-reset", "") != row["reset"]:
            await end(store, row["token"])
        elif r.status_code == 400 and _said(r).get("folder") is True:
            return RedirectResponse(request.url.path + "/", 307)
        return _page(404, NOT_FOUND)
    kind = mimetypes.guess_type(rel)[0] or "application/octet-stream"
    if kind.startswith("text/") or kind in ("application/javascript", "application/json"):
        kind += "; charset=utf-8"
    head = [(k.encode(), r.headers[k].encode()) for k in
            ("content-length", "content-range", "accept-ranges") if k in r.headers]

    async def body():
        try:
            if request.method == "GET":
                async for chunk in r.aiter_raw():
                    yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    out = StreamingResponse(body(), r.status_code)
    out.raw_headers = [(b"content-type", kind.encode()), *head, *HEADERS]
    return out


@router.websocket("/share/{token}/{rest:path}")
async def share_socket(ws: WebSocket, token: str, rest: str) -> None:
    """A WebSocket to its server (a game, a dev server's live reload), message by
    message both ways until either side closes."""
    cfg, store = _services(ws.app)
    row = await get(store, token)
    got = await tunnel(cfg, row["port"]) if opens(cfg, row) and row["port"] and \
        allowed(row, ws.headers, False, await from_browser(cfg, ws.client and ws.client.host)) \
        else None
    if got is None:
        return await ws.close(1008)
    sock, reset = got
    if reset != row["reset"]:
        sock.close()
        await end(store, token)
        return await ws.close(1008)
    query = ws.scope.get("query_string", b"").decode()
    target = "/" + rest + (f"?{query}" if query else "")
    try:
        up = await ws_connect(f"ws://localhost:{row['port']}{target}", sock=sock,
                              origin=f"http://localhost:{row['port']}",
                              subprotocols=ws.scope.get("subprotocols") or None,
                              compression=None, ping_interval=None, max_size=None,
                              open_timeout=CONNECT)
    except Exception:            # its server said no, or is no WebSocket server
        sock.close()
        return await ws.close(1011)
    await ws.accept(subprotocol=up.subprotocol)

    async def down() -> None:
        async for msg in up:
            await (ws.send_text(msg) if isinstance(msg, str) else ws.send_bytes(msg))

    async def upward() -> None:
        while (m := await ws.receive())["type"] != "websocket.disconnect":
            await up.send(m["text"] if m.get("text") is not None else m.get("bytes") or b"")

    async with up, anyio.create_task_group() as tg:
        async def one(fn) -> None:
            try:
                await fn()
            except Exception:
                pass
            tg.cancel_scope.cancel()
        tg.start_soon(one, down)
        tg.start_soon(one, upward)
    try:
        await ws.close()
    except RuntimeError:
        pass                     # the browser already went


@router.get("/api/shares")
async def share_list(request: Request, conversation: str | None = None) -> dict:
    """The shares that run, newest first, each with its state: live or down."""
    cfg, store = _services(request.app)
    rows = await listing(store, conversation)
    states = await asyncio.gather(*(state(cfg, store, r) for r in rows))
    return {"shares": [shown(r, s) for r, s in zip(rows, states) if s != "ended"]}


@router.get("/api/shares/{token}")
async def share_one(request: Request, token: str) -> dict:
    cfg, store = _services(request.app)
    row = await get(store, token)
    if row is None:
        raise NotFound("share")
    return shown(row, await state(cfg, store, row))


@router.get("/api/shares/{token}/open", include_in_schema=False)
async def share_open(request: Request, token: str) -> Response:
    """Behind the login: the share's cookie, until its end, and on to its page."""
    cfg, store = _services(request.app)
    row = await get(store, token)
    if not opens(cfg, row):
        return _page(404, NOT_FOUND)
    out = RedirectResponse(page_path(row), 303)
    left = datetime.fromisoformat(row["ends_at"]) - clock()
    # Lax: its page's links and a reload are top-level navigations from its opaque origin
    out.set_cookie(COOKIE, row["secret"], max_age=max(1, int(left.total_seconds())),
                   path=f"{PREFIX}{token}/", httponly=True, samesite="lax",
                   secure=_origin(cfg).startswith("https:"))
    return out


@router.post("/api/shares/{token}/stop")
async def share_stop(request: Request, token: str) -> dict:
    """The user ends a share for everyone, it too."""
    _, store = _services(request.app)
    if await get(store, token) is None:
        raise NotFound("share")
    await end(store, token)
    return shown(await get(store, token), "ended")
