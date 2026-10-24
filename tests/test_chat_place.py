"""The agent's place, the chats: the list's groups, row state and source come from the
server, search finds titles then messages with marks, Branch from here copies the path,
and its mood next to its name is its own to set (mind__set_status, GET /api/agent/mood, event `mood`)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from chat_fixtures import user_says
from celine.backends.base import Done, TextDelta
from celine.api import her as her_api
from fake_backend import FakeBackend
from celine.boot import load
from celine.conversations import api as conversations_api, store as conversations, tree, view
from celine.core.bus import EventBus
from celine.core.services import Services
from celine.errors import install_error_handler
from celine.skills.mind import mood_refusal
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn

pytestmark = pytest.mark.anyio

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 1, 16, 2, 52, tzinfo=BERLIN)


@pytest.fixture
async def c(store):
    app = FastAPI()
    app.state.services = Services()
    install_error_handler(app)
    app.include_router(conversations_api.router)
    app.include_router(her_api.router)
    app.state.services.store, app.state.services.cfg = store, load()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        yield client


async def say(store, reply: str) -> None:
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, load(), FakeBackend([TextDelta(reply), Done(reason="stop")]),
                   EventBus(), Token(turn["id"]))


async def at(store, cid: str, when: str) -> None:
    await store.write(lambda db: db.execute(
        "UPDATE conversations SET updated_at=? WHERE id=?", (when, cid)))


def test_the_group_of_a_row_is_running_pinned_or_its_day_in_the_app_zone():
    g = lambda rank, iso: conversations.group_of(rank, iso, BERLIN, NOW)  # noqa: E731
    assert g(2, "2026-01-01T00:00:00+00:00") == "Running"
    assert g(1, "2026-01-01T00:00:00+00:00") == "Pinned"
    # 23:30 UTC on the 15th is 01:30 on the 16th in Berlin
    assert g(0, "2026-01-15T23:30:00+00:00") == "Today"
    assert g(0, "2026-01-15T12:00:00+00:00") == "Yesterday"
    assert g(0, "2026-01-13T12:00:00+00:00") == "Tuesday"
    assert g(0, "2026-01-09T12:00:00+00:00") == "9 Jan"
    assert g(0, "2025-10-09T12:00:00+00:00") == "October 2025"


async def test_the_list_runs_first_then_pinned_and_every_row_says_its_state(c, store):
    old = await conversations.create(store, "old")
    pinned = await conversations.create(store, "pinned")
    await conversations.update(store, pinned, pinned=True)
    failed = await conversations.create(store, "failed")
    await user_says(store, failed, "hi")
    t = await turns.claim(store, "w")
    await turns.transition(store, t["id"], failed, "failed")
    running = await conversations.create(store, "running")
    await user_says(store, running, "are you there")      # queued: live
    bridged = await conversations.create(store, "bridged", trigger_key="bridge:dc")
    mission = await conversations.create(store, "mission", kind="mission")
    await at(store, old, "2025-10-09T12:00:00+00:00")

    body = (await c.get("/api/conversations", params={"place": "celine"})).json()
    rows = {r["id"]: r for r in body["conversations"]}
    order = [r["id"] for r in body["conversations"]]
    assert order[:2] == [running, pinned]
    assert mission not in rows and body["count"] == len(rows) == 5
    assert rows[running]["group"] == "Running"
    assert rows[running]["state"] == {"tone": "running", "words": ""}
    assert rows[pinned]["group"] == "Pinned" and rows[pinned]["state"] is None
    assert rows[failed]["state"] == {"tone": "error", "words": "The reply failed."}
    assert rows[old]["group"] == "October 2025"
    assert rows[bridged]["source"] is None, "no source without its kb"
    assert "rank" not in rows[old]

    # frozen (guard/freeze.py): the queued turn waits, nothing runs
    await store.write(lambda c: c.execute("INSERT INTO kv(k, v) VALUES('frozen', '1')"))
    rows = {r["id"]: r for r in (await c.get("/api/conversations",
                                             params={"place": "celine"})).json()["conversations"]}
    assert rows[running]["group"] != "Running" and rows[running]["state"] is None


async def test_the_list_pages_by_rank_and_counts_each_shelf(c, store):
    ids = [await conversations.create(store, f"c{i}") for i in range(5)]
    await conversations.update(store, ids[2], pinned=True)
    await conversations.archive(store, ids[4])
    paged, cursor = [], None
    while True:
        body = (await c.get("/api/conversations", params={
            "limit": 2, **({"cursor": cursor} if cursor else {})})).json()
        paged += [r["id"] for r in body["conversations"]]
        if (cursor := body["next"]) is None:
            break
    assert paged[0] == ids[2] and sorted(paged) == sorted(ids[:4])
    assert (await c.get("/api/conversations?archived=true")).json()["count"] == 1


async def test_search_finds_titles_first_then_messages_with_their_marks(c, store):
    tides = await conversations.create(store, "How tides work")
    other = await conversations.create(store, "Something else")
    await user_says(store, other, "the tides were high at the lake today")
    await say(store, "two bulges, so two high tides a day")
    body = (await c.get("/api/messages", params={"q": "tides", "place": "celine"})).json()
    assert body["titles"] == [tides]
    hit = body["hits"][0]
    assert hit["conversation_id"] == other and hit["message_id"]
    a, b = hit["marks"][0]
    assert hit["snippet"][a:b].lower() == "tides" and "\x02" not in hit["snippet"]
    empty = (await c.get("/api/messages", params={"q": "tides", "archived": True})).json()
    assert empty == {"hits": [], "titles": []}


async def test_branch_from_here_copies_the_path_up_to_the_message(c, store):
    cid = await conversations.create(store, "Hedy")
    first, _, _ = await user_says(store, cid, "who invented frequency hopping")
    await say(store, "hedy lamarr and george antheil")
    reply = (await view.snapshot(store, cid))["messages"][-1]["id"]
    await user_says(store, cid, "and today?")
    await say(store, "every phone does it")

    r = await c.post(f"/api/conversations/{cid}/branch", json={"message_id": reply})
    out = r.json()
    assert out["title"] == "Branch of Hedy" and out["id"] != cid
    msgs = (await view.snapshot(store, out["id"]))["messages"]
    assert [m["content"] for m in msgs] == ["who invented frequency hopping",
                                            "hedy lamarr and george antheil"]
    assert (await c.post(f"/api/conversations/{cid}/branch",
                         json={"message_id": "nope"})).status_code == 404
    assert first not in [m["id"] for m in msgs], "copies, not the same rows"


async def test_branch_joins_her_steps_and_leaves_tool_rows_out(store):
    from celine.skills import ToolResult
    cid = await conversations.create(store)
    umid, tid, _ = await user_says(store, cid, "look it up")
    await turns.claim(store, "w")
    step = await tree.start_assistant_message(store, cid, umid, tid)
    await tree.finish_assistant_message(store, cid, step, "Let me look.")
    await turns.record_tool_calls(store, cid, step, [{
        "id": f"{tid}:0:0", "name": "arch__find", "arguments": "{}"}])
    await turns.finish_tool_call(store, cid, step, f"{tid}:0:0", "arch__find",
                                 ToolResult(content="found it"), "ok")
    final = await tree.start_assistant_message(store, cid, await tree.active_leaf(store, cid),
                                               tid)
    await tree.finish_assistant_message(store, cid, final, "It is 42.")
    await turns.transition(store, tid, cid, "done")
    out = await tree.branch(store, cid, final)
    msgs = (await view.snapshot(store, out["id"]))["messages"]
    assert out["title"] == "Branch"
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "look it up"), ("assistant", "Let me look.\n\nIt is 42.")]


async def test_export_is_the_path_as_markdown(c, store):
    cid = await conversations.create(store, "Tides & moon")
    await user_says(store, cid, "why two tides")
    await say(store, "two bulges")
    r = await c.get(f"/api/conversations/{cid}/export")
    assert r.status_code == 200 and "Tides---moon.md" in r.headers["content-disposition"]
    body = r.text
    assert body.startswith("# Tides & moon") and "why two tides" in body and "two bulges" in body
    assert (await c.get("/api/conversations/nope/export")).status_code == 404


@pytest.mark.parametrize("text", ["\U0001f44d\U0001f3fd" * 24, "\U0001f468‍\U0001f469‍\U0001f467",
                                  "^_^"])
def test_a_mood_is_1_to_24_emojis_or_a_kaomoji(text):
    assert mood_refusal(text) is None


@pytest.mark.parametrize("text", ["\U0001f319" * 25, "(^_^)\n(^_^)"])
def test_anything_else_is_refused(text):
    assert mood_refusal(text)


async def test_she_sets_her_mood_and_it_is_stored_sent_and_served(c, store):
    from types import SimpleNamespace
    from celine.skills import ToolContext
    from celine.skills import mind
    cid = await conversations.create(store)
    store.bus = EventBus()
    _, feed = store.bus.subscribe("global")
    assert (await c.get("/api/agent/mood")).json() == {"mood": None}
    cfg = load()
    skill = mind.Mind(cfg)
    skill.app = SimpleNamespace(store=store, cfg=cfg, llm=None, registry=None)
    ctx = ToolContext(cid, "t", "x", store, cfg, None)  # type: ignore[arg-type]
    bad = await skill.set_status(ctx, "feeling great")
    assert bad.is_error and bad.content == "That is words, not emojis or a kaomoji."
    assert not (await skill.set_status(ctx, " \U0001f319✨ ")).is_error
    got = (await c.get("/api/agent/mood")).json()["mood"]
    assert got["text"] == "\U0001f319✨" and got["set_at"]
    ev = feed.receive_nowait()
    assert ev["type"] == "mood" and ev["mood"] == got
