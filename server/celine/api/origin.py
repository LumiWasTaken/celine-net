"""Origin and Host checking, applied to every route but the model proxy (/llm/v1/, its
own key) and to the WebSocket.

Allowed: loopback, and with the login on the app's stored address (settings/address.py)
and [server] allowed_hosts. While no address is stored, a login or setup request (the
open POST routes of auth/routes.py) whose Origin names its own Host passes too: that
request's success stores its origin (routes.py `_signed_in`). Every other request from
elsewhere is refused until then."""
from __future__ import annotations

from typing import Callable
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from ..errors import ForbiddenOrigin
from ..settings import address
from .deps import LLM_PROXY

# Cross-origin POSTs with simple content types skip preflight and WebSocket
# handshakes are not subject to CORS, so any open tab could drive this API.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# the login and the setup: the only requests a browser at an unknown address may send
_LEARN = ("/api/auth/login", "/api/auth/register/")

# with the login on: the stored address and [server] allowed_hosts. With the login off
# the server is loopback only and so is this.
_conf: dict = {"stored": lambda: "", "hosts": frozenset()}


def configure(server: dict, login_on: bool, stored: Callable[[], str] = lambda: "") -> None:
    _conf["stored"] = stored if login_on else (lambda: "")
    _conf["hosts"] = frozenset(str(h) for h in (server.get("allowed_hosts") or [])
                               if login_on and h)


def allowed_origins(port: int, dev: bool) -> set[str]:
    out = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }
    if dev:
        for p in (5174, 4173):
            out |= {f"http://127.0.0.1:{p}", f"http://localhost:{p}"}
    return out


def _bare(host_header: str) -> str:
    return host_header.rsplit(":", 1)[0] if not host_header.startswith("[") \
        else host_header.split("]")[0] + "]"


def host_ok(host_header: str) -> bool:
    if not host_header:
        return False
    host = _bare(host_header).lower()
    stored = _conf["stored"]()
    return (host in _LOOPBACK_HOSTS or host in _conf["hosts"]
            or (bool(stored) and host.strip("[]") == urlsplit(stored).hostname))


def _origin_ok(origin: str, allowed) -> bool:
    return origin in allowed or origin == _conf["stored"]()


def check(request_headers, method: str, allowed, path: str = "") -> None:
    """Raises ForbiddenOrigin. Shared by the HTTP middleware and the WS route."""
    if (method.upper() == "POST" and path.startswith(_LEARN) and not _conf["stored"]()
            and address.same_origin(request_headers)):
        return                  # the learning window: a login or setup from its own page
    host = request_headers.get("host", "")
    if not host_ok(host):
        raise ForbiddenOrigin(host or "<no host>")

    origin = request_headers.get("origin")
    if origin is None:
        # curl sends no Origin; reads are fine, writes must say where they came from,
        # except with a named token (a browser cannot add that header cross-site)
        if method.upper() in _SAFE_METHODS or request_headers.get(
                "authorization", "").lower().startswith("bearer "):
            return
        raise ForbiddenOrigin("<none>")

    if not _origin_ok(origin, allowed):
        raise ForbiddenOrigin(origin)


def ws_origin_ok(headers, allowed) -> bool:
    origin = headers.get("origin")
    if origin is None:
        return host_ok(headers.get("host", ""))
    return _origin_ok(origin, allowed) and host_ok(headers.get("host", ""))


def install(app, port: int, dev: bool, log, server: dict | None = None,
            login_on: bool = False, stored: Callable[[], str] = lambda: "") -> None:
    configure(server or {}, login_on, stored)
    allowed = allowed_origins(port, dev)
    app.state.allowed_origins = allowed

    @app.middleware("http")
    async def _origin_guard(request: Request, call_next):
        # the model proxy is called by its shellbox at the app's service name, with a key
        # of its own and never a cookie (api/llm_proxy.py)
        if request.url.path.startswith(LLM_PROXY):
            return await call_next(request)
        # a share is opened from an opaque origin (its own sandbox) and by its browser at
        # the app's service name; its token is its key, it carries no session (shares.py)
        if request.url.path.startswith("/share/"):
            return await call_next(request)
        try:
            check(request.headers, request.method, allowed, request.url.path)
        except ForbiddenOrigin as e:
            log.warning("rejected request", extra={"event": "origin.rejected",
                                                   "origin": e.message})
            return JSONResponse(e.as_dict(), status_code=403)
        return await call_next(request)
