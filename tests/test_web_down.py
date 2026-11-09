"""Web search down: the way around it, what Settings shows after a failed search, and
results pages labelled as such. A fake network."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from celine import boot
from celine.settings import general
from celine.skills import ToolContext, web
from celine.skills.web import Web

pytestmark = pytest.mark.anyio

CFG = SimpleNamespace(user="sam", name="Celine", substitute=lambda s: s,
                      skill=lambda n: {"enabled": True}, r=lambda key: general.value({}, key),
                      settings=SimpleNamespace(features={"browser": False}, services={}))


def _ctx():
    return ToolContext(conversation_id="c9", turn_id="t1", tool_call_id="x", store=None,
                       config=None, emit=None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("CELINE_SEARXNG_URL", "http://searx.test")    # seeds a load()'s
    web._numbers.clear()
    web._pages.clear()


REFUSED = "SearXNG at searx.test does not answer."


async def test_a_dead_search_fails_with_searxng_named_and_no_way_around():
    def dead(req):
        raise httpx.ConnectError("refused", request=req)
    w = Web(CFG, transport=httpx.MockTransport(dead))
    r = await w.search(_ctx(), "muon vs adamw")
    assert r.is_error and r.content == REFUSED
    assert "duckduckgo" not in r.content, "no fallback inside its turn"
    assert w.search_down == REFUSED
    w.transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"results": []}))
    assert not (await w.search(_ctx(), "x")).is_error
    assert w.search_down is None, "the next search that answers clears it"


async def test_no_results_with_dead_engines_is_search_down():
    empty = {"results": [], "unresponsive_engines": [["google", "timeout"], ["bing", "timeout"]]}
    w = Web(CFG, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=empty)))
    r = await w.search(_ctx(), "muon vs adamw")
    assert r.is_error
    assert r.content == "SearXNG at searx.test reached none of its engines (google, bing)."
    assert w.search_down == r.content
    w = Web(CFG, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"results": []})))
    r = await w.search(_ctx(), "muon vs adamw")
    assert not r.is_error and r.content.endswith("no results. Try other words, fewer words, "
                                                 "or no time_range.")
    assert w.search_down is None


async def test_a_dead_resolver_names_the_resolver_not_searxng():
    import socket

    def dead(req):
        e = httpx.ConnectError("lookup", request=req)
        e.__cause__ = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        raise e
    w = Web(CFG, transport=httpx.MockTransport(dead))
    r = await w.search(_ctx(), "x")
    assert r.is_error and r.content == ("DNS failed in the app (Temporary failure in name "
                                        "resolution): every host fails until its DNS server "
                                        "answers")
    assert w.search_down == r.content


async def test_a_wrong_searxng_says_which():
    html = Web(CFG, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>not searx</html>")))
    r = await html.search(_ctx(), "x")
    assert r.is_error and r.content == ("SearXNG at searx.test answered something that is "
                                        "not JSON.")
    w = Web(CFG, transport=httpx.MockTransport(lambda req: httpx.Response(403)))
    r = await w.search(_ctx(), "x")
    assert r.content == "SearXNG at searx.test returned 403: Forbidden"
    assert w.search_down == r.content, "an error answer marks search down too"


async def test_a_searxng_error_answer_says_its_words():
    r = await Web(CFG, transport=httpx.MockTransport(lambda req: httpx.Response(
        429, text="Too many requests from this client"))).search(_ctx(), "x")
    assert r.content == "SearXNG at searx.test returned 429: Too many requests from this client"


async def test_a_page_that_answers_an_error_says_its_title():
    w = Web(CFG, check_addresses=False, transport=httpx.MockTransport(lambda req: httpx.Response(
        403, text="<html><title>Just a moment...</title></html>")))
    r = await w.read_page(_ctx(), "https://example.com/a")
    assert r.is_error and r.content == ("could not read https://example.com/a: HTTP 403: "
                                        "Just a moment...")


async def test_settings_tools_say_where_each_skill_is_offered_and_web_down(tmp_path, monkeypatch,
                                                                         store):
    from settings_fixtures import client, make_app, scratch

    scratch(tmp_path, monkeypatch)
    app = make_app(boot.load(), store)
    web_skill = app.state.services.registry.skills["web"]
    web_skill.transport = httpx.MockTransport(lambda r: httpx.Response(502))
    async with client(app) as api:
        view = (await api.get("/api/tool-defaults")).json()
        assert {s["name"]: s for s in view["skills"]}["web"]["status"] != "down", \
            "nothing asks SearXNG until a search does"
        await web_skill.search(_ctx(), "x")
        view = (await api.get("/api/tool-defaults")).json()
    assert [p["name"] for p in view["places"]] == ["chat", "mission", "background"]
    assert view["places"][1]["label"] == "Missions"
    skills = {s["name"]: s for s in view["skills"]}
    assert skills["web"]["places"] == ["chat", "mission", "background"]
    assert skills["web"]["status"] == "down"
    assert skills["web"]["error"].startswith("SearXNG at ") and "returned 502" in skills["web"]["error"]


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


def test_a_saved_address_that_hides_the_env_one_names_both(monkeypatch):
    from celine.errors import unreachable_words
    from celine.settings.services import unreachable
    e = httpx.ConnectError("lookup")
    monkeypatch.setenv("CELINE_SEARXNG_URL", "http://10.0.0.9:8888")
    said = unreachable("SearXNG", "http://searxng:8080", e, "searxng")
    assert said == ("SearXNG did not answer at searxng:8080. CELINE_SEARXNG_URL is "
                    "http://10.0.0.9:8888; the address saved here is used instead.")
    assert "COMPOSE_PROFILES" in unreachable_words("SearXNG", "http://searxng:8080", e), \
        "without a service id the profile advice stays"
    monkeypatch.setenv("CELINE_SEARXNG_URL", "http://searxng:8080")
    assert "COMPOSE_PROFILES" in unreachable("SearXNG", "http://searxng:8080", e, "searxng")
