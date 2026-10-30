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
    cid = await conversations.create(store)
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
    assert got.content.startswith("SearXNG at searx.test does not answer.")
    cfg.settings.features = {"browser": False}
    got = await Web(cfg, transport=httpx.MockTransport(down), check_addresses=False).search(
        _ctx(), query="celine")
    assert got.is_error and "browser" not in got.content


async def test_save_attaches_the_file_at_the_url_to_the_call(store, monkeypatch):
    from celine import attachments
    from celine.conversations import tree
    from celine.skills import words
    data = b"\x89PNG\r\n\x1a\nnot really"

    def files(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("big.zip"):
            return httpx.Response(200, content=b"x" * 64,
                                  headers={"content-type": "application/zip"})
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})
    cfg = load()
    cfg.user = "Joe"
    real = cfg.r
    monkeypatch.setattr(cfg, "r", lambda k: 32 if k == "max_attachment_bytes" else real(k))
    cid = await conversations.create(store)
    _, tid, _ = await user_says(store, cid, "get it")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [{"id": "c1", "name": "web__read_page",
                                                     "arguments": "{}"}])
    ctx = ToolContext(conversation_id=cid, turn_id=tid, tool_call_id="c1", store=store,
                      config=cfg, emit=None)
    w = Web(cfg, transport=httpx.MockTransport(files), check_addresses=False)
    got = await w.read_page(ctx, "https://pics.example.com/a/cat%20face.png", save=True)
    assert not got.is_error and got.content == (
        "Saved cat face.png (18 bytes, image/png) from https://pics.example.com/a/"
        "cat%20face.png, attached to this conversation for Joe.")
    [row] = await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT sha256, filename, kind FROM attachments WHERE tool_call_id='c1'")])
    assert (row["filename"], row["kind"]) == ("cat face.png", "image")
    assert attachments.blob_path(cfg, row["sha256"]).read_bytes() == data
    big = await w.read_page(ctx, "https://pics.example.com/big.zip", save=True)
    assert big.is_error and "over the 0 MB limit" in big.content
    assert words.sentence("web__read_page", {"url": "https://pics.example.com/a.png",
                                             "save": True}, {}) == \
        "Saved a file from pics.example.com/a.png"


def test_a_part_of_a_page_that_could_not_be_read_is_said(monkeypatch):
    import trafilatura

    def broken(*_a, **_k):
        raise ValueError("bad meta tag")
    monkeypatch.setattr(trafilatura, "extract_metadata", broken)
    doc = web.extract(web.Page("https://x.org/a", "text/html",
                               b"<html><body><p>" + b"word " * 50 + b"</p></body></html>", 200))
    assert doc["problems"] == ["Its title, date and author could not be read (bad meta tag)"]
    assert "word" in doc["text"]
