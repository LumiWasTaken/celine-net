"""What every route module needs: the request's JSON object, the loaded settings, the
ETag header, the logged-in-person and fresh-proof checks and a live view's socket loop.
Packages import these from here, never from another route module."""
from __future__ import annotations

import json
from typing import Any

import anyio
from fastapi import Request, Response, WebSocket, WebSocketDisconnect

from ..errors import CelineError
from ..settings.files import Invalid

# the model proxy for its shellbox (api/llm_proxy.py): it checks its own key, the gate
# (auth/gate.py) lets it through
LLM_PROXY = "/llm/v1/"


def settings_of(request: Request):
    s = getattr(request.app.state.services.cfg, "settings", None)
    if s is None:
        raise CelineError("unavailable", "Settings are not loaded.", 503)
    return s


def etag(response: Response, v: str) -> str:
    response.headers["ETag"] = f'"{v}"'
    return v


async def json_body(request: Request, *, need: tuple[str, ...] = (),
                    allow: tuple[str, ...] | None = None) -> dict[str, Any]:
    """The JSON object of a request. Never echoes what was sent (it may hold a key)."""
    try:
        data = await request.json()
    except Exception:
        raise Invalid("body", "The request body is not JSON.") from None
    if not isinstance(data, dict):
        raise Invalid("body", "The request body must be a JSON object.")
    for k in need:
        if k not in data:
            raise Invalid(k, f"'{k}' is missing.")
    if allow is not None:
        for k in data:
            if k not in allow:
                raise Invalid(k, f"'{k}' is not expected here.")
    return data


def person_only(request: Request) -> None:
    """A logged-in person, not a named token (auth/gate.py sets the session). Without a
    session, a request that carries a token or a key is refused even with the login off;
    a session counts whatever headers a proxy adds."""
    if getattr(request.state, "session", None) is not None:
        return
    h = request.headers
    if h.get("authorization") or h.get("x-api-key") or \
            getattr(request.app.state, "auth", None) is not None:
        raise CelineError("passkey_only", "Only a logged-in session can do this, not a "
                          "token.", 403)


def proven(request: Request) -> None:
    """A proof from the last minutes in this session (auth/routes.py, /api/auth/reauth)
    before adding a way to log in, new recovery codes or a named token. With the login
    off there is nothing to prove."""
    auth = getattr(request.app.state, "auth", None)
    if auth is None:
        return
    s = getattr(request.state, "session", None)
    if s is None or not auth.store.proven(s):
        raise CelineError("proof_needed", "Confirm access first.", 403)


def services_of(request: Request):
    return request.app.state.services


def cfg_of(request: Request):
    return request.app.state.services.cfg


def store_of(request: Request):
    return request.app.state.services.store


def in_thread(fn, *args, **kw):
    """`fn(*args, **kw)` in a worker thread (a kb file's sync calls)."""
    return anyio.to_thread.run_sync(lambda: fn(*args, **kw))


# ---- live views over a socket (the browser's screen, a terminal) --------------------

GONE = (OSError, RuntimeError, WebSocketDisconnect, anyio.BrokenResourceError,
        anyio.ClosedResourceError)


async def page_frames(ws: WebSocket, on_bytes) -> bool:
    """A live view's socket (the browser's screen, a terminal) until one side leaves:
    each binary frame of the page to `on_bytes`, which returns None to go on or the
    answer; a ping answered with pong. True when the page left or this side closed the
    socket."""
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            return True
        if msg.get("bytes") is not None:
            done = await on_bytes(msg["bytes"])
            if done is not None:
                return done
            continue
        try:
            text = json.loads(msg.get("text") or "")
        except ValueError:
            continue
        if isinstance(text, dict) and text.get("type") == "ping":
            try:
                await ws.send_json({"type": "pong"})
            except RuntimeError:
                return True
