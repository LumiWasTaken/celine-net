"""Web search down: the way around it, the check Settings shows, and results pages
labelled as such. A fake network."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from celine import boot
from celine.skills import ToolContext, web
from celine.skills.web import Web

pytestmark = pytest.mark.anyio

CFG = SimpleNamespace(user="sam", name="Celine", substitute=lambda s: s,
                      skill=lambda n: {"enabled": True},
                      settings=SimpleNamespace(features={"browser": False}, services={}))


def _ctx():
    return ToolContext(conversation_id="c9", turn_id="t1", tool_call_id="x", store=None,
                       config=None, emit=None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("CELINE_SEARXNG_URL", "http://searx.test")    # seeds a load()'s
    web._numbers.clear()
    web._pages.clear()


REFUSED = "SearXNG did not answer at searx.test. Start it, or fix the address."


async def test_a_dead_search_fails_with_searxng_named_and_no_way_around():
    def dead(req):
        raise httpx.ConnectError("refused", request=req)
    w = Web(CFG, transport=httpx.MockTransport(dead))
    r = await w.search(_ctx(), "muon vs adamw")
    assert r.is_error and r.content == REFUSED
    assert "duckduckgo" not in r.content, "no fallback inside its turn"
    assert w.search_down == REFUSED
    assert await w.check() == REFUSED, "a fresh failure answers for a minute"
    w.transport = httpx.MockTransport(lambda req: httpx.Response(200, text="OK"))
    assert await w.check(max_age=0) is None


async def test_a_wrong_searxng_says_which():
    html = Web(CFG, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>not searx</html>")))
    r = await html.search(_ctx(), "x")
    assert r.is_error and r.content == ("SearXNG at searx.test answered something that is "
                                        "not JSON: fix the address so it points at SearXNG.")
    r = await Web(CFG, transport=httpx.MockTransport(
        lambda req: httpx.Response(403))).search(_ctx(), "x")
    assert r.content.startswith("SearXNG at searx.test answered 403 instead of JSON results")
    assert "search.formats" in r.content


async def test_settings_tools_say_where_each_skill_is_offered_and_web_down(tmp_path, monkeypatch,
                                                                         store):
    from settings_fixtures import client, make_app, scratch

    scratch(tmp_path, monkeypatch)
    app = make_app(boot.load(), store)
    web_skill = app.state.services.registry.skills["web"]
    web_skill.transport = httpx.MockTransport(lambda r: httpx.Response(502))
    async with client(app) as api:
        view = (await api.get("/api/tool-defaults")).json()
    assert [p["name"] for p in view["places"]] == ["chat", "mission", "background"]
    assert view["places"][1]["label"] == "Missions"
    skills = {s["name"]: s for s in view["skills"]}
    assert skills["web"]["places"] == ["chat", "mission", "background"]
    assert skills["web"]["status"] == "down"
    assert skills["web"]["error"].startswith("SearXNG at ") and "answered 502" in skills["web"]["error"]


async def test_a_results_page_is_labelled_with_no_date():
    html = (b"<html><head><title>A first hit's title</title>"
            b"<meta name='date' content='2014-01-01'></head><body><p>"
            + b"Some result text that is long enough to read. " * 20 + b"</p></body></html>")
    w = Web(CFG, check_addresses=False, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=html, headers={"content-type": "text/html"})))
    r = await w.read_page(_ctx(), "https://html.duckduckgo.com/html/?q=muon")
    head = r.content.splitlines()[0]
    assert head == "[web:1] Search results"
    assert web.page_label("https://www.reddit.com/login/?dest=x") == "Login page"
    assert web.page_label("https://www.google.de/search?q=x") == "Search results"
    assert web.page_label("https://example.com/search") is None
