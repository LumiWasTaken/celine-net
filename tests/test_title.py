"""The title of a conversation: the model's line in plain words, never a label it echoed;
the small model, then the chat model, then the user's first line."""
import anyio
import pytest

from chat_fixtures import user_says

from celine.backends.base import BackendError, Done, TextDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.core.bus import EventBus
from celine.turns.dispatcher import Dispatcher
from celine.turns.title import plain, title_for


def test_title_skips_an_echoed_label():
    async def run():
        # the fake model echoes the prompt: "You said: First message:" is no title
        assert await title_for(FakeBackend(echo=True), "hello there", "hi") == ""
        real = FakeBackend([TextDelta("Saying hello\n"), Done(reason="stop")])
        assert await title_for(real, "hello there", "hi") == "Saying hello"
    anyio.run(run)


def test_a_title_is_plain_words():
    assert plain("**Saying** `hello`.") == "Saying hello"
    assert plain("## A *quiet*  night") == "A quiet night"
    assert plain("Invoice due,") == "Invoice due"


def test_the_models_title_is_at_most_four_words():
    async def run():
        long = FakeBackend([TextDelta("Two gates that stop costing me whole missions.\n"),
                            Done(reason="stop")])
        assert plain(await title_for(long, "x", "y")) == "Two gates that stop"
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
async def test_a_failing_small_model_hands_the_title_to_the_chat_model(store):
    made = iter([Broken(), FakeBackend([TextDelta("Rain in the hills"), Done(reason="stop")])])
    d = Dispatcher(store, load(), EventBus(), backend_factory=lambda: next(made))
    cid = await conversations.create(store)
    await _first_exchange(store, cid)
    await d._title(cid)
    assert await conversations.title(store, cid) == "Rain in the hills"


@pytest.mark.anyio
async def test_with_no_model_answering_the_title_is_the_first_line(store):
    d = Dispatcher(store, load(), EventBus(), backend_factory=Broken)
    cid = await conversations.create(store)
    await _first_exchange(store, cid)
    await d._title(cid)
    assert await conversations.title(store, cid) == "Tell me about rain"
