"""Discord over REST with the user's own login. Read only and rate limited.

Discord's terms forbid automating a user account. This client only reads, rate
limited, and never sends; docs/DEPLOY.md states the risk.

Every request goes through `Rest.request`: one at a time, at least `interval` seconds
apart, shared by the sync and every client's tools. Only GET exists: any other method
raises WriteRefused before a socket is opened.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import httpx

from .errors import Ctx, DiscordError, Unreachable, WriteRefused
from .state import State, now_ms

log = logging.getLogger("discord_link.rest")

API = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) celine-net discord-link"


class Rest:
    def __init__(self, state: State, *, interval: float = 1.2, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.state = state
        self.interval = interval
        self.sleep = sleep
        self._gate = threading.Lock()
        self._last = 0.0
        self.client = httpx.Client(transport=transport,
                                   timeout=httpx.Timeout(30.0, connect=10.0))

    def close(self) -> None:
        self.client.close()

    # ---- the one door ------------------------------------------------------------------
    def request(self, method: str, path: str, *, params: dict | None = None,
                ctx: Ctx | None = None, raw: bool = False) -> Any:
        """One request, spaced from the previous one, retried on 429 and 5xx. No answer
        at all is Unreachable at once: the cursor and the schedule do the retrying."""
        method = method.upper()
        if method != "GET":
            raise WriteRefused(f"{method} {path} is not allowed")
        token = self.state.token()
        if not token:
            raise DiscordError("Not logged in to Discord. Log in with the QR code.")
        headers = {"Authorization": token, "User-Agent": UA}
        for attempt in range(8):
            if ctx is not None:
                ctx.check()
            with self._gate:
                wait = self._last + self.interval - time.monotonic()
                if wait > 0:
                    self.sleep(wait)
                self._last = time.monotonic()
            try:
                r = self.client.request(method, f"{API}{path}", headers=headers,
                                        params=params)
            except httpx.HTTPError:
                raise Unreachable("Discord is not reachable.") from None
            if r.status_code == 429:
                try:
                    after = float(r.json().get("retry_after", 5))
                except ValueError:
                    after = 5.0
                self.sleep(after + 0.5)
                continue
            if r.status_code >= 500:
                self.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code == 401:
                self.state.set("token_rejected", now_ms())
                raise DiscordError("Discord no longer accepts this login. Log in again "
                                   "with the QR code.")
            if r.status_code == 403:
                raise PermissionError(path)
            if r.status_code == 404:
                raise FileNotFoundError(path)
            if r.status_code >= 400:
                raise DiscordError(f"Discord refused the request ({r.status_code}).")
            if raw:
                return r
            if r.status_code == 204 or not r.content:
                return None
            return r.json()
        raise DiscordError("Discord kept rate limiting this request.")

    def get(self, path: str, params: dict | None = None, ctx: Ctx | None = None) -> Any:
        return self.request("GET", path, params=params, ctx=ctx)
