"""Celine's Discord tools over MCP (streamable HTTP at /mcp).

Read tools: list_chats, read_messages, read_around_message, search_messages,
list_user_messages, get_user_profile, get_server_info, list_members. Every message a
tool returns is journaled too (reading is archiving), and the result's structured
content carries those records, so the app can cite them like any archived message.
The two reads answer what the journal already holds and ask Discord only for the gaps
(spans.py).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import policy, spans, sync
from .errors import Failed, Unreachable
from .link import Link, Refused
from .records import DM, GROUP_DM, message, snowflake_at, snowflake_ms

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
SEARCH_PAGE = 25
USER_PAGES = 8


def _result(note: str, *, records: list[dict] | None = None, where: str | None = None,
            lines: list[str] | None = None, **extra: Any) -> CallToolResult:
    structured: dict[str, Any] = {"note": note}
    if where:
        structured["where"] = where
    if records is not None:
        structured["records"] = records
    structured.update({k: v for k, v in extra.items() if v is not None})
    text = "\n".join([note, *(lines or [])])
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          structured_content=structured)


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _time(v: str | None, *, end: bool = False) -> int | None:
    """A date or a date and time, in the container's time zone (TZ in .env). A bare
    date as an end means the end of that day."""
    if not v:
        return None
    s = str(v).strip()
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise Refused(f"{v!r} is not a date like 2026-09-24 or 2026-09-24T18:00.") from None
    if end and len(s) <= 10:
        d += timedelta(days=1)
    return int(d.timestamp() * 1000)


def _id(v: str | None, what: str, *, me: bool = False) -> str | None:
    """A Discord id (digits), or None when not given. `me`: "@me" is the account."""
    if v is None or v == "":
        return None
    v = str(v).strip()
    if v.isdigit() or (me and v == "@me"):
        return v
    raise Refused(f"{v!r} is not a {what} id. Discord ids are digits.")


def _lines(records: list[dict]) -> list[str]:
    out = []
    for r in records:
        m = r["message"]
        who = "me" if m.get("from_me") else (m.get("sender") or {}).get("display_name") or \
            (m.get("sender") or {}).get("username") or "someone"
        text = m.get("text") or ""
        if m.get("type") == "media":
            text = f"[{(m.get('media') or {}).get('type', 'file')}] {text}".strip()
        out.append(f"{m['key']} {_when(m['ts'])} {who}: {text}")
    return out


def _where(chat: dict[str, Any]) -> str:
    if chat["kind"] == "dm":
        p = chat.get("peer") or {}
        return f"DM with {p.get('display_name') or p.get('username') or chat.get('name') or chat['key']}"
    if chat["kind"] == "group":
        return f"group DM {chat.get('name') or chat['key']}"
    return f"{chat.get('name')} on {chat.get('parent') or 'a server'}"


def build(link: Link) -> MCPServer:
    server = MCPServer("discord-link", instructions=(
        "The user's Discord account, read only. Prefer the "
        "app's chat archive for anything already synced."))
    state = link.state
    rest = link.rest

    def me_id() -> str | None:
        return (state.get("account") or {}).get("id")

    def known(chat: str) -> dict[str, Any]:
        """The channel, asked from Discord once when the connector has not seen it. A
        chat the user left out is not read."""
        if state.is_ignored(chat):
            raise Refused("The user does not include this chat. The connection does not read it.")
        ch = state.channel(chat)
        if ch is None or ch.get("kind") is None:
            raw = rest.get(f"/channels/{chat}")
            if raw.get("type") in (DM, GROUP_DM):
                sync.learn_private(state, raw)
            else:
                gid = str(raw.get("guild_id") or "") or None
                state.learn_channel(str(raw["id"]), kind="channel", type=raw.get("type"),
                                    name=raw.get("name"), guild_id=gid,
                                    guild_name=policy.guild_name_of(state, gid) if gid else None,
                                    parent_id=str(raw["parent_id"]) if raw.get("parent_id")
                                    else None)
            ch = state.channel(chat)
        return ch

    def records_of(msgs: list[dict]) -> list[dict]:
        recs = []
        for m in sorted(msgs, key=lambda m: int(m["id"])):
            if state.is_ignored(str(m["channel_id"])):
                continue
            known(str(m["channel_id"]))
            r = message(state, m, me_id())
            if r is not None:
                recs.append(r)
        return state.journal(recs)

    async def run(fn, *a):
        try:
            return await asyncio.to_thread(fn, *a)
        except Refused as e:
            return _error(str(e))
        except (Failed, Unreachable) as e:
            return _error(str(e))
        except PermissionError:
            return _error("Discord does not let this account see that.")
        except FileNotFoundError:
            return _error("Discord does not know that chat, message, user or server.")

    # ---- chats -------------------------------------------------------------------------
    @server.tool(annotations=LOCAL)
    async def list_chats(
        kind: Annotated[str | None, Field(description="dm, group or server. Empty: all.")] = None,
        query: Annotated[str | None, Field(description="Part of a name.")] = None,
        limit: Annotated[int, Field(ge=1, le=200, description="The most chats, 1 to 200.")] = 50,
    ) -> CallToolResult:
        """List the Discord DMs, group DMs and servers of {{username}}'s account, newest
        activity first. Included servers are synced into the chat archive. No request
        goes to Discord."""
        def work():
            q = (query or "").lower().strip()
            out = []
            if kind in (None, "", "dm", "group"):
                for r in state.q("SELECT key FROM channels WHERE kind IN ('dm', 'group')"):
                    ch = state.channel(r["key"])
                    if kind in ("dm", "group") and ch["kind"] != kind:
                        continue
                    name = ch.get("name") or (ch.get("peer") or {}).get("global_name") or \
                        (ch.get("peer") or {}).get("username") or ch["key"]
                    if q and q not in name.lower():
                        continue
                    last = ch.get("last_message_id")
                    out.append({"id": ch["key"], "kind": ch["kind"], "name": name,
                                "last_activity": snowflake_ms(last) if last else None})
                out.sort(key=lambda c: -(c["last_activity"] or 0))
            if kind in (None, "", "server"):
                subs = policy.active(state)
                for g in state.get("guilds", []) or []:
                    if q and q not in (g.get("name") or "").lower():
                        continue
                    out.append({"id": g["id"], "kind": "server", "name": g.get("name"),
                                "included": g["id"] in subs})
            out = out[:limit]
            lines = []
            for c in out:
                extra = (f", last activity {_when(c['last_activity'])}"
                         if c.get("last_activity") else "")
                if c["kind"] == "server":
                    extra = ", included" if c["included"] else ""
                lines.append(f"{c['kind']} {c['name']} (id {c['id']}{extra})")
            return _result(f"Listed {len(out)} chats and servers.", lines=lines, chats=out)
        return await run(work)

    # ---- read --------------------------------------------------------------------------
    @server.tool(annotations=READ)
    async def read_messages(
        chat: Annotated[str, Field(description="A channel or DM id")],
        before: Annotated[str | None, Field(description="Only older messages than this "
                                                        "message id")] = None,
        after: Annotated[str | None, Field(description="Only newer messages than this "
                                                       "message id")] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="The most messages, 1 to "
                                                              "100.")] = 50,
    ) -> CallToolResult:
        """Read messages of one Discord chat: the newest, or before or after one message.
        What the connection already holds comes from it, the rest live. For anything
        already in the chat archive, use chat_archive__read_period: it is faster and
        works offline."""
        def work():
            cid = _id(chat, "chat")
            if cid is None:
                raise Refused("Pass the chat's id.")
            older, newer = _id(before, "message"), _id(after, "message")
            ch = known(cid)
            local, params = spans.plan(state, cid, before=older, after=newer, limit=limit,
                                       live=link.gateway.live(cid))
            msgs = rest.get(f"/channels/{cid}/messages", params) if params else []
            recs = records_of(msgs)
            if params and not (older and newer):
                spans.covered(state, cid, params, msgs)
            recs = sorted(local + recs, key=lambda r: int(r["message"]["key"]))
            where = _where(recs[0]["chat"]) if recs else _where(
                {"key": cid, "kind": ch["kind"], "name": ch.get("name"),
                 "peer": ch.get("peer"), "parent": ch.get("guild_name")})
            ids = sorted({*(m["id"] for m in msgs), *(r["message"]["key"] for r in local)},
                         key=int)
            earlier = {"chat": cid, "before": ids[0]} if ids and len(ids) >= limit else None
            later = {"chat": cid, "after": ids[-1]} if ids and (older or newer) else None
            return _result(f"Read {len(recs)} messages of {where}.", records=recs,
                           where=where, lines=_lines(recs), earlier=earlier, later=later)
        return await run(work)

    @server.tool(annotations=READ)
    async def read_around_message(
        chat: Annotated[str, Field(description="A channel or DM id")],
        message: Annotated[str, Field(description="A message id")],
        limit: Annotated[int, Field(ge=2, le=100, description="Messages in all, 2 to "
                                                              "100.")] = 40,
    ) -> CallToolResult:
        """Read the messages before and after one Discord message, half on each side.
        Use it before you say anything about a message from discord__search_messages."""
        def work():
            cid, mid = _id(chat, "chat"), _id(message, "message")
            if cid is None or mid is None:
                raise Refused("Pass the chat's id and the message's id.")
            known(cid)
            recs = spans.around(state, cid, mid, limit, live=link.gateway.live(cid))
            if recs is None:
                params = {"around": mid, "limit": limit}
                msgs = rest.get(f"/channels/{cid}/messages", params)
                recs = records_of(msgs)
                spans.covered(state, cid, params, msgs)
                ids = sorted((m["id"] for m in msgs), key=int)
            else:
                ids = [r["message"]["key"] for r in recs]
            where = _where(recs[0]["chat"]) if recs else cid
            return _result(f"Read {len(recs)} messages around one message in {where}.",
                           records=recs, where=where, lines=_lines(recs),
                           earlier={"chat": cid, "before": ids[0]} if ids else None,
                           later={"chat": cid, "after": ids[-1]} if ids else None)
        return await run(work)

    # ---- search ------------------------------------------------------------------------
    def _search(path: str, params: dict[str, Any]) -> tuple[list[dict], int, bool]:
        r = rest.request("GET", path, params=params, raw=True)
        if r.status_code == 202:
            return [], 0, True
        data = r.json()
        hits = []
        for group in data.get("messages") or []:
            if isinstance(group, dict):
                hits.append(group)
                continue
            hit = next((m for m in group if m.get("hit")), group[0] if group else None)
            if hit is not None:
                hits.append(hit)
        return hits, int(data.get("total_results") or 0), False

    def _scope(chat: str | None, server_id: str | None) -> str:
        chat, server_id = _id(chat, "chat"), _id(server_id, "server")
        if bool(chat) == bool(server_id):
            raise Refused("Pass either a chat or a server.")
        return (f"/channels/{chat}/messages/search" if chat
                else f"/guilds/{server_id}/messages/search")

    @server.tool(annotations=READ)
    async def search_messages(
        query: Annotated[str | None, Field(description="The words.")] = None,
        chat: Annotated[str | None, Field(description="A DM or group DM id")] = None,
        server: Annotated[str | None, Field(description="A server id.")] = None,
        from_user: Annotated[str | None, Field(description="An author id. @me is "
                                                           "{{username}}'s account")] = None,
        since: Annotated[str | None, Field(description="A start date, such as "
                                                       "2026-09-24.")] = None,
        until: Annotated[str | None, Field(description="An end date, included.")] = None,
        page: Annotated[int, Field(ge=1, le=40, description="The result page, 1 to 40.")] = 1,
    ) -> CallToolResult:
        """Run Discord's own search in one DM, group DM or server: words, author, date
        range. Use it for chats and servers that are not in the chat archive. Returns: 25
        results per page, newest first."""
        def work():
            path = _scope(chat, server)
            params: dict[str, Any] = {"offset": (page - 1) * SEARCH_PAGE}
            if query:
                params["content"] = query
            author = _id(from_user, "user", me=True)
            if author:
                params["author_id"] = author if author != "@me" else me_id()
            lo, hi = _time(since), _time(until, end=True)
            if lo:
                params["min_id"] = snowflake_at(lo)
            if hi:
                params["max_id"] = snowflake_at(hi)
            hits, total, indexing = _search(path, params)
            if indexing:
                return _result("Discord is still indexing this chat. Try again in a minute.")
            recs = records_of(hits)
            where = _where(recs[0]["chat"]) if chat and recs else (
                f"server {policy.guild_name_of(state, server) or server}" if server else chat)
            return _result(f"Found {total} messages. This is page {page}.", records=recs,
                           where=where, lines=_lines(recs))
        return await run(work)

    @server.tool(annotations=READ)
    async def list_user_messages(
        user_id: Annotated[str, Field(description="An author id. @me is {{username}}'s "
                                                  "account")],
        server: Annotated[str, Field(description="A server id.")],
        day: Annotated[str | None, Field(description="One date. Default: yesterday.")] = None,
        since: Annotated[str | None, Field(description="Instead of `day`: a start "
                                                       "date.")] = None,
        until: Annotated[str | None, Field(description="Instead of `day`: an end date, "
                                                       "included.")] = None,
        limit: Annotated[int, Field(ge=1, le=200, description="The most messages, 1 to "
                                                              "200.")] = 200,
    ) -> CallToolResult:
        """List every message one user wrote on one server in a date range, oldest first,
        with the channel of each. Use it first when you review a day."""
        def work():
            if since or until:
                lo, hi = _time(since), _time(until, end=True)
            else:
                d = day or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                lo, hi = _time(d), _time(d, end=True)
            author, sid = _id(user_id, "user", me=True), _id(server, "server")
            if author is None or sid is None:
                raise Refused("Pass the user's id and the server's id.")
            params: dict[str, Any] = {"author_id": author if author != "@me" else me_id()}
            if lo:
                params["min_id"] = snowflake_at(lo)
            if hi:
                params["max_id"] = snowflake_at(hi)
            found: list[dict] = []
            total = 0
            for p in range(USER_PAGES):
                hits, total, indexing = _search(f"/guilds/{sid}/messages/search",
                                                {**params, "offset": p * SEARCH_PAGE})
                if indexing:
                    return _result("Discord is still indexing this server. Try again in "
                                   "a minute.")
                found += hits
                if len(hits) < SEARCH_PAGE or len(found) >= limit:
                    break
            recs = records_of(found[:limit])
            gname = policy.guild_name_of(state, sid) or sid
            more = f" of {total}" if total > len(recs) else ""
            return _result(f"Found {len(recs)}{more} messages by that user on {gname}.",
                           records=recs, where=f"server {gname}", lines=[
                               f"{r['chat'].get('name')}: {line}"
                               for r, line in zip(recs, _lines(recs))])
        return await run(work)

    # ---- people and servers --------------------------------------------------------------
    @server.tool(annotations=READ)
    async def get_user_profile(
        user_id: Annotated[str, Field(description="A user id")],
    ) -> CallToolResult:
        """Get one Discord user's profile: names, bio, mutual servers, mutual friends,
        connected accounts. A profile is not a message and cannot be cited."""
        def work():
            uid = _id(user_id, "user")
            if uid is None:
                raise Refused("Pass the user's id.")
            try:
                p = rest.get(f"/users/{uid}/profile", {"with_mutual_guilds": "true",
                                                      "with_mutual_friends_count": "true"})
                u = p.get("user") or {}
            except (PermissionError, FileNotFoundError):
                p, u = {}, rest.get(f"/users/{uid}")
            names = {g["id"]: g.get("name") for g in state.get("guilds", []) or []}
            info = {"id": str(u.get("id") or uid), "username": u.get("username"),
                    "global_name": u.get("global_name"),
                    "bio": u.get("bio") or (p.get("user_profile") or {}).get("bio"),
                    "pronouns": (p.get("user_profile") or {}).get("pronouns"),
                    "mutual_servers": [names.get(str(g.get("id"))) or str(g.get("id"))
                                       for g in p.get("mutual_guilds") or []],
                    "mutual_friends": p.get("mutual_friends_count"),
                    "connected_accounts": [f"{a.get('type')}: {a.get('name')}"
                                           for a in p.get("connected_accounts") or []]}
            lines = [f"{k}: {', '.join(v) if isinstance(v, list) else v}"
                     for k, v in info.items() if v not in (None, [], "")]
            return _result(f"The profile of {info['global_name'] or info['username']}.",
                           lines=lines, user=info)
        return await run(work)

    @server.tool(annotations=READ)
    async def get_server_info(
        server_id: Annotated[str, Field(description="A server id from "
                                                    "discord__list_chats.")],
    ) -> CallToolResult:
        """Get a Discord server's description, member count, roles and text channels."""
        def work():
            gid = _id(server_id, "server")
            if gid is None:
                raise Refused("Pass the server's id.")
            g = rest.get(f"/guilds/{gid}", {"with_counts": "true"})
            chans = policy.text_channels(state, gid)
            if not chans:
                sync.guild_channels(rest, gid)
                chans = policy.text_channels(state, gid)
            names = sorted(f"#{state.channel(key)['name'] or key} (id {key})"
                           for key in chans)
            info = {"id": str(g.get("id")), "name": g.get("name"),
                    "description": g.get("description"),
                    "members": g.get("approximate_member_count"),
                    "online": g.get("approximate_presence_count"),
                    "roles": [r.get("name") for r in g.get("roles") or []
                              if r.get("name") != "@everyone"],
                    "channels": names}
            lines = [f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
                     for k, v in info.items() if v not in (None, [], "")]
            return _result(f"The server {info['name']}.", lines=lines, server=info)
        return await run(work)

    @server.tool(annotations=READ)
    async def list_members(
        server_id: Annotated[str, Field(description="A server id.")],
        query: Annotated[str, Field(description="The start of a name.")],
        limit: Annotated[int, Field(ge=1, le=100, description="The most members, 1 to "
                                                              "100.")] = 50,
    ) -> CallToolResult:
        """List the members of a Discord server whose name starts with a query. It needs
        the live connection."""
        try:
            gid = _id(server_id, "server")
        except Refused as e:
            return _error(str(e))
        if gid is None:
            return _error("Pass the server's id.")
        try:
            got = await link.gateway.members(gid, query, limit)
        except (ConnectionError, TimeoutError, asyncio.TimeoutError):
            return _error("The Discord connection is not up right now.")
        out = []
        for mem in got:
            u = mem.get("user") or {}
            out.append({"id": str(u.get("id")), "username": u.get("username"),
                        "global_name": u.get("global_name"), "nick": mem.get("nick")})
        lines = [f"{m['nick'] or m['global_name'] or m['username']} "
                 f"(@{m['username']}, id {m['id']})" for m in out]
        return _result(f"Found {len(out)} members.", lines=lines, members=out)

    return server

