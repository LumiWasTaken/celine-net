"""The source pages' routes for Discord and WhatsApp. The accounts live in the
connectors (discord-link, wa-link); these routes forward to their `/admin/*` doors and
add what only the app knows (the archive, the bridge switch, tasks). Delete and ignore
(kb/ignore.py) is here too, for every chat source."""
from __future__ import annotations

import logging
import re
from typing import Any

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ..errors import CelineError
from ..kb.connectors import Refused
from ..kb.ignore import delete_and_ignore, include_again
from ..kb.importers import Unreachable
from .deps import in_thread
from .kb import chat_kb, kb_changed

router = APIRouter(prefix="/api")
log = logging.getLogger("celine.connectors")


def not_set_up(request: Request, sid: str) -> CelineError:
    """A source with no connection: the sentence names the source, the log says why."""
    kb = request.app.state.services.kbs.get(sid)
    log.warning("This source has no connection. Add a server block with kb = %r to "
                "config/mcp.toml.", sid, extra={"event": "connector.missing", "kb": sid})
    return CelineError("not_found", f"The {getattr(kb, 'title', None) or sid} connection "
                                    f"is not set up.", 404)


def connector(request: Request, sid: str, platform: str | None = None):
    connectors = request.app.state.services.connectors
    c = connectors.get(sid) if connectors is not None else None
    if c is None:
        raise not_set_up(request, sid)
    if platform is not None and c.kb.platform != platform:
        raise CelineError("bad_request", f"{sid} is not a {platform.title()} source.", 400)
    return c


async def _admin(c, method: str, path: str, body: Any = None) -> Any:
    try:
        return await c.admin(method, path, body)
    except Unreachable as e:
        raise CelineError("unreachable", str(e), 503) from None
    except Refused as e:
        raise CelineError("connector", str(e),
                          e.code if e.code in (400, 403, 404, 409) else 502) from None


LOGIN_WAIT_SECONDS = 8.0       # a QR login answers once Discord sent the code, or then
LOGIN_POLL_SECONDS = 0.2
_LOGIN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _guild(guild: str) -> str:
    """A Discord server id (a number), before it goes into a connector's path."""
    if not guild.isascii() or not guild.isdigit() or len(guild) > 25:
        raise CelineError("bad_request", f"{guild!r} is not a Discord server id.", 400)
    return guild


# ---- both --------------------------------------------------------------------------------

@router.get("/connectors")
async def connectors_list(request: Request) -> dict:
    """Every connector's connection record and lost login, for the source list and the
    app's banner. From the last check (every few seconds); asks no connector."""
    connectors = request.app.state.services.connectors
    return {"connectors": connectors.states() if connectors is not None else []}


@router.get("/sources/{sid}/connector")
async def connector_state(request: Request, sid: str) -> dict:
    """The connection record, the account, the login in progress (`login`) or the code
    to scan (`qr_at`, the code at GET .../connector/qr.png), and the bridge when the
    connector has a chat it writes to (`write_chat` in its /admin/info): that chat, its
    marker, this instance's switch. Null when it has none."""
    c = connector(request, sid)
    await c.poll()
    st = c.status or {}
    info = c.info or {}
    out: dict[str, Any] = {
        "platform": c.kb.platform, "connection": c.connection(), "account": st.get("account"),
        "login_lost_at": c.lost_at, "login": st.get("login"), "qr_at": st.get("qr_at"),
        "bridge": None,
    }
    if "write_chat" in info:
        out["bridge"] = {"chat": info["write_chat"], "chat_name": info.get("write_chat_name"),
                         "marker": info.get("marker"),
                         **request.app.state.services.bridge.state(sid)}
        if out["connection"]["state"] != "connected":
            out["bridge"]["error"] = None         # the connection's own line says why
    return out


class BridgeBody(BaseModel):
    on: bool | None = None
    marker: str | None = None           # what its messages start with


@router.put("/sources/{sid}/bridge")
async def bridge_set(request: Request, sid: str, body: BridgeBody) -> dict:
    """Switch the bridge on or off for this instance, or set the marker its messages
    start with in the connector."""
    c = connector(request, sid)
    bridge = request.app.state.services.bridge
    if body.marker is not None:
        await _admin(c, "PUT", "/admin/bridge", {"marker": body.marker})
        await anyio.to_thread.run_sync(c.own_marker, body.marker,
                                       request.app.state.services.cfg.name)
        await c.poll()
    if body.on is not None:
        if body.on:
            await c.poll()
            if not (c.info or {}).get("write_chat"):
                raise CelineError("bad_request",
                                  f"The {c.title} connection has no bridged chat.", 400)
        await anyio.to_thread.run_sync(bridge.set_on, sid, body.on)
        await bridge.lease(bridge.sides[sid])
    return await connector_state(request, sid)


def _optional_connector(request: Request, sid: str):
    connectors = request.app.state.services.connectors
    return connectors.get(sid) if connectors is not None else None


async def _ignoring(fn, *args):
    try:
        return await in_thread(fn, *args)
    except Refused as e:
        raise CelineError("bad_request", str(e), 400) from None
    except Unreachable as e:
        raise CelineError("unreachable", str(e), 503) from None


@router.delete("/sources/{sid}/chats/{chat_id}")
async def delete_chat(request: Request, sid: str, chat_id: int) -> dict:
    """Delete and ignore: the chat leaves the archive and no sync or import brings it
    back until it is included again (kb/ignore.py). Discord's connector stops reading
    it. The bridged chat is refused."""
    kb = chat_kb(request, sid)
    if await anyio.to_thread.run_sync(kb.get_chat, chat_id) is None:
        raise CelineError("not_found", "There is no such chat.", 404)
    out = await _ignoring(delete_and_ignore, kb, [chat_id],
                          _optional_connector(request, sid))
    kb_changed(request, sid)
    return {"deleted": out["messages"]}


@router.get("/sources/{sid}/ignored-chats")
async def ignored(request: Request, sid: str) -> dict:
    """The chats deleted and ignored: `{"chats": [{"key", "kind", "name", "at"}]}`."""
    kb = chat_kb(request, sid)
    return {"chats": await anyio.to_thread.run_sync(kb.ignored_chats)}


class IncludeBody(BaseModel):
    key: str


@router.post("/sources/{sid}/ignored-chats/include")
async def include(request: Request, sid: str, body: IncludeBody) -> dict:
    """Include an ignored chat again. Discord: the connector reads it from the start
    (task kind `sync`). WhatsApp: what comes of it from now on lands; wa-link deleted
    its history. `{"task": id|null}`."""
    kb = chat_kb(request, sid)
    c = _optional_connector(request, sid)
    job = await _ignoring(include_again, kb, body.key, c)
    task = _follow(request, sid, "sync", "Discord sync", job) if job else None
    kb_changed(request, sid)
    return {"task": task}


@router.post("/sources/{sid}/import")
async def first_import(request: Request, sid: str) -> dict:
    """The first import, from the onboarding: a visible task `import` (its progress the
    archive's message count, kb/connectors.py first_import), done once the archive has
    what the connector had. Discord starts its sync first. `{"task": id}`."""
    c = connector(request, sid)
    tasks = request.app.state.services.kbs.tasks
    running = tasks.running(sid, "import")
    if running is not None:
        return {"task": running.id}
    job = (await _admin(c, "POST", "/admin/sync"))["job"] \
        if c.kb.platform == "discord" else None
    return {"task": tasks.start(sid, "import", f"{c.title} import",
                                lambda ctx: c.first_import(job, ctx)).id}


# ---- Discord -----------------------------------------------------------------------------

@router.get("/sources/{sid}/discord/targets")
async def discord_targets(request: Request, sid: str) -> dict:
    return await _admin(connector(request, sid, "discord"), "GET", "/admin/targets")


class TargetSet(BaseModel):
    set: dict[str, bool]


@router.put("/sources/{sid}/discord/targets")
async def discord_set(request: Request, sid: str, body: TargetSet) -> dict:
    """Include (true) or leave out (false) DMs, group DMs, server channels and threads
    by their Discord id. A chat included again is read from the start at once (task
    kind `sync`); one that was deleted and ignored is no longer ignored."""
    c = connector(request, sid, "discord")
    out = await _admin(c, "PUT", "/admin/targets", {"set": body.set})
    # a deleted and ignored chat ticked again is included again
    await anyio.to_thread.run_sync(c.kb.unignore, [k for k, on in body.set.items() if on])
    task = _follow(request, sid, "sync", "Discord sync", out["job"]) if out.get("job") \
        else None
    kb_changed(request, sid)
    return {"changed": out.get("changed", 0), "task": task}


@router.get("/sources/{sid}/discord/guilds/{guild}/channels")
async def discord_channels(request: Request, sid: str, guild: str) -> dict:
    c = connector(request, sid, "discord")
    return await _admin(c, "GET", f"/admin/guilds/{_guild(guild)}/channels")


@router.post("/sources/{sid}/discord/login-sessions")
async def discord_login(request: Request, sid: str) -> dict:
    """Start a QR login in the Discord link; answers once Discord sent the code (at
    most 8 s). Poll GET .../connector for what happens next (`login`)."""
    c = connector(request, sid, "discord")
    session = await _admin(c, "POST", "/admin/login")
    with anyio.move_on_after(LOGIN_WAIT_SECONDS):
        while session.get("state") == "starting":
            await anyio.sleep(LOGIN_POLL_SECONDS)
            session = (await _admin(c, "GET", "/admin/login")).get("login") or session
    return session


@router.delete("/sources/{sid}/discord/login-sessions/{lid}")
async def discord_login_cancel(request: Request, sid: str, lid: str) -> dict:
    if not _LOGIN_ID.match(lid):
        raise CelineError("not_found", "There is no such login.", 404)
    return await _admin(connector(request, sid, "discord"), "DELETE", f"/admin/login/{lid}")


@router.post("/sources/{sid}/discord/logout")
async def discord_logout(request: Request, sid: str) -> dict:
    out = await _admin(connector(request, sid, "discord"), "POST", "/admin/logout")
    kb_changed(request, sid)
    return out


class TokenBody(BaseModel):
    token: str


@router.put("/sources/{sid}/discord/token")
async def discord_token(request: Request, sid: str, body: TokenBody) -> dict:
    """A token pasted in Settings replaces the stored one (the connector checks it)."""
    out = await _admin(connector(request, sid, "discord"), "PUT", "/admin/token",
                       {"token": body.token})
    kb_changed(request, sid)
    return out


def _follow(request: Request, sid: str, kind: str, label: str, job: str) -> str:
    """The connector's job as a task of this app (its progress, Stop, its end)."""
    c = connector(request, sid)
    tasks = request.app.state.services.kbs.tasks
    running = tasks.running(sid, kind)
    if running is not None:
        return running.id
    return tasks.start(sid, kind, label, lambda ctx: c.follow(job, ctx)).id


@router.post("/sources/{sid}/discord/sync")
async def discord_sync(request: Request, sid: str) -> dict:
    c = connector(request, sid, "discord")
    data = await _admin(c, "POST", "/admin/sync")
    return {"task": _follow(request, sid, "sync", "Discord sync", data["job"])}


@router.get("/sources/{sid}/discord/subscriptions")
async def discord_subscriptions(request: Request, sid: str) -> dict:
    return await _admin(connector(request, sid, "discord"), "GET", "/admin/subscriptions")


class SubPut(BaseModel):
    """`enabled: false` pauses a running subscription; starting is POST .../start."""
    enabled: bool | None = None
    settings: dict[str, Any] | None = None
    guild_name: str | None = None
    icon: str | None = None


def _sub_task(request: Request, sid: str, guild: str, data: dict, kind: str,
              label: str) -> dict:
    task = None
    if data.get("job"):
        task = _follow(request, sid, f"{kind}:{guild}", label, data["job"])
    kb_changed(request, sid)
    return {"subscription": data["subscription"], "task": task}


@router.put("/sources/{sid}/discord/subscriptions/{guild}")
async def discord_subscribe(request: Request, sid: str, guild: str, body: SubPut) -> dict:
    """Create or change a subscription (a new one is a draft). On a running one,
    channels ticked by this change get their history at once (task `channels:<id>`)."""
    c = connector(request, sid, "discord")
    data = await _admin(c, "PUT", f"/admin/subscriptions/{_guild(guild)}",
                        body.model_dump(exclude_none=True))
    name = data["subscription"].get("guild_name") or guild
    return _sub_task(request, sid, guild, data, "channels", f"New channels in {name}")


@router.post("/sources/{sid}/discord/subscriptions/{guild}/start")
async def discord_start(request: Request, sid: str, guild: str,
                        body: SubPut | None = None) -> dict:
    c = connector(request, sid, "discord")
    data = await _admin(c, "POST", f"/admin/subscriptions/{_guild(guild)}/start",
                        (body or SubPut()).model_dump(exclude_none=True))
    name = data["subscription"].get("guild_name") or guild
    return _sub_task(request, sid, guild, data, "subscribe", f"Discord server {name}")


@router.delete("/sources/{sid}/discord/subscriptions/{guild}")
async def discord_unsubscribe(request: Request, sid: str, guild: str) -> dict:
    out = await _admin(connector(request, sid, "discord"), "DELETE",
                       f"/admin/subscriptions/{_guild(guild)}")
    kb_changed(request, sid)
    return out


# ---- WhatsApp ----------------------------------------------------------------------------

@router.get("/sources/{sid}/connector/qr.png")
async def wa_qr(request: Request, sid: str) -> Response:
    c = connector(request, sid, "whatsapp")
    try:
        r = await c.raw("GET", "/admin/qr", timeout=30.0)
    except Unreachable as e:
        raise CelineError("unreachable", str(e), 503) from None
    if r.status_code != 200:
        raise CelineError("not_found", "There is no code to scan right now.", 404)
    return Response(r.content, media_type="image/png", headers={"Cache-Control": "no-store"})
