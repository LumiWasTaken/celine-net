"""Every user-role message it reads starts with its time line; the stored row stays
bare and the prompt prefix stays byte-stable."""
from __future__ import annotations

from chat_fixtures import user_says

import pytest

from celine.conversations import store as conversations, tree, view
from fake_backend import FakeBackend
from celine.boot import load
from celine.turns import context
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


async def test_user_messages_carry_their_time(store):
    cfg = load()
    cfg.timezone = "Europe/Berlin"
    cid = await conversations.create(store)
    mid, tid, _ = await user_says(store, cid, "hello")
    await store.write(lambda c: c.execute(
        "UPDATE messages SET created_at=? WHERE id=?", ("2026-10-01T23:40:12.345+00:00", mid)))
    reply = await tree.start_assistant_message(store, cid, mid, tid)
    await tree.finish_assistant_message(store, cid, reply, "answered")

    built = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[])
    assert [m["content"] for m in built.messages if m["role"] == "user"] == [
        "[Fri 2 Oct 01:40]\nhello"]
    twice = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[])
    assert twice.messages == built.messages, "the prompt prefix is byte-stable"
    snap = await view.snapshot(store, cid)
    assert snap["messages"][0]["content"] == "hello", "the stored row stays bare"


async def test_keep_thinking_sends_the_thinking_of_earlier_exchanges(store):
    cfg = load()
    cid = await conversations.create(store)
    for n in (1, 2):
        mid, tid, _ = await user_says(store, cid, f"question {n}")
        reply = await tree.start_assistant_message(store, cid, mid, tid)
        await tree.finish_assistant_message(store, cid, reply, f"answer {n}", reasoning=f"thought {n}")
        await turns.transition(store, tid, cid, "done")
    await user_says(store, cid, "question 3")

    def thoughts(built):
        return [m.get("reasoning_content") for m in built.messages if m["role"] == "assistant"]

    backend = FakeBackend()
    assert thoughts(await context.build(store, cfg, backend, cid, system="S", tools=[])) == [None, None]
    backend.cfg = type("Cfg", (), {"raw": {"keep_thinking": 1}})()
    assert thoughts(await context.build(store, cfg, backend, cid, system="S", tools=[])) == [None, "thought 2"]

    # the exchange going on: only its last two replies keep their thinking
    backend = FakeBackend()
    leaf = await store.read(lambda c: c.execute(
        "SELECT active_leaf_id FROM conversations WHERE id=?", (cid,)).fetchone()[0])
    tid = await store.read(lambda c: c.execute(
        "SELECT id FROM turns WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
        (cid,)).fetchone()[0])
    for n in (1, 2, 3):
        reply = await tree.start_assistant_message(store, cid, leaf, tid)
        await tree.finish_assistant_message(store, cid, reply, f"step {n}", reasoning=f"t{n}")
        leaf = reply
    assert thoughts(await context.build(store, cfg, backend, cid, system="S", tools=[])) == \
        [None, None, None, "t2", "t3"]


async def test_her_system_prompt_ends_with_today_in_the_app_zone(store):
    from celine.turns.prompt import request_parts
    cfg = load()
    cfg.timezone = "Europe/Berlin"
    cid = await conversations.create(store)
    blocks, _, _ = await request_parts(store, cfg, None, FakeBackend(), cid)
    name, text = blocks[-1]
    assert name == "tools" and text.startswith("Today is ") and text.endswith("(Europe/Berlin).")


async def test_her_request_tells_her_now_and_the_zone_of_every_time(store, monkeypatch):
    from celine import clock
    from celine.core.bus import EventBus
    from celine.turns.cancel import Token
    from celine.turns.runner import run_turn
    cfg = load()
    cfg.timezone = "Europe/Berlin"
    monkeypatch.setattr(clock, "now", lambda: "2026-10-23T10:38:00.000+00:00")
    cid = await conversations.create(store)
    await user_says(store, cid, "when is 15:00?")
    turn = await turns.claim(store, "w1")
    backend = FakeBackend()
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), None, None)
    last = [m for m in backend.calls[0] if m["role"] == "user"][-1]
    tail = last["content"][-1]["text"]
    assert tail.startswith("Now it is [Fri 23 Oct 12:38]. "), tail
    assert "is Europe/Berlin time" in tail
    assert last["content"][0]["text"].endswith("\nwhen is 15:00?"), "the message first"
    system = backend.calls[0][0]["content"]
    assert "Now it is" not in system, "now stays out of the cached prefix"


async def test_her_request_tells_her_the_fallback_and_why(tmp_path, monkeypatch, store):
    """One tail line while the turn's role runs on a model that is not its chain's
    first (a turn that walks the list); none on the first choice, none for a turn
    that runs on the first model alone."""
    from types import SimpleNamespace

    import httpx
    from settings_fixtures import scratch

    from celine.backends.llm import LLM
    from celine.backends.queue import Priority
    from celine.backends.router import Router
    from celine.turns.prompt import request_text

    scratch(tmp_path, monkeypatch, roles={"chat": {"chain": ["main/big", "lan/small"]}})
    cfg = load()
    down = {"127.0.0.1"}                      # main refuses, lan answers

    def reply(request):
        if request.url.host in down:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"running": [], "models": []})
    llm = LLM(cfg, router=Router(cfg, None, transport=httpx.MockTransport(reply)))
    cid = await conversations.create(store)
    carried = SimpleNamespace(notes=[], skills=set(), loaded_notes=frozenset())

    async def tail(fallback=True):
        backend = llm.bind(role="chat", priority=Priority.AGENT, conversation_id=cid,
                           fallback=fallback)
        _, parts = await request_text(store, cfg, None, backend, cid, {"id": "t"}, carried)
        return [p.text for p in parts if p.text.startswith("You are running in fallback")]
    [line] = await tail()
    assert line.startswith("You are running in fallback mode. Instead of main/big you are "
                           "running on lan/small. ")
    assert line.endswith(" main/big: llama-swap does not answer.")
    assert await tail(fallback=False) == [], "a turn on the first model alone fails instead"
    down.clear()
    llm.router._providers["main"].checked = 0.0          # the cached probe has run out
    assert await tail() == []


async def test_the_fallback_line_says_what_the_app_knows_of_the_model():
    """The fixed sentence, the facts of the model it runs on, the reasons; a fact the app
    does not know is left out."""
    from types import SimpleNamespace

    from celine.backends.router import Ordered
    from celine.turns.prompt import fallback_line

    class Router:
        async def order(self, role, conversation_id=None):
            return Ordered(role, ["lan/small"], ["main/big", "lan/small"],
                           [("main/big", "llama-swap does not answer.")])

    def backend(model):
        llm = SimpleNamespace(router=Router(), make=lambda ref: model)
        return SimpleNamespace(llm=llm, fallback=True, role="chat")
    known = SimpleNamespace(context_window=32_768, supports_images=False,
                            supports_tools=True, cfg=None)
    assert await fallback_line(backend(known), "c") == (
        "You are running in fallback mode. Instead of main/big you are running on "
        "lan/small. Its context window is 32,768 tokens. It does not see images. It calls "
        "tools. main/big: llama-swap does not answer.")
    unknown = SimpleNamespace(cfg=None)
    assert await fallback_line(backend(unknown), "c") == (
        "You are running in fallback mode. Instead of main/big you are running on "
        "lan/small. main/big: llama-swap does not answer.")
