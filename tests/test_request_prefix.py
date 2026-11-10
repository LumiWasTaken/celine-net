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


def _said(chat, turn: int = -1) -> str:
    """The last user message of a turn's request: the message with its tail."""
    return [m for m in chat.turns[turn][0] if m["role"] == "user"][-1]["content"]


async def test_an_always_loaded_note_is_written(chat):
    """The notes it always carries go with the message, once: a turn after a note
    changed sends that note again, a turn after nothing changed sends none, and the
    system prompt holds none."""
    await write_notes(chat.store, chat.cfg, {
        "style.md": "load: always\n\nShort sentences.",
        "food.md": "load: always\n\nNo olives."})
    await chat.say("one")
    assert "Short sentences." in _said(chat) and "No olives." in _said(chat)
    assert "Short sentences." not in chat.turns[-1][0][0]["content"], "the system prompt"
    await write_notes(chat.store, chat.cfg, {"style.md": "load: always\n\nLong sentences."})
    await chat.say("two")
    chat.held()
    assert "Long sentences." in _said(chat) and "No olives." not in _said(chat)
    await chat.say("three")
    chat.held()
    assert "[your notes]" not in _said(chat) and "Today is" not in _said(chat)
    assert "Now it is [" in _said(chat), "the time goes every turn"


async def test_a_note_is_left_out_of_the_chat(chat):
    """The user's switch (Context, a note off here) is a setting: the note leaves every
    tail in view."""
    await write_notes(chat.store, chat.cfg, {
        "style.md": "load: always\n\nShort sentences.",
        "food.md": "load: always\n\nNo olives."})
    await chat.say("one")
    await chat.store.write(lambda c: c.execute(
        "UPDATE conversations SET notes_off='[\"food.md\"]' WHERE id=?", (chat.cid,)))
    await chat.say("two")
    whole = str(chat.turns[-1][0])
    assert "No olives." not in whole and "Short sentences." in whole


async def test_a_summary_inside_a_turn_leaves_the_notes_in_view(chat):
    """After a summary moved the start of the view, the tail that is first there carries
    the notes and the date whole."""
    await write_notes(chat.store, chat.cfg, {"style.md": "load: always\n\nShort sentences."})
    chat.reg.add(Arch(lines=100))
    chat.backend = Searching(window=60_000)
    for n in range(40):
        await chat.say(f"search {n}")
        if chat.backend.summaries:
            break
    assert chat.backend.summaries, "a summary was made"
    first = next(m for m in chat.turns[-1][0] if m["role"] == "user")["content"]
    assert "Short sentences." in first and "Today is" in first
    assert "Short sentences." not in str(chat.backend.summaries[-1]), "a summary reads no tail"


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


async def test_a_skill_goes_down_and_comes_back(chat):
    await chat.say("one")
    web = chat.reg.skills["web"]
    web.status, web.error = "down", "The server does not answer."
    await chat.say("two")
    chat.held()
    del web.status, web.error
    await chat.say("three")
    chat.held()


async def test_the_chat_grows_long(chat):
    for n in range(12):
        await chat.say(f"message {n}")
        if n:
            chat.held()


async def test_a_reply_cites_a_ref_an_older_shortened_result_holds(chat):
    """Searches fill the window until the oldest results go as stubs (a large step,
    once). A later reply then cites a line of the first result: that stub stays as it
    went."""
    chat.reg.add(Arch(lines=100))
    chat.backend = Searching(window=60_000)

    def stubs() -> list[str]:
        return [m["content"] for m in chat.turns[-1][0]
                if m["role"] == "tool" and m["content"].startswith(compact.STUB_HEAD)]
    for n in range(20):
        await chat.say(f"search {n}")
        if stubs():
            break
    assert "[wa:6003]" in stubs()[0], "the first result is a stub that names the ref"
    chat.backend.answer = "It is in [wa:6003]."
    await chat.say("which one")
    chat.held()
    await chat.say("thanks")
    chat.held()
