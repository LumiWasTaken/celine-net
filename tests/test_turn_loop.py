"""The turn loop's ends: a step with an answer and a note ends the turn, a tool that fails
twice the same way is out, a step runs at most 12 calls, no tool goes out as an empty
stub, and Continue on a capped turn gets a fresh budget."""
from __future__ import annotations

import json

import pytest

from chat_fixtures import until, user_says

from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.skills import Registry, ToolResult, tool
from celine.skills.meta import Skills
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg():
    return load()


class Replies(FakeBackend):
    """Replies in order (the last one again when they run out); records the tools each
    request offered."""

    def __init__(self, replies):
        super().__init__()
        self.replies = replies
        self.offered: list[list[dict]] = []

    async def stream(self, messages, tools, body=None):
        self.calls.append(messages)
        self.offered.append(list(tools or []))
        for ev in self.replies[min(len(self.calls), len(self.replies)) - 1]:
            yield ev


def _calls(*calls) -> list:
    out = [ToolCallDelta(index=i, id=f"c{i}", name=n, arguments=json.dumps(a))
           for i, (n, a) in enumerate(calls)]
    return out + [Done(reason="tool_calls")]


class Notebook:
    name = "notebook"
    description = "Her notes."

    @tool(read_only=False, needs_answer=False)
    async def write(self, ctx, path: str, text: str) -> ToolResult:
        """Write a note."""
        return ToolResult(f"wrote {path}")


class Web:
    name = "web"
    description = "Searches the web."

    @tool()
    async def search(self, ctx, query: str) -> ToolResult:
        """Search."""
        return ToolResult("Search failed: the search server did not answer.", is_error=True)


class Browser:
    name = "browser"
    description = "A browser."
    status = "down"
    error = "Not set up: see Settings, Tools."

    @tool()
    async def open(self, ctx, url: str) -> ToolResult:
        """Open a page."""
        return ToolResult("page")


def _registry(cfg, *skills) -> Registry:
    reg = Registry(cfg)
    for s in skills:
        reg.add(s)
    reg.add(Skills(reg, cfg))
    return reg


async def _run(store, cfg, reg, cid, backend) -> dict:
    await user_says(store, cid, "hello there")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), reg)
    return turn


async def _results(store) -> list[tuple[str, str, str]]:
    return await store.read(lambda c: [tuple(r) for r in c.execute(
        "SELECT tc.name, tc.state, m.content FROM tool_calls tc JOIN messages m ON "
        "m.id = tc.result_message_id ORDER BY tc.rowid")])


async def test_an_answer_with_a_note_written_ends_the_turn_in_one_step(store, cfg):
    reg = _registry(cfg, Notebook())
    cid = await conversations.create(store)
    b = Replies([[TextDelta("Sure, noted."), *_calls(
        ("notebook__write", {"path": "a.md", "text": "x"}))]])
    turn = await _run(store, cfg, reg, cid, b)
    assert len(b.calls) == 1
    state, texts = await store.read(lambda c: (
        c.execute("SELECT state FROM turns WHERE id=?", (turn["id"],)).fetchone()[0],
        [r[0] for r in c.execute("SELECT content FROM messages WHERE role='assistant' AND "
                                 "conversation_id=?", (cid,))]))
    assert state == "done" and texts == ["Sure, noted."]


async def test_a_tool_that_fails_the_same_way_stays_usable(store, cfg, monkeypatch):
    from celine.turns import loads
    monkeypatch.setattr(loads, "CORE", loads.CORE | {"web"})
    reg = _registry(cfg, Web())
    cid = await conversations.create(store)
    b = Replies([_calls(("web__search", {"query": "cats"})),
                 _calls(("web__search", {"query": "dogs"})),
                 _calls(("web__search", {"query": "birds"})),
                 [TextDelta("I could not search."), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    rows = await _results(store)
    assert len(rows) == 3
    assert not any(r[2].startswith("Not run") for r in rows)


async def test_a_step_runs_every_call(store, cfg):
    reg = _registry(cfg, Notebook())
    cid = await conversations.create(store)
    many = [("notebook__write", {"path": f"n{i}.md", "text": "x"}) for i in range(30)]
    b = Replies([_calls(*many), [TextDelta("ok"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    rows = await _results(store)
    assert [r[1] for r in rows] == ["ok"] * 30


async def test_a_down_skill_sends_no_tool_and_says_why(store, cfg, monkeypatch):
    from celine.turns import loads
    monkeypatch.setattr(loads, "CORE", loads.CORE | {"browser"})
    reg = _registry(cfg, Browser(), Notebook())
    cid = await conversations.create(store)
    b = Replies([_calls(("browser__open", {})), [TextDelta("ok"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    sent = [t["function"] for t in b.offered[0]]
    assert not [t for t in sent if t["name"].startswith("browser__")]
    assert all(t["parameters"].get("properties") for t in sent)
    rows = await _results(store)
    assert rows[0][2] == "browser is down: Not set up: see Settings, Tools. Work without it."
    system = b.calls[0][0]["content"]
    assert "browser is down: Not set up: see Settings, Tools. Work without it." in system


async def test_load_refuses_a_down_skill(store, cfg):
    from celine.skills import ToolContext
    reg = _registry(cfg, Browser())
    cid = await conversations.create(store)
    ctx = ToolContext(conversation_id=cid, turn_id="t", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    out = await reg.skills["skills"].load(ctx, name="browser")
    assert out.is_error and out.content == (
        "browser is down: Not set up: see Settings, Tools. Its tools stay out.")


async def test_continue_on_a_capped_turn_gets_a_fresh_step_budget(store, cfg):
    cfg.settings.general.update({"max_tool_iterations": 2})
    reg = _registry(cfg, Notebook())
    cid = await conversations.create(store)
    # every reply writes a note without answering: the turn caps after two steps
    b = Replies([_calls(("notebook__write", {"path": "a.md", "text": "x"}))])
    turn = await _run(store, cfg, reg, cid, b)
    state = await store.read(lambda c: c.execute(
        "SELECT state FROM turns WHERE id=?", (turn["id"],)).fetchone()[0])
    assert state == "capped" and len(b.calls) == 2
    await turns.continue_turn(store, cid, turn["id"], reg)
    again = await turns.claim(store, "w")
    assert again["cap_base"] == 2
    await run_turn(again, store, cfg, b, EventBus(), Token(again["id"]), reg)
    assert len(b.calls) == 4               # two more steps, not none


async def test_her_voice_line_closes_the_newest_tool_result(store, cfg):
    cfg.her.talk = "a chat.\n\n## how I sound\n\nshort and dry, a little formal\nmore\n"
    reg = _registry(cfg, Notebook())
    cid = await conversations.create(store)
    b = Replies([_calls(("notebook__write", {"path": "a.md", "text": "x"}),
                        ("notebook__write", {"path": "b.md", "text": "y"})),
                 [TextDelta("done"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    tools = [m for m in b.calls[1] if m["role"] == "tool"]
    assert tools[-1]["content"] == \
        "wrote b.md\n\n[how you sound] short and dry, a little formal"
    assert tools[0]["content"] == "wrote a.md"
    stored = await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT content FROM messages WHERE role='tool'")])
    assert "how you sound" not in "".join(stored), "the request only"


async def test_a_stop_before_any_word_leaves_no_empty_reply(store, cfg):
    import anyio
    cid = await conversations.create(store)
    await user_says(store, cid, "hello there")
    turn = await turns.claim(store, "w")
    token = Token(turn["id"])
    slow = FakeBackend([TextDelta("late"), Done(reason="stop")], delay=3600)
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_turn, turn, store, cfg, slow, EventBus(), token, None)
        await until(lambda: slow.calls)          # asked, no word yet
        token.cancel()
    state, replies, leaf, user = await store.read(lambda c: (
        c.execute("SELECT state FROM turns WHERE id=?", (turn["id"],)).fetchone()[0],
        c.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0],
        c.execute("SELECT active_leaf_id FROM conversations WHERE id=?", (cid,)).fetchone()[0],
        c.execute("SELECT id FROM messages WHERE role='user'").fetchone()[0]))
    assert (state, replies, leaf) == ("aborted", 0, user)
