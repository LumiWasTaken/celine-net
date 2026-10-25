"""The app's side of the connectors (discord-link, wa-link): an MCP server block in
config/mcp.toml with `kb = "<archive>"` and a streamable HTTP `url`.

For each one the app
- runs the feed (kb/feed.py) as a quiet job `feed` of that archive: it streams the
  connector's journal into the archive and ends Unreachable when the connector does
  not answer; it is started again on its own;
- asks `/admin/status` every few seconds and publishes the connection record as
  `{"type": "source.state", "kb", "connection", "login_lost_at"}` when it changes;
- publishes `{"type": "kb.changed", "kb"}` after a feed batch that changed the archive,
  at most about once a second per archive, so open chat lists and threads follow live;
- notices a login that was lost (the phone unlinked the device, Discord refused the
  token): kv `login_lost_at` in the archive, set when a source that was connected
  with an account needs a login, cleared when it is connected again. The app shows a
  banner for it until then.
- forwards the source page's calls to the connector's `/admin/*` doors (api/connectors.py).

The connector's base url is the block's url without `/mcp`; the block's headers carry
this instance's key.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Callable

import anyio
import httpx

from ..core.ids import now_ms
from .feed import Applied, Consumer, KeyRefused
from .importers import Failed, ImportContext, Unreachable
from .media_text import file_shas
from ..core.event_types import KB_CHANGED, SOURCE_STATE

log = logging.getLogger("celine.kb.connectors")

POLL_SECONDS = 3.0
# a feed that did not end done tries again after these, the last one from then on; a
# refused key waits until the connection record changes (a new key, a login)
FEED_BACKOFF_MS = (10_000, 60_000, 600_000)
CHANGED_EVERY = 1.0             # seconds between two kb.changed of one archive


class Throttle:
    """Calls `fn` at most once per `every` seconds, from any thread. A call inside the
    interval is made at its end, so the last change is always told."""

    def __init__(self, fn: Callable[[], None], every: float):
        self.fn = fn
        self.every = every
        self._lock = threading.Lock()
        self._last = float("-inf")
        self._timer: threading.Timer | None = None

    def __call__(self) -> None:
        with self._lock:
            if self._timer is not None:
                return
            wait = self._last + self.every - time.monotonic()
            if wait > 0:
                self._timer = threading.Timer(wait, self._fire)
                self._timer.daemon = True
                self._timer.start()
                return
            self._last = time.monotonic()
        self.fn()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            self._last = time.monotonic()
        self.fn()


class Refused(Exception):
    """A sentence for the user: the connector (or the archive) said no. `code`: the
    connector's HTTP status."""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {}


def base_url(url: str) -> str:
    url = url.rstrip("/")
    return url.removesuffix("/mcp")


class Connector:
    def __init__(self, server: str, url: str, headers: dict[str, str], kb, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sync_transport: httpx.BaseTransport | None = None):
        self.server = server
        self.kb = kb
        self.base = base_url(url)
        self.headers = dict(headers)
        self._transport = transport
        self._sync_transport = sync_transport
        self.status: dict[str, Any] | None = None
        self.down_since: int | None = None
        self.refused: str | None = None
        self.changed: Callable[[], None] | None = None       # kb.changed, throttled
        self.lost_at: int | None = kb.file.get_json("login_lost_at")
        self.feed_fails = 0                 # feeds in a row that did not end done
        self.feed_refused = False           # the last feed was refused this app's key

    @property
    def title(self) -> str:
        return self.kb.title

    @property
    def key(self) -> str | None:
        auth = self.headers.get("Authorization") or self.headers.get("authorization") or ""
        return auth[7:] if auth.startswith("Bearer ") else None

    def _down(self) -> str:
        return f"The {self.title} connection is not running."

    # ---- the admin doors -----------------------------------------------------------------
    def _send(self, c, method: str, path: str, body: Any):
        """One admin call on either client: a Response, or for the async one its awaitable."""
        return c.request(method, f"{self.base}{path}", json=body if method != "GET" else None)

    async def raw(self, method: str, path: str, body: Any = None,
                  timeout: float = 15.0) -> httpx.Response:
        try:
            async with httpx.AsyncClient(transport=self._transport, headers=self.headers,
                                         timeout=timeout) as c:
                return await self._send(c, method, path, body)
        except httpx.HTTPError:
            raise Unreachable(self._down()) from None

    def raw_sync(self, method: str, path: str, body: Any = None,
                 timeout: float = 120.0) -> httpx.Response:
        try:
            with httpx.Client(transport=self._sync_transport, headers=self.headers,
                              timeout=timeout) as c:
                return self._send(c, method, path, body)
        except httpx.HTTPError:
            raise Unreachable(self._down()) from None

    async def request(self, method: str, path: str, body: Any = None,
                      timeout: float = 15.0) -> tuple[int, Any]:
        r = await self.raw(method, path, body, timeout)
        return r.status_code, _json(r)

    def request_sync(self, method: str, path: str, body: Any = None,
                     timeout: float = 30.0) -> tuple[int, Any]:
        r = self.raw_sync(method, path, body, timeout)
        return r.status_code, _json(r)

    def refused(self, code: int, data: Any = None) -> "Refused":
        """The connector's error answer as one sentence: its own `error`, else the code."""
        msg = data.get("error") if isinstance(data, dict) else None
        return Refused(msg or f"The {self.title} connection refused the request ({code}).", code)

    async def admin(self, method: str, path: str, body: Any = None) -> Any:
        """An admin call's answer; Refused when the connector said no, Unreachable."""
        code, data = await self.request(method, path, body)
        if code >= 400:
            raise self.refused(code, data)
        return data

    def admin_sync(self, method: str, path: str, body: Any = None) -> Any:
        code, data = self.request_sync(method, path, body)
        if code >= 400:
            raise self.refused(code, data)
        return data

    def pull(self, sha: str) -> bool:
        """Copy one file the connector keeps into this archive's media folder."""
        from .media import media_dir
        d = media_dir(self.kb)
        p = d / sha
        if p.exists():
            return True
        try:
            r = self.raw_sync("GET", f"/admin/media/{sha}")
        except Unreachable:
            return False
        if r.status_code != 200 or hashlib.sha256(r.content).hexdigest() != sha:
            return False
        d.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(r.content)
        tmp.replace(p)
        return True

    def pull_files(self, applied: Applied) -> None:
        """Copy every file the batch's messages carry."""
        for r in applied.messages:
            for sha in file_shas(r["message"].get("media")):
                self.pull(sha)

    def _applied(self, applied: Applied) -> None:
        """After each feed batch: copy the files its messages carry, tell open pages
        when the archive changed."""
        self.pull_files(applied)
        if self.changed is not None and (applied.messages or applied.edits
                                         or applied.deleted):
            self.changed()

    # ---- state -------------------------------------------------------------------------
    def connection(self) -> dict[str, Any]:
        """The connection record the source page shows. A connector that does not
        answer is unreachable, with one plain sentence."""
        if self.status is None:
            return {"state": "unreachable", "since": self.down_since,
                    "error": self.refused or self._down(), "last_sync": None,
                    "next_sync": None}
        return dict(self.status.get("connection") or {})

    async def poll(self) -> bool:
        """The status from the connector. True when the connection record changed."""
        before = self.connection()
        try:
            code, status = await self.request("GET", "/admin/status", timeout=5.0)
            if code == 401:
                raise Unreachable(f"The {self.title} connection does not know this app's key "
                                  f"(its clients file is not the app's keys/connector-clients).")
            if code != 200:
                raise Unreachable(self._down())
            self.status, self.down_since = status, None
            self.refused = None
        except Unreachable as e:
            if self.status is not None or self.down_since is None:
                self.down_since = now_ms()
            self.status, self.refused = None, str(e)
        after = self.connection()
        lost = self.lost_at
        if self.status is not None:
            await anyio.to_thread.run_sync(self._note_login)
        return (before.get("state"), before.get("error"), lost) != \
            (after.get("state"), after.get("error"), self.lost_at)

    def _note_login(self) -> None:
        state = self.connection().get("state")
        account = (self.status or {}).get("account")
        if state == "connected" and account:
            if self.kb.file.get_json("login_account") != account:
                self.kb.file.set_json("login_account", account)
            if self.lost_at is not None:
                self.kb.file.set_json("login_lost_at", None)
                self.lost_at = None
        elif state == "needs_login" and self.kb.file.get_json("login_account") is not None:
            self.lost_at = now_ms()
            self.kb.file.set_json("login_lost_at", self.lost_at)
            self.kb.file.set_json("login_account", None)
            log.info("login lost", extra={"event": "connection.login_lost",
                                          "kb": self.kb.id})

    # ---- the feed ----------------------------------------------------------------------
    def feed(self, ctx: ImportContext) -> dict[str, Any]:
        try:
            got = Consumer(self.kb, name=self.title, on_batch=self._applied).run(
                ctx, self.base, self.key, transport=self._sync_transport)
        except KeyRefused:
            self.feed_refused = True
            raise
        except Exception:
            self.feed_fails += 1
            raise
        self.feed_fails = 0
        return got

    def feed_due(self, last: dict[str, Any] | None) -> bool:
        """Whether the feed starts again now, after its last run `last` (tasks.job)."""
        if last is None or last["state"] == "done":
            return True
        if self.feed_refused:
            return False
        wait = FEED_BACKOFF_MS[min(max(self.feed_fails, 1), len(FEED_BACKOFF_MS)) - 1]
        return now_ms() - last["finished_at"] >= wait

    def follow(self, job: str, ctx: ImportContext, *, every: float = 1.0,
               sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
        """Mirror one of the connector's jobs (a sync, a server's history) as this
        task: its progress, its end. Cancelling the task cancels the job."""
        while True:
            if ctx.cancelled():
                self.request_sync("DELETE", f"/admin/jobs/{job}")
                ctx.check()
            code, j = self.request_sync("GET", f"/admin/jobs/{job}")
            if code != 200:
                raise Failed(f"The {self.title} connection lost this task.")
            p = j.get("progress") or {}
            ctx.progress(int(p.get("done") or 0), p.get("total"), p.get("unit") or "")
            if j["state"] == "done":
                return j.get("result") or {}
            if j["state"] == "unreachable":
                raise Unreachable(j.get("error") or f"{self.title} is not reachable.")
            if j["state"] == "failed":
                raise Failed(j.get("error") or f"The {self.title} connection failed.")
            if j["state"] == "cancelled":
                ctx.check()
                return {}
            sleep(every)

    def first_import(self, job: str | None, ctx: ImportContext, *, every: float = 1.0,
                     sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
        """The first import (the onboarding's Import): its progress is the archive's
        message count. It waits for the connector's job `job` (Discord's sync) to end,
        then for the feed to apply the connector's journal up to its head as it was
        then; WhatsApp has no job, its phone's history is in the journal already.
        Cancelling the task cancels the job."""
        from .feed import current_seq
        head: int | None = None
        while True:
            if ctx.cancelled():
                if job is not None:
                    self.request_sync("DELETE", f"/admin/jobs/{job}")
                ctx.check()
            if job is not None:
                code, j = self.request_sync("GET", f"/admin/jobs/{job}")
                if code != 200:
                    raise Failed(f"The {self.title} connection lost this task.")
                if j["state"] == "unreachable":
                    raise Unreachable(j.get("error") or f"{self.title} is not reachable.")
                if j["state"] == "failed":
                    raise Failed(j.get("error") or f"The {self.title} connection failed.")
                if j["state"] in ("done", "cancelled"):
                    job = None
            if job is None and head is None:
                code, st = self.request_sync("GET", "/admin/status")
                if code != 200:
                    raise Unreachable(self._down())
                head = int((st.get("journal") or {}).get("head") or 0)
            n = self.kb.file.read_sync(lambda c: c.execute(
                "SELECT COALESCE(SUM(message_count), 0) FROM chats").fetchone()[0])
            ctx.progress(n, None, "messages")
            if head is not None and current_seq(self.kb) >= head:
                return {"messages": n, "sentence": f"Done. {n:,} message"
                                                   f"{'' if n == 1 else 's'} in the archive."}
            sleep(every)


class Connectors:
    """Every connector of this instance, its feed job and its state events."""

    def __init__(self, cfg, kbs, publish: Callable[[dict], None]):
        self.cfg = cfg
        self.kbs = kbs
        self.publish = publish
        self.all: dict[str, Connector] = {}   # kb id -> its connector, while its feature is on

    def open(self, sid: str) -> None:
        """The connector of MCP server `sid`, once its archive is open (kbs.open)."""
        scfg = self.cfg.mcp.get(sid)
        kb = self.kbs.get(scfg.kb) if scfg is not None and scfg.enabled and scfg.kb else None
        if kb is None or kb.id in self.all or not scfg.url or \
                scfg.transport not in ("http", "streamable-http"):
            return
        c = Connector(scfg.id, scfg.url, scfg.headers, kb)
        c.changed = Throttle(lambda kid=kb.id: self.kbs.tasks.emit(
            {"type": KB_CHANGED, "kb": kid}), CHANGED_EVERY)
        self.all[kb.id] = c

    def close(self, sid: str) -> None:
        scfg = self.cfg.mcp.get(sid)
        if scfg is not None and scfg.kb:
            self.all.pop(scfg.kb, None)

    def get(self, kb_id: str) -> Connector | None:
        return self.all.get(kb_id)

    def states(self) -> list[dict[str, Any]]:
        """What the app shell needs of every connector, from the last check."""
        return [{"kb": kid, "platform": c.kb.platform, "title": c.title,
                 "connection": c.connection(), "account": (c.status or {}).get("account"),
                 "login_lost_at": c.lost_at} for kid, c in self.all.items()]

    def _feed(self, c: Connector) -> None:
        tasks = self.kbs.tasks
        if tasks.running(c.kb.id, "feed") is not None:
            return
        if not c.feed_due(tasks.job(c.kb.id, "feed")):
            return
        tasks.start(c.kb.id, "feed", f"{c.title} sync", c.feed, quiet=True)

    async def run(self, every: float = POLL_SECONDS) -> None:
        while True:
            for c in list(self.all.values()):
                try:
                    if await c.poll():
                        c.feed_fails, c.feed_refused = 0, False
                        conn = c.connection()
                        log.info("connection state", extra={
                            "event": "connection.state", "kb": c.kb.id,
                            "state": conn.get("state"), "error": conn.get("error")})
                        self.publish({"type": SOURCE_STATE, "kb": c.kb.id,
                                      "connection": conn, "login_lost_at": c.lost_at})
                    self._feed(c)
                except Exception:
                    log.exception("connector check failed")
            await anyio.sleep(every)


__all__ = ["Connector", "Connectors", "Refused", "base_url"]
