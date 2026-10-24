"""The gateway against a fake Discord, and the QR login. No network."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import time

import pytest
from cryptography.hazmat.primitives import serialization
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from conftest import ME, T0, msg, sid
from discord_link import gateway as gwmod, login, policy
from discord_link.gateway import Gateway, OpRefused

pytestmark = pytest.mark.anyio


class FakeWS:
    """Discord's side of one gateway connection. `script` runs after identify/resume."""

    def __init__(self, script):
        self.script = script
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.seq = 0

    async def __aenter__(self):
        await self.inbox.put({"op": 10, "d": {"heartbeat_interval": 41250}})
        return self

    async def __aexit__(self, *a):
        return False

    async def dispatch(self, t: str, d: dict) -> None:
        self.seq += 1
        await self.inbox.put({"op": 0, "t": t, "s": self.seq, "d": d})

    async def drop(self, code: int = 4000) -> None:
        await self.inbox.put(ConnectionClosedError(Close(code, "bye"), None))

    async def send(self, raw: str) -> None:
        m = json.loads(raw)
        self.sent.append(m)
        if m["op"] in (2, 6):
            await self.script(self, m)

    async def recv(self):
        m = await self.inbox.get()
        if isinstance(m, Exception):
            raise m
        return json.dumps(m)

    async def close(self, code: int = 1000) -> None:
        await self.drop(code)


READY = {
    "session_id": "S1", "resume_gateway_url": "wss://resume.example",
    "user": {"id": ME, "username": "sam"},
    "guilds": [{"id": "G1", "name": "Subscribed", "channels": [
                    {"id": "C1", "name": "general", "type": 0},
                    {"id": "C3", "name": "other", "type": 0}],
                "threads": [{"id": "T1", "name": "a thread", "type": 11, "parent_id": "C1"}]},
               {"id": "G2", "name": "Not subscribed", "channels": [
                    {"id": "C2", "name": "general", "type": 0}]}],
    "private_channels": [{"id": "D1", "type": 1, "recipients": [{"id": "U2",
                                                                 "username": "u2"}]}],
    "relationships": [{"id": "BAD", "type": 2}],
}


async def wait_for(cond, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


def texts(state):
    return [r["message"]["text"] for r in state.after(0, 1000) if r["kind"] == "message"]


async def test_reads_along_subscribed_servers_and_dms(state):
    policy.put(state, "G1", settings={"channels": ["C1"]})
    policy.start(state, "G1")
    sockets: list[FakeWS] = []

    async def script(ws, m):
        await ws.dispatch("READY", READY)

    def connect(url):
        ws = FakeWS(script)
        sockets.append(ws)
        return ws

    backs = []
    gw = Gateway(state, on_back=lambda: backs.append(1), connect=connect)
    gw.start()
    await wait_for(lambda: gw.state_name == "connected")
    ws = sockets[0]
    assert ws.sent[0]["op"] == 2
    assert any(m["op"] == 37 and "G1" in m["d"]["subscriptions"] for m in ws.sent)
    now = T0 + 10 ** 9
    await ws.dispatch("MESSAGE_CREATE", {**msg(sid(now), "C1", "U2", "server hi"),
                                         "guild_id": "G1"})
    await ws.dispatch("MESSAGE_CREATE", {**msg(sid(now + 1), "C3", "U2", "not ticked"),
                                         "guild_id": "G1"})
    await ws.dispatch("MESSAGE_CREATE", {**msg(sid(now + 2), "C2", "U2", "other server"),
                                         "guild_id": "G2"})
    await ws.dispatch("MESSAGE_CREATE", {**msg(sid(now + 3), "T1", "U2", "in thread"),
                                         "guild_id": "G1"})
    await ws.dispatch("MESSAGE_CREATE", {**msg(sid(now + 4), "C1", "BAD", "blocked"),
                                         "guild_id": "G1"})
    await ws.dispatch("MESSAGE_CREATE", msg(sid(now + 5), "D9", "U9", "new dm"))
    await ws.dispatch("MESSAGE_UPDATE", {"id": sid(now), "channel_id": "C1",
                                         "guild_id": "G1", "content": "server hi!"})
    await ws.dispatch("MESSAGE_DELETE", {"id": sid(now), "channel_id": "C1",
                                         "guild_id": "G1"})
    await wait_for(lambda: any(r["kind"] == "delete" for r in state.after(0, 1000)))
    assert texts(state) == ["server hi", "in thread"]
    kinds = [r["kind"] for r in state.after(0, 1000)]
    assert kinds.count("edit") == 1 and kinds.count("delete") == 1
    thread = [r for r in state.after(0, 1000) if r["kind"] == "message"][1]["chat"]
    assert thread["name"] == "#general › a thread"
    assert state.channel("D9")["enabled"] == 0           # a new DM waits for a tick
    # a drop, then a fresh READY: the catch-up runs once
    ws.seq = 0
    gw.session_id = None
    await ws.drop(1006)
    await wait_for(lambda: len(sockets) == 2 and gw.state_name == "connected", 10)
    assert backs == [1]
    with pytest.raises(OpRefused):
        await gw.send_op(3, {"status": "online"})       # presence: never
    await gw.stop()
    ops = {m["op"] for s in sockets for m in s.sent}
    assert ops <= {1, 2, 6, 8, 37}


async def test_members_asks_with_op_8(state):
    async def script(ws, m):
        await ws.dispatch("READY", READY)

    sockets = []

    def connect(url):
        sockets.append(FakeWS(script))
        return sockets[-1]
    gw = Gateway(state, connect=connect)
    gw.start()
    await wait_for(lambda: gw.state_name == "connected")

    async def answer():
        await wait_for(lambda: any(m["op"] == 8 for m in sockets[0].sent))
        req = next(m for m in sockets[0].sent if m["op"] == 8)
        await sockets[0].dispatch("GUILD_MEMBERS_CHUNK", {
            "nonce": req["d"]["nonce"], "chunk_index": 0, "chunk_count": 1,
            "members": [{"user": {"id": "U2", "username": "ana"}, "nick": "A"}]})
    asyncio.get_running_loop().create_task(answer())
    got = await gw.members("G1", "an")
    assert got[0]["nick"] == "A"
    await gw.stop()


async def test_a_rejected_login_stops_the_gateway(state):
    async def script(ws, m):
        await ws.drop(4004)

    gw = Gateway(state, connect=lambda url: FakeWS(script))
    gw.start()
    await wait_for(lambda: gw.state_name == "rejected")
    assert state.get("token_rejected")


async def test_a_close_of_our_own_names_its_cause(state, monkeypatch):
    op, why = 7, gwmod.RECONNECT
    monkeypatch.setattr(gwmod.random, "random", lambda: 0.0)

    async def script(ws, m):
        await ws.inbox.put({"op": op, "d": False})

    gw = Gateway(state, connect=lambda url: FakeWS(script))
    gw.start()
    await wait_for(lambda: gw.state_name == "reconnecting")
    await gw.stop()
    assert gw.last_error == why


# ---- the QR login ------------------------------------------------------------------------

class FakeRemoteAuth:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.key = None
        self.close_code = 1000

    async def __aenter__(self):
        await self.inbox.put({"op": "hello", "heartbeat_interval": 41250, "timeout_ms": 120000})
        return self

    async def __aexit__(self, *a):
        return False

    def enc(self, data: bytes) -> str:
        return base64.b64encode(self.key.encrypt(data, login.OAEP)).decode()

    async def send(self, raw: str) -> None:
        m = json.loads(raw)
        if m["op"] == "init":
            der = base64.b64decode(m["encoded_public_key"])
            self.key = serialization.load_der_public_key(der)
            self.spki = der
            await self.inbox.put({"op": "nonce_proof", "encrypted_nonce": self.enc(b"n0nce")})
        elif m["op"] == "nonce_proof":
            fp = login.b64url(hashlib.sha256(self.spki).digest())
            await self.inbox.put({"op": "pending_remote_init", "fingerprint": fp})
            await self.inbox.put({"op": "pending_ticket",
                                  "encrypted_user_payload": self.enc(b"42:0:abc:sam")})
            await self.inbox.put({"op": "pending_login", "ticket": "T1"})

    def __aiter__(self):
        return self

    async def __anext__(self):
        return json.dumps(await self.inbox.get())


async def test_qr_login_stores_the_token_privately(tmp_path):
    from discord_link.state import State
    state = State(tmp_path / "s")
    ra = FakeRemoteAuth()
    done = []

    async def exchange(ticket):
        return ra.enc(b"the-new-token")
    logins = login.Logins(state, on_done=lambda: done.append(1), connect=lambda url: ra,
                          exchange=exchange)
    s = logins.start()
    await wait_for(lambda: s.state == "done")
    assert done == [1] and state.token() == "the-new-token"
    assert stat.S_IMODE(os.stat(state.token_path).st_mode) == 0o600
    assert "the-new-token" not in json.dumps(s.as_dict())
    assert s.user["name"] == "sam"
    state.close()


async def test_reconnects_back_off_after_an_old_long_session(state, monkeypatch):
    """A session that was up for long, long ago, does not keep the retry at one second:
    only a session of this attempt that lasted a minute starts the backoff over."""
    import discord_link.gateway as gwmod
    from discord_link.state import now_ms
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep(d, *a, **kw):
        if d >= 1:
            delays.append(d)
            d = 0
        await real_sleep(d, *a, **kw)
    monkeypatch.setattr(gwmod.random, "random", lambda: 0.0)
    monkeypatch.setattr(gwmod.asyncio, "sleep", sleep)

    def connect(url):
        raise OSError("no route")
    gw = Gateway(state, connect=connect)
    gw.connected_at = now_ms() - 3_600_000
    gw.start()
    await wait_for(lambda: len(delays) >= 4)
    await gw.stop()
    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]
    assert gw.state_name == "stopped" and gw.last_error == "Discord is not reachable."


async def test_a_login_that_fails_says_why(tmp_path):
    from discord_link.state import State
    state = State(tmp_path / "s")
    ra = FakeRemoteAuth()
    real_send = ra.send

    async def send(raw):
        if json.loads(raw)["op"] == "nonce_proof":
            await ra.inbox.put({"op": "pending_remote_init", "fingerprint": "not-ours"})
            return
        await real_send(raw)
    ra.send = send
    s = login.Logins(state, connect=lambda url: ra).start()
    await wait_for(lambda: s.state == "failed")
    assert s.error == "Discord's answer did not match this login's key. Try a new code."
    assert state.token() is None
    state.close()


async def test_a_server_the_account_left_is_not_subscribed(state):
    """op 37 for a server the account is no longer in makes Discord close with 4000."""
    for gid in ("G1", "G9"):
        policy.put(state, gid, settings={"channels": []})
        policy.start(state, gid)
    sockets: list[FakeWS] = []

    async def script(ws, m):
        await ws.dispatch("READY", READY)

    def connect(url):
        sockets.append(FakeWS(script))
        return sockets[-1]

    gw = Gateway(state, connect=connect)
    gw.start()
    await wait_for(lambda: gw.state_name == "connected")
    ws = sockets[0]
    subs = [set(m["d"]["subscriptions"]) for m in ws.sent if m["op"] == 37]
    assert subs == [{"G1"}]
    assert gw.as_dict()["not_member"] == ["G9"]
    await ws.dispatch("GUILD_CREATE", {"id": "G9", "name": "Back again", "channels": []})
    await wait_for(lambda: sum(m["op"] == 37 for m in ws.sent) == 2)
    assert set(ws.sent[-1]["d"]["subscriptions"]) == {"G9"}
    assert gw.as_dict()["not_member"] == []
    await gw.stop()


async def test_a_live_edit_names_mentions_like_the_sync(state):
    """An edit journals the text with mentions as names, the way a message record does,
    so the next REST sync of that message finds no difference."""
    from discord_link.records import message
    state.learn_channel("D1", kind="dm", type=1, enabled=1)
    gw = Gateway(state)
    gw.me_id = ME
    w = gwmod.Writer(gw)
    ana = [{"id": "333", "username": "ana", "global_name": "Ana"}]
    m = msg(sid(T0 + 10 ** 9), "D1", "U2", "hey <@333>", mentions=ana)
    w.apply("MESSAGE_CREATE", m)
    w.apply("MESSAGE_UPDATE", {**m, "content": "hey <@333>"})
    assert [r["kind"] for r in state.after(0, 100)] == ["message"], \
        "the same text again is no edit"
    edited = {**m, "content": "hey <@333>!", "edited_timestamp": m["timestamp"]}
    w.apply("MESSAGE_UPDATE", edited)
    edits = [r for r in state.after(0, 100) if r["kind"] == "edit"]
    assert [r["text"] for r in edits] == ["hey @Ana!"]
    state.journal([message(state, edited, ME)])          # the REST sync reads it again
    assert [r for r in state.after(0, 100) if r["kind"] == "edit"] == edits
