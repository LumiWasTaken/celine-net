"""Terminal rows and the commands union: its calls and the user's lines, newest first;
a trashed terminal is restorable, never deleted."""
from __future__ import annotations

from chat_fixtures import user_says

import json

import pytest

from celine.conversations import store as conversations, tree
from celine.missions import store as missions
from celine.skills import ToolResult
from celine.terminals import store as terminals
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


async def test_a_terminal_ends_once_and_trash_keeps_it(store):
    await terminals.upsert(store, "ab12cd", "build", "celine", "2026-10-02T01:00:00.000+00:00")
    await terminals.upsert(store, "ab12cd", "other", "sam", "2026-10-02T02:00:00.000+00:00")
    assert (await terminals.get(store, "ab12cd"))["name"] == "build", "a known id stays"
    await terminals.end(store, "ab12cd", "exit", 130)
    await terminals.end(store, "ab12cd", "user", None)
    row = await terminals.get(store, "ab12cd")
    assert row["ended_by"] == "exit" and row["exit"] == 130
    assert await terminals.open_by_name(store, "build") is None
    await terminals.upsert(store, "ef34gh", "build", "celine", "2026-10-02T03:00:00.000+00:00")
    assert (await terminals.open_by_name(store, "build"))["id"] == "ef34gh"
    assert await terminals.trash_ended(store) == 1
    assert [t["id"] for t in await terminals.list_terminals(store)] == ["ef34gh"]
    assert [t["id"] for t in await terminals.list_terminals(store, trashed=True)] == ["ab12cd"]
    assert not await terminals.set_trashed(store, "ef34gh", True), "an open one stays"
    assert await terminals.set_trashed(store, "ab12cd", False)
    assert [t["id"] for t in await terminals.list_terminals(store)] == ["ef34gh", "ab12cd"]


async def test_commands_join_her_calls_and_the_users_lines(store):
    cid = await conversations.create(store, "Build llama.cpp")
    await terminals.upsert(store, "ab12cd", "build", "celine", "2026-10-02T01:00:00.000+00:00")
    _, tid, _ = await user_says(store, cid, "go")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [{"id": "c0", "name": "shell__run", "arguments":
                                              json.dumps({"terminal": "build", "command": "make"})}])
    display = {"terminal": "build", "terminal_id": "ab12cd", "state": "done", "exit": 0, "head": 1}
    await turns.finish_tool_call(store, cid, mid, "c0", "shell__run", ToolResult(
        content="terminal build ~: exit 0 after 0.5 s. Log: /var/lib/shellbox/cmd/k3x9ab.log\nok",
        display=display), "ok")
    k = await terminals.add_command(store, "ab12cd", "line", "echo hi")
    await store.write(lambda c: c.execute("UPDATE terminal_commands SET created_at=? WHERE id=?",
                                          ("2999-01-01T00:00:00.000+00:00", k)))
    rows, total = await terminals.commands(store, terminal_id="ab12cd")
    assert total == 2 and [r["id"] for r in rows] == [k, "c0"]
    assert rows[0]["from"]["kind"] == "you" and rows[1]["outcome"]["exit"] == 0
    assert rows[1]["result"] == "ok" and rows[1]["text"] == "make", "its status line is not shown"
    assert rows[1]["from"] == {"kind": "conversation", "conversation_id": cid,
                               "title": "Build llama.cpp"}
    assert await terminals.last_conversation(store, "ab12cd") == {
        "id": cid, "title": "Build llama.cpp", "agent_of": None, "kind": "conversation"}
    await missions.create(store, cid, {"name": "Build", "terminal": "build", "brief": "make it"})
    assert (await terminals.last_conversation(store, "ab12cd"))["kind"] == "mission", \
        "a terminal says which kind of conversation used it last"
    rows, _ = await terminals.commands(store, live={"ab12cd": {"busy": False, "program": None,
                                                               "last_exit": 2}})
    assert (rows[0]["outcome"]["state"], rows[0]["outcome"]["exit"]) == ("done", 2), \
        "a typed line settles like hers"


async def test_default_terminal_for_a_mission_a_sub_agent_and_a_plain_conversation(store):
    mission = await conversations.create(store, "RSS feed")
    await missions.create(store, mission, {"name": "RSS feed", "terminal": "rss-feed-2",
                                           "brief": "a feed"})
    agent = await conversations.create(store, "x", agent_of=mission, agent_name="Site Scout")
    plain = await conversations.create(store, "Plain")
    other = await conversations.create(store, "Other")
    assert await terminals.default_terminal(store, mission) == "rss-feed-2"
    assert await terminals.default_terminal(store, agent) == "site-scout-" + agent[:4]
    twins = [await conversations.create(store, "x", agent_of=mission, agent_name=n)
             for n in ("a long name for a scout", "a long name for a scout too")]
    a, b = [await terminals.default_terminal(store, t) for t in twins]
    assert a != b and len(a) == len(b) == 16, "unique after the cut"
    assert await terminals.default_terminal(store, plain) == "c-" + plain[:6]
    assert await terminals.default_terminal(store, other) != \
        await terminals.default_terminal(store, plain), "plain conversations never share one"


async def test_a_call_that_returned_while_its_program_ran_is_settled(store):
    cid = await conversations.create(store, "Build")
    await terminals.upsert(store, "ab12cd", "build", "celine", "2026-10-02T01:00:00.000+00:00")
    _, tid, _ = await user_says(store, cid, "go")
    mid = await tree.start_assistant_message(store, cid, None, tid)

    async def call(id: str, text: str, display: dict) -> None:
        await turns.record_tool_calls(store, cid, mid, [{"id": id, "name": "shell__run",
                                      "arguments": json.dumps({"terminal": "build", "command": text})}])
        await turns.finish_tool_call(store, cid, mid, id, "shell__run",
                                     ToolResult(content="r", display=display), "ok")

    quiet = {"terminal_id": "ab12cd", "state": "waiting", "quiet_for": 3, "program": "sleep"}
    await call("c0", "sleep 9; false", quiet)
    rows, _ = await terminals.commands(store, live={"ab12cd": {"busy": True, "program": "sleep"}})
    assert rows[0]["outcome"]["state"] == "running" and rows[0]["outcome"]["quiet_for"] is None
    rows, _ = await terminals.commands(store, live={"ab12cd": {"busy": False, "program": None,
                                                               "last_exit": 1}})
    assert (rows[0]["outcome"]["state"], rows[0]["outcome"]["exit"]) == ("done", 1)
    await call("c1", "", {"terminal_id": "ab12cd", "state": "done", "exit": 1})
    rows, _ = await terminals.commands(store)
    assert [(r["outcome"]["state"], r["outcome"]["exit"]) for r in rows] == \
        [("done", 1), ("done", 1)], "the watch that came back done carries the exit"
