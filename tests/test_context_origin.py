"""Every message of a request says where it came from, and a request keeps the rules:
a tool result right after the reply that made its call, each call answered once, no
message twice (a ghost prompt), nothing from another thread. A summary that fails never
drops messages: a turn fails with the reason, the background build waits."""
from __future__ import annotations

import json

import pytest

from chat_fixtures import user_says
from compaction_fixtures import Model, archive_text, seed_exchange

from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.turns import compact, context
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.context import malformed, origin
from celine.turns.runner import run_turn

pytestmark = pytest.mark.anyio

SMALL = context.Policy(age_at=0.99, compact_at=0.5, compact_to=0.3)


@pytest.fixture
def cfg():
    c = load()
    c.settings.general.update({"tool_timeout_seconds": 10, "turn_wall_clock_seconds": 60,
                               "max_concurrent_turns": 1, "history_messages": 200})
    return c


@pytest.fixture(autouse=True)
def _policy():
    old = context.POLICY
    yield
    context.POLICY = old


def _reply(cid, mid, *call_ids):
    return {"role": "assistant", "content": "", "origin": origin(cid, message=mid),
            "tool_calls": [{"id": i, "type": "function",
                            "function": {"name": "arch__find", "arguments": "{}"}}
                           for i in call_ids]}


def _result(cid, call):
    return {"role": "tool", "tool_call_id": call, "content": "found",
            "origin": origin(cid, call=call)}


def _user(cid, mid, text="hi"):
    return {"role": "user", "content": text, "origin": origin(cid, message=mid)}


def test_the_rules_every_request_keeps():
    c = "c1"
    good = [{"role": "system", "content": "s"}, _user(c, "u1"), _reply(c, "a1", "x", "y"),
            _result(c, "x"), _result(c, "y"), _user(c, "u2")]
    assert malformed(good, c) is None
    assert "does not follow its call" in malformed([_user(c, "u1"), _result(c, "x")], c)
    assert "no result before" in malformed(
        [_user(c, "u1"), _reply(c, "a1", "x", "y"), _result(c, "x"), _user(c, "u2")], c)
    assert "made twice" in malformed(
        [_user(c, "u1"), _reply(c, "a1", "x"), _result(c, "x"), _reply(c, "a2", "x"),
         _result(c, "x")], c)
    assert "answered twice" in malformed(
        [_user(c, "u1"), _reply(c, "a1", "x"), _result(c, "x"), _result(c, "x")], c)
    assert "sent twice" in malformed([_user(c, "u1"), _user(c, "u1")], c), "a ghost prompt"
    assert "another" not in (malformed([_user(c, "u1")], c) or "")
    assert "conversation c2" in malformed([_user(c, "u1"), _user("c2", "u9")], c)
    assert "before any user message" in malformed([_reply(c, "a1")], c)
    assert "has no result" in malformed([_user(c, "u1"), _reply(c, "a1", "x")], c)


def _ok(messages, cid) -> None:
    assert malformed(messages, cid) is None, malformed(messages, cid)
    for m in messages:
        assert m.get("origin"), f"no origin: {m}"
        if m["role"] == "tool":
            assert m["origin"][0]["call"] == m["tool_call_id"]


async def test_a_screen_follows_every_result_of_its_reply(store, cfg):
    """A browser__look screen is a user message: it comes after the reply's last result,
    never between two results of one reply."""
    cid = await conversations.create(store)
    _, mid = await seed_exchange(store, cid, "look", [("browser__look", "the page"),
                                                      ("arch__find", "found")])
    shot = {"id": "s1", "filename": "s.png", "sha256": "a" * 64, "media_type": "image/png"}
    await store.write(lambda c: c.execute(
        "UPDATE tool_calls SET display=? WHERE message_id=? AND name='browser__look'",
        (json.dumps({"see": True, "shot": shot}), mid)))
    await user_says(store, cid, "and now?")
    built = await context.build(store, cfg, Model(window=50_000), cid, system="S",
                                tools=[])
    _ok(built.messages, cid)
    roles = [m["role"] for m in built.messages]
    at = roles.index("tool")
    assert roles[at:at + 3] == ["tool", "tool", "user"], roles
    assert built.messages[at + 2]["origin"][0]["note"] == "screen"


async def test_a_summary_keeps_every_call_with_its_result_and_every_origin(store, cfg):
    cid = await conversations.create(store)
    for i in range(10):
        await seed_exchange(store, cid, f"question {i}",
                            [("arch__find", archive_text(60, start=1000 * (i + 1))),
                             ("notebook__read", "note " * 400)])
    await user_says(store, cid, "and now?")
    context.POLICY = SMALL
    built = await context.build(store, cfg, Model(window=20_000), cid, system="S", tools=[])
    assert built.compaction is not None, built.actions
    _ok(built.messages, cid)
    rows = [o["message"] for m in built.messages if m["role"] != "tool"
            for o in m["origin"] if "message" in o]
    assert len(rows) == len(set(rows)), "no message twice"


async def _too_long(store):
    cid = await conversations.create(store)
    for i in range(10):
        await seed_exchange(store, cid, f"question {i}",
                            [("arch__find", archive_text(60, start=1000 * (i + 1))),
                             ("notebook__read", "note " * 400)])
    await user_says(store, cid, "and now?")
    return cid


async def _failed_events(store, cid) -> list[dict]:
    return await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT data FROM events WHERE conversation_id=? AND type='context.compact_failed'",
        (cid,))])


async def test_a_failed_summary_fails_the_build_with_its_reason(store, cfg):
    cid = await _too_long(store)
    context.POLICY = SMALL
    with pytest.raises(compact.SummaryFailed, match="summary backend down"):
        await context.build(store, cfg, Model(window=20_000, summary=None), cid,
                            system="S", tools=[])
    assert [e["reason"] for e in await _failed_events(store, cid)] == [
        "summary backend down"]


async def test_the_background_build_leaves_every_message_when_the_summary_fails(store, cfg):
    cid = await _too_long(store)
    context.POLICY = SMALL
    built = await context.after_turn(store, cfg, Model(window=20_000, summary=None), cid,
                                     system="S", tools=[])
    assert built is not None and built.compaction is None
    users = [m["content"] for m in built.messages if m["role"] == "user"]
    assert any("question 0" in u for u in users), "nothing is dropped"
    assert "left out" not in built.messages[0]["content"]
    assert (await _failed_events(store, cid))[0]["reason"] == "summary backend down"
    _ok(built.messages, cid)


async def test_a_turn_whose_summary_fails_fails_with_the_reason(store, cfg):
    await _too_long(store)
    context.POLICY = SMALL
    turn = await turns.claim(store, "w")
    backend = Model(window=20_000, summary=None)
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), None, None)
    row = await store.read(lambda c: c.execute(
        "SELECT state, reason_code, reason FROM turns WHERE id=?",
        (turn["id"],)).fetchone())
    assert (row["state"], row["reason_code"]) == ("failed", "summary_failed")
    assert "summary backend down" in row["reason"]
    assert backend.calls == [], "nothing went out without the older messages"


async def test_a_message_taken_mid_turn_is_sent_once_and_the_reply_stays_on_its_branch(
        store, cfg):
    """A message that lands while it works joins the next step once, under its own
    tail; every request of the turn keeps the rules."""
    from compaction_fixtures import arch_registry
    cid = await conversations.create(store)
    await user_says(store, cid, "find it")
    turn = await turns.claim(store, "w")

    class Twice(Model):
        async def stream(self, messages, tools):
            if len(self.calls) == 0:
                await user_says(store, cid, "and the other")
            async for ev in super().stream(messages, tools):
                yield ev
    backend = Twice(tools=["arch__find"], window=50_000)
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]),
                   arch_registry(), None)
    for request in backend.calls:
        _ok(request, cid)
    last = backend.calls[-1]
    assert sum("and the other" in json.dumps(m["content"]) for m in last) == 1
    msgs = await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT id, parent_id, role FROM messages WHERE conversation_id=? ORDER BY rowid",
        (cid,))])
    tool = next(m for m in msgs if m["role"] == "tool")
    taken = [m for m in msgs if m["role"] == "user"][-1]
    assert taken["parent_id"] == tool["id"], "under the turn's own tail"
    assert [m["role"] for m in msgs if m["parent_id"] == taken["id"]] == ["assistant"]
