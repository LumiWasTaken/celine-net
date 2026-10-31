"""One turn, end to end, against the fake backend."""
from __future__ import annotations

from chat_fixtures import user_says


import anyio
import pytest

from celine.conversations import store as conversations, view
from celine.backends.base import BackendError, Done, ReasoningDelta, TextDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.core.bus import EventBus
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg():
    return load()


async def _run(store, cfg, backend, cid, text, token=None):
    await user_says(store, cid, text)
    turn = await turns.claim(store, "w1")
    await run_turn(turn, store, cfg, backend, EventBus(), token or Token(turn["id"]))
    return turn


async def test_abort_stops_mid_stream_and_keeps_the_partial(store, cfg):
    cid = await conversations.create(store)
    await user_says(store, cid, "go")
    turn = await turns.claim(store, "w1")
    token = Token(turn["id"])
    streaming = anyio.Event()

    class Streaming(FakeBackend):
        async def stream(self, messages, tools, body=None):
            n = 0
            async for ev in super().stream(messages, tools, body):
                yield ev
                n += 1
                if n == 3:  # the runner took three deltas
                    streaming.set()

    backend = Streaming([TextDelta(f"{i} ") for i in range(50)] + [Done(reason="stop")],
                        delay=0.01)
    async with anyio.create_task_group() as tg:
        async def stopper():
            await streaming.wait()
            await turns.request_abort(store, cid)
            token.cancel()
        tg.start_soon(stopper)
        await run_turn(turn, store, cfg, backend, EventBus(), token)
    snap = await view.snapshot(store, cid)
    assert snap["turn"]["state"] == "aborted"
    partial = snap["messages"][1]["content"]
    assert partial, "the transcript must never be rewound by an abort"
    assert len(partial.split()) < 50, "abort did not break the stream loop"


async def test_backend_failure_lands_in_the_transcript(store, cfg):
    class Broken(FakeBackend):
        async def stream(self, messages, tools):
            raise BackendError("Couldn't reach OpenRouter.")
            yield  # pragma: no cover

    cid = await conversations.create(store)
    await _run(store, cfg, Broken(), cid, "hi")
    assert (await view.snapshot(store, cid))["turn"]["state"] == "failed"
    errors = [e for e in (await view.resume(store, cid, 0))["events"] if e["type"] == "error"]
    assert errors and "OpenRouter" in errors[0]["data"]["message"]


async def test_an_adapter_that_breaks_mid_reply_finishes_it_and_fails_the_turn(store, cfg):
    """Not a BackendError (a bug in an adapter, a reply it cannot read): the reply keeps
    every word it streamed and ends (message.done), the turn fails with the reason."""
    class Breaks(FakeBackend):
        async def stream(self, messages, tools):
            yield TextDelta("half ")
            yield TextDelta("a reply")
            raise TypeError("int() argument must be a string, not 'NoneType'")

    cid = await conversations.create(store)
    await _run(store, cfg, Breaks(), cid, "hi")
    snap = await view.snapshot(store, cid)
    assert snap["turn"]["state"] == "failed"
    assert snap["messages"][-1]["content"] == "half a reply"
    events = (await view.resume(store, cid, 0))["events"]
    done = [e for e in events if e["type"] == "message.done"]
    assert done and done[-1]["data"]["text"] == "half a reply"
    errors = [e["data"] for e in events if e["type"] == "error"]
    assert len(errors) == 1 and "TypeError" in errors[0]["message"]
    assert "NoneType" in errors[0]["message"]


async def test_resume_from_every_offset_reconstructs_the_same_transcript(store, cfg):
    def render(events) -> str:
        """Apply events the way the client does: append deltas, replace on done."""
        out: dict[str, str] = {}
        for e in events:
            d = e["data"]
            if e["type"] == "message.delta":
                out[d["message_id"]] = out.get(d["message_id"], "") + d["text"]
            elif e["type"] == "message.done":
                out[d["message_id"]] = d["text"]
        return "|".join(f"{k}={v}" for k, v in sorted(out.items()))

    cid = await conversations.create(store)
    await _run(store, cfg, FakeBackend(
        [TextDelta(f"{i} ") for i in range(12)] + [Done(reason="stop")]), cid, "count")
    full = await view.resume(store, cid, 0)
    expected = render(full["events"])
    assert expected
    for after in range(0, full["seq"] + 1):
        had = [e for e in full["events"] if e["seq"] <= after]
        part = (await view.resume(store, cid, after))["events"]
        assert render(had + part) == expected, f"broken at after={after}"


async def test_lease_is_per_conversation(store, cfg):
    a = await conversations.create(store, "a")
    b = await conversations.create(store, "b")
    for cid, text in ((a, "1"), (a, "2"), (b, "1")):
        await user_says(store, cid, text)
    assert (await turns.claim(store, "w1"))["conversation_id"] == a
    assert (await turns.claim(store, "w2"))["conversation_id"] == b
    assert await turns.claim(store, "w3") is None, "a busy conversation is not leased twice"


async def test_reasoning_is_stored_and_goes_back_with_its_reply(store, cfg):
    cid = await conversations.create(store)
    await _run(store, cfg, FakeBackend([ReasoningDelta("weighing it up"), TextDelta("Blue"),
                                        Done(reason="stop")]), cid, "think then answer")
    got = await store.read(lambda c: c.execute(
        "SELECT content, reasoning FROM messages WHERE role='assistant'").fetchone())
    assert (got["content"], got["reasoning"]) == ("Blue", "weighing it up")
    assert (await view.snapshot(store, cid))["turn"]["state"] == "done"
    backend = FakeBackend([TextDelta("ok"), Done(reason="stop")])
    await _run(store, cfg, backend, cid, "again")
    [reply] = [m for m in backend.calls[0] if m["role"] == "assistant"]
    assert reply["reasoning_content"] == "weighing it up", \
        "the same bytes as in the turn that wrote it"
