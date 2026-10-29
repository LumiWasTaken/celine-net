"""Web push (push.py): the VAPID key is made once; a device subscribes and unsubscribes; a
message to the user pushes only while no page is open and focused; a push service's 410
removes the subscription; a subscription ends with the login session that made it; an
approval ask is urgent."""
from __future__ import annotations

import base64
import json
import os

import anyio
import httpx
import http_ece
import pytest
from chat_fixtures import until
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from celine import boot, push
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.core.services import Services
from celine.core.store import Store
from celine.intercom import Intercom
from celine.settings import secrets

pytestmark = pytest.mark.anyio


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _browser() -> tuple[ec.EllipticCurvePrivateKey, bytes, dict]:
    """A browser's side: its key, its auth secret, and the subscription it posts."""
    key = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    point = key.public_key().public_bytes(serialization.Encoding.X962,
                                          serialization.PublicFormat.UncompressedPoint)
    return key, auth, {"endpoint": "https://push.example/send/abc",
                       "keys": {"p256dh": _b64(point), "auth": _b64(auth)},
                       "expirationTime": None, "kinds": ["intercom", "reply"]}


class FakeSender:
    def __init__(self, status: int = 201):
        self.status, self.sent, self.urgency = status, [], []

    async def __call__(self, row, body: bytes, urgency: str) -> int:
        self.sent.append((row["endpoint"], json.loads(body)))
        self.urgency.append(urgency)
        return self.status


async def _subscribe(store, sub: dict) -> int:
    k = sub["keys"]
    return await store.write(lambda c: c.execute(
        "INSERT INTO push_subscriptions(endpoint, p256dh, auth, device, kinds, created_at) "
        "VALUES(?,?,?,?,?,?) RETURNING id", (sub["endpoint"], k["p256dh"], k["auth"],
                                             "Firefox on Android", json.dumps(sub["kinds"]),
                                             "2026-03-11T10:00:00.000+00:00")).fetchone()[0])


async def _count(store) -> int:
    return await store.read(lambda c: c.execute(
        "SELECT count(*) FROM push_subscriptions").fetchone()[0])


def test_the_key_is_made_once(tmp_path):
    root = tmp_path / "secrets"
    _, first = push.keys(root)
    stored = secrets.read_key(root, push.SECRET)
    _, again = push.keys(root)
    assert first == again and secrets.read_key(root, push.SECRET) == stored
    assert len(base64.urlsafe_b64decode(first + "=")) == 65      # an uncompressed P-256 point


async def test_a_device_subscribes_and_unsubscribes(store):
    from celine.__main__ import build_app
    cfg = boot.load()
    app = build_app(cfg, dev=False)
    app.state.services = Services(cfg=cfg, store=store, push=push.Push(cfg, store, EventBus()))
    _, _, sub = _browser()
    base = "http://localhost:8766"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base,
                                 headers={"origin": base, "user-agent":
                                          "Mozilla/5.0 (Android 15) Firefox/140.0"}) as http:
        key = (await http.get("/api/push/key")).json()["key"]
        assert key == app.state.services.push.public_key
        made = (await http.post("/api/push-subscriptions", json=sub)).json()
        assert made["device"] == "Firefox on Android" and made["kinds"] == ["intercom", "reply"]
        again = (await http.post("/api/push-subscriptions", json={**sub, "kinds": ["reply"]})).json()
        assert again["id"] == made["id"], "the same endpoint is the same device"
        [row] = (await http.get("/api/push-subscriptions")).json()["subscriptions"]
        assert row["kinds"] == ["reply"] and row["endpoint"] == sub["endpoint"]
        bad = {**sub, "keys": {**sub["keys"], "p256dh": _b64(b"\x04" + b"\x01" * 64)}}
        assert (await http.post("/api/push-subscriptions", json=bad)).status_code == 400
        assert (await http.delete(f"/api/push-subscriptions/{made['id']}")).status_code == 200
        assert (await http.get("/api/push-subscriptions")).json()["subscriptions"] == []
        assert (await http.delete(f"/api/push-subscriptions/{made['id']}")).status_code == 404


async def test_a_message_to_the_user_pushes_only_without_a_focused_page(db_path):
    cfg = boot.load()
    sender = FakeSender()
    bus = EventBus()
    async with anyio.create_task_group() as tg:
        store = Store(db_path, bus=bus)
        await store.start(tg)
        p = push.Push(cfg, store, bus, send=sender)
        tg.start_soon(p.run)
        await until(lambda: bus._subs["global"])
        _, _, sub = _browser()
        await _subscribe(store, sub)
        intercom = Intercom(store)
        cid = await conversations.create(store)
        page = object()
        p.focus(page, True)
        await intercom.send(f"agent:{cid}", "user", "seen on the open page")
        # the push loop has taken every event: its focus check for that one is done
        await until(lambda: all(tx.statistics().current_buffer_used == 0
                                for tx in bus._subs["global"]))
        p.focus(page, False)
        await intercom.send(f"agent:{cid}", "user", "the server is back up\nsecond line")
        await until(lambda: sender.sent)
        [(endpoint, body)] = sender.sent
        assert endpoint == sub["endpoint"]
        assert body["body"] == "the server is back up" and body["link"].startswith("/intercom/")
        assert body["tag"].startswith("intercom:") and body["title"] == "Celine"
        tg.cancel_scope.cancel()


async def test_a_410_removes_the_subscription(store):
    cfg = boot.load()
    p = push.Push(cfg, store, EventBus(), send=FakeSender(410))
    _, _, sub = _browser()
    await _subscribe(store, sub)
    notice = {"type": "notice", "kind": "reply", "title": "Celine", "body": "done",
              "link": "/c/1", "tag": "reply:1"}
    await p.push({**notice, "kind": "mission"})
    assert await _count(store) == 1, "a kind it does not take is not sent"
    await p.push(notice)
    assert await _count(store) == 0


async def test_an_approval_ask_is_urgent(store):
    cfg = boot.load()
    sender = FakeSender()
    p = push.Push(cfg, store, EventBus(), send=sender)
    _, _, sub = _browser()
    await _subscribe(store, {**sub, "kinds": ["approval", "reply"]})
    for kind in ("approval", "reply"):
        await p.push({"type": "notice", "kind": kind, "title": "t", "body": "b",
                      "link": None, "tag": kind})
    assert sender.urgency == ["high", "normal"]


async def test_a_subscription_ends_with_its_session(store):
    """Logout, a session ended from another, the login's expiry: each takes its devices
    along; a row made while the login was off ends once it is on."""
    from celine import auth as auth_mod
    from celine.__main__ import build_app
    cfg = boot.load()
    cfg.server.update({"login": "on"})
    app = build_app(cfg, dev=False)
    auth = app.state.auth
    now = [1_800_000_000]
    auth.store.clock = lambda: now[0]
    app.state.services = Services(cfg=cfg, store=store,
                                  push=push.Push(cfg, store, EventBus(), auth))
    rid = auth.store.add_credential("pin", "04815162", "")
    base = "http://localhost:8766"

    def client(token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base,
                                 headers={"origin": base}, cookies={auth_mod.COOKIE: token})

    async def endpoints(http) -> list[str]:
        r = (await http.get("/api/push-subscriptions")).json()
        return [d["endpoint"] for d in r["subscriptions"]]

    tokens = [auth.store.new_session(credential=rid) for _ in range(3)]
    subs = []
    for i, token in enumerate(tokens):
        _, _, sub = _browser()
        subs.append({**sub, "endpoint": f"https://push.example/send/{i}"})
        async with client(token) as http:
            assert (await http.post("/api/push-subscriptions", json=subs[-1])).status_code == 200
    _, _, off = _browser()
    await _subscribe(store, {**off, "endpoint": "https://push.example/send/login-off"})
    async with client(tokens[0]) as http:
        assert (await http.post("/api/auth/logout")).status_code == 200
    async with client(tokens[1]) as http:
        assert await endpoints(http) == [subs[1]["endpoint"], subs[2]["endpoint"]]
        [other] = [s for s in auth.store.sessions() if s.id != auth.store.session(tokens[1]).id]
        assert (await http.delete(f"/api/auth/sessions/{other.id}")).status_code == 200
        assert await endpoints(http) == [subs[1]["endpoint"]]
    now[0] += 31 * 86400                       # the last one expires
    async with client(auth.store.new_session(credential=rid)) as http:
        assert await endpoints(http) == []


async def test_the_push_is_encrypted_for_the_browser_and_signed(store, monkeypatch):
    """The real POST: the browser's key decrypts the body; the VAPID header names the
    endpoint's origin."""
    cfg = boot.load()
    p = push.Push(cfg, store, EventBus())
    key, auth, sub = _browser()
    await _subscribe(store, sub)
    got = []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(request)
        return httpx.Response(201)
    real = httpx.AsyncClient
    monkeypatch.setattr(push.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    await p.push({"type": "notice", "kind": "reply", "title": "Celine", "body": "hi",
                  "link": "/c/1", "tag": "reply:1"})
    [r] = got
    assert r.headers["urgency"] == "normal"
    assert r.headers["authorization"].startswith("vapid t=") and \
        f"k={p.public_key}" in r.headers["authorization"]
    plain = http_ece.decrypt(r.content, private_key=key, auth_secret=auth, version="aes128gcm")
    assert json.loads(plain) == {"title": "Celine", "body": "hi", "link": "/c/1", "tag": "reply:1"}
