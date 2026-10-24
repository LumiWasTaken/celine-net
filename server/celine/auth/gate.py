"""The session gate: an ASGI middleware around the whole app, HTTP and WebSocket.

Without a valid session cookie every request gets `not_found(path)`, the same answer
an unknown /api route gives, so nothing tells which routes exist. The only exceptions
are the login screen (GET /), the login endpoints it calls (routes.open_router), and any /api route or /ws
socket with a named token (intercom/tokens.py, made in Settings) in `Authorization:
Bearer`: the app is usable through the API alone (a host script, a CLI); while it is
frozen such a token only reads (GET), any other method is 403 `frozen`. A WebSocket
without either is refused in the handshake with that same 404. The model proxy for its
shellbox (/llm/v1/, api/llm_proxy.py) checks its own key and passes the gate, and so does
a share (/share/<token>/, shares.py), whose token is its key: the session is never looked
at there.

The login endpoints are reached without a session, so their request bodies are read
here first and refused above OPEN_BODY_MAX bytes (a WebAuthn answer is a few KB).

With a valid session the request goes on (the session is in `request.state.session`)
and, at most once an hour, an HTTP request slides the expiry forward and gets the
cookie again.

A request that carries a forwarded client address the server did not take (uvicorn
takes X-Forwarded-For from CELINE_TRUSTED_PROXIES only, and nothing else) is logged
once: behind a proxy that is not trusted every client is the proxy's one address, so
one client's wrong PINs pause everyone's.
"""
from __future__ import annotations

import json
import logging

from starlette.requests import cookie_parser
from starlette.responses import JSONResponse, Response

from ..api.deps import LLM_PROXY
from ..shares import PREFIX as SHARE
from ..intercom import tokens
from . import COOKIE, Auth
from .page import login_page
from .routes import open_router

OPEN = {(m, r.path) for r in open_router.routes for m in r.methods}
OPEN_BODY_MAX = 64 * 1024
FORWARDED = (b"x-forwarded-for", b"forwarded", b"x-real-ip")

log = logging.getLogger("celine.auth")


def not_found(path: str) -> JSONResponse:
    """What an unknown route answers. The gate answers exactly this, too."""
    return JSONResponse({"error": {"code": "not_found", "message": path}}, status_code=404)


def cookie_value(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Parsed like request.cookies, so another site's odd cookie on the same domain
    cannot hide this one."""
    for k, v in headers:
        if k == b"cookie":
            value = cookie_parser(v.decode("latin-1")).get(COOKIE)
            if value:
                return value
    return None


class Gate:
    def __init__(self, app, auth: Auth):
        self.app = app
        self.auth = auth
        self.proxy_noted = False

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if not self.proxy_noted:
            self.proxy_noted = _untrusted_proxy(scope)
        path = scope.get("path", "")
        if path.startswith(SHARE):          # its token is its key, never the cookie
            return await self.app(scope, receive, send)
        store = self.auth.store
        session = store.session(cookie_value(scope.get("headers") or []))
        if session is not None:
            scope.setdefault("state", {})["session"] = session
            # only where the new cookie can be sent, so the browser's copy and the
            # stored expiry move together
            if kind == "http" and store.needs_refresh(session):
                send = self._with_cookie(send, scope, store.refresh(session))
            return await self.app(scope, receive, send)

        if kind == "websocket":
            return await self._deny_socket(scope, receive, send, path)
        method = scope.get("method", "GET")
        if method == "GET" and path == "/":
            page = login_page(not store.has_credentials(), store.methods(),
                              self.auth.settings.origin)
            return await page(scope, receive, send)
        if (method, path) in OPEN:
            body = await _small_body(receive)
            if body is None:
                return await JSONResponse(
                    {"error": {"code": "too_large", "message": "The request is too large."}},
                    status_code=413)(scope, receive, send)
            return await self.app(scope, _replay(body, receive), send)
        if path.startswith(LLM_PROXY):
            return await self.app(scope, receive, send)
        if await _named_token(scope, path):
            if method not in ("GET", "HEAD") and await _frozen(scope):
                return await JSONResponse({"error": {
                    "code": "frozen", "message": "Frozen: a token only reads until "
                    "you unfreeze the agent."}}, status_code=403)(
                    scope, receive, send)
            return await self.app(scope, receive, send)
        return await not_found(path)(scope, receive, send)

    def _with_cookie(self, send, scope, expires: int):
        token = cookie_value(scope.get("headers") or [])
        header = Response()
        header.set_cookie(**self.auth.cookie(token, max(0, expires - self.auth.store.clock())))
        set_cookie = [(k, v) for k, v in header.raw_headers if k == b"set-cookie"]

        async def wrapped(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *set_cookie]}
            await send(message)
        return wrapped

    async def _deny_socket(self, scope, receive, send, path: str):
        msg = await receive()                        # websocket.connect
        if msg.get("type") != "websocket.connect":
            return
        body = json.dumps({"error": {"code": "not_found", "message": path}},
                          separators=(",", ":")).encode()
        if "websocket.http.response" in (scope.get("extensions") or {}):
            await send({"type": "websocket.http.response.start", "status": 404,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "websocket.http.response.body", "body": body})
        else:
            await send({"type": "websocket.close", "code": 1008})


def _untrusted_proxy(scope) -> bool:
    """True, and the warning logged, when the request names a forwarded client the server
    did not take: a header from FORWARDED, and the peer is not a client X-Forwarded-For
    names (which it is once uvicorn took the address from a trusted proxy)."""
    headers = dict((k, v) for k, v in scope.get("headers") or [] if k in FORWARDED)
    if not headers:
        return False
    peer = (scope.get("client") or ("", 0))[0]
    named = headers.get(b"x-forwarded-for", b"").decode("latin-1")
    if peer and peer in {a.strip() for a in named.split(",")}:
        return False
    which = ", ".join(k.decode() for k in FORWARDED if k in headers)
    log.warning(f"Requests from {peer} carry {which}, which the server does not take: "
                f"it takes a client address only from X-Forwarded-For sent by an address "
                f"in CELINE_TRUSTED_PROXIES. Until then every client behind {peer} counts "
                f"as {peer}, for the login limit too (docs/DEPLOY.md).",
                extra={"event": "auth.proxy_not_trusted", "client": peer})
    return True


async def _named_token(scope, path: str) -> bool:
    """A valid named token on the intercom routes, nothing else: a host script posts and
    reads there (intercom/tokens.py). Every other route and every socket needs a session."""
    if path != "/api/intercom" and not path.startswith("/api/intercom/"):
        return False
    token = tokens.bearer(scope.get("headers") or [])
    store = scope["app"].state.services.store if "app" in scope else None
    if token is None or store is None:
        return False
    return await store.read(lambda c: tokens.name_of(c, token)) is not None


async def _frozen(scope) -> bool:
    """Is it frozen (guard/freeze.py)? A named token then reads, it does nothing."""
    from ..guard.freeze import frozen
    return await frozen(scope["app"].state.services.store) is not None


async def _small_body(receive) -> bytes | None:
    """The whole request body, or None when it is over OPEN_BODY_MAX or the client left."""
    body = b""
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            return None
        body += msg.get("body", b"")
        if len(body) > OPEN_BODY_MAX:
            return None
        if not msg.get("more_body"):
            return body


def _replay(body: bytes, receive):
    sent = False

    async def again():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()
    return again
