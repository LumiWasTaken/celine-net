"""The turn loop: concurrent tool calls, every call runs, the wall clock, Stop, and the
citation check. Fake backend, tiny synthetic archive."""
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
    cid = await conversations.create(store)
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
    assert snap["turn"]["state"] == "done"


async def test_a_citation_check_that_breaks_tells_the_user_and_keeps_the_text(
        store, cfg, archive, monkeypatch):
    from celine.turns import verify

    def broken(*a, **kw):
        raise RuntimeError("index gone")
    monkeypatch.setattr(verify, "check", broken)
    b = Script([
        [_find(), Done(reason="tool_calls")],
        [TextDelta("Alex said it [[whatsapp:1]]."), Done(reason="stop")],
    ])
    snap, events, final = await _turn(store, cfg, archive, b)
    assert final["content"] == "Alex said it [[whatsapp:1]].", "no text rewrite"
    [err] = [e["data"] for e in events if e["type"] == "error"]
    assert err["code"] == "citation_check_failed" and "index gone" in err["message"]
    assert snap["turn"]["state"] == "done"


def test_a_tool_name_written_as_a_citation_is_marked_invalid():
    from celine.turns import verify
    text = "BAD BOY [[chat_archive__look_up: person discord:265 BAD BOY]] said hi [[discord:7]]."
    assert verify.mark_tool_citations(text) == \
        "BAD BOY [[chat_archive__look_up|invalid]] said hi [[discord:7]]."


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


async def test_the_same_call_with_the_same_result_runs_every_time(store, cfg):
    """A call she repeats runs again, with tools offered on every step."""
    t = T()
    b = Script([_call(i, "t__same", {"q": "pizza"}) for i in range(4)]
               + [[TextDelta("Nothing found."), Done(reason="stop")]])
    _, _, final = await _turn(store, cfg, _reg(t), b)
    assert t.runs == 4 and await _tool_results(store) == ["nothing found"] * 4
    assert b.tools == [True] * 5 and final["content"] == "Nothing found."


async def test_a_malformed_argument_fails_with_its_reason(store, cfg):
    """A value of the wrong type is not converted: the call fails with the schema's
    reason and the tool never runs."""
    t = T()
    b = Script([_call(0, "t__same", {"q": 5}), [TextDelta("k"), Done(reason="stop")]])
    await _turn(store, cfg, _reg(t), b)
    assert t.runs == 0
    assert await _tool_results(store) == ["invalid arguments: q is a string"]
    state = await store.read(lambda c: c.execute("SELECT state FROM tool_calls").fetchone()[0])
    assert state == "error"


async def test_identical_calls_of_one_step_each_run(store, cfg):
    t = T()
    calls = [ToolCallDelta(index=i, id=f"s{i}", name="t__same", arguments='{"q": "pizza"}')
             for i in range(2)] + [Done(reason="tool_calls")]
    await _turn(store, cfg, _reg(t), Script([calls, [TextDelta("k"), Done(reason="stop")]]))
    assert t.runs == 2
    assert (await _tool_results(store))[-2:] == ["nothing found", "nothing found"]


async def test_when_her_time_is_up_the_turn_ends_capped(store, cfg):
    """A turn that hits the wall clock mid-search ends with its reason: no hidden note,
    no extra step."""
    cfg.settings.general.update({"turn_wall_clock_seconds": 0.4, "last_step_seconds": 5})
    t = T()
    b = Script([_call(0, "t__hang", {}), [TextDelta("never asked"), Done(reason="stop")]])
    t0 = anyio.current_time()
    snap, _, _ = await _turn(store, cfg, _reg(t), b)
    assert anyio.current_time() - t0 < 2 and t.runs == 0
    assert len(b.calls) == 1, "no step after the time is up"
    assert (snap["turn"]["state"], snap["turn"]["reason_code"]) == ("capped", "wall_clock")
