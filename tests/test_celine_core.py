"""celine-core: terminal states settle their tool calls, a side effect is never re-run,
Continue picks a turn up where it stopped. Fakes only."""
from __future__ import annotations

from chat_fixtures import user_says

import json

import pytest

from celine.conversations import store as conversations, tree, view
from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.core.bus import EventBus
from celine.skills import Registry, ToolResult, tool
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg():
    c = load()
    c.settings.general.update({"tool_timeout_seconds": 10,
                 "turn_wall_clock_seconds": 30, "max_concurrent_turns": 1})
    return c


class Arch:
    name = "arch"
    description = "test tools"

    def __init__(self):
        self.find_runs = self.write_runs = 0

    @tool()
    async def find(self, ctx) -> ToolResult:
        """Finds."""
        self.find_runs += 1
        return ToolResult(content="FIND RESULT")

    @tool(read_only=False)
    async def write(self, ctx) -> ToolResult:
        """Writes."""
        self.write_runs += 1
        return ToolResult(content="WROTE")


class Scripted(FakeBackend):
    """Calls `tools` in order, one per reply, then answers `answer`."""

    def __init__(self, tools=(), answer="the answer", *, prefill=None, finish="stop"):
        super().__init__()
        self.tools, self.answer, self.finish = list(tools), answer, finish
        if prefill is not None:
            self.supports_prefill = prefill

    async def stream(self, messages, tools):
        self.calls.append(messages)
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"),
                        default=-1)
        seen = sum(1 for m in messages[last_user + 1:] if m.get("role") == "tool")
        if seen < len(self.tools):
            yield ToolCallDelta(index=0, id=f"w{seen}", name=self.tools[seen], arguments="{}")
            yield Done(reason="tool_calls")
        else:
            yield TextDelta(self.answer)
            yield Done(reason=self.finish)


def _reg(arch):
    reg = Registry()
    reg.add(arch)
    return reg


async def _turn(store, tid):
    return await store.read(lambda c: dict(c.execute(
        "SELECT * FROM turns WHERE id=?", (tid,)).fetchone()))


async def _calls(store, tid):
    return await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT tc.* FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
        "WHERE m.turn_id=? ORDER BY tc.rowid", (tid,))])


async def _stranded(store, name="arch__find", mutating=False):
    """A turn stopped with one call in flight, as the runner leaves it."""
    cid = await conversations.create(store)
    await user_says(store, cid, "go")
    turn = await turns.claim(store, "w1")
    tid = turn["id"]
    await turns.transition(store, tid, cid, "streaming")
    mid = await tree.start_assistant_message(store, cid, turn["root_message_id"], tid)
    await tree.finish_assistant_message(store, cid, mid, "")
    await turns.record_tool_calls(store, cid, mid, [{
        "id": f"{tid}:0:0", "name": name, "arguments": "{}",
        "mutating": mutating, "mode": "blocking"}])
    await turns.transition(store, tid, cid, "tools")
    return cid, tid, mid


@pytest.mark.parametrize("how", ["aborted", "failed", "sweep"])
async def test_terminal_transition_settles_open_calls(store, how):
    cid, tid, mid = await _stranded(store)
    if how == "aborted":
        await turns.request_abort(store, cid)
        await turns.transition(store, tid, cid, "streaming")       # the runner's next write
    elif how == "failed":
        await turns.fail_if_active(store, tid, cid, "exceeded turn_wall_clock_seconds (30s)",
                                   "wall_clock")
    else:
        await store.write(lambda c: c.execute(      # a hard crash, no registry
            "UPDATE tool_calls SET mutating=NULL, mode=NULL"))
        assert (await turns.sweep(store))["interrupted"] == 1
    calls = await _calls(store, tid)
    assert [c["state"] for c in calls] == ["interrupted"]
    assert calls[0]["result_message_id"], "a result row, so the wire and UI have one"
    assert (await _turn(store, tid))["reason"], "the turn says why it ended"
    snap = await view.snapshot(store, cid)
    assert snap["tool_calls"][mid][0]["state"] == "interrupted"
    assert snap["turn"]["continuable"]["ok"] is True


async def test_sweep_interrupts_every_running_turn_with_its_reason(store):
    """Nothing runs again on its own after a restart: a read-only step's turn ends
    `interrupted` as a side effect's does, each with the sentence the UI shows."""
    _, tid_ro, _ = await _stranded(store, "arch__find", mutating=False)
    _, tid_mut, _ = await _stranded(store, "arch__write", mutating=True)
    out = await turns.sweep(store)
    assert out["interrupted"] == 2
    ro, mut = await _turn(store, tid_ro), await _turn(store, tid_mut)
    assert (ro["state"], ro["reason_code"]) == ("interrupted", "restart")
    assert ro["reason"] == "The server restarted during the answer."
    assert (await _calls(store, tid_ro))[0]["state"] == "interrupted"
    assert mut["state"] == "interrupted" and "arch__write" in mut["reason"]
    assert (await _calls(store, tid_mut))[0]["state"] == "interrupted"


async def test_continue_reruns_interrupted_read_only_call_same_id(store, cfg):
    arch = Arch()
    reg = _reg(arch)
    cid, tid, _ = await _stranded(store)
    await turns.sweep(store)        # no flags known: interrupted
    await store.write(lambda c: c.execute(
        "UPDATE turns SET state='interrupted' WHERE id=?", (tid,)))
    assert (await turns.continue_turn(store, cid, tid, reg))[0] == tid
    turn = await turns.claim(store, "w3")
    await run_turn(turn, store, cfg, Scripted(["arch__find"]), EventBus(),
                   Token(tid), reg, None)
    [call] = await _calls(store, tid)
    assert call["id"] == f"{tid}:0:0" and call["state"] == "ok" and arch.find_runs == 1
    snap = await view.snapshot(store, cid)
    assert snap["messages"][-1]["content"] == "the answer"
    assert snap["turn"]["state"] == "done"


async def test_continue_never_reruns_an_interrupted_side_effect(store, cfg):
    arch = Arch()
    reg = _reg(arch)
    cid, tid, _ = await _stranded(store, "arch__write", mutating=True)
    await turns.fail_if_active(store, tid, cid, "exceeded turn_wall_clock_seconds (30s)",
                               "wall_clock")
    await turns.continue_turn(store, cid, tid, reg)
    turn = await turns.claim(store, "w3")
    backend = Scripted(["arch__write"])
    await run_turn(turn, store, cfg, backend, EventBus(), Token(tid), reg, None)
    assert arch.write_runs == 0, "a side effect is never re-run silently"
    sent = [m["content"] for m in backend.calls[0] if m.get("role") == "tool"]
    assert sent and sent[0].startswith("interrupted:"), sent


async def test_runner_never_reruns_a_side_effect_it_finds_pending(store, cfg):
    """A put-down turn claimed before the startup sweep ran."""
    arch = Arch()
    reg = _reg(arch)
    _, tid, _ = await _stranded(store, "arch__write", mutating=True)
    await turns.put_down(store, ["w1"])
    turn = await turns.claim(store, "w9")
    await run_turn(turn, store, cfg, Scripted(["arch__write"]), EventBus(), Token(tid),
                   reg, None)
    assert arch.write_runs == 0
    assert (await _turn(store, tid))["state"] == "interrupted"
    assert (await _calls(store, tid))[0]["state"] == "interrupted"


async def test_continue_extends_a_cut_reply_in_place(store, cfg):
    cid = await conversations.create(store)
    await user_says(store, cid, "say hello world")
    turn = await turns.claim(store, "w1")
    tid = turn["id"]
    await run_turn(turn, store, cfg, Scripted(answer="Hello wor", finish="length"),
                   EventBus(), Token(tid), None, None)
    mid = (await view.snapshot(store, cid))["messages"][-1]["id"]
    await turns.continue_turn(store, cid, tid)
    turn = await turns.claim(store, "w2")
    backend = Scripted(answer="ld.", prefill=True)
    await run_turn(turn, store, cfg, backend, EventBus(), Token(tid), None, None)
    assert backend.calls[0][-1] == {"role": "assistant", "content": "Hello wor", "origin": [
        {"conversation": cid, "turn": tid, "message": mid}]}, "the reply it continues"
    msgs = (await view.snapshot(store, cid))["messages"]
    assert msgs[-1]["id"] == mid and msgs[-1]["content"] == "Hello world."
    assert [m["role"] for m in msgs].count("assistant") == 1
    rows = await store.read(lambda c: c.execute(
        "SELECT data FROM events WHERE conversation_id=? AND type='message.delta'",
        (cid,)).fetchall())
    assert {json.loads(r["data"])["message_id"] for r in rows} == {mid}


async def test_continue_after_a_prune_carries_on_from_the_rows_partial_text(store, cfg):
    cid = await conversations.create(store)
    umid, tid, _ = await user_says(store, cid, "say hello world")
    await turns.claim(store, "w1")
    # the process died mid-stream: deltas went out, the reply never finished
    mid = await tree.start_assistant_message(store, cid, umid, tid)
    await tree.append_delta(store, cid, mid, "Hello ", "Hello ")
    await tree.append_delta(store, cid, mid, "wor", "Hello wor")
    await turns.transition(store, tid, cid, "interrupted", reason_code="crash")
    assert await conversations.prune_events(store, keep=0, cid=cid)
    await turns.continue_turn(store, cid, tid)
    turn = await turns.claim(store, "w2")
    backend = Scripted(answer="ld.", prefill=True)
    await run_turn(turn, store, cfg, backend, EventBus(), Token(tid), None, None)
    assert backend.calls[0][-1] == {"role": "assistant", "content": "Hello wor", "origin": [
        {"conversation": cid, "turn": tid, "message": mid}]}, "the reply it continues"
    msgs = (await view.snapshot(store, cid))["messages"]
    assert msgs[-1]["id"] == mid and msgs[-1]["content"] == "Hello world."


class Busy(FakeBackend):
    async def stream(self, messages, tools):
        from celine.backends.base import BackendError
        e = BackendError("main returned 429.")
        e.status, e.code = 429, "busy"
        raise e
        yield


async def test_keep_waiting_continues_a_busy_turn_marked_to_wait(store, cfg):
    cid = await conversations.create(store)
    await user_says(store, cid, "hi")
    turn = await turns.claim(store, "w1")
    tid = turn["id"]
    await run_turn(turn, store, cfg, Busy(), EventBus(), Token(tid), None, None)
    t = await _turn(store, tid)
    assert (t["state"], t["reason_code"]) == ("failed", "busy")
    await turns.continue_turn(store, cid, tid, wait=True)
    assert (await turns.claim(store, "w2"))["reason"] == "keep_waiting"
