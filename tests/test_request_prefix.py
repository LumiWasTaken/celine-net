"""A request starts with the request before it: between two turns of one chat, with no
settings change, the bytes of the earlier request are the start of the later one and the
tool list is the same, so a model server's cached prompt holds (turns/context.py). One
row per thing that happens between two turns."""
from __future__ import annotations

import pytest
from chat_fixtures import user_says
from compaction_fixtures import Arch, Model
from notes_fixtures import write_notes
from settings_fixtures import make_app
from test_turn_loop import Replies

from celine.backends.base import Done, TextDelta
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.turns import compact, store as turns, tokens
from celine.turns.cancel import Token
from celine.turns.runner import run_turn

pytestmark = pytest.mark.anyio


class Chat:
    """One chat on the app's own registry: `say` runs a turn, `held` compares the last
    two turns' requests."""

    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg
        self.reg = make_app(cfg, store).state.services.registry
        self.backend = Replies([[TextDelta("ok"), Done(reason="stop")]])
        self.cid: str | None = None
        self.turns: list[tuple[list[dict], list[dict]]] = []    # (last request, its tools)

    async def say(self, text: str) -> None:
        if self.cid is None:
            self.cid = await conversations.create(self.store, "chat")
        await user_says(self.store, self.cid, text)
        turn = await turns.claim(self.store, "w")
        await run_turn(turn, self.store, self.cfg, self.backend, EventBus(),
                       Token(turn["id"]), self.reg)
        self.turns.append((self.backend.calls[-1], self.backend.offered[-1]))

    def held(self) -> None:
        (before, tools), (after, tools_after) = self.turns[-2:]
        assert [t["function"]["name"] for t in tools_after] == \
            [t["function"]["name"] for t in tools], "the tool list"
        assert tools_after == tools, "the tools' bytes"
        for i, (a, b) in enumerate(zip(before, after)):
            assert a == b, f"message {i} ({a.get('role')}) changed"
        assert len(after) > len(before)


class Searching(Model):
    """Calls arch__find once per turn, then answers `answer`; records the tools."""

    def __init__(self, window: int):
        super().__init__(tools=["arch__find"], window=window)
        self.offered: list[list[dict]] = []

    async def stream(self, messages, tools):
        if not messages[0]["content"].startswith("You write the memory"):
            self.offered.append(list(tools or []))
        async for ev in super().stream(messages, tools):
            yield ev


@pytest.fixture
def chat(store):
    return Chat(store, load())


async def test_nothing_happens_between_two_turns(chat):
    await chat.say("one")
    await chat.say("two")
    chat.held()
    await chat.say("three")
    chat.held()


@pytest.mark.xfail(strict=True, reason="2.2: always-loaded notes are in the system prompt")
async def test_an_always_loaded_note_is_written(chat):
    await write_notes(chat.store, chat.cfg, {"style.md": "load: always\n\nShort sentences."})
    await chat.say("one")
    await write_notes(chat.store, chat.cfg, {"style.md": "load: always\n\nLong sentences."})
    await chat.say("two")
    chat.held()
    assert "Long sentences." in str(chat.turns[-1][0])


async def test_a_skill_is_switched_off_and_on(chat):
    await chat.say("one")
    web = chat.reg.skills["web"]
    chat.reg.remove("web")
    chat.reg.add(web)
    await chat.say("two")
    chat.held()


async def test_the_process_restarts(chat):
    await chat.say("one")
    chat.reg = make_app(chat.cfg, chat.store).state.services.registry
    tokens._counters.clear()
    await chat.say("two")
    chat.held()


@pytest.mark.xfail(strict=True, reason="2.5: a down skill's tools leave the list")
async def test_a_skill_goes_down_and_comes_back(chat):
    await chat.say("one")
    web = chat.reg.skills["web"]
    web.status, web.error = "down", "The server does not answer."
    await chat.say("two")
    chat.held()
    del web.status, web.error
    await chat.say("three")
    chat.held()


@pytest.mark.xfail(strict=True, reason="2.1: past history_messages every turn drops the oldest")
async def test_the_chat_passes_200_messages(chat):
    chat.cfg.settings.general["history_messages"] = 4
    for text in ("one", "two", "three", "four"):
        await chat.say(text)
    chat.held()


async def test_a_reply_cites_a_ref_an_older_shortened_result_holds(chat):
    """Seven searches fill the window, so the oldest results go as stubs (a large step,
    once). A later reply then cites a line of the first result: that stub stays as it
    went."""
    chat.reg.add(Arch(lines=100))
    chat.backend = Searching(window=60_000)
    for n in range(7):
        await chat.say(f"search {n}")
    stubs = [m["content"] for m in chat.turns[-1][0]
             if m["role"] == "tool" and m["content"].startswith(compact.STUB_HEAD)]
    assert stubs and "[wa:6003]" in stubs[0], "the first result is a stub that names the ref"
    chat.backend.answer = "It is in [wa:6003]."
    await chat.say("which one")
    chat.held()
    await chat.say("thanks")
    chat.held()
