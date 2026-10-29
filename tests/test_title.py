"""The title of a conversation: the first line the small model wrote, as it wrote it; a
failure leaves it without one."""
import anyio
import pytest

from chat_fixtures import user_says

from celine.backends.base import BackendError, Done, TextDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.core.bus import EventBus
from celine.turns.dispatcher import Dispatcher
from celine.turns.title import title_for


def test_the_title_is_the_first_line_as_the_model_wrote_it():
    async def run():
        made = FakeBackend([TextDelta("**Two gates** that stop costing me missions.\nmore"),
                            Done(reason="stop")])
        assert await title_for(made, "x", "y") == \
            "**Two gates** that stop costing me missions."
    anyio.run(run)


class Broken(FakeBackend):
    async def stream(self, messages, tools, body=None):
        raise BackendError("the small model is gone")
        yield  # pragma: no cover


async def _first_exchange(store, cid):
    _, tid, _ = await user_says(store, cid, "Tell me about rain in the hills")
    mid = await tree.start_assistant_message(store, cid, await tree.active_leaf(store, cid),
                                             tid)
    await tree.finish_assistant_message(store, cid, mid, "It rains a lot there.", None,
                                        "stop")


@pytest.mark.anyio
async def test_the_small_models_title_is_stored_as_it_came(store):
    asked = []

    def small():
        asked.append(1)
        return FakeBackend([TextDelta("Rain, in the hills!"), Done(reason="stop")])
    d = Dispatcher(store, load(), EventBus(), backend_factory=small)
    cid = await conversations.create(store)
    await _first_exchange(store, cid)
    await d._title(cid)
    assert await conversations.title(store, cid) == "Rain, in the hills!"
    assert len(asked) == 1


@pytest.mark.anyio
async def test_a_failing_small_model_leaves_no_title_and_asks_no_other(store):
    asked = []

    def broken():
        asked.append(1)
        return Broken()
    d = Dispatcher(store, load(), EventBus(), backend_factory=broken)
    cid = await conversations.create(store)
    await _first_exchange(store, cid)
    await d._title(cid)
    assert not await conversations.title(store, cid)
    assert len(asked) == 1, "no fallback to the chat model"
