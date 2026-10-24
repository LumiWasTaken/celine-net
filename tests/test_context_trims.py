"""What its context leaves out or names: the own-mind block in full only when
something in it changed (skills/mind.py), always-loaded notes over 1.5 KB as their head
(turns/loads.py), and every tool result named by its tool on the wire (backends)."""
from __future__ import annotations

import json

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from notes_fixtures import notes_cfg, select_turn, write_notes
from test_anthropic import thought_and_called

from celine.backends.ollama import OllamaBackend
from celine.backends.openai import build
from celine.config import BackendConfig
from celine.boot import load
from celine.conversations import store as conversations
from celine.skills import Registry, mind
from celine.turns import context, loads, message_context

pytestmark = pytest.mark.anyio


# ---- the own-mind block ------------------------------------------------------------------

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


class _Her:
    """One conversation of its own on a fixed clock. Each message asks a fresh skill (as
    after a restart: what it was sent last is read from the database)."""

    def __init__(self, store, cfg, cid):
        self.store, self.cfg, self.cid = store, cfg, cid
        self.model, self.down = "m1", []

    async def says(self, mid: str, parent: str | None, minutes: float) -> str:
        """User message `mid` under `parent` at T0 + `minutes` and its reply a minute
        later (on a turn of the model now). -> the block stored for the message."""
        at = (T0 + timedelta(minutes=minutes)).isoformat()
        back = (T0 + timedelta(minutes=minutes + 1)).isoformat()

        def rows(c, model=self.model):
            c.execute("INSERT INTO messages(id, conversation_id, parent_id, role, content, "
                      "created_at) VALUES(?, ?, ?, 'user', 'hi', ?)",
                      (mid, self.cid, parent, at))
            c.execute("INSERT INTO turns(id, conversation_id, state, model, created_at) "
                      "VALUES(?, ?, 'done', ?, ?)", ("t" + mid, self.cid, model, back))
        await self.store.write(rows)
        skill = mind.Mind(self.cfg)
        router = SimpleNamespace(chain=lambda role, cid: [self.model],
                                 backend_config=lambda b: SimpleNamespace(model=b))
        skill.app = SimpleNamespace(store=self.store, cfg=self.cfg, registry=None,
                                    llm=SimpleNamespace(router=router))
        skill._down = lambda place: list(self.down)
        reg = Registry()
        reg.add_specs(skill, [])
        await message_context.ensure(self.store, self.cfg, reg, self.cid, "t" + mid,
                                     [{"id": mid, "role": "user", "content": "hi",
                                       "created_at": at}])
        await self.store.write(lambda c: c.execute(
            "INSERT INTO messages(id, conversation_id, parent_id, role, content, turn_id, "
            "created_at) VALUES(?, ?, ?, 'assistant', 'ok', ?, ?)",
            (mid + "r", self.cid, mid, "t" + mid, back)))
        text, report = await self.store.read(lambda c: c.execute(
            "SELECT text, report FROM message_context WHERE message_id=?", (mid,)).fetchone())
        return Block(text, "state" in json.loads(report).get("mind", {}))


class Block(str):
    """A stored block, and whether it was whole (its report has the state)."""
    def __new__(cls, text: str, whole: bool):
        b = super().__new__(cls, text)
        b.whole = whole
        return b


async def _feel(store, emotion: str, minutes: float, why: str) -> None:
    await store.write(lambda c: c.execute(
        "INSERT INTO moods(at, emotion, strength, why, by) VALUES(?, ?, 0.9, ?, 'her')",
        ((T0 + timedelta(minutes=minutes)).isoformat(), emotion, why)))


def _whole(text: str) -> bool:
    """A whole block: its state is kept; its text is empty when it has nothing to say."""
    return (text == "" or text.startswith("[your mind]\n")) and "\nnow: " not in text and text.whole


def _nothing(text: str) -> bool:
    return text == "" and not text.whole


async def test_the_mind_block_comes_whole_only_when_something_in_it_changed(
        store, monkeypatch):
    cfg = load()
    her = _Her(store, cfg, await conversations.create(store))
    await _feel(store, "happy", -10, "a good morning")
    first = await her.says("u1", None, 0)
    assert _whole(first) and "\nmood: " in first and "happy since" in first
    assert _nothing(await her.says("u2", "u1r", 5)), "nothing changed: no block"
    # its mood
    await _feel(store, "angry", 8, "he forgot")
    assert "angry since" in await her.says("u3", "u2r", 10)
    assert _nothing(await her.says("u4", "u3r", 15))
    # a new model
    her.model = "m2"
    assert "\nmodel now: m2" in await her.says("u5", "u4r", 20)
    assert _nothing(await her.says("u6", "u5r", 25))
    # something down, and back
    her.down = ["browser"]
    assert "\ndown now: browser" in await her.says("u7", "u6r", 30)
    her.down = []
    assert _whole(await her.says("u8", "u7r", 35))
    # an unfreeze
    async def unfrozen(store, cfg, tz):
        return "unfrozen at 09:39"
    monkeypatch.setattr(mind.awareness, "unfrozen_line", unfrozen)
    assert "\nunfrozen at 09:39" in await her.says("u9", "u8r", 40)
    monkeypatch.undo()
    assert _nothing(await her.says("u10", "u9r", 45))
    # another conversation has its own last block
    other = _Her(store, cfg, await conversations.create(store))
    other.model = "m2"
    assert _whole(await other.says("o1", None, 50))


async def test_a_pause_and_a_step_of_the_absence_make_the_block_whole(store):
    cfg = load()
    her = _Her(store, cfg, await conversations.create(store))
    assert _whole(await her.says("u1", None, 0))
    # nine hours later: a pause here is always news, and the absence is one step
    later = await her.says("u2", "u1r", 9 * 60)
    assert _whole(later) and "last message here 9 hours ago" in later
    # minutes after: the absence is back to no step, so the block is whole once more
    back = await her.says("u3", "u2r", 9 * 60 + 5)
    assert _whole(back) and "last message here" not in back
    assert _nothing(await her.says("u4", "u3r", 9 * 60 + 10))


async def test_the_last_block_is_the_one_on_her_branch_she_still_sees(store):
    cfg = load()
    cid = await conversations.create(store)
    her = _Her(store, cfg, cid)
    await _feel(store, "happy", -10, "a good morning")
    assert _whole(await her.says("u1", None, 0))
    await _feel(store, "angry", 3, "he forgot")
    assert "angry since" in await her.says("u2", "u1r", 5)
    # an edit of u2: a sibling under u1's reply, whose branch last carried "happy"
    edited = await her.says("u2b", "u1r", 10)
    assert _whole(edited) and "angry since" in edited
    assert _nothing(await her.says("u3", "u2br", 15))
    # a compaction through u3's reply: the last whole block is in the summary now
    await store.write(lambda c: c.execute(
        "INSERT INTO compactions(id, conversation_id, through_message_id, trigger, "
        "covers_messages, created_at) VALUES('k', ?, 'u3r', 'auto', 6, ?)",
        (cid, T0.isoformat())))
    assert _whole(await her.says("u4", "u3r", 20))
    assert _nothing(await her.says("u5", "u4r", 25))


# ---- always-loaded notes over 1.5 KB -------------------------------------------------------

def _carried(got, path: str) -> str:
    return next(n["text"] for n in got.notes if n["path"] == path)


async def test_an_always_note_over_the_limit_carries_its_head_and_marked_lines(
        store, tmp_path):
    cfg = notes_cfg(tmp_path)
    head = "load: always\nkeys: jobs\n\n"
    whole = head + "x" * (loads.ALWAYS_MAX - len(head))           # exactly at the limit
    marked = head + "!! the interview is on friday\n" + "filler line\n" * 200 + \
        "- !! never call before 10\n"
    plain = "a line of the note\n" * 200
    await write_notes(store, cfg, {"jobs.md": whole, "people/sam-jobs.md": marked,
                                   "plain.md": "load: always\n\n" + plain,
                                   "samples.md": "load: always\n\n" + plain})
    cid = await conversations.create(store)
    got = await select_turn(store, cfg, None, cid, 1)
    assert len(whole.strip().encode()) == loads.ALWAYS_MAX
    assert _carried(got, "jobs.md") == whole.strip(), "at the limit: whole"
    assert _carried(got, "people/sam-jobs.md") == (
        "load: always\nkeys: jobs\n!! the interview is on friday\n- !! never call before 10"
        '\n\nrest via notebook__read(path="people/sam-jobs.md")')
    first = _carried(got, "plain.md")
    assert first.endswith('\n\nrest via notebook__read(path="plain.md")')
    assert first.startswith("load: always\n\na line of the note\n")
    assert len(first.split("\n\nrest via")[0].encode()) <= loads.ALWAYS_MAX
    assert _carried(got, "samples.md").endswith("a line of the note"), "its voice whole"

    await write_notes(store, cfg, {"jobs.md": whole + "y"})        # one byte over
    got = await select_turn(store, cfg, None, cid, 2)
    assert _carried(got, "jobs.md") == \
        'load: always\nkeys: jobs\n\nrest via notebook__read(path="jobs.md")'


# ---- every tool result with its tool's name ------------------------------------------------

def _openai(kind: str):
    return build(BackendConfig(id="x", kind=kind, label="x", base_url="http://x",
                               model="m", raw={}))


async def test_every_tool_result_reaches_the_model_with_its_tool_name(store, monkeypatch):
    cfg, b, _, cid = await thought_and_called(store, monkeypatch)
    built = await context.build(store, cfg, b, cid, system="", tools=[])
    call_id = next(m for m in built.messages if m.get("tool_calls"))["tool_calls"][0]["id"]
    msgs = _openai("llama-server").request_body(built.messages, [])["messages"]
    result = next(m for m in msgs if m["role"] == "tool")
    assert (result["name"], result["tool_call_id"]) == ("t__echo", call_id)
    ollama = OllamaBackend(BackendConfig(id="o", kind="ollama", label="o",
                                         base_url="http://o", model="m", raw={}))
    assert [m["tool_name"] for m in ollama.request_body(built.messages, [])["messages"]
            if m["role"] == "tool"] == ["t__echo"]
    # Anthropic ties a result to its call by id, and the call carries the name
    blocks = [x for m in b.request_body(built.messages, [])["messages"]
              for x in m["content"]]
    uses = [(x["id"], x["name"]) for x in blocks if x.get("type") == "tool_use"]
    results = [x["tool_use_id"] for x in blocks if x.get("type") == "tool_result"]
    assert [n for i, n in uses if i in results] == ["t__echo"]
