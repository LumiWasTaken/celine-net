"""Origin and Host checking, applied to every route but the model proxy (/llm/v1/, its
own key) and to the WebSocket."""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from ..errors import ForbiddenOrigin
from .deps import LLM_PROXY

# Cross-origin POSTs with simple content types skip preflight and WebSocket
# handshakes are not subject to CORS, so any open tab could drive this API.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# widened only when the login is on (celine.auth): the public origin's host, and any
# [server] allowed_hosts. With the login off the server is loopback only and so is this set.
_EXTRA_HOSTS: set[str] = set()


def configure(server: dict, login_on: bool) -> set[str]:
    """The origin and host from [server] public_origin, and the hosts in
    [server] allowed_hosts. Only honoured with the login on; returns the extra allowed
    origin (none or one)."""
    _EXTRA_HOSTS.clear()
    if not login_on:
        return set()
    origins: set[str] = set()
    pub = str(server.get("public_origin") or "").rstrip("/")
    if pub:
        origins.add(pub)
        _EXTRA_HOSTS.add(urlsplit(pub).hostname or "")
    for h in server.get("allowed_hosts", []) or []:
        _EXTRA_HOSTS.add(str(h))
    _EXTRA_HOSTS.discard("")
    return origins


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


def _host_ok(host_header: str) -> bool:
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") \
        else host_header.split("]")[0] + "]"
    return host in _LOOPBACK_HOSTS or host in _EXTRA_HOSTS


def check(request_headers, method: str, allowed: set[str]) -> None:
    """Raises ForbiddenOrigin. Shared by the HTTP middleware and the WS route."""
    host = request_headers.get("host", "")
    if not _host_ok(host):
        raise ForbiddenOrigin(host or "<no host>")

    origin = request_headers.get("origin")
    if origin is None:
        # curl sends no Origin; reads are fine, writes must say where they came from,
        # except with a named token (a browser cannot add that header cross-site)
        if method.upper() in _SAFE_METHODS or request_headers.get(
                "authorization", "").lower().startswith("bearer "):
            return
        raise ForbiddenOrigin("<none>")

    if origin not in allowed:
        raise ForbiddenOrigin(origin)


def ws_origin_ok(headers, allowed: set[str]) -> bool:
    origin = headers.get("origin")
    if origin is None:
        return _host_ok(headers.get("host", ""))
    return origin in allowed and _host_ok(headers.get("host", ""))


def install(app, port: int, dev: bool, log, server: dict | None = None,
            login_on: bool = False) -> None:
    allowed = allowed_origins(port, dev) | configure(server or {}, login_on)
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
            check(request.headers, request.method, allowed)
        except ForbiddenOrigin as e:
            log.warning("rejected request", extra={"event": "origin.rejected",
                                                   "origin": e.message})
            return JSONResponse(e.as_dict(), status_code=403)
        return await call_next(request)
