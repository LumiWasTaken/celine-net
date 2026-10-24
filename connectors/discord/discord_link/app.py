"""The doors, on one port, each behind a bearer key (the clients file):

    POST /mcp            Celine's tools (tools.py)
    GET  /feed?after=N   the journal as NDJSON from seq N, then live; an empty line
                         every 15 s while idle
    /admin/...           status, info, login, logout, chats included or left out,
                         server channels, subscriptions, sync, jobs, media

An unknown key gets 401. A reader key gets 403 on anything that changes the account
or its lists.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import policy, sync, tools
from .errors import Failed, Unreachable
from .link import Client, Link, Refused
from .state import chat_of

HEARTBEAT = 15.0
POLL = 0.5
_SHA = re.compile(r"^[0-9a-f]{64}$")


def _err(status: int, text: str) -> JSONResponse:
    return JSONResponse({"error": text}, status_code=status)


class Auth:
    """Every request needs a known bearer key."""

    def __init__(self, app, link: Link):
        self.app = app
        self.link = link

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if self.link.client(headers.get("authorization")) is None:
                await _err(401, "Unknown key.")(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def feed_lines(link: Link, after: int, *, heartbeat: float = HEARTBEAT,
                     poll: float = POLL) -> AsyncIterator[str]:
    """The journal from `after`, then live. Records of a chat the user left out are
    skipped, so no app instance archives it again."""
    seq = after
    idle = 0.0
    while True:
        rows = await asyncio.to_thread(link.state.after, seq, 500)
        if rows:
            idle = 0.0
            ignored = await asyncio.to_thread(link.state.ignored)
            for r in rows:
                seq = r["seq"]
                if chat_of(r) in ignored:
                    continue
                yield json.dumps(r, ensure_ascii=False) + "\n"
            continue
        await asyncio.sleep(poll)
        idle += poll
        if idle >= heartbeat:
            idle = 0.0
            yield "\n"


def build(link: Link):
    mcp = tools.build(link)

    def client(request: Request) -> Client:
        return link.client(request.headers.get("authorization"))   # Auth made sure

    def owner(request: Request) -> Client:
        c = client(request)
        link.need_owner(c)
        return c

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def route(path: str, methods: list[str]):
        def wrap(fn):
            async def handler(request: Request) -> Response:
                try:
                    return await fn(request)
                except Refused as e:
                    return _err(403, str(e))
                except policy.SettingsError as e:
                    return _err(400, str(e))
            mcp.custom_route(path, methods=methods)(handler)
            return fn
        return wrap

    @route("/feed", ["GET"])
    async def feed(request: Request) -> Response:
        try:
            after = max(0, int(request.query_params.get("after", "0")))
        except ValueError:
            return _err(400, "after is a number.")
        return StreamingResponse(feed_lines(link, after), media_type="application/x-ndjson")

    @route("/admin/status", ["GET"])
    async def status(request: Request) -> Response:
        return JSONResponse(link.status())

    @route("/admin/info", ["GET"])
    async def info(request: Request) -> Response:
        return JSONResponse(link.info())

    @route("/admin/login", ["GET", "POST"])
    async def login(request: Request) -> Response:
        if request.method == "POST":
            owner(request)
            return JSONResponse(link.logins.start().as_dict())
        s = link.logins.current()
        return JSONResponse({"login": s.as_dict() if s else None})

    @route("/admin/login/{sid}", ["DELETE"])
    async def login_cancel(request: Request) -> Response:
        owner(request)
        return JSONResponse({"ok": link.logins.cancel(request.path_params["sid"])})

    @route("/admin/logout", ["POST"])
    async def logout(request: Request) -> Response:
        owner(request)
        return JSONResponse({"ok": await link.logout()})

    @route("/admin/token", ["PUT"])
    async def token(request: Request) -> Response:
        """PUT {"token": "..."}: store a token the user pasted, instead of a QR login."""
        owner(request)
        t = (await body(request)).get("token")
        if not isinstance(t, str) or not t.strip() or len(t.strip()) > 200 or \
                any(ch.isspace() for ch in t.strip()):
            return _err(400, "The token is one word without spaces.")
        await link.set_token(t.strip())
        return JSONResponse({"ok": True})

    @route("/admin/targets", ["GET", "PUT"])
    async def targets(request: Request) -> Response:
        """PUT {"set": {chat id: true | false}}: include or leave out DMs, group DMs,
        server channels and threads. A chat left out is never read again; one included
        again is read from the start at once (`job`)."""
        if request.method == "PUT":
            owner(request)
            changes = (await body(request)).get("set") or {}
            if not isinstance(changes, dict) or \
                    not all(isinstance(v, bool) for v in changes.values()):
                return _err(400, "set is an object of chat id to true or false.")
            n, back = await asyncio.to_thread(sync.set_targets, link.state, changes)
            link.reload_subs()
            job = link.start_chats(back).id if back else None
            return JSONResponse({"changed": n, "job": job})
        return JSONResponse(await asyncio.to_thread(sync.targets, link.state))

    @route("/admin/guilds/{guild}/channels", ["GET"])
    async def guild_channels(request: Request) -> Response:
        g = request.path_params["guild"]
        if not g.isdigit():
            return _err(400, "The server id is digits.")
        try:
            chans = await asyncio.to_thread(sync.guild_channels, link.rest, g)
            return JSONResponse({"channels": chans, "error": None})
        except (PermissionError, FileNotFoundError):
            return JSONResponse({"channels": [], "error": "Discord does not let this "
                                                          "account list that server's channels."})
        except Exception as e:           # Unreachable, a rejected login: one sentence
            return JSONResponse({"channels": [], "error": str(e)})

    @route("/admin/subscriptions", ["GET"])
    async def subscriptions(request: Request) -> Response:
        subs = await asyncio.to_thread(policy.list_all, link.state)
        return JSONResponse({"subscriptions": subs, "defaults": policy.DEFAULTS})

    @route("/admin/subscriptions/{guild}", ["PUT", "DELETE"])
    async def subscription(request: Request) -> Response:
        owner(request)
        g = request.path_params["guild"]
        if not g.isdigit():
            return _err(400, "The server id is digits.")
        if request.method == "DELETE":
            ok = await asyncio.to_thread(policy.unsubscribe, link.state, g)
            link.reload_subs()
            return JSONResponse({"ok": ok}, status_code=200 if ok else 404)
        b = await body(request)
        if b.get("enabled") is True:
            return _err(400, "Start a subscription with POST .../start.")
        sub, before = await asyncio.to_thread(lambda: policy.put(
            link.state, g, settings=b.get("settings"), guild_name=b.get("guild_name"),
            icon=b.get("icon"), pause=b.get("enabled") is False))
        link.reload_subs()
        job = None
        if before is not None:
            job = link.start_channels(g, before, f"New channels in {sub['guild_name'] or g}").id
        return JSONResponse({"subscription": sub, "job": job})

    @route("/admin/subscriptions/{guild}/start", ["POST"])
    async def subscription_start(request: Request) -> Response:
        owner(request)
        g = request.path_params["guild"]
        if not g.isdigit():
            return _err(400, "The server id is digits.")
        b = await body(request)
        await asyncio.to_thread(lambda: policy.put(
            link.state, g, settings=b.get("settings"), guild_name=b.get("guild_name"),
            icon=b.get("icon")))
        sub, newly = await asyncio.to_thread(policy.start, link.state, g)
        link.reload_subs()
        job = link.start_backfill(g, f"Discord server {sub['guild_name'] or g}").id \
            if newly else None
        return JSONResponse({"subscription": sub, "job": job})

    @route("/admin/sync", ["POST"])
    async def sync_now(request: Request) -> Response:
        owner(request)
        return JSONResponse({"job": link.start_sync().id})

    @route("/admin/jobs/{jid}", ["GET", "DELETE"])
    async def job(request: Request) -> Response:
        jid = request.path_params["jid"]
        if request.method == "DELETE":
            owner(request)
            return JSONResponse({"ok": link.jobs.cancel(jid)})
        j = link.jobs.get(jid)
        return JSONResponse(j.as_dict()) if j else _err(404, "No such job.")

    @route("/admin/media/refresh", ["POST"])
    async def media_refresh(request: Request) -> Response:
        """Read one message again (its attachment links expire) and download its
        files into the media folder: {"chat", "message", "max_mb"}."""
        owner(request)
        b = await body(request)
        chat, mid = str(b.get("chat") or ""), str(b.get("message") or "")
        if not chat.isdigit() or not mid.isdigit():
            return _err(400, "chat and message are Discord ids.")
        max_mb = b.get("max_mb", policy.ATTACHMENT_MAX_MB)
        if isinstance(max_mb, bool) or not isinstance(max_mb, (int, float)) or max_mb <= 0:
            return _err(400, "max_mb is a number of megabytes.")
        try:
            out = await asyncio.to_thread(sync.refresh_files, link.rest, chat, mid,
                                          float(max_mb))
        except (Unreachable, Failed) as e:
            return _err(503 if isinstance(e, Unreachable) else 502, str(e))
        return JSONResponse(out)

    @route("/admin/media/{sha}", ["GET"])
    async def media(request: Request) -> Response:
        sha = request.path_params["sha"]
        p = link.state.media / sha
        if not _SHA.match(sha) or not p.is_file():
            return _err(404, "No such file.")
        return FileResponse(p)

    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return Auth(app, link)
