"""A message from the user ends a wait in her turn (turns/cancel.Token.user_wrote): the
call returns at once and the message lands at the next step. Through the real runner,
Intercom and the dispatcher's hook."""
from __future__ import annotations

from chat_fixtures import user_says

import anyio
import pytest

from celine.backends.base import Done, TextDelta, ToolCallDelta
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.intercom import Intercom
from celine.skills import Registry, ToolResult, tool
from celine.turns import store as turns
from celine.turns.cancel import until_set
from celine.turns.dispatcher import Dispatcher
from celine.turns.runner import run_turn
from fake_backend import FakeBackend, _text

pytestmark = pytest.mark.anyio


class Waiter:
    name = "waiter"
    description = "waits"

    def __init__(self):
        self.waiting = anyio.Event()

    @tool()
    async def wait(self, ctx) -> ToolResult:
        """Waits."""
        self.waiting.set()
        ended, _ = await until_set(anyio.sleep_forever, ctx.user_wrote)
        return ToolResult(content="the wait ended" if ended else "never")


class Model(FakeBackend):
    """Calls waiter__wait once, then answers."""

    async def stream(self, messages, tools, body=None, model=None):
        self.calls.append(messages)
        if len(self.calls) == 1:
            yield ToolCallDelta(index=0, id="w1", name="waiter__wait", arguments="{}")
            yield Done(reason="tool_calls")
        else:
            yield TextDelta("going fine")
            yield Done(reason="stop")


async def test_the_users_message_ends_her_wait_and_lands_next(store):
    cfg = load()
    reg, waiter, model = Registry(), Waiter(), Model()
    reg.add(waiter)
    dispatcher = Dispatcher(store, cfg, EventBus())
    intercom = Intercom(store, user_wrote=dispatcher.user_wrote)
    cid = await conversations.create(store)
    await user_says(store, cid, "download the model")
    turn = await turns.claim(store, "w")
    token = dispatcher.tokens.open(turn["id"])
    dispatcher._active[turn["id"]] = cid           # held by a worker
    async with anyio.create_task_group() as tg:
        async def write() -> None:
            await waiter.waiting.wait()
            await intercom.send("user", f"agent:{cid}", "how is it going?")
        tg.start_soon(write)
        with anyio.fail_after(5):
            await run_turn(turn, store, cfg, model, EventBus(), token, reg)
    last = model.calls[-1]
    tool_at = next(i for i, m in enumerate(last) if m.get("role") == "tool")
    assert _text(last[tool_at]["content"]).endswith("the wait ended")
    assert any(m.get("role") == "user" and "how is it going?" in _text(m["content"])
               for m in last[tool_at + 1:]), "the message lands at the next step"
    assert not token.user_wrote.is_set(), "taken: the next wait waits again"
