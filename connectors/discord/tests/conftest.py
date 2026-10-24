"""A fake Discord for discord-link's tests. No network, ever."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from discord_link.link import Link, parse_clients
from discord_link.records import EPOCH
from discord_link.state import State

OWNER = "owner-key-0123456789"
OWNER2 = "owner2-key-0123456789"
READER = "reader-key-0123456789"
CLIENTS = f"live:{OWNER}:owner,second:{OWNER2}:owner,test:{READER}:reader"
ME = "1111111111111111042"            # a made-up account id


@pytest.fixture
def anyio_backend():
    return "asyncio"


def sid(ms: int, n: int = 0) -> str:
    """A snowflake at that time."""
    return str(((ms - EPOCH) << 22) + n)


T0 = 1_758_700_000_000


def msg(mid: str, channel: str, author: str = "U2", text: str = "hi", **kw) -> dict:
    ts = datetime.fromtimestamp(((int(mid) >> 22) + EPOCH) / 1000, tz=timezone.utc)
    a = author if isinstance(author, dict) else {"id": author, "username": author.lower(),
                                                 "global_name": author.title()}
    return {"id": mid, "channel_id": channel, "author": a, "content": text,
            "timestamp": ts.isoformat(), "type": 0, "attachments": [], **kw}


def _body(request: httpx.Request) -> dict | None:
    return json.loads(request.content) if request.content else None


class FakeDiscord:
    """Discord's REST API as far as discord-link uses it. Records every request;
    `writes` are the requests that are not GET, `authed` the requests that carried the
    user token."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.channels: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.guilds: list[dict] = []
        self.guild_channels: dict[str, list[dict]] = {}
        self.search_hits: dict[str, list[dict]] = {}
        self.indexing = False
        self.down = False
        self.rate_limit_once: set[str] = set()
        self.next_id = int(sid(T0 + 10 ** 7))
        self.relationships: list[dict] = [{"id": "BAD", "type": 2}]
        self.me = {"id": ME, "username": "sam", "global_name": "Sam"}
        self.authed: list[tuple[str, str]] = []

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.requests if m != "GET"]

    def add_channel(self, ch: dict, msgs: list[dict] | None = None) -> None:
        self.channels[ch["id"]] = ch
        self.messages.setdefault(ch["id"], []).extend(msgs or [])

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route")
        path = request.url.path.removeprefix("/api/v10")
        params = dict(request.url.params)
        body = _body(request)
        self.requests.append((request.method, path, params if request.method == "GET"
                              else (body or {})))
        if "authorization" in request.headers:
            self.authed.append((request.method, path))
        if path in self.rate_limit_once:
            self.rate_limit_once.discard(path)
            return httpx.Response(429, json={"retry_after": 0})
        if request.method != "GET":
            return httpx.Response(405, json={})
        if path == "/users/@me":
            return httpx.Response(200, json=self.me)
        if path == "/users/@me/channels":
            return httpx.Response(200, json=[c for c in self.channels.values()
                                             if c.get("type") in (1, 3)])
        if path == "/users/@me/guilds":
            return httpx.Response(200, json=self.guilds)
        if path == "/users/@me/relationships":
            return httpx.Response(200, json=self.relationships)
        m = re.fullmatch(r"/guilds/(\w+)/channels", path)
        if m:
            return httpx.Response(200, json=self.guild_channels.get(m.group(1), []))
        m = re.fullmatch(r"/(channels|guilds)/(\w+)/messages/search", path)
        if m:
            if self.indexing:
                return httpx.Response(202, json={"retry_after": 2})
            hits = self.search_hits.get(m.group(2), [])
            if params.get("author_id"):
                hits = [h for h in hits if h["author"]["id"] == params["author_id"]]
            if params.get("min_id"):
                hits = [h for h in hits if int(h["id"]) >= int(params["min_id"])]
            if params.get("max_id"):
                hits = [h for h in hits if int(h["id"]) < int(params["max_id"])]
            off = int(params.get("offset", 0))
            page = hits[off:off + 25]
            return httpx.Response(200, json={"total_results": len(hits),
                                             "messages": [[{**h, "hit": True}] for h in page]})
        m = re.fullmatch(r"/guilds/(\w+)", path)
        if m:
            g = next((g for g in self.guilds if g["id"] == m.group(1)), None)
            return httpx.Response(200, json={**g, "approximate_member_count": 42,
                                             "roles": [{"name": "@everyone"},
                                                       {"name": "mods"}]}) \
                if g else httpx.Response(404, json={})
        m = re.fullmatch(r"/users/(\w+)/profile", path)
        if m:
            return httpx.Response(200, json={
                "user": {"id": m.group(1), "username": "ana", "global_name": "Ana",
                         "bio": "cars"},
                "mutual_guilds": [{"id": g["id"]} for g in self.guilds[:1]],
                "mutual_friends_count": 3,
                "connected_accounts": [{"type": "steam", "name": "ana_s"}]})
        m = re.fullmatch(r"/channels/(\w+)/messages", path)
        if m:
            return httpx.Response(200, json=self._page(m.group(1), params))
        m = re.fullmatch(r"/channels/(\w+)", path)
        if m and m.group(1) in self.channels:
            return httpx.Response(200, json=self.channels[m.group(1)])
        return httpx.Response(404, json={})

    def _page(self, chat: str, params: dict) -> list[dict]:
        limit = int(params.get("limit", 50))
        ids = sorted(self.messages.get(chat, []), key=lambda m: int(m["id"]))
        if "around" in params:
            a = int(params["around"])
            before = [m for m in ids if int(m["id"]) < a][-(limit // 2):]
            after = [m for m in ids if int(m["id"]) >= a][:limit - len(before)]
            page = before + after
        elif "after" in params:
            page = [m for m in ids if int(m["id"]) > int(params["after"])][:limit]
        elif "before" in params:
            page = [m for m in ids if int(m["id"]) < int(params["before"])][-limit:]
        else:
            page = ids[-limit:]
        return list(reversed(page))


@pytest.fixture
def discord() -> FakeDiscord:
    return FakeDiscord()


@pytest.fixture
def state(tmp_path: Path) -> State:
    s = State(tmp_path / "link")
    s.save_token("fake-token")
    yield s
    s.close()


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def link(state, discord, clock) -> Link:
    lk = Link(state, clients=parse_clients(CLIENTS), interval=0,
              transport=discord.transport(),
              sleep=lambda s: None, clock=clock)
    yield lk
    lk.rest.close()


def tick_dms(rest, state: State) -> None:
    """The account's DMs learned and all ticked, the user's pick in the app."""
    from discord_link import sync
    from discord_link.errors import NO_CTX
    for ch in rest.get("/users/@me/channels", ctx=NO_CTX):
        sync.learn_private(state, ch)
    state.x("UPDATE channels SET enabled = 1 WHERE kind IN ('dm', 'group')")
