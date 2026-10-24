"""The past_conversations skill."""
from __future__ import annotations

from chat_fixtures import user_says

import pytest

from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.skills import ToolContext
from celine.skills.conversations import PastConversations
from celine.turns import store as turns


async def _day(store, cid: str) -> str:
    """The UTC day the store stamped a conversation's last row with (the store keeps the
    wall clock), so a test that runs across midnight still asks for the right day."""
    def read(c):
        row = c.execute("SELECT max(created_at) FROM messages WHERE conversation_id = ?",
                        (cid,)).fetchone()
        return row[0] or c.execute("SELECT created_at FROM conversations WHERE id = ?",
                                   (cid,)).fetchone()[0]
    return (await store.read(read))[:10]

pytestmark = pytest.mark.anyio


async def test_search_skips_the_current_thread_and_hidden_branches(store):
    a = await conversations.create(store)
    b = await conversations.create(store)
    await user_says(store, a, "the pelican ate my sandwich")
    first, tid, _ = await user_says(store, b, "pelican sighting at the harbour")
    await turns.transition(store, tid, b, "done")          # the tree is edited once it is done
    await tree.edit(store, b, first, "gull sighting at the harbour")
    assert [h["conversation_id"] for h in await conversations.search(store, "pelican", exclude=b)] \
        == [a]

    async def search(cid):
        ctx = ToolContext(conversation_id=cid, turn_id="t", tool_call_id="x", store=store,
                          config=None, emit=None)
        return (await PastConversations(load()).search(ctx, "pelican")).content
    assert "sandwich" in await search(b)
    assert "sandwich" not in await search(a)


async def test_a_day_is_an_index_a_read_is_a_slice_and_a_tool_call_reads_raw(store):
    cfg = load()
    cfg.timezone = "UTC"
    skill = PastConversations(cfg)
    a = await conversations.create(store, "Groceries")
    here = await conversations.create(store, "The review")

    def chat(c):
        m = tree.append(c, store, a, None, "user", "buy milk", "user")
        r = tree.append(c, store, a, m["id"], "assistant", "", "model")
        for cid_, name, state, out in (("t1", "web__search", "ok", "milk is 1 euro"),
                                       ("t2", "web__read_page", "error", "403 at the shop")):
            res = tree.append(c, store, a, r["id"], "tool", out, "model")
            c.execute("INSERT INTO tool_calls(id, message_id, result_message_id, name, "
                      "arguments, state) VALUES(?,?,?,?,?,?)",
                      (cid_, r["id"], res["id"], name, '{"query": "milk"}', state))
        tree.append(c, store, a, res["id"], "assistant", "Milk is on the list.", "model")
    await store.write(chat)
    ctx = ToolContext(conversation_id=here, turn_id="t", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    today = await _day(store, a)

    day = (await skill.list_day(ctx, today)).content
    assert "1 conversation\n" in day and a[:8] in day and here[:8] not in day
    assert "#1 to #3" in day and "2 tool calls (web__search, web__read_page)" in day
    assert "1 failed (web__read_page)" in day
    assert "first: User: buy milk" in day or f"first: {cfg.user}: buy milk" in day
    assert "last: you: Milk is on the list." in day

    names = (await skill.read(ctx, a[:8])).content
    assert "    call t1 web__search ok" in names and "milk is 1 euro" not in names
    full = (await skill.read(ctx, a[:8], start=2, end=2, tools="full")).content
    assert "result: milk is 1 euro" in full and "Milk is on the list" not in full
    assert f"earlier: past_conversations__read(conversation=\"{a[:8]}\", end=1)" in full
    assert f"later: past_conversations__read(conversation=\"{a[:8]}\", start=3)" in full

    raw = (await skill.read_tool_call(ctx, "t2")).content
    assert "web__read_page error" in raw and '"query": "milk"' in raw
    assert raw.endswith("403 at the shop")
    assert (await skill.read_tool_call(ctx, "nope")).is_error


async def test_read_and_search_give_times_in_the_apps_time_zone(store):
    cfg = load()
    cfg.timezone = "Europe/Berlin"
    skill = PastConversations(cfg)
    a = await conversations.create(store, "Late")
    here = await conversations.create(store)
    await user_says(store, a, "the owl calls at midnight")
    await store.write(lambda c: c.execute(
        "UPDATE messages SET created_at='2026-10-09T23:30:00.000+00:00' "
        "WHERE conversation_id=?", (a,)))
    ctx = ToolContext(conversation_id=here, turn_id="t", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    assert "#1 2026-10-10 01:30 " in (await skill.read(ctx, a[:8])).content
    assert "[2026-10-10]" in (await skill.search(ctx, "owl")).content


async def test_search_reads_punctuation_and_names_the_intercom(store):
    skill = PastConversations(load())
    a = await conversations.create(store)
    here = await conversations.create(store)
    await user_says(store, a, "the kid's well-known kite")
    await store.write(lambda c: c.execute(
        "UPDATE messages SET author='intercom' WHERE conversation_id=?", (a,)))
    ctx = ToolContext(conversation_id=here, turn_id="t", tool_call_id="x", store=store,
                      config=None, emit=None)
    out = (await skill.search(ctx, "kid's well-known kite?")).content
    assert "kite" in out and "the intercom" in out


async def test_a_trashed_conversation_is_out_of_search_list_day_and_read(store):
    cfg = load()
    cfg.timezone = "UTC"
    skill = PastConversations(cfg)
    a = await conversations.create(store, "Pelicans")
    here = await conversations.create(store, "The review")

    def chat(c):
        m = tree.append(c, store, a, None, "user", "the pelican ate my sandwich", "user")
        r = tree.append(c, store, a, m["id"], "assistant", "", "model")
        res = tree.append(c, store, a, r["id"], "tool", "a pelican", "model")
        c.execute("INSERT INTO tool_calls(id, message_id, result_message_id, name, "
                  "arguments, state) VALUES('t1', ?, ?, 'web__search', '{}', 'ok')",
                  (r["id"], res["id"]))
    await store.write(chat)
    today = await _day(store, a)
    assert await conversations.delete(store, a)
    ctx = ToolContext(conversation_id=here, turn_id="t", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    assert "sandwich" not in (await skill.search(ctx, "pelican")).content
    assert a[:8] not in (await skill.list_day(ctx, today)).content
    assert (await skill.read(ctx, a[:8])).is_error
    assert (await skill.read_tool_call(ctx, "t1")).is_error


async def test_a_dead_run_and_a_failed_chat_are_marked_a_draft_not_a_source(store):
    """A run's conversation names its run and how it ended; a conversation whose last
    turn failed reads as a draft, in list_day and in read alike."""
    cfg = load()
    cfg.timezone = "UTC"
    skill = PastConversations(cfg)
    here = await conversations.create(store, "The review")
    dead = await conversations.for_trigger(store, "run:r1", "World news, Thu 22 Oct")
    fine = await conversations.for_trigger(store, "run:r2", "World news, Fri 23 Oct")
    chat = await conversations.create(store, "Groceries")
    good = await conversations.create(store, "Plants")
    ends = {dead: "failed", fine: "done", chat: "failed", good: "done"}
    for cid, state in ends.items():
        first, tid, _ = await user_says(store, cid, "ten items, all checked")
        await store.write(lambda c: tree.append(c, store, cid, first, "assistant",
                                                "ten items", "model"))
        await turns.transition(store, tid, cid, state, "the model broke")
    mute = await conversations.for_trigger(store, "run:r3", "World news, Sat 24 Oct")
    _, tid, _ = await user_says(store, mute, "ten items, all checked")   # 502, no word
    await turns.transition(store, tid, mute, "failed", "the server returned 502")
    await store.write(lambda c: c.executemany(
        "INSERT INTO runs(id, schedule, label, slot, night, state, reason, created_at, "
        "conversation_id, error) VALUES (?, 'world', 'World news', ?, ?, ?, '', ?, ?, ?)", [
            ("r1", "2026-10-22T02:00:00Z", "2026-10-21", "failed", "2026-10-22T02:00:00Z",
             dead, "Ran over 3 hours"),
            ("r2", "2026-10-23T02:00:00Z", "2026-10-22", "done", "2026-10-23T02:00:00Z",
             fine, None),
            ("r3", "2026-10-24T02:00:00Z", "2026-10-23", "failed", "2026-10-24T02:00:00Z",
             mute, "the server returned 502")]))
    ctx = ToolContext(conversation_id=here, turn_id="t", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    words = "run World news, failed: Ran over 3 hours (a draft, not a source)"
    day = (await skill.list_day(ctx, await _day(store, dead))).content
    lines = {cid: next(x for x in day.splitlines() if cid[:8] in x) for cid in ends}
    assert lines[dead].endswith(words)
    assert lines[fine].endswith("run World news, done")
    assert lines[chat].endswith("last turn failed")
    assert "draft" not in lines[good]
    assert next(x for x in day.splitlines() if mute[:8] in x).endswith(
        "run World news, failed: the server returned 502")
    assert (await skill.read(ctx, dead[:8])).content.splitlines()[0].endswith(words)
    assert "draft" not in (await skill.read(ctx, good[:8])).content.splitlines()[0]
