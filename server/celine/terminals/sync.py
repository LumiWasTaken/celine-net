"""The app follows its terminals (docs/ARCHITECTURE.md, terminals): every few seconds
the daemon's listing becomes terminal rows and their live state, rows end the way they
ended, each terminal's new output is appended to its history in the data dir, and every
open page hears a bare terminal.state when something changed. The daemon calls the
terminal routes make go through here too, with the one key.

A stored history holds no known secret (errors.blank): a vault password it printed with
cat is stars there, byte for byte. The live view to an open page is not blanked."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import httpx

from .. import errors, shellbox
from ..core.ids import iso
from ..errors import ToolRefused, unreachable_words
from ..turns.dispatcher import READY_SECONDS
from . import store as terminals
from ..core.event_types import TERMINAL_STATE

log = logging.getLogger("celine.terminals")

EVERY = 5               # seconds between rounds
MISSES = 3              # rounds without an answer before live goes null
LIVE = ("shell", "cwd", "program", "pid", "busy", "paused", "fullscreen", "waiting",
        "running_for", "quiet_for", "last_exit")
WATCHED = ("busy", "paused", "fullscreen", "waiting", "program")   # a change pages hear of
IDLE = 24 * 3600        # seconds quiet with nothing running before its terminal closes
                        # (the box's IDLE_CLOSE only lets a new terminal take its place)


class TerminalSync:
    def __init__(self, cfg, store, transport=None):
        self.cfg = cfg
        self.store = store
        self.transport = transport                  # tests: a fake daemon
        self.live: dict[str, dict] = {}             # id -> live state of an open terminal
        self.names: set[str] = set()                # the daemon's terminal names, last seen
        self.boot_id: str | None = None
        self.misses = 0
        self.down: str | None = None               # why the last round got no listing
        self._told: tuple | None = None             # the state pages last heard of
        self.lock = anyio.Lock()                    # one round at a time: logs append once
        self.ready = None       # the vault's first unlock (__main__): its passwords blanked

    @property
    def url(self) -> str:
        """The shellbox's address as saved now (Settings, Features)."""
        return shellbox.url(self.cfg)

    async def run(self) -> None:
        """Follow the box until cancelled (the shellbox feature's runner, __main__)."""
        while True:
            try:
                if self.url:
                    await self.refresh()
            except Exception:
                log.exception("terminal sync failed", extra={"event": "terminals.sync_failed"})
            await anyio.sleep(EVERY)

    async def refresh(self) -> None:
        """One round now: the daemon's listing, which keeps its notes for it."""
        async with self.lock:
            ended: set[str] = set()
            try:
                status, listing = await self.post("/read", {}, read=15.0)
            except httpx.HTTPError as e:
                status, listing = 0, {}
                self.down = unreachable_words("the shellbox", self.url, e)
            except ToolRefused as e:
                status, listing = 0, {}
                self.down = str(e)
            if status != 200 or not isinstance(listing.get("terminals"), list):
                if status:
                    self.down = (f"The shellbox at {urlsplit(self.url).netloc} answered "
                                 f"{status} instead of its terminal list.")
                self.misses += 1
                if self.misses >= MISSES:
                    self.live, self.names = {}, set()
            else:
                self.misses, self.down = 0, None
                ended = await self._apply(listing)
                ended |= await self._sweep()
                await self._logs(ended)
            told = tuple(sorted((tid, *(d.get(k) for k in WATCHED))
                                for tid, d in self.live.items()))
            if ended or told != self._told:
                self._told = told
                self.store.emit_global({"type": TERMINAL_STATE})

    async def _apply(self, listing: dict) -> set[str]:
        """The listing into rows, in this order: a reset ends every open row, a dead
        terminal ends with its exit, a stored name that is missing or carries
        another id now is gone, then every listed terminal has its row. Returns the ids of
        the rows that ended."""
        store = self.store
        rows = [r for r in listing["terminals"] if r.get("id") and r.get("name")]
        ended = set()
        boot = listing.get("boot_id")
        if self.boot_id is not None and boot != self.boot_id:
            for t in await terminals.list_terminals(store):
                if t["ended_at"] is None:
                    await terminals.end(store, t["id"], "reset", None)
                    ended.add(t["id"])
        self.boot_id = boot
        open_rows = {}
        for r in rows:
            if not r.get("dead"):
                open_rows[r["id"]] = r
                continue
            await terminals.upsert(store, r["id"], r["name"], r.get("owner") or "celine",
                                   _iso(r.get("opened_at")))
            t = await terminals.get(store, r["id"])
            if t and t["ended_at"] is None:
                await terminals.end(store, r["id"], "exit", r.get("exit"))
                ended.add(r["id"])
        by_name = {r["name"]: r["id"] for r in rows}
        for t in await terminals.list_terminals(store):
            if t["ended_at"] is None and by_name.get(t["name"]) != t["id"]:
                await terminals.end(store, t["id"], "gone", None)
                ended.add(t["id"])
        for r in open_rows.values():
            await terminals.upsert(store, r["id"], r["name"], r.get("owner") or "celine",
                                   _iso(r.get("opened_at")))
        self.live = {tid: {k: r.get(k) for k in LIVE} for tid, r in open_rows.items()}
        self.names = set(by_name)
        return ended

    async def _sweep(self) -> set[str]:
        """The agent's terminals with nothing running that printed nothing for IDLE seconds close
        (ended_by idle); the user's own stay. Returns the ids that ended."""
        ended = set()
        for t in await terminals.list_terminals(self.store):
            live = self.live.get(t["id"])
            if t["owner"] == "celine" and live and not live.get("busy") \
                    and not live.get("program") and not live.get("paused") \
                    and (live.get("quiet_for") or 0) >= IDLE \
                    and await self._close(t, "idle"):
                ended.add(t["id"])
        return ended

    async def close_opened_by(self, cid: str) -> None:
        """Every open terminal conversation `cid` opened closes (ended_by done), whatever
        runs in it; its history is kept like any ended terminal's."""
        if not self.url:
            return
        async with self.lock:
            ended = {t["id"] for t in await terminals.opened_by(self.store, cid)
                     if await self._close(t, "done")}
            await self._logs(ended)
        if ended:
            self.store.emit_global({"type": TERMINAL_STATE})

    async def _close(self, t: dict, ended_by: str) -> bool:
        """The daemon closes the terminal and the row ends; False when it did not."""
        try:
            status, _ = await self.post("/control", {"terminal": t["name"],
                                                     "action": "close"}, 15.0)
        except (httpx.HTTPError, ToolRefused):
            return False
        if status != 200:
            return False
        await terminals.end(self.store, t["id"], ended_by, None)
        self.live.pop(t["id"], None)
        return True

    async def _logs(self, ended: set[str]) -> None:
        """The agent's history: every open row and every row that just ended has a .raw (one that
        opened and ended between two rounds too), and every .raw not packed yet gets the
        daemon's bytes after the stored ones. An ended row's log is gzipped once the
        daemon gave all it has; a failed fetch is tried again next round. Nothing is ever
        deleted."""
        if self.ready is not None:
            with anyio.move_on_after(READY_SECONDS):
                await self.ready.wait()
        folder = self.cfg.data_dir / "terminals"
        folder.mkdir(exist_ok=True)
        open_ids = {t["id"] for t in await terminals.list_terminals(self.store)
                    if t["ended_at"] is None}
        for tid in open_ids | ended:
            raw = terminals.log_path(self.cfg.data_dir, tid)
            if raw is not None and not terminals.packed(raw).exists():
                raw.touch()
        for raw in sorted(folder.glob("*.raw")):
            tid = raw.name.removesuffix(".raw")
            if await self._fetch(tid, raw) and tid not in open_ids:
                await anyio.to_thread.run_sync(terminals.pack, raw)

    async def _fetch(self, tid: str, raw: Path) -> bool:
        """GET /log from the stored size on, up to the size the first answer names (a
        terminal that keeps printing waits for the next round). True when the stored
        log is whole: caught up, or the daemon has no log of it (after a reset)."""
        have = raw.stat().st_size
        until = None
        async with shellbox.client(self.cfg, 30.0, self.transport) as client:
            while until is None or have < until:
                try:
                    r = await client.get(self.url + "/log", params={"id": tid, "from": have})
                except httpx.HTTPError:
                    return False
                if r.status_code == 404:
                    return True
                size = r.headers.get("x-log-size", "")
                if r.status_code != 200 or not size.isdigit():
                    return False
                until = until if until is not None else int(size)
                if not r.content:
                    break
                _append(raw, r.content)
                have += len(r.content)
        return have >= until

    # ---- the daemon over HTTP ------------------------------------------------------------

    async def post(self, path: str, body: dict, read: float = 10.0) -> tuple[int, dict]:
        """A plain JSON call (/read, /type, /control, /keys), see shellbox.call."""
        return await shellbox.call(self.cfg, "POST", path, body, read=read, transport=self.transport)

    async def view(self, name: str, deliver) -> bool:
        """GET /view, the terminal's bytes as they come, each chunk to `deliver`. True
        when the body ended cleanly (the terminal ended), False when the daemon refused
        or the connection broke."""
        try:
            async with shellbox.client(self.cfg, None, self.transport) as client, \
                    client.stream("GET", self.url + "/view", params={"terminal": name}) as r:
                if r.status_code != 200:
                    return False
                async for chunk in r.aiter_raw():
                    if chunk:
                        deliver(chunk)
                return True
        except httpx.HTTPError:
            return False


def _append(raw: Path, data: bytes) -> None:
    """data after the stored bytes, every known secret blanked over the stored tail and
    data together, so one that came in two fetches is caught too. The tail is rewritten
    in place and blanking keeps the length: the stored size stays the daemon's offset."""
    with open(raw, "r+b") as f:
        end = f.seek(0, os.SEEK_END)
        start = max(0, end - max(errors.longest() - 1, 0))
        f.seek(start)
        joined = f.read() + data
        f.seek(start)
        f.write(errors.blank(joined))


def _iso(opened_at) -> str:
    """The daemon's unix seconds as the store writes times."""
    try:
        t = datetime.fromtimestamp(float(opened_at), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        t = datetime.now(timezone.utc)
    return iso(t)
