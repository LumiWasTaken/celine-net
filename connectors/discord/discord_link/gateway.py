"""Discord's gateway, read only, the way the desktop client stays connected.

It sends only identify (2), heartbeat (1), resume (6), request guild members (8, the
`members` tool, what the client sends when you search the member list) and guild
subscriptions (37, what the client sends when you look at a server); `send_op`
refuses every other opcode. It never sends messages, typing, read acks or presence.

What is journaled: DMs by their tick (new DMs wait for one), servers only when
subscribed, with their settings: the include list (threads follow their channel), bots,
attachments; never messages of blocked users, never an ignored chat. Edits become edit
records, deletions delete records; the archive keeps the old text.

When a session that was connected drops and a new one gets READY (not RESUMED, which
replays), `on_back` runs: the REST catch-up for what happened in between. A start of
the process counts as a drop once a sync has run, so what happened while it was down is
caught up at the first READY too.

Threads: the websocket runs on the event loop; journal writes happen in one writer
thread so a slow disk never delays a heartbeat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import random
import threading
import uuid
from typing import Any, Callable

from . import policy
from .records import DM, GROUP_DM, message, person
from .state import State, now_ms
from .sync import learn_author_peer, learn_private

log = logging.getLogger("discord_link.gateway")

GATEWAY = "wss://gateway.discord.gg"
QUERY = "/?v=10&encoding=json"
ALLOWED_OPS = {1, 2, 6, 8, 37}
_FATAL = {4004: "login rejected", 4010: "invalid shard", 4011: "sharding required",
          4012: "invalid API version", 4013: "invalid intents", 4014: "disallowed intents"}
_RESET_SESSION = {4007, 4009}
# why this side closed the socket itself (with 4000), so the state names the real cause
RECONNECT = "Discord asked to reconnect."
INVALID_SESSION = "Discord refused the session. A new or unverified account often gets this."
NO_HEARTBEAT = "Discord stopped answering heartbeats."


class _Stop(Exception):
    pass


class OpRefused(Exception):
    pass


class Writer(threading.Thread):
    """Applies dispatch events to the journal. Owns every write of the gateway."""

    def __init__(self, gw: "Gateway"):
        super().__init__(name="discord-gw-writer", daemon=True)
        self.gw = gw
        self.state = gw.state
        self.q: queue.Queue = queue.Queue()

    def put(self, event: str, data: dict) -> None:
        self.q.put((event, data))

    def stop(self) -> None:
        self.q.put(None)

    def run(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                return
            event, data = item
            try:
                self.apply(event, data)
            except Exception as e:           # one bad event never stops the stream
                log.warning("event failed", extra={"event": "gateway.event_failed",
                                                   "kind": event, "error": repr(e)})

    def apply(self, event: str, d: dict) -> None:
        g = self.gw
        if event in ("CHANNEL_CREATE", "CHANNEL_UPDATE", "THREAD_CREATE", "THREAD_UPDATE"):
            g.learn_channel(d, d.get("guild_id"))
        elif event == "THREAD_LIST_SYNC":
            for t in d.get("threads") or []:
                g.learn_channel(t, d.get("guild_id"))
        elif event == "RELATIONSHIP_ADD" and d.get("type") == 2:
            g.blocked.add(str(d.get("id") or (d.get("user") or {}).get("id")))
            self.state.set("blocked_users", sorted(g.blocked))
        elif event == "RELATIONSHIP_REMOVE":
            g.blocked.discard(str(d.get("id")))
            self.state.set("blocked_users", sorted(g.blocked))
        elif event == "MESSAGE_CREATE":
            self.create(d)
        elif event == "MESSAGE_UPDATE":
            self.update(d)
        elif event == "MESSAGE_DELETE":
            self.delete(d.get("channel_id"), [d.get("id")], d.get("guild_id"))
        elif event == "MESSAGE_DELETE_BULK":
            self.delete(d.get("channel_id"), d.get("ids") or [], d.get("guild_id"))

    def _scope(self, d: dict) -> tuple[bool, dict | None]:
        """(wanted, settings); settings is None for a DM."""
        gid = d.get("guild_id")
        cid = str(d.get("channel_id"))
        if not gid:
            ch = self.state.channel(cid)
            return bool(ch and ch["enabled"]), None
        sub = policy.synced_channel(self.state, self.gw.subs, cid)
        return (sub is not None), (sub["settings"] if sub else None)

    def create(self, d: dict) -> None:
        cid = str(d.get("channel_id"))
        if not d.get("guild_id") and self.state.channel(cid) is None:
            # a DM that started while connected: unticked, like the REST sync's pickup
            self.state.learn_channel(cid, kind="dm", type=DM, enabled=0)
        if not d.get("guild_id"):
            learn_author_peer(self.state, cid, d, self.gw.me_id or "")
        wanted, settings = self._scope(d)
        if not wanted or not policy.keep_message(settings, d, self.gw.blocked):
            return
        files = None
        if settings is not None and settings["include_attachments"] and d.get("attachments"):
            files = policy.download_attachments(self.state, d["attachments"])
        r = message(self.state, d, self.gw.me_id, files=files)
        if r is not None:
            self.state.journal([r])
            self.state.learn_channel(cid, kind=r["chat"]["kind"], last_message_id=str(d["id"]))
        self.gw.count(d.get("guild_id"))

    def update(self, d: dict) -> None:
        wanted, settings = self._scope(d)
        if not wanted:
            return
        if "content" not in d:
            return                           # embeds unfurling, pins: not an edit
        cid, mid = str(d.get("channel_id")), str(d.get("id"))
        prev = self.state.one("SELECT text FROM seen WHERE kind = 'message' AND chat_key = ? "
                              "AND message_key = ?", (cid, mid))
        if prev is None:
            if d.get("author") and d.get("timestamp"):
                self.create(d)               # an edit of a message never seen: keep it now
            return
        if prev["text"] == d["content"]:
            return
        self.state.journal([{"kind": "edit", "chat_key": cid, "message_key": mid,
                             "ts": now_ms(), "text": d["content"]}])
        self.state.x("UPDATE seen SET text = ? WHERE kind = 'message' AND chat_key = ? "
                     "AND message_key = ?", (d["content"], cid, mid))
        self.gw.count(d.get("guild_id"))

    def delete(self, channel_id: str | None, ids: list, guild_id: str | None) -> None:
        wanted, _ = self._scope({"channel_id": channel_id, "guild_id": guild_id})
        if not wanted:
            return
        keys = [str(i) for i in ids if i]
        if keys:
            self.state.journal([{"kind": "delete", "chat_key": str(channel_id),
                                 "message_keys": keys, "ts": now_ms()}])
        self.gw.count(guild_id)


class Gateway:
    def __init__(self, state: State, *, on_back: Callable[[], None] | None = None,
                 connect: Callable[..., Any] | None = None):
        self.state = state
        self.on_back = on_back
        self._connect = connect
        self.state_name = "stopped"          # stopped connecting connected reconnecting rejected
        self.connected_at: int | None = None
        self.dropped_at: int | None = now_ms() if state.get("last_sync") else None
        self.events = 0
        self.last_event_at: int | None = None
        self.last_error: str | None = None
        self.closed_why: str | None = None      # why this side closed the socket (4000)
        self.session_id: str | None = None
        self.resume_url: str | None = None
        self.seq: int | None = None
        self.me_id: str | None = None
        self.guild_count = 0
        self.member_of: set[str] = set()     # servers the account is in; op 37 names only these
        self.blocked: set[str] = set(state.get("blocked_users", []) or [])
        self.subs: dict[str, dict] = {}
        self.ws = None
        self.writer: Writer | None = None
        self.task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.acked = True
        self.sent_subs: set[str] = set()
        self._members: dict[str, tuple[asyncio.Future, list]] = {}

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state_name, "connected_at": self.connected_at,
                "events": self.events, "last_event_at": self.last_event_at,
                "last_error": self.last_error, "guilds": self.guild_count,
                "subscribed": sorted(self.subs),
                "not_member": sorted(set(self.subs) - self.member_of) if self.member_of else []}

    def _set(self, name: str, error: str | None = None) -> None:
        if name == "connected":
            self.last_error = None
        elif error is not None:
            self.last_error = error
        if self.state_name == "connected" and name != "connected":
            self.dropped_at = now_ms()
        self.state_name = name

    def learn_channel(self, ch: dict, guild_id: str | None) -> None:
        if ch.get("type") in (DM, GROUP_DM):
            learn_private(self.state, ch)
            return
        gid = str(guild_id or ch.get("guild_id") or "") or None
        self.state.learn_channel(
            str(ch.get("id")), kind="channel", type=ch.get("type"), name=ch.get("name"),
            guild_id=gid, guild_name=policy.guild_name_of(self.state, gid) if gid else None,
            parent_id=str(ch["parent_id"]) if ch.get("parent_id") else None,
            last_message_id=ch.get("last_message_id"))

    def count(self, guild_id: str | None) -> None:
        self.events += 1
        self.last_event_at = now_ms()
        if guild_id:
            policy.note_event(self.state, str(guild_id))

    def reload(self) -> None:
        """Subscriptions changed: reload them and tell Discord about new servers."""
        self.subs = policy.active(self.state)
        if self.ws is not None and self.loop is not None and self.state_name == "connected":
            asyncio.run_coroutine_threadsafe(self._subscribe_guilds(), self.loop)

    async def send_op(self, op: int, d: Any) -> None:
        if op not in ALLOWED_OPS:
            raise OpRefused(f"gateway op {op} is not allowed")
        ws = self.ws
        if ws is None:
            raise ConnectionError("not connected")
        await ws.send(json.dumps({"op": op, "d": d}))

    # ---- members (op 8) ------------------------------------------------------------------
    async def members(self, guild_id: str, query: str, limit: int = 100,
                      timeout: float = 10.0) -> list[dict]:
        if self.state_name != "connected":
            raise ConnectionError("The Discord gateway is not connected right now.")
        nonce = uuid.uuid4().hex[:16]
        fut = asyncio.get_running_loop().create_future()
        self._members[nonce] = (fut, [])
        try:
            await self.send_op(8, {"guild_id": [str(guild_id)], "query": query,
                                   "limit": min(100, max(1, limit)), "presences": False,
                                   "nonce": nonce})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._members.pop(nonce, None)

    # ---- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self.loop = asyncio.get_running_loop()
        self.task = self.loop.create_task(self.run())

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self._set("stopped")

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.reload)
        self.writer = Writer(self)
        self.writer.start()
        delay = 1.0
        try:
            while True:
                token = self.state.token()
                if not token or self.state.get("token_rejected"):
                    self._set("stopped", "not logged in")
                    return
                attempt = now_ms()
                try:
                    await self._session(token)
                except _Stop as e:
                    self._set("rejected" if "rejected" in str(e) else "stopped", str(e))
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._set("reconnecting", self.closed_why or _plain(e))
                # a session that stayed up for a minute starts the backoff over
                if self.connected_at and self.connected_at >= attempt and \
                        now_ms() - self.connected_at > 60_000:
                    delay = 1.0
                await asyncio.sleep(delay + random.random())
                delay = min(60.0, delay * 2)
        finally:
            if self.writer is not None:
                self.writer.stop()
                await asyncio.to_thread(self.writer.join, 10)

    def _open(self, url: str):
        if self._connect is not None:
            return self._connect(url)
        import websockets
        return websockets.connect(url, max_size=None, open_timeout=30, compression=None)

    async def _session(self, token: str) -> None:
        url = (self.resume_url or GATEWAY).rstrip("/") + QUERY
        self._set("connecting")
        self.closed_why = None
        async with self._open(url) as ws:
            self.ws = ws
            beat: asyncio.Task | None = None
            try:
                hello = json.loads(await ws.recv())
                if hello.get("op") != 10:
                    raise ConnectionError(f"expected hello, got op {hello.get('op')}")
                interval = hello["d"]["heartbeat_interval"] / 1000
                self.acked = True
                beat = asyncio.create_task(self._heartbeat(interval))
                if self.session_id and self.seq is not None:
                    await self.send_op(6, {"token": token, "session_id": self.session_id,
                                           "seq": self.seq})
                else:
                    await self.send_op(2, {"token": token, "compress": False,
                                           "properties": {"os": "Linux",
                                                          "browser": "celine-net",
                                                          "device": "celine-net"}})
                while True:
                    msg = json.loads(await ws.recv())
                    await self._handle(ws, msg)
            except Exception as e:
                code = getattr(getattr(e, "rcvd", None), "code", None) or \
                    getattr(e, "close_code", None)
                if code in _FATAL:
                    if code == 4004:
                        self.state.set("token_rejected", now_ms())
                    raise _Stop(_FATAL[code]) from None
                if code in _RESET_SESSION:
                    self.session_id = self.seq = self.resume_url = None
                raise
            finally:
                self.ws = None
                if beat is not None:
                    beat.cancel()

    async def _heartbeat(self, interval: float) -> None:
        await asyncio.sleep(interval * random.random())
        while True:
            if not self.acked:
                if self.ws is not None:
                    self.closed_why = NO_HEARTBEAT     # zombie connection: resume elsewhere
                    await self.ws.close(code=4000)
                return
            self.acked = False
            await self.send_op(1, self.seq)
            await asyncio.sleep(interval)

    async def _handle(self, ws, msg: dict) -> None:
        op = msg.get("op")
        if op == 11:
            self.acked = True
        elif op == 1:
            await self.send_op(1, self.seq)
        elif op == 7:
            self.closed_why = RECONNECT
            await ws.close(code=4000)
        elif op == 9:
            if not msg.get("d"):
                self.session_id = self.seq = self.resume_url = None
            await asyncio.sleep(1 + 4 * random.random())
            self.closed_why = INVALID_SESSION
            await ws.close(code=4000)
        elif op == 0:
            if msg.get("s") is not None:
                self.seq = msg["s"]
            await self._dispatch(msg.get("t"), msg.get("d") or {})

    async def _dispatch(self, t: str, d: dict) -> None:
        if t == "READY":
            self.session_id = d.get("session_id")
            self.resume_url = d.get("resume_gateway_url")
            me = d.get("user") or {}
            self.me_id = str(me.get("id") or "") or None
            guilds = []
            for g in d.get("guilds") or []:
                gid = str(g.get("id"))
                name = g.get("name") or (g.get("properties") or {}).get("name")
                guilds.append({"id": gid, "name": name, "icon": None})
            await asyncio.to_thread(self._ready, d, guilds)
            self.guild_count = len(guilds)
            self.member_of = {g["id"] for g in guilds}
            self.connected_at = now_ms()
            self.sent_subs = set()
            await self._subscribe_guilds()
            back = self.dropped_at is not None
            self._set("connected")
            if back and self.on_back is not None:
                self.dropped_at = None
                self.on_back()                         # the catch-up after a drop
        elif t == "RESUMED":
            self.connected_at = now_ms()
            self.dropped_at = None
            self.sent_subs = set()
            await self._subscribe_guilds()
            self._set("connected")
        elif t == "GUILD_CREATE":
            gid = str(d.get("id"))
            self.member_of.add(gid)
            await asyncio.to_thread(self._guild, d, gid)
            await self._subscribe_guilds()
        elif t == "GUILD_DELETE" and not d.get("unavailable"):
            self.member_of.discard(str(d.get("id")))     # left or removed
        elif t == "GUILD_MEMBERS_CHUNK":
            hit = self._members.get(str(d.get("nonce") or ""))
            if hit is not None:
                fut, acc = hit
                acc.extend(d.get("members") or [])
                if d.get("chunk_index", 0) + 1 >= d.get("chunk_count", 1) and not fut.done():
                    fut.set_result(acc)
        elif t and self.writer is not None:
            self.writer.put(t, d)

    def _ready(self, d: dict, guilds: list[dict]) -> None:
        known = {g["id"]: g for g in self.state.get("guilds", []) or []}
        merged = [{**known.get(g["id"], {}), **{k: v for k, v in g.items() if v}}
                  for g in guilds]
        if merged:
            self.state.set("guilds", sorted(merged, key=lambda g: (g.get("name") or "").lower()))
        me = d.get("user") or {}
        if me.get("id"):
            self.state.set("account", {"id": str(me["id"]),
                                       "name": me.get("global_name") or me.get("username")})
            self.state.journal([{"kind": "person", "person": person(me, is_me=True)}])
        for g in d.get("guilds") or []:
            self._guild(g, str(g.get("id")))
        for ch in d.get("private_channels") or []:
            self.learn_channel(ch, None)
        if "relationships" in d:
            self.blocked = {str(r.get("id")) for r in d["relationships"] if r.get("type") == 2}
            self.state.set("blocked_users", sorted(self.blocked))

    def _guild(self, g: dict, gid: str) -> None:
        for ch in (g.get("channels") or []) + (g.get("threads") or []):
            self.learn_channel(ch, gid)

    async def _subscribe_guilds(self) -> None:
        # a server the account is no longer in makes Discord close the session (4000)
        new = {gid: s for gid, s in self.subs.items()
               if gid not in self.sent_subs and gid in self.member_of}
        if self.ws is None or not new:
            return
        await self.send_op(37, {"subscriptions": {
            gid: {"typing": False, "activities": False, "member_updates": False,
                  "threads": True}
            for gid, s in new.items()}})
        self.sent_subs |= set(new)


def _plain(e: Exception) -> str:
    code = getattr(getattr(e, "rcvd", None), "code", None) or getattr(e, "close_code", None)
    if code:
        return f"Discord closed the connection (code {code})."
    if isinstance(e, (OSError, TimeoutError, ConnectionError)):
        return "Discord is not reachable."
    return f"The connection stopped ({type(e).__name__})."

