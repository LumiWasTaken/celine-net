"""The turn loop: concurrent tool calls, the repeat guard and the ledger it seeds after a
restart, the wall clock, Stop, and the citation check. Fake backend, tiny
synthetic archive."""
from __future__ import annotations

from chat_fixtures import Script, user_says

import json

import anyio
import pytest

from celine.conversations import store as conversations, view
from celine.backends.base import Done, TextDelta, ToolCallDelta
from celine.boot import load
from celine.core.bus import EventBus
from celine.skills import Registry, ToolResult, tool
from celine.turns import ledger
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns
from kb_fixtures import WordReranker, build

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg():
    c = load()
    c.skills = {**c.skills, "chat_archive": {**c.skills.get("chat_archive", {}),
                                              "enabled": True}, "t": {"enabled": True}}
    return c


@pytest.fixture
def archive(cfg, tmp_path, monkeypatch):
    from celine.core.services import Services
    from celine.kb.retrieve import rerank
    from celine.skills.archive import ChatArchive
    monkeypatch.setattr(rerank, "from_config", lambda cfg: WordReranker())
    monkeypatch.setattr(ChatArchive, "_embedder", lambda self: (None, "", None))
    app = Services(cfg=cfg, kbs=build(tmp_path / "kb"))
    reg = Registry(app=app)
    skill = ChatArchive(cfg)
    skill.app = app
    reg.add(skill)
    return reg


def _find(index=0, queries=("celine",)):
    return ToolCallDelta(index=index, id=f"w{index}", name="chat_archive__search",
                         arguments=json.dumps({"about": list(queries), "words": list(queries),
                                               "scope": "all"}))


def _call(i, name, args):
    return [ToolCallDelta(index=0, id=f"r{i}", name=name, arguments=json.dumps(args)),
            Done(reason="tool_calls")]


async def _turn(store, cfg, registry, backend):
    from celine.turns import loads
    cid = await conversations.create(store)
    await loads.touch(store, cid, "skill", list(registry.skills))   # loaded before
    await user_says(store, cid, "when did we talk about the celine?")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), registry)
    snap = await view.snapshot(store, cid)
    events = (await view.resume(store, cid, 0))["events"]
    final = [m for m in snap["messages"] if m["role"] == "assistant"][-1]
    return snap, events, final


async def _tool_results(store) -> list[str]:
    return await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT r.content FROM tool_calls tc JOIN messages r ON r.id = tc.result_message_id "
        "ORDER BY tc.rowid")])


class T:
    """`a` and `b` only finish if they run at the same time, `w` mutates; `same` always
    answers the same; `wait` sleeps."""
    name = "t"
    description = "test"

    def __init__(self):
        self.ev = anyio.Event()
        self.runs = 0

    @tool()
    async def a(self, ctx) -> ToolResult:
        """a"""
        with anyio.fail_after(2):
            await self.ev.wait()
        return ToolResult(content="a done")

    @tool()
    async def b(self, ctx) -> ToolResult:
        """b"""
        self.ev.set()
        return ToolResult(content="b done")

    @tool(read_only=False)
    async def w(self, ctx) -> ToolResult:
        """w"""
        with anyio.fail_after(0.3):
            await self.ev.wait()
        return ToolResult(content="w done")

    @tool()
    async def same(self, ctx, q: str) -> ToolResult:
        """same"""
        self.runs += 1
        return ToolResult(content="nothing found")

    @tool()
    async def hang(self, ctx) -> ToolResult:
        """hang"""
        await anyio.sleep_forever()          # until its turn's wall clock cuts it
        self.runs += 1
        return ToolResult(content="waited")


def _reg(skill) -> Registry:
    reg = Registry()
    reg.add(skill)
    return reg


async def test_a_bad_citation_is_marked_invalid(store, cfg, archive):
    b = Script([
        [_find(), Done(reason="tool_calls")],
        [TextDelta('Alex said "the celine is broken" [[whatsapp:1]], also [[whatsapp:4]].'),
         Done(reason="stop")],
    ])
    snap, events, final = await _turn(store, cfg, archive, b)
    assert len(b.calls) == 2, "no second generation"
    assert final["content"] == ('Alex said "the celine is broken" [[whatsapp:1|invalid]], '
                                'also [[whatsapp:4|invalid]].')
    checked = [e["data"] for e in events if e["type"] == "citations.checked"]
    assert {f["reason"] for f in checked[0]["invalid"]} == {"quote", "not_shown"}
    rewritten = [e["data"] for e in events if e["type"] == "message.rewritten"]
    assert [r["text"] for r in rewritten] == [final["content"]], "the marks, once"
    assert snap["turn"]["state"] == "done" and ledger._open == {}


def test_a_tool_name_written_as_a_citation_is_marked_invalid():
    from celine.turns import verify
    text = "BAD BOY [[chat_archive__look_up: person discord:265 BAD BOY]] said hi [[discord:7]]."
    assert verify.mark_tool_citations(text) == \
        "BAD BOY [[chat_archive__look_up|invalid]] said hi [[discord:7]]."


def test_the_ledger_seeds_from_stored_calls_after_a_restart():
    led = ledger.Ledger("t")
    led.seed([("chat_archive__search", '{"queries": ["a"]}', "> [whatsapp:1] x", "ok"),
              ("chat_archive__search", '{"queries": ["a"]}', "same as call 1.", "ok"),
              ("chat_archive__read_session", '{"ref": "whatsapp:1"}',
               "[whatsapp:1] [whatsapp:2]", "ok")])
    assert led.searches == 1 and len(led.calls) == 2, "a repeat is not a call"
    assert led.shown == {"whatsapp:1": 1, "whatsapp:2": 2}
    assert led.same_as(led.spec("search", {"queries": [" A "]})).n == 1
    again = ledger.Ledger("t")
    again.seed([("chat_archive__search", '{"scope": ["all"]}',
                 f"{ledger.BLOCKED}: 6 searches in a row found nothing new", "error"),
                ("chat_archive__search", '{"scope": ["whatsapp:1"]}',
                 "Not run: you made this exact call 2 times in this turn", "error")])
    assert again.searches == 0 and again.dry_streak() == (1, True)
    assert len(again.calls) == 1, "a call refused before the tool never reached the ledger"


async def test_only_non_mutating_calls_of_one_reply_run_concurrently(store, cfg):
    for first in ("a", "w"):
        calls = [ToolCallDelta(index=0, id=f"{first}0", name=f"t__{first}", arguments="{}"),
                 ToolCallDelta(index=1, id=f"{first}1", name="t__b", arguments="{}"),
                 Done(reason="tool_calls")]
        await _turn(store, cfg, _reg(T()),
                    Script([calls, [TextDelta("k"), Done(reason="stop")]]))
        mine, b = (await _tool_results(store))[-2:]
        assert b == "b done"
        assert mine == "a done" if first == "a" else "w done" not in mine, "one after another"


async def test_the_same_call_with_the_same_result_is_not_run_a_third_time(store, cfg):
    """A model that repeats one call with the same result is stopped."""
    t = T()
    b = Script([_call(i, "t__same", {"q": "Pizza"} if i % 2 else {"q": " pizza "})
                for i in range(3)] + [[TextDelta("Nothing found."), Done(reason="stop")]])
    _, _, final = await _turn(store, cfg, _reg(t), b)
    assert t.runs == 2 and (await _tool_results(store))[2].startswith("Not run:")
    assert b.tools == [True, True, True, False] and final["content"] == "Nothing found."


async def test_identical_calls_of_one_step_run_once(store, cfg):
    t = T()
    calls = [ToolCallDelta(index=i, id=f"s{i}", name="t__same", arguments='{"q": "pizza"}')
             for i in range(2)] + [Done(reason="tool_calls")]
    await _turn(store, cfg, _reg(t), Script([calls, [TextDelta("k"), Done(reason="stop")]]))
    assert t.runs == 1
    assert (await _tool_results(store))[-2:] == ["nothing found",
                                                 "same as the call above in this step."]


async def test_when_her_time_is_up_she_answers_in_a_last_step_without_tools(store, cfg):
    """A turn that hits the wall clock mid-search still answers."""
    cfg.settings.general.update({"turn_wall_clock_seconds": 0.4, "last_step_seconds": 5})
    t = T()
    b = Script([_call(0, "t__hang", {}),
                [TextDelta("Here is what I found so far."), Done(reason="stop")]])
    t0 = anyio.current_time()
    snap, _, final = await _turn(store, cfg, _reg(t), b)
    assert anyio.current_time() - t0 < 2 and t.runs == 0
    assert b.tools == [True, False] and final["content"] == "Here is what I found so far."
    assert (snap["turn"]["state"], snap["turn"]["reason_code"]) == ("done", "time_up")


async def test_a_stop_ends_the_last_step_too(store, cfg):
    cfg.settings.general.update({"turn_wall_clock_seconds": 0.2, "last_step_seconds": 5})

    class StopInLast(Script):
        async def stream(self, messages, tools):
            if not tools:                        # the last step: the user presses Stop
                await turns.request_abort(store, cid)
                token.cancel()
            async for ev in super().stream(messages, tools):
                yield ev
    b = StopInLast([_call(0, "t__hang", {}),
                    [TextDelta("too late"), Done(reason="stop")]])
    cid = await conversations.create(store)
    await user_says(store, cid, "go")
    turn = await turns.claim(store, "w")
    token = Token(turn["id"])
    await run_turn(turn, store, cfg, b, EventBus(), token, _reg(T()))
    snap = await view.snapshot(store, cid)
    assert b.tools == [True, False] and snap["turn"]["state"] == "aborted"
    assert "too late" not in [m["content"] for m in snap["messages"]]
