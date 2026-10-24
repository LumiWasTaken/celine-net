"""The web skill: the SSRF guard, and a search answered with linked citations through
the runner. A fake SearXNG, no network."""
from __future__ import annotations

from chat_fixtures import Script, user_says

import json
from types import SimpleNamespace

import httpx
import pytest

from celine.conversations import store as conversations, view
from celine.backends.base import Done, TextDelta, ToolCallDelta
from celine.boot import load
from celine.core.bus import EventBus
from celine.skills import Registry, ToolContext
from celine.skills import web
from celine.skills.web import Web
from celine.errors import ToolRefused
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("CELINE_SEARXNG_URL", "http://searx.test")    # seeds a load()'s
    web._numbers.clear()
    web._pages.clear()
    yield
    web._numbers.clear()
    web._pages.clear()


def searx(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "searx.test", "nothing but SearXNG is asked"
    return httpx.Response(200, json={"results": [
        {"url": "https://boats.example.com/celine", "title": "Celine refit log",
         "content": "The 1987 Celine ketch needed a new keel bolt"},
        {"url": "https://forum.example.org/t/42", "title": "Keel bolt FAQ",
         "content": "Torque specs and bolt choices."}]})


def _ctx():
    return ToolContext(conversation_id="c1", turn_id="t1", tool_call_id="x", store=None,
                       config=None, emit=None)  # type: ignore[arg-type]


async def test_private_and_local_addresses_are_refused(monkeypatch):
    for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.20", "172.16.0.1", "169.254.169.254",
               "::1", "fe80::1", "fc00::1", "0.0.0.0", "100.64.0.1", "::ffff:127.0.0.1",
               "224.0.0.1"):
        with pytest.raises(ToolRefused):
            await web._resolve_public(ip, 80)
    await web._resolve_public("8.8.8.8", 443)

    async def fake_gai(host, port, **kw):
        return [(2, 1, 6, "", ("127.0.0.1", port))]
    monkeypatch.setattr(web.anyio, "getaddrinfo", fake_gai)
    with pytest.raises(ToolRefused):
        await web._resolve_public("evil.example.com", 80)


async def test_read_refuses_local_urls_and_redirects_to_them():
    from celine.settings import general
    cfg = SimpleNamespace(user="sam", name="Celine", substitute=lambda s: s,
                          skill=lambda n: {"enabled": True}, r=lambda key: general.value({}, key),
                          settings=SimpleNamespace(services={}),
                          skill_enabled=lambda n: n == "web")
    hosts: list[str] = []

    def handler(req):
        hosts.append(req.url.host)
        if req.url.host == "93.184.215.14":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8766/api"})
        raise AssertionError("must never connect to " + str(req.url))
    s = Web(cfg, transport=httpx.MockTransport(handler), check_addresses=True)
    for bad in ("file:///etc/passwd", "http://localhost:8766/api", "http://printer.local/",
                "https://user:pw@example.com/", "http://93.184.215.14/"):
        assert (await s.read_page(_ctx(), bad)).is_error, bad
    assert hosts == ["93.184.215.14"]


async def test_runner_links_good_web_refs_and_marks_bad_ones(store, monkeypatch):
    from celine.core.services import Services
    kbs = SimpleNamespace(citable=lambda: {"whatsapp"},
                          resolve_refs=lambda refs: {r: None for r in refs})
    cfg = load()
    reg = Registry(app=Services(kbs=kbs))
    reg.add(Web(cfg, transport=httpx.MockTransport(searx), check_addresses=False))
    call = ToolCallDelta(index=0, id="s0", name="web__search",
                         arguments=json.dumps({"query": "celine keel bolt"}))
    bad = [TextDelta("It needed a keel bolt [web:1]; torque specs [[web:2]]; "
                     "see [[web:12]]."), Done(reason="stop")]
    b = Script([[call, Done(reason="tool_calls")], bad])
    from celine.turns import loads
    cid = await conversations.create(store)
    await loads.touch(store, cid, "skill", ["web"])
    await user_says(store, cid, "what went wrong with the celine?")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, b, EventBus(), Token(turn["id"]), reg)
    snap = await view.snapshot(store, cid)
    final = [m for m in snap["messages"] if m["role"] == "assistant"][-1]["content"]
    assert final == ("It needed a keel bolt [boats.example.com](https://boats.example.com/"
                     "celine); torque specs [forum.example.org](https://forum.example.org/"
                     "t/42); see [[web:12|invalid]].")
    assert len(b.calls) == 2                                   # no second generation


async def test_search_down_points_at_her_browser_when_she_has_one():
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")
    cfg = load()
    cfg.settings.features = {"browser": True}
    got = await Web(cfg, transport=httpx.MockTransport(down), check_addresses=False).search(
        _ctx(), query="celine")
    assert got.is_error and got.content.endswith(" Your browser is on: search there.")
    assert got.content.startswith("SearXNG did not answer at ")
    cfg.settings.features = {"browser": False}
    got = await Web(cfg, transport=httpx.MockTransport(down), check_addresses=False).search(
        _ctx(), query="celine")
    assert got.is_error and "browser" not in got.content
