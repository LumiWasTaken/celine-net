"""The message tree: retry and edit branch and keep every version; what is refused."""
from __future__ import annotations

import json

from chat_fixtures import user_says

import pytest

from celine.conversations import store as conversations, tree, view
from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.core.ids import new_id
from celine.core.bus import EventBus
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns
from celine.skills import Registry, ToolResult, tool

pytestmark = pytest.mark.anyio


async def say(store, cid, reply: str) -> FakeBackend:
    """Run whatever turn is queued, with a fixed reply."""
    turn = await turns.claim(store, "w")
    backend = FakeBackend([TextDelta(reply), Done(reason="stop")])
    await run_turn(turn, store, load(), backend, EventBus(), Token(turn["id"]))
    return backend


async def shown(store, cid) -> list[str]:
    return [m["content"] for m in (await view.snapshot(store, cid))["messages"]]


async def test_retry_and_edit_keep_every_version_and_swipe_back(store):
    cid = await conversations.create(store)
    await user_says(store, cid, "a")
    await say(store, cid, "A1")
    first = (await view.snapshot(store, cid))["messages"][1]["id"]
    await user_says(store, cid, "b")
    await say(store, cid, "B")
    for n in (2, 3):                      # siblings share an ordinal: no UNIQUE to trip
        await tree.retry(store, cid, (await view.snapshot(store, cid))["messages"][1]["id"])
        backend = await say(store, cid, f"A{n}")
    sent = [m for m in backend.calls[0] if m["role"] != "system"]
    assert [m["content"].split("\n", 1)[1].split("\n\n")[0] for m in sent] == ["a"], \
        "the model sees only the active branch (the time line precedes, the tail follows)"
    sib = await tree.siblings(store, cid, first)
    assert [s["content"] for s in sib["siblings"]] == ["A1", "A2", "A3"]
    assert await shown(store, cid) == ["a", "A3"]
    await tree.switch_version(store, cid, first)
    assert await shown(store, cid) == ["a", "A1", "b", "B"], "the whole subtree comes back"

    user = (await view.snapshot(store, cid))["messages"][0]["id"]
    await tree.edit(store, cid, user, "c")
    await say(store, cid, "C")
    assert await shown(store, cid) == ["c", "C"]
    await tree.switch_version(store, cid, user)
    assert await shown(store, cid) == ["a", "A1", "b", "B"]

    b = (await view.snapshot(store, cid))["messages"][2]["id"]
    out = await store.write(lambda c: conversations.delete_message_sync(c, store, cid, b))
    assert len(out["message_ids"]) == 2 and await shown(store, cid) == ["a", "A1"], \
        "it goes with everything after it"


async def test_what_is_refused(store):
    a = await conversations.create(store)
    await user_says(store, a, "in a")
    with pytest.raises(tree.Busy):
        await conversations.delete(store, a)               # a turn is queued
    await say(store, a, "A")
    b = await conversations.create(store)
    await user_says(store, b, "in b")
    await say(store, b, "B")
    mine, hers = (await view.snapshot(store, a))["messages"]
    foreign = (await view.snapshot(store, b))["messages"]
    with pytest.raises(KeyError):
        await tree.retry(store, a, foreign[1]["id"])
    with pytest.raises(KeyError):
        await tree.edit(store, a, foreign[0]["id"], "x")
    await store.write(lambda c: c.execute(
        "INSERT INTO tool_calls(id,message_id,name,arguments,state) "
        "VALUES(?,?,'shell__type','{}','running')", (new_id(), hers["id"])))
    with pytest.raises(tree.Unresolved):
        await tree.retry(store, a, hers["id"])
    with pytest.raises(tree.Unresolved):
        await tree.edit(store, a, mine["id"], "x")
    assert await shown(store, a) == ["in a", "A"] and await shown(store, b) == ["in b", "B"]


async def test_an_edit_further_up_says_version_changed(store):
    """The server says when the visible path changes; no client guesses it."""
    cid = await conversations.create(store)
    first = await store.write(lambda c: tree.append(c, store, cid, None, "user", "one", "user"))
    await store.write(lambda c: tree.append(c, store, cid, first["id"], "assistant", "two", "model"))
    await store.write(lambda c: tree.append(c, store, cid, None, "user", "one, edited", "user"))
    events = (await view.resume(store, cid, 0))["events"]
    assert [e["type"] for e in events][-2:] == ["message.created", "version.changed"]
    assert sum(e["type"] == "version.changed" for e in events) == 1, "continuing never says it"


async def test_a_failed_reply_that_said_nothing_is_no_version(store):
    """Retry after a request that failed before the model said anything: the empty
    replies stay in the store, but the transcript counts no 2/2, 3/3 for them."""
    from celine.backends.base import BackendError

    class Broken(FakeBackend):
        async def stream(self, messages, tools):
            raise BackendError("fake has no model called that.")
            yield  # pragma: no cover

    cid = await conversations.create(store)
    await user_says(store, cid, "a")
    for _ in range(3):
        turn = await turns.claim(store, "w")
        await run_turn(turn, store, load(), Broken(), EventBus(), Token(turn["id"]))
        assert (await view.snapshot(store, cid))["turn"]["state"] == "failed"
        await tree.regenerate(store, cid)
    await say(store, cid, "A")
    msgs = (await view.snapshot(store, cid))["messages"]
    assert [m["content"] for m in msgs] == ["a", "A"]
    assert msgs[1]["siblings"] == 1
    assert (await tree.siblings(store, cid, msgs[1]["id"]))["count"] == 1
    n = await store.read(lambda c: c.execute(
        "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0])
    assert n == 4, "every attempt is kept"


async def test_a_failed_reply_that_said_something_regenerates_away(store):
    """When a turn fails mid-stream after emitting text, regenerate walks past the
    failed assistant message and queues a turn for the user message."""
    from celine.backends.base import BackendError, TextDelta

    class SaidThenBroke(FakeBackend):
        async def stream(self, messages, tools):
            yield TextDelta("Starting to answer...")
            raise BackendError("The model sent an error.")

    cid = await conversations.create(store)
    await user_says(store, cid, "hello")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, load(), SaidThenBroke(), EventBus(), Token(turn["id"]))
    assert (await view.snapshot(store, cid))["turn"]["state"] == "failed"
    await tree.regenerate(store, cid)
    next_turn = await turns.claim(store, "w")
    assert next_turn["conversation_id"] == cid


async def test_editing_her_reply_is_a_new_version_and_keeps_her_tool_steps(store):
    """The agent's edited reply: a sibling of the final step with the new text, the leaf; the
    steps above it (its tool calls) are shared, what followed stays on the old path."""
    cid = await conversations.create(store)
    umid, tid, _ = await user_says(store, cid, "look it up")
    await turns.claim(store, "w")
    step = await tree.start_assistant_message(store, cid, umid, tid)
    await tree.finish_assistant_message(store, cid, step, "Let me look.")
    await turns.record_tool_calls(store, cid, step, [{
        "id": f"{tid}:0:0", "name": "arch__find", "arguments": "{}"}])
    from celine.skills import ToolResult
    await turns.finish_tool_call(store, cid, step, f"{tid}:0:0", "arch__find",
                                 ToolResult(content="found it"), "ok")
    tool_row = await tree.active_leaf(store, cid)
    final = await tree.start_assistant_message(store, cid, tool_row, tid)
    await tree.finish_assistant_message(store, cid, final, "It is 42.", "I counted.",
                                        "stop")
    await turns.transition(store, tid, cid, "done")
    await user_says(store, cid, "thanks")
    await say(store, cid, "you're welcome")

    mid, new_tid, _ = await tree.edit(store, cid, final, "It is 43.")
    assert new_tid is None, "its edit runs no turn"
    msgs = (await view.snapshot(store, cid))["messages"]
    assert [m["content"] for m in msgs if m["role"] != "tool"] == \
        ["look it up", "Let me look.", "It is 43."]
    assert msgs[-1]["id"] == mid and msgs[-1]["siblings"] == 2
    assert msgs[-1]["reasoning"] == "I counted." and msgs[-1]["turn_id"] == tid
    assert any(m["id"] == step for m in msgs), "the tool step is the same row"
    sib = await tree.siblings(store, cid, mid)
    assert [s["content"] for s in sib["siblings"]] == ["It is 42.", "It is 43."]
    await tree.switch_version(store, cid, final)
    assert await shown(store, cid) == ["look it up", "Let me look.", "found it",
                                       "It is 42.", "thanks", "you're welcome"]


async def _updated(store, cid):
    return await store.read(lambda c: c.execute(
        "SELECT updated_at FROM conversations WHERE id=?", (cid,)).fetchone()[0])


async def test_continue_her_reply_grows_a_new_version(store):
    class Prefill(FakeBackend):
        """A queued backend's surface: a copy per message, a thinking level."""
        supports_prefill = True
        thinking = None

        def for_message(self, mid, thinking=None):
            self.thinking = thinking or self.thinking
            return self

        async def stream(self, messages, tools, body=None):
            self.thought = self.thinking
            async for ev in super().stream(messages, tools, body):
                yield ev

    cid = await conversations.create(store)
    await user_says(store, cid, "count")
    await say(store, cid, "one, two")
    snap = await view.snapshot(store, cid)
    old, was = snap["messages"][-1]["id"], snap["messages"][-1]["created_at"]
    before = await _updated(store, cid)
    mid, tid, _ = await tree.continue_message(store, cid, old, prefill=True)
    turn = await turns.claim(store, "w")
    assert turn["id"] == tid
    backend = Prefill([TextDelta(", three"), Done(reason="stop")])
    await run_turn(turn, store, load(), backend, EventBus(), Token(tid))
    assert backend.calls[0][-1] == {"role": "assistant", "content": "one, two", "origin": [
        {"conversation": cid, "turn": tid, "message": mid}]}
    assert backend.thought == "off", "the continued step runs without thinking"
    msgs = (await view.snapshot(store, cid))["messages"]
    assert msgs[-1]["id"] == mid and msgs[-1]["content"] == "one, two, three"
    assert msgs[-1]["created_at"] == was, "the continued version keeps the reply's time"
    assert await _updated(store, cid) == before, \
        "continuing does not move the conversation up the list"
    sib = await tree.siblings(store, cid, mid)
    assert [s["content"] for s in sib["siblings"]] == ["one, two", "one, two, three"]


class _Notes:
    name = "notes"
    description = "Notes."

    @tool(read_only=False, needs_answer=True)
    async def write(self, ctx, path: str) -> ToolResult:
        """Write a note."""
        return ToolResult(f"wrote {path}")


async def test_a_continued_reply_that_calls_a_tool_goes_on(store):
    """The continued step ends in a call: the next request carries the whole reply once,
    as stored, and not the prefill."""
    class Steps(FakeBackend):
        supports_prefill = True

        def __init__(self, replies):
            super().__init__()
            self.replies = replies

        def for_message(self, mid, thinking=None):
            return self

        async def stream(self, messages, tools, body=None):
            self.calls.append(messages)
            for ev in self.replies[len(self.calls) - 1]:
                yield ev

    cfg = load()
    reg = Registry(cfg)
    reg.add(_Notes())
    cid = await conversations.create(store)
    await user_says(store, cid, "count")
    await say(store, cid, "one, two")
    old = (await view.snapshot(store, cid))["messages"][-1]["id"]
    mid, tid, _ = await tree.continue_message(store, cid, old, prefill=True)
    turn = await turns.claim(store, "w")
    backend = Steps([
        [TextDelta(", three"), ToolCallDelta(index=0, name="notes__write",
                                             arguments=json.dumps({"path": "a.md"})),
         Done(reason="tool_calls")],
        [TextDelta("four"), Done(reason="stop")],
    ])
    await run_turn(turn, store, cfg, backend, EventBus(), Token(tid), reg)
    row = await store.read(lambda c: dict(c.execute(
        "SELECT state, reason_code FROM turns WHERE id=?", (tid,)).fetchone()))
    assert row == {"state": "done", "reason_code": None}
    second = backend.calls[1]
    replies = [m for m in second if m["role"] == "assistant"]
    assert [m["content"] for m in replies] == ["one, two, three"]
    assert replies[0]["tool_calls"][0]["function"]["name"] == "notes__write"


async def test_continue_without_a_prefill_is_refused_before_anything_is_written(store):
    cid = await conversations.create(store)
    await user_says(store, cid, "go on")
    await say(store, cid, "She walked to the")
    before = await view.snapshot(store, cid)
    with pytest.raises(ValueError, match="cannot continue a reply where it stopped"):
        await tree.continue_message(store, cid, before["messages"][-1]["id"], prefill=False)
    after = await view.snapshot(store, cid)
    assert after["messages"] == before["messages"], "no new version"
    assert await turns.claim(store, "w") is None, "no turn queued"


async def test_continue_of_a_cut_turn_without_a_prefill_writes_nothing(store):
    cid = await conversations.create(store)
    await user_says(store, cid, "go on")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, load(), FakeBackend([TextDelta("She walked to the"),
                                                     Done(reason="length")]),
                   EventBus(), Token(turn["id"]))
    def row(c):
        return dict(c.execute("SELECT * FROM turns WHERE id=?", (turn["id"],)).fetchone())
    was = await store.read(row)
    assert (was["state"], was["cut"]) == ("done", "length")
    with pytest.raises(ValueError, match="cannot continue a reply where it stopped"):
        await turns.continue_turn(store, cid, turn["id"], prefill=False)
    assert await store.read(row) == was, "the turn as it was"


async def test_a_cut_reply_without_a_prefill_ends_with_its_reason(store):
    """A model that cannot be prefilled is never asked to continue behind a note: the turn
    ends with the reason, and the reply stays as it was."""
    cid = await conversations.create(store)
    await user_says(store, cid, "go on")
    await say(store, cid, "She walked to the")
    old = (await view.snapshot(store, cid))["messages"][-1]["id"]
    # the model changed between the check and the run
    mid, tid, _ = await tree.continue_message(store, cid, old, prefill=True)
    turn = await turns.claim(store, "w")
    backend = FakeBackend([TextDelta("door"), Done(reason="stop")])
    await run_turn(turn, store, load(), backend, EventBus(), Token(tid))
    assert backend.calls == [], "no request"
    row = await store.read(lambda c: dict(c.execute(
        "SELECT state, reason_code, reason FROM turns WHERE id=?", (tid,)).fetchone()))
    assert (row["state"], row["reason_code"]) == ("failed", "no_prefill")
    assert "cannot continue a reply where it stopped" in row["reason"]
    assert (await view.snapshot(store, cid))["messages"][-1]["content"] == "She walked to the"


async def test_continue_is_refused_with_a_sentence(store):
    cid = await conversations.create(store)
    umid, _, _ = await user_says(store, cid, "hi")
    with pytest.raises(ValueError, match="replies can be continued"):
        await tree.continue_message(store, cid, umid, prefill=True)
    await say(store, cid, "hello")
    hers = (await view.snapshot(store, cid))["messages"][-1]["id"]
    await user_says(store, cid, "more")
    with pytest.raises(ValueError, match="is still answering"):
        await tree.continue_message(store, cid, hers, prefill=True)
    await say(store, cid, "sure")
    with pytest.raises(ValueError, match="last message"):
        await tree.continue_message(store, cid, hers, prefill=True)


async def test_an_older_version_whose_later_lines_moved_ends_the_path_there(store):
    """A mid-story line answered again keeps the lines after it on the new version; the
    old version's pointer still names one of them. Swiping back shows the old version,
    and the path ends there instead of running through the new one."""
    cid = await conversations.create(store)

    def build(c):
        a = tree.append(c, store, cid, None, "user", "a", "user")
        b = tree.append(c, store, cid, a["id"], "assistant", "b", "model")
        later = tree.append(c, store, cid, b["id"], "user", "later", "user")
        b2 = tree.append(c, store, cid, a["id"], "assistant", "b again", "model")
        c.execute("UPDATE messages SET parent_id=? WHERE id=?", (b2["id"], later["id"]))
        return b["id"], b2["id"], later["id"]
    b, b2, later = await store.write(build)
    assert await store.read(lambda c: tree._deepest_leaf(c, b)) == b
    assert await store.read(lambda c: tree._deepest_leaf(c, b2)) == later


async def _last_state_event(store, cid) -> dict:
    events = (await view.resume(store, cid, 0))["events"]
    return [e for e in events if e["type"] == "turn.state"][-1]["data"]


async def test_a_reply_that_said_nothing_leaves_the_message_unanswered(store):
    """Thinking and no words: the server names the message Regenerate answers, in the
    snapshot and in the turn's last event; while a turn waits it names none."""
    from celine.backends.base import ReasoningDelta
    cid = await conversations.create(store)
    mid, _, _ = await user_says(store, cid, "hello")
    assert (await _last_state_event(store, cid))["unanswered"] is None, "queued"
    assert (await view.snapshot(store, cid))["unanswered"] is None
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, load(), FakeBackend([ReasoningDelta("hm"), Done(reason="stop")]),
                   EventBus(), Token(turn["id"]))
    told = {"message_id": mid, "turn_id": turn["id"], "state": "done",
            "reason": "The answer was empty.", "continues": False}
    assert (await view.snapshot(store, cid))["unanswered"] == told
    assert (await _last_state_event(store, cid))["unanswered"] == told
    await tree.regenerate(store, cid)
    assert (await view.snapshot(store, cid))["unanswered"] is None
    await say(store, cid, "hi")
    assert (await view.snapshot(store, cid))["unanswered"] is None
    assert (await _last_state_event(store, cid))["unanswered"] is None


async def test_a_failed_turn_leaves_the_message_unanswered_with_its_reason(store):
    from celine.backends.base import BackendError

    class Broken(FakeBackend):
        async def stream(self, messages, tools):
            raise BackendError("fake has no model called that.")
            yield  # pragma: no cover

    cid = await conversations.create(store)
    mid, _, _ = await user_says(store, cid, "a")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, load(), Broken(), EventBus(), Token(turn["id"]))
    assert (await view.snapshot(store, cid))["unanswered"] == {
        "message_id": mid, "turn_id": turn["id"], "state": "failed",
        "reason": "fake has no model called that.", "continues": False}, "nothing to continue"


@pytest.mark.parametrize("prefill", [True, False])
async def test_a_failed_reply_that_said_something_offers_continue_where_the_model_can(
        store, monkeypatch, prefill):
    """Continue is offered beside Regenerate when the failed reply streamed words and the
    conversation's chat model continues a reply in place; the server says which."""
    from celine.backends.base import BackendError

    class SaidThenBroke(FakeBackend):
        async def stream(self, messages, tools):
            yield TextDelta("Starting to answer...")
            raise BackendError("The model sent an error.")

    asked = []
    monkeypatch.setattr(tree, "prefills", lambda c: asked.append(c) or prefill)
    cid = await conversations.create(store)
    mid, _, _ = await user_says(store, cid, "hello")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, load(), SaidThenBroke(), EventBus(), Token(turn["id"]))
    told = (await view.snapshot(store, cid))["unanswered"]
    assert (told["message_id"], told["turn_id"], told["continues"]) == (mid, turn["id"], prefill)
    assert (await _last_state_event(store, cid))["unanswered"] == told
    assert asked and set(asked) == {cid}


async def test_regenerate_starts_a_turn_for_the_unanswered_message(store):
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI
    from celine.conversations import api as conversations_api
    from celine.core.services import Services
    from celine.errors import install_error_handler

    cid = await conversations.create(store)
    mid, tid, _ = await user_says(store, cid, "a")
    await turns.claim(store, "w")
    await turns.interrupt_held(store, ["w"])          # the app stopped during it
    assert (await view.snapshot(store, cid))["unanswered"]["state"] == "interrupted"
    app = FastAPI()
    app.state.services = Services(store=store, cfg=load(),
                                  dispatcher=SimpleNamespace(notify=lambda: None))
    install_error_handler(app)
    app.include_router(conversations_api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r = await client.post(f"/api/conversations/{cid}/regenerate")
        assert r.status_code == 200, r.text
        again = await turns.claim(store, "w")
        assert again["id"] == r.json()["turn_id"] != tid
        assert again["root_message_id"] == mid
        r = await client.post(f"/api/conversations/{cid}/regenerate")
        assert r.status_code == 409, "a turn runs"


async def test_a_quiet_notice_after_the_message_leaves_it_unanswered(store):
    from celine.intercom import send
    cid = await conversations.create(store)
    mid, tid, _ = await user_says(store, cid, "a")
    await turns.claim(store, "w")
    await turns.interrupt_held(store, ["w"])
    await send(store, "system:app", f"agent:{cid}", "The app is restarting now.",
               kind="notice", wake=False)
    snap = await view.snapshot(store, cid)
    assert snap["messages"][-1]["intercom"]["wake"] is False, "it landed"
    assert snap["unanswered"]["message_id"] == mid
    await tree.regenerate(store, cid)
    assert (await turns.claim(store, "w"))["root_message_id"] == mid
