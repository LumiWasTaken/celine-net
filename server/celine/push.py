"""Web push: the feed's notices (events.notice) reach a device while no page of the app is
open and focused.

The VAPID key pair is made once and kept in var/secrets (push.vapid.key: the private key,
raw, base64url). GET /api/push/key gives a page its public half; the page subscribes its
browser (the service worker, web/public/sw.js) and posts the subscription to
/api/push-subscriptions: one row per device, with the notice kinds it takes. Every `notice`
on the global feed goes to each subscription that takes its kind, unless a page is open and
focused (its socket says so: api/ws.py `focus`). The payload is the notice as the page
shows it: {title, body, link, tag}, urgent for an approval ask (RFC 8030 Urgency). A push
service's 404 or 410 means the subscription ended: its row is deleted.

A subscription belongs to the login session that made it (session_id) and ends with it:
the routes that end sessions call `forget_ended`, and every push and every listing calls it
first, so a session that expired or ended with its passkey or credential takes its devices
along too. With the login off there is no session: session_id is NULL and the device takes
pushes until it is removed; once the login is on, those rows end as well."""
from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import http_ece
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Request
from py_vapid import Vapid02
from pydantic import BaseModel

from .auth.routes import device
from .core.event_types import NOTICE, PUSH_SUBSCRIPTIONS
from .core.ids import now
from .errors import CelineError, NotFound
from .settings import address, secrets

log = logging.getLogger("celine.push")
router = APIRouter(prefix="/api")

SECRET = "push.vapid"          # var/secrets/push.vapid.key
TTL = 24 * 3600                # seconds a push service keeps a push for an offline device
GONE = (404, 410)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def keys(root: Path) -> tuple[Vapid02, str]:
    """The VAPID key, made on the first call. -> (the signer, its public key, base64url)."""
    raw = secrets.read_key(root, SECRET)
    if raw is None:
        k = ec.generate_private_key(ec.SECP256R1())
        raw = _b64(k.private_numbers().private_value.to_bytes(32, "big"))
        secrets.set_key(root, SECRET, raw)
    v = Vapid02.from_raw(raw.encode())
    return v, _b64(v.public_key.public_bytes(serialization.Encoding.X962,
                                             serialization.PublicFormat.UncompressedPoint))


class Push:
    """`send(subscription row, body) -> HTTP status` is the push service's POST; a test
    gives its own."""

    def __init__(self, cfg, store, bus, auth=None, send=None):
        self.store, self.bus, self.auth = store, bus, auth
        self.vapid, self.public_key = keys(secrets.secrets_dir(cfg.data_dir))
        self.cfg = cfg
        self.send = send or self._post
        self.focused: set = set()          # the sockets of pages open and focused

    @property
    def sub(self) -> str:
        """The RFC 8292 contact: the app's address's host."""
        return f"https://{urlsplit(address.base(self.cfg)).hostname or 'localhost'}"

    def focus(self, page, on: bool) -> None:
        (self.focused.add if on else self.focused.discard)(page)

    async def run(self) -> None:
        """Every notice of the global feed, for as long as the app runs; the bus drops a
        slow reader, so this one hands each notice to a task and subscribes again."""
        async with anyio.create_task_group() as tg:
            while True:
                tx, rx = self.bus.subscribe("global")
                try:
                    async with rx:
                        async for ev in rx:
                            if ev["type"] == NOTICE and not self.focused:
                                tg.start_soon(self.push, ev)
                finally:
                    self.bus.unsubscribe("global", tx)

    async def forget_ended(self) -> None:
        """With the login on: the subscriptions of sessions that ended, and those made
        while it was off, are deleted."""
        if self.auth is None:
            return
        live = [s.id for s in self.auth.store.sessions()]

        def fn(c: sqlite3.Connection) -> None:
            if c.execute(f"DELETE FROM push_subscriptions WHERE session_id IS NULL OR "
                         f"session_id NOT IN ({','.join('?' * len(live))})",
                         live).rowcount:
                self.store.global_event(c, PUSH_SUBSCRIPTIONS, {})
        await self.store.write(fn)

    async def push(self, notice: dict) -> None:
        """One notice to every subscription that takes its kind."""
        await self.forget_ended()
        urgency = "high" if notice["kind"] == "approval" else "normal"
        rows = await self.store.read(lambda c: c.execute(
            "SELECT * FROM push_subscriptions").fetchall())
        body = json.dumps({k: notice[k] for k in ("title", "body", "link", "tag")},
                          ensure_ascii=False).encode()
        for r in rows:
            if notice["kind"] not in json.loads(r["kinds"]):
                continue
            try:
                status = await self.send(r, body, urgency)
            except httpx.HTTPError as e:
                log.warning("push failed", extra={"event": "push.failed", "device": r["device"],
                                                  "error": str(e)})
                continue
            except Exception:           # a bug: logged, the app and the other devices go on
                log.exception("push failed", extra={"event": "push.failed",
                                                    "device": r["device"]})
                continue
            if status in GONE:
                await self.store.write(lambda c, i=r["id"]: _delete(c, self.store, i))
            elif status >= 400:
                log.warning("push refused", extra={"event": "push.refused",
                                                   "device": r["device"], "status": status})

    async def _post(self, r: sqlite3.Row, body: bytes, urgency: str) -> int:
        data = http_ece.encrypt(body, salt=os.urandom(16),
                                private_key=ec.generate_private_key(ec.SECP256R1()),
                                dh=_unb64(r["p256dh"]), auth_secret=_unb64(r["auth"]),
                                version="aes128gcm")
        end = urlsplit(r["endpoint"])
        headers = {**self.vapid.sign({"aud": f"{end.scheme}://{end.netloc}", "sub": self.sub}),
                   "TTL": str(TTL), "Urgency": urgency, "Content-Encoding": "aes128gcm",
                   "Content-Type": "application/octet-stream"}
        async with httpx.AsyncClient(timeout=30) as client:
            return (await client.post(r["endpoint"], content=data, headers=headers)).status_code


def _delete(c: sqlite3.Connection, store, sid: int) -> bool:
    gone = c.execute("DELETE FROM push_subscriptions WHERE id=?", (sid,)).rowcount > 0
    if gone:
        store.global_event(c, PUSH_SUBSCRIPTIONS, {})
    return gone


def _shown(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "device": r["device"], "endpoint": r["endpoint"],
            "kinds": json.loads(r["kinds"]), "created_at": r["created_at"]}


def _push(request: Request) -> Push:
    return request.app.state.services.push


@router.get("/push/key")
async def public_key(request: Request) -> dict:
    """The VAPID public key a browser subscribes with (applicationServerKey)."""
    return {"key": _push(request).public_key}


@router.get("/push-subscriptions")
async def subscriptions(request: Request) -> dict:
    """The devices that take pushes, oldest first."""
    await _push(request).forget_ended()
    rows = await _push(request).store.read(lambda c: c.execute(
        "SELECT * FROM push_subscriptions ORDER BY id").fetchall())
    return {"subscriptions": [_shown(r) for r in rows]}


class Keys(BaseModel):
    p256dh: str
    auth: str


class SubscriptionBody(BaseModel):
    """A browser's PushSubscription as toJSON() gives it, and the notice kinds it takes."""
    endpoint: str
    keys: Keys
    kinds: list[str]


@router.post("/push-subscriptions")
async def subscribe(request: Request, body: SubscriptionBody) -> dict:
    """A device takes pushes; the same endpoint again replaces its keys and kinds."""
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _unb64(body.keys.p256dh))
        ok = body.endpoint.startswith("https://") and len(_unb64(body.keys.auth)) == 16
    except ValueError:
        ok = False
    if not ok:
        raise CelineError("invalid", "That is not a push subscription.")
    name = device(request.headers.get("user-agent", ""))
    session = getattr(request.state, "session", None)     # none with the login off
    store = _push(request).store

    def fn(c: sqlite3.Connection) -> sqlite3.Row:
        row = c.execute(
            "INSERT INTO push_subscriptions(endpoint, p256dh, auth, device, kinds, session_id, "
            "created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET "
            "p256dh=excluded.p256dh, auth=excluded.auth, device=excluded.device, "
            "kinds=excluded.kinds, session_id=excluded.session_id RETURNING *",
            (body.endpoint, body.keys.p256dh, body.keys.auth, name, json.dumps(body.kinds),
             session.id if session is not None else None, now())).fetchone()
        store.global_event(c, PUSH_SUBSCRIPTIONS, {})
        return row
    return _shown(await store.write(fn))


@router.delete("/push-subscriptions/{sid}")
async def unsubscribe(request: Request, sid: int) -> dict:
    store = _push(request).store
    if not await store.write(lambda c: _delete(c, store, sid)):
        raise NotFound("subscription")
    return {"deleted": True}
