"""Stop at once in every phase, and a resumed turn after a restart. Fakes only."""
from __future__ import annotations

from chat_fixtures import until, user_says

import time
from types import SimpleNamespace

import anyio
import httpx
import pytest

from celine.conversations import store as conversations, view
from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.backends.llm import LLM
from celine.backends.queue import Priority
from celine.backends.router import Router
from celine.boot import load
from celine.core.bus import EventBus
from celine.skills import Registry, ToolContext, ToolResult, tool
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

URL = "http://main:11434/v1"
PROVIDERS = {"main": {"kind": "llama-swap", "base_url": URL,
                      "models": {"unc": {"caps": ["chat"], "slots": 1},
                                 "big": {"caps": ["chat"], "slots": 1}}}}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS,
            roles={"chat": {"chain": ["main/unc", "main/big"]}})
    c = load()
    c.llm = {"queue": {"wait_seconds": 30, "background_wait_seconds": 30}}
    return c


class Net:
    """A backend with a base_url. `hang`: no event comes, the way llama-swap holds a
    request while it loads a model."""

    def __init__(self, name, hang=False):
        self.name = self.model = name.split("/")[1]
        self.cfg = SimpleNamespace(id=name, base_url=URL, raw={})
        self.supports_tools, self.supports_images = True, False
        self.hang, self.calls, self.closed = hang, 0, 0

    async def stream(self, messages, tools):
        self.calls += 1
        try:
            if self.hang:
                await anyio.sleep_forever()
            yield TextDelta("hello there")
            yield Done(reason="stop")
        finally:
            self.closed += 1


def make_llm(cfg, unc, big, running):
    """An LLM whose router sees llama-swap's /running as `running`."""
    by_id = {"main/unc": unc, "main/big": big}
    answer = httpx.MockTransport(lambda r: httpx.Response(200, json={"running": running}))
    return LLM(cfg, build=lambda bc: by_id[bc.id], router=Router(cfg, transport=answer))


async def _turn(store, text="hi"):
    cid = await conversations.create(store)
    await user_says(store, cid, text)
    return cid, await turns.claim(store, "w1")


def _bind(llm, turn, token, store):
    async def emit(d):
        await store.append_event(turn["conversation_id"], "llm.queue", d)
    return llm.bind(role="chat", priority=Priority.INTERACTIVE,
                    conversation_id=turn["conversation_id"], turn_id=turn["id"],
                    token=token, emit=emit)


async def _run_and_stop(store, cfg, cid, turn, backend, busy, reg=None):
    """Stops the turn once `busy()` is true; it must end within a second of the Stop."""
    token = Token(turn["id"])
    stopped = []

    async def stop():
        await until(busy)
        stopped.append(time.monotonic())
        await turns.request_abort(store, cid)
        token.cancel()
    async with anyio.create_task_group() as tg:
        tg.start_soon(stop)
        with anyio.fail_after(5):
            b = _bind(backend, turn, token, store) if isinstance(backend, LLM) else backend
            await run_turn(turn, store, cfg, b, EventBus(), token, reg)
    assert time.monotonic() - stopped[0] < 1.0
    snap = await view.snapshot(store, cid)
    assert snap["turn"]["state"] == "aborted"
    return snap


# ---- Stop at once --------------------------------------------------------------------------

async def test_stop_while_the_model_loads_cancels_the_request_at_once(store, cfg):
    unc, big = Net("main/unc", hang=True), Net("main/big")
    llm = make_llm(cfg, unc, big, [{"model": "unc", "state": "starting"}])
    cid, turn = await _turn(store)
    await _run_and_stop(store, cfg, cid, turn, llm, lambda: unc.calls)
    assert unc.closed == 1, "the in-flight request was cancelled, not left running"
    assert big.calls == 0, "a Stop never moves on to the next model"
    assert llm.scheduler.running == [] and llm.scheduler.waiting == []


class Tools:
    """A read-only tool that takes forever, and one with a side effect."""
    name, description = "t", "test tools"

    def __init__(self):
        self.ran: list[str] = []
        self.waiting = anyio.Event()

    @tool()
    async def wait(self, ctx: ToolContext) -> ToolResult:
        """Waits a long time."""
        self.waiting.set()
        await anyio.sleep_forever()
        self.ran.append("wait")
        return ToolResult(content="done")

    @tool(read_only=False)
    async def poke(self, ctx: ToolContext) -> ToolResult:
        """Changes something."""
        self.ran.append("poke")
        return ToolResult("poked")


async def test_stop_while_a_tool_runs(store, cfg):
    reg, tools = Registry(), Tools()
    reg.add(tools)
    cid, turn = await _turn(store)
    backend = FakeBackend([ToolCallDelta(index=0, id="c1", name="t__wait", arguments="{}"),
                           Done(reason="tool_calls")])
    snap = await _run_and_stop(store, cfg, cid, turn, backend, tools.waiting.is_set, reg)
    assert [c["state"] for cs in snap["tool_calls"].values() for c in cs] == ["cancelled"]
    assert tools.ran == []


async def test_stop_on_an_unclaimed_turn_ends_it_for_good(store):
    cid = await conversations.create(store)
    await user_says(store, cid, "hi")          # queued, nobody claimed it
    tid = await turns.request_abort(store, cid)
    snap = await view.snapshot(store, cid)
    assert snap["turn"]["id"] == tid and snap["turn"]["state"] == "aborted"
    assert await turns.claim(store, "w1") is None


async def test_a_queued_user_turn_is_claimed_before_an_older_job_turn(store):
    busy = await conversations.create(store)
    await user_says(store, busy, "a job ended")
    await store.write(lambda c: c.execute("UPDATE turns SET origin='intercom'"))
    cid = await conversations.create(store)
    await user_says(store, cid, "hi")
    assert (await turns.claim(store, "w1"))["conversation_id"] == cid
    assert (await turns.claim(store, "w2"))["conversation_id"] == busy


async def test_a_stop_recorded_while_the_reply_streamed_runs_none_of_its_calls(store):
    """The abort flag lands while the reply with a tool call streams (the worker's token
    not yet told): the turn ends aborted and the tool never runs."""
    reg, tools = Registry(), Tools()
    reg.add(tools)
    cid, turn = await _turn(store, "go")

    class Stopper(FakeBackend):
        async def stream(self, messages, tools):
            await turns.request_abort(store, cid)
            yield ToolCallDelta(index=0, id="a", name="t__poke", arguments="{}")
            yield Done(reason="tool_calls")
    await run_turn(turn, store, load(), Stopper(), EventBus(), Token(turn["id"]), reg)
    assert tools.ran == []
    assert (await view.snapshot(store, cid))["turn"]["state"] == "aborted"


# ---- a resumed turn ----------------------------------------------------------------------------

async def test_a_resumed_turn_whose_answer_was_complete_is_finished_not_rewritten(store, cfg):
    import celine.turns.runner as runner
    cid, turn = await _turn(store)

    async def halt(*a, **k):        # the server stops right after the answer streamed
        raise RuntimeError("process exited")
    orig, runner._verify_citations = runner._verify_citations, halt
    try:
        with pytest.raises(RuntimeError):
            await run_turn(turn, store, cfg, FakeBackend([TextDelta("the whole answer."),
                                                          Done(reason="stop")]),
                           EventBus(), Token(turn["id"]))
    finally:
        runner._verify_citations = orig
    assert (await turns.sweep(store))["interrupted"] == 1
    await turns.continue_turn(store, cid, turn["id"])          # the user's Continue
    again = await turns.claim(store, "w2")
    assert again["id"] == turn["id"]
    second = FakeBackend([TextDelta("written again"), Done(reason="stop")])
    await run_turn(again, store, cfg, second, EventBus(), Token(again["id"]))
    snap = await view.snapshot(store, cid)
    assert snap["turn"]["state"] == "done"
    assert second.calls == [], "the model is not asked again"
    assert snap["messages"][-1]["content"] == "the whole answer."
