"""An aged call's long string arguments go shortened on the wire (compact.short_arguments):
its first line and a mark that names the call and how to read it back; the JSON stays
valid and the stored call never changes."""
from __future__ import annotations

import json

import pytest

from chat_fixtures import user_says
from fake_backend import FakeBackend

from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.skills import ToolResult
from celine.turns import compactions, context
from celine.turns import store as turns

pytestmark = pytest.mark.anyio

SCRIPT = "cat > /tmp/build.sh <<'EOF'\n" + "".join(
    f"echo step {i}: compile the module number {i} with its flags\n" for i in range(200)) + "EOF"


async def _heredoc_call(store, cid) -> tuple[str, str]:
    await user_says(store, cid, "write the build script")
    turn = await turns.claim(store, "w")
    tid = turn["id"]
    mid = await tree.start_assistant_message(store, cid, turn["root_message_id"], tid)
    await tree.finish_assistant_message(store, cid, mid, "")
    call = {"id": f"{tid}:0:0", "name": "shell__run",
            "arguments": json.dumps({"command": SCRIPT, "tab": "main"}),
            "mutating": True, "mode": "blocking"}
    await turns.record_tool_calls(store, cid, mid, [call])
    await turns.finish_tool_call(store, cid, mid, call["id"], "shell__run",
                                 ToolResult(content="$ done"), "ok", None)
    a = await tree.start_assistant_message(store, cid, await tree.active_leaf(store, cid), tid)
    await tree.finish_assistant_message(store, cid, a, "Written.")
    await turns.transition(store, tid, cid, "done")
    return call["id"], mid


def _sent(built, call_id) -> str:
    return next(t["function"]["arguments"] for m in built.messages
                for t in m.get("tool_calls") or () if t["id"] == call_id)


async def test_an_aged_heredoc_call_goes_short_and_valid(store):
    cfg = load()
    cid = await conversations.create(store)
    call_id, mid = await _heredoc_call(store, cid)
    fresh = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[],
                                mode="dry")
    assert json.loads(_sent(fresh, call_id))["command"] == SCRIPT, "verbatim until aged"

    await compactions.set_aged_through(store, cid, mid, {"results": 1})
    aged = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[],
                               mode="dry")
    args = json.loads(_sent(aged, call_id))
    assert args["tab"] == "main", "a short value stays"
    assert args["command"].startswith("cat > /tmp/build.sh <<'EOF' … [202 lines, about ")
    assert args["command"].endswith(
        f"tokens, shortened; the call is kept: {call_id}; read it back with "
        f"past_conversations__read_tool_call(id=\"{call_id}\")]")
    assert len(_sent(aged, call_id)) < 400
    again = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[],
                                mode="dry")
    assert _sent(again, call_id) == _sent(aged, call_id), "byte-identical every build"
    stored = await store.read(lambda c: c.execute(
        "SELECT arguments FROM tool_calls WHERE id=?", (call_id,)).fetchone()[0])
    assert json.loads(stored)["command"] == SCRIPT, "the row never changes"
