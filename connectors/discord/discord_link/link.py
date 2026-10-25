"""The connector as one object: the account, the connection, the schedule, the
clients. The doors (app.py) and the tools (tools.py) call it. Nothing is ever sent
from the user's account: the connector only reads."""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from . import sync
from .gateway import Gateway
from .jobs import Jobs, due_at
from .login import Logins
from .rest import Rest
from .state import State, now_ms

log = logging.getLogger("discord_link")


@dataclass(frozen=True)
class Client:
    name: str
    key: str
    role: str                   # owner | reader


def parse_clients(spec: str) -> dict[str, Client]:
    """The clients file: "name:key:role", comma or line separated, role owner or reader."""
    out: dict[str, Client] = {}
    for part in (spec or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 3 or bits[2] not in ("owner", "reader") or not bits[0] or \
                len(bits[1]) < 16:
            raise ValueError("a client is name:key:role, role owner or "
                             "reader, keys of at least 16 characters")
        out[bits[1]] = Client(bits[0], bits[1], bits[2])
    return out


class Refused(Exception):
    """A caller may not do this. One sentence."""


class Link:
    def __init__(self, state: State, *, clients: dict[str, Client],
                 interval: float = 1.2,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 gateway_connect: Callable[..., Any] | None = None,
                 login_connect: Callable[..., Any] | None = None,
                 login_exchange: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 sync_every_minutes: float = 360):
        self.state = state
        self.clients = clients
        self.rest = Rest(state, interval=interval, transport=transport, sleep=sleep)
        self.jobs = Jobs()
        self.gateway = Gateway(state, on_back=lambda: self.start_sync(),
                               connect=gateway_connect)
        self.logins = Logins(state, on_done=self._logged_in, connect=login_connect,
                             exchange=login_exchange)
        self.clock = clock
        self.every_ms = float(sync_every_minutes) * 60_000
        self._conn: tuple[str, int] = ("needs_login", now_ms())

    def _now(self) -> int:
        return int(self.clock() * 1000)

    # ---- clients -----------------------------------------------------------
    def client(self, auth: str | None) -> Client | None:
        if not auth or not auth.startswith("Bearer "):
            return None
        key = auth[7:].strip().encode()
        return next((c for k, c in self.clients.items()
                     if hmac.compare_digest(k.encode(), key)), None)

    @staticmethod
    def need_owner(c: Client) -> None:
        if c.role != "owner":
            raise Refused("This app instance may only read from the Discord link.")

    # ---- state -------------------------------------------------------------------------
    def connection(self) -> dict[str, Any]:
        gw = self.gateway
        token = self.state.token()
        rejected = self.state.get("token_rejected")
        unreachable_since = self.state.get("sync_unreachable_since")
        error = None
        if not token or rejected:
            name = "needs_login"
            error = "Discord no longer accepts this login." if rejected and token else None
        elif gw.state_name == "connected" or (gw.state_name in ("stopped", "connecting")
                                              and not gw.last_error and not unreachable_since):
            name = "connected"
        else:
            name = "unreachable"
            error = gw.last_error or "Discord is not reachable."
        if name != self._conn[0]:
            self._conn = (name, now_ms())
            log.info("connection state %s: %s", name, error or "no problem",
                     extra={"event": "connection.state", "state": name, "reason": error})
        last_sync = self.state.get("last_sync")
        return {"state": name, "since": self._conn[1], "error": error, "last_sync": last_sync,
                "next_sync": self.next_sync() if name != "needs_login" else None}

    def next_sync(self) -> int:
        return due_at(self.every_ms, last_ok=self.state.get("last_sync"),
                      last_attempt=self.state.get("sync_last_attempt"),
                      unreachable=bool(self.state.get("sync_unreachable_since")))

    def status(self) -> dict[str, Any]:
        s = self.logins.current()
        return {"connection": self.connection(), "account": self.state.get("account"),
                "gateway": self.gateway.as_dict(), "journal": {"head": self.state.head},
                "sync_every_minutes": self.every_ms / 60_000,
                "login": s.as_dict() if s is not None and s.state != "done" else None}

    # ---- work --------------------------------------------------------------------------
    def start_sync(self):
        return self.jobs.start("sync", "Discord sync",
                               lambda ctx: sync.sync(self.rest, ctx))

    def start_chats(self, keys: list[str]):
        """Chats included again: read from the start."""
        return self.jobs.start("chats:" + ",".join(sorted(keys)), "Discord chats",
                               lambda ctx: sync.sync(self.rest, ctx, keys=set(keys)))

    def start_backfill(self, guild_id: str, label: str):
        return self.jobs.start(f"subscribe:{guild_id}", label,
                               lambda ctx: sync.backfill_guild(self.rest, guild_id, ctx))

    def start_channels(self, guild_id: str, before: list[str], label: str):
        return self.jobs.start(f"channels:{guild_id}", label,
                               lambda ctx: sync.backfill_channels(self.rest, guild_id,
                                                                  before, ctx))

    def reload_subs(self) -> None:
        self.gateway.reload()

    def _logged_in(self) -> None:
        self.gateway.dropped_at = None
        self.gateway.start()
        self.start_sync()

    async def logout(self) -> bool:
        """The stored token is removed; Discord is not told, the token stays valid there."""
        await self.gateway.stop()
        ok = self.state.forget_token()
        self.state.set("token_rejected", None)
        return ok

    async def set_token(self, token: str) -> None:
        """A token pasted by the user replaces the stored one; the gateway starts on it."""
        await self.gateway.stop()
        await asyncio.to_thread(self.state.save_token, token)
        self._logged_in()

    async def run(self, every: float = 20.0) -> None:
        """Keep the gateway up while there is a login, and run the schedule."""
        try:
            while True:
                try:
                    await self._tick()
                except Exception:
                    log.exception("schedule failed")
                await asyncio.sleep(every)
        finally:
            await self.gateway.stop()

    async def _tick(self) -> None:
        token = self.state.token()
        rejected = self.state.get("token_rejected")
        gw = self.gateway
        running = gw.task is not None and not gw.task.done()
        if token and not rejected:
            if not running:
                gw.start()
            if self._now() >= self.next_sync():
                self.start_sync()
        elif running:
            await gw.stop()

