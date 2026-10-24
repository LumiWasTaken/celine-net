"""The agent's bug reports (bugreports/): it files one with bugreport__file and it opens its own
Intercom thread to the user at once, from the conversation that filed it; they close it
on the thread (what waits in it is read), Events lists it under Bugs (not twice under
Messages), and any thread continues in a new chat with it."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from celine import events
from celine.bugreports import api, store as reports
from celine.bugreports.skill import BugReport
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.services import Services
from celine.errors import install_error_handler
from celine.intercom import Intercom, api as intercom_api, store as rows
from celine.skills import ToolContext

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg():
    c = load()
    c.timezone = "UTC"
    c.user = "Joe"
    return c


async def _file(store, cfg, cid, **kw) -> object:
    async def emit(*_a, **_k):
        return None
    ctx = ToolContext(conversation_id=cid, turn_id="", tool_call_id="", store=store,
                      config=cfg, emit=emit, max_output_tokens=8000)
    args = {"title": "read cuts results", "severity": "high",
            "what": "past_conversations__read cut a 9k result.", **kw}
    return await BugReport(cfg).file(ctx, **args)


def _app(store, cfg) -> FastAPI:
    app = FastAPI()
    install_error_handler(app)
    app.state.services = Services(cfg=cfg, store=store, intercom=Intercom(store))
    for r in (api.router, events.router, intercom_api.router):
        app.include_router(r)
    return app


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_a_report_is_a_thread_he_closes_and_events_lists_it_once(store, cfg):
    cid = await conversations.create(store, "a chat")
    out = await _file(store, cfg, cid, where="self-review", wish="keep the whole result")
    assert not out.is_error and out.content.startswith(
        "Filed bug report #1 (high): read cuts results. It went to Joe as message #1.")
    out = await _file(store, cfg, cid, title="  tabs   cap  ", severity="low", what="x")
    assert "Your other open reports: #1 read cuts results." in out.content
    bad = await _file(store, cfg, cid, what=" ")
    assert bad.is_error, "a report needs what happened"

    first = await store.read(lambda c: rows.thread(c, 1))
    assert [(m["from"], m["to"], m["subject"], m["text"]) for m in first] == [
        (f"agent:{cid}", "user", "Bug #1: read cuts results",
         "High: past_conversations__read cut a 9k result.\n\nWhere: self-review\n\n"
         "Wish: keep the whole result")]
    waiting = await store.read(lambda c: rows.threads(c, "user", unread=True, received=True))
    assert sorted(t["thread"] for t in waiting) == [1, 2], "each report waits for them"

    async with _http(_app(store, cfg)) as h:
        got = (await h.get("/api/bugreports", params={"state": "open"})).json()["reports"]
        assert [(r["id"], r["title"], r["where"], r["thread"]) for r in got] == [
            (2, "tabs cap", "", 2), (1, "read cuts results", "self-review", 1)]
        async def of(thread):
            return (await h.get("/api/bugreports", params={"thread": thread})).json()["reports"]
        assert [(r["id"], r["state"]) for r in await of(1)] == [(1, "open")]
        assert [r["id"] for r in await of(2)] == [2]
        assert await of(99) == [], "a thread that is no report"
        closed = (await h.post("/api/bugreports/1/close")).json()
        assert closed["state"] == "closed" and closed["closed_at"]
        assert (await h.get("/api/intercom", params={"thread": 1})).json()[
            "messages"][0]["unread"] is False, "closing reads its thread"
        assert (await h.post("/api/bugreports/9/close")).status_code == 404
        assert (await h.get("/api/bugreports", params={"state": "x"})).status_code == 400
        ev = (await h.get("/api/events", params={"kind": "bugreport"})).json()
        assert [(e["title"], e["outcome"], e["links"]["open"]) for e in ev["events"]] == [
            ("tabs cap", "Open, low", "/intercom/2"), ("read cuts results", "Closed", "/intercom/1")]
        assert {"id": "bugreport", "label": "Bugs"} in ev["kinds"]
        msgs = (await h.get("/api/events", params={"kind": "message"})).json()["events"]
        assert msgs == [], "a report is under Bugs, not again under Messages"


async def test_continue_in_a_chat_carries_the_thread_into_a_new_chat_with_her(
        store, cfg, monkeypatch):
    monkeypatch.setitem(rows.NAMES, "her", "Celine")
    cid = await conversations.create(store, "a chat")
    await _file(store, cfg, cid)
    ic = Intercom(store)
    await ic.send("user", "", "which chat was it?", reply_to=1)
    app = _app(store, cfg)
    async with _http(app) as h:
        assert (await h.post("/api/intercom/threads/99/conversations")).status_code == 404
        r = await h.post("/api/intercom/threads/1/conversations")
        assert r.status_code == 200, r.text
    new = r.json()["conversation_id"]
    assert new != cid
    conv, held, turns = await store.read(lambda c: (
        c.execute("SELECT kind, title FROM conversations WHERE id = ?", (new,)).fetchone(),
        [r[0] for r in c.execute("SELECT content FROM messages WHERE conversation_id = ?",
                                 (new,))],
        c.execute("SELECT count(*) FROM turns WHERE conversation_id = ?", (new,)).fetchone()[0]))
    assert tuple(conv) == ("chat", "Bug #1: read cuts results")
    assert held == [
        "Joe continued an Intercom thread in this chat. The thread:\n\n"
        '[From you (#1, "Bug #1: read cuts results")]\n'
        "High: past_conversations__read cut a 9k result.\n\n"
        '[From Joe (#2, re #1, "Bug #1: read cuts results")]\nwhich chat was it?']
    assert turns == 0, "it lands without waking it; their first line does"
    notice = await store.read(lambda c: rows.view(c, c.execute(
        "SELECT max(id) FROM intercom").fetchone()[0], new))
    assert (notice["from"], notice["kind"], notice["wake"]) == ("system:app", "notice", False), \
        "the transcript waits for no answer to it"
    assert await store.read(lambda c: rows.threads(c, "user", unread=True,
                                                   received=True)) == [], "read"


async def test_a_report_outlives_the_chat_that_filed_it_purged_from_the_trash(store, cfg):
    cid = await conversations.create(store, "a chat")
    await _file(store, cfg, cid)
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET trashed_at = 'x' WHERE id = ?", (cid,)))
    assert await store.write(lambda c: conversations.purge_sync(c, store, cid)) == [cid]
    r = await store.read(lambda c: reports.get(c, 1))
    assert (r["title"], r["conversation_id"], r["thread"]) == ("read cuts results", None, None)


async def test_a_field_over_its_limit_is_refused_and_names_it_never_cut(store, cfg):
    cid = await conversations.create(store, "x")
    r = await _file(store, cfg, cid, title="t" * 121)
    assert r.is_error and "at most 120 characters" in r.content
    r = await _file(store, cfg, cid, wish="w" * 4001)
    assert r.is_error and "at most 4,000 characters" in r.content
    r = await _file(store, cfg, cid, what="w" * 4000)
    assert not r.is_error
