"""Log in to Discord by QR code, the way the desktop app does.

discord-link opens Discord's remote-auth gateway, shows the QR code it gets, and the
user scans it with the Discord app and confirms. The token that comes back is written
to `<state>/token` (mode 600); no endpoint ever returns it. Protocol:
https://docs.discord.food/remote-authentication/desktop

    hello -> init(public key) -> nonce_proof -> pending_remote_init(fingerprint)
    -> [scan] pending_ticket(user) -> [confirm] pending_login(ticket)
    -> POST /users/@me/remote-auth/login -> encrypted_token

One login session at a time. A session lives as long as Discord's `timeout_ms`
(about two minutes); an expired one says so and offers a new code.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .state import State

log = logging.getLogger("discord_link.login")

GATEWAY = "wss://remote-auth-gateway.discord.gg/?v=2"
API = "https://discord.com/api/v9"
ORIGIN = "https://discord.com"
OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(),
                    label=None)


class LoginFailed(Exception):
    """Why the login did not finish, in one sentence for the user."""


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def qr_svg(url: str) -> str:
    import segno
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#2a1d24",
                                    light="#ffffff", xmldecl=False, svgns=True, omitsize=True)
    return buf.getvalue().decode()


@dataclass
class Session:
    id: str
    state: str = "starting"      # starting, waiting, scanned, done, failed, cancelled, expired
    qr_url: str | None = None
    qr_svg: str | None = None
    user: dict[str, Any] | None = None
    error: str | None = None
    expires_at: int | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def live(self) -> bool:
        return self.state in ("starting", "waiting", "scanned")

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "qr_url": self.qr_url,
                "qr_svg": self.qr_svg, "user": self.user, "error": self.error,
                "expires_at": self.expires_at}


class Logins:
    def __init__(self, state: State, *, on_done: Callable[[], None] | None = None,
                 connect: Callable[..., Any] | None = None,
                 exchange: Callable[..., Any] | None = None):
        self.state = state
        self.on_done = on_done
        self._connect = connect
        self._exchange_fn = exchange
        self.session: Session | None = None

    def current(self) -> Session | None:
        return self.session

    def start(self) -> Session:
        if self.session is not None and self.session.live:
            return self.session
        s = Session(id=f"l_{uuid.uuid4().hex[:10]}")
        self.session = s
        s.task = asyncio.get_running_loop().create_task(self._run(s))
        return s

    def cancel(self, sid: str) -> bool:
        s = self.session
        if s is None or s.id != sid or s.task is None or s.task.done():
            return False
        s.task.cancel()
        return True

    def _open(self):
        if self._connect is not None:
            return self._connect(GATEWAY)
        import websockets
        return websockets.connect(GATEWAY, origin=ORIGIN, open_timeout=20, max_size=2**20)

    async def _run(self, s: Session) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        spki = key.public_key().public_bytes(serialization.Encoding.DER,
                                             serialization.PublicFormat.SubjectPublicKeyInfo)
        try:
            async with self._open() as ws:
                beat: asyncio.Task | None = None
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        op = msg.get("op")
                        if op == "hello":
                            interval = msg["heartbeat_interval"] / 1000
                            s.expires_at = int(time.time() * 1000) + int(msg.get("timeout_ms", 120000))

                            async def heartbeat() -> None:
                                while True:
                                    await asyncio.sleep(interval)
                                    await ws.send(json.dumps({"op": "heartbeat"}))
                            beat = asyncio.create_task(heartbeat())
                            await ws.send(json.dumps(
                                {"op": "init", "encoded_public_key": base64.b64encode(spki).decode()}))
                        elif op == "nonce_proof":
                            nonce = key.decrypt(base64.b64decode(msg["encrypted_nonce"]), OAEP)
                            await ws.send(json.dumps({"op": "nonce_proof", "nonce": b64url(nonce)}))
                        elif op == "pending_remote_init":
                            fp = msg["fingerprint"]
                            if fp != b64url(hashlib.sha256(spki).digest()):
                                raise LoginFailed("Discord's answer did not match this "
                                                  "login's key. Try a new code.")
                            s.qr_url = f"https://discord.com/ra/{fp}"
                            s.qr_svg = qr_svg(s.qr_url)
                            s.state = "waiting"
                        elif op == "pending_ticket":
                            payload = key.decrypt(base64.b64decode(msg["encrypted_user_payload"]),
                                                  OAEP).decode()
                            uid, _disc, avatar, name = (payload.split(":", 3) + ["", "", ""])[:4]
                            s.user = {"id": uid, "name": name,
                                      "avatar": f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size=128"
                                      if avatar and avatar != "0" else None}
                            s.state = "scanned"
                        elif op == "pending_login":
                            token = await self._exchange(key, msg["ticket"])
                            await asyncio.to_thread(self.state.save_token, token)
                            s.state = "done"
                            s.qr_svg = None
                            log.info("discord login done", extra={"event": "login.done"})
                            if self.on_done is not None:
                                self.on_done()
                            return
                        elif op == "cancel":
                            s.state = "cancelled"
                            s.error = "The login was declined on the phone."
                            return
                finally:
                    if beat is not None:
                        beat.cancel()
                if s.live:
                    code = getattr(ws, "close_code", None)
                    s.state = "expired" if code == 4003 else "failed"
                    s.error = ("The code expired before it was confirmed."
                               if code == 4003 else f"Discord closed the login (code {code}).")
        except asyncio.CancelledError:
            s.state = "cancelled"
            s.error = None
            raise
        except Exception as e:
            s.state = "failed"
            s.error = str(e) if isinstance(e, LoginFailed) else \
                f"The login failed ({type(e).__name__})."
            log.warning("discord login failed", extra={"event": "login.failed",
                                                       "error": type(e).__name__})

    async def _exchange(self, key, ticket: str) -> str:
        if self._exchange_fn is not None:
            enc = await self._exchange_fn(ticket)
        else:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{API}/users/@me/remote-auth/login",
                                      json={"ticket": ticket},
                                      headers={"Origin": ORIGIN,
                                               "Content-Type": "application/json"})
            if r.status_code >= 400:
                if "captcha" in r.text[:300]:
                    raise LoginFailed("Discord asked for a captcha, which this login "
                                      "cannot show.")
                raise LoginFailed(f"Discord refused the login ({r.status_code}).")
            enc = r.json()["encrypted_token"]
        return key.decrypt(base64.b64decode(enc), OAEP).decode()
