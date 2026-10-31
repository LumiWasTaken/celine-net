"""A thread reads like a chat (intercom/answer.py, store.follows): its answer to the
user's reply lands in the thread, a message it sends from that turn answers there by
itself, the thread holds everything oldest first, every message and thread says where it
came from (store.source_of)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from celine.conversations import store as conversations, tree
from celine.intercom import Intercom, answer, store as rows

from jobs_fixtures import cfg  # noqa: F401  (the fixture)

pytestmark = pytest.mark.anyio


async def _replied(store, ic, cid):
    """The agent wrote to the user; the user replied in that thread: one queued turn."""
    first = await ic.send(f"agent:{cid}", "user", "the download stopped", subject="videos")
    reply = await ic.send("user", "", "why?", reply_to=first["intercom_id"])
    assert reply["turn_id"], "the reply wakes her"
    return first["intercom_id"], reply


async def _answers(store, cid, turn_id, text):
    """The agent's turn writes `text` and ends."""
    def fn(c):
        leaf = c.execute("SELECT active_leaf_id FROM conversations WHERE id=?",
                         (cid,)).fetchone()[0]
        tree.append(c, store, cid, leaf, "assistant", text, "model", None, turn_id)
        c.execute("UPDATE turns SET state='done' WHERE id=?", (turn_id,))
    await store.write(fn)


async def test_her_answer_to_the_users_reply_lands_in_the_thread(store):
    ic = Intercom(store)
    cid = await conversations.create(store, title="downloads")
    first, reply = await _replied(store, ic, cid)
    await _answers(store, cid, reply["turn_id"], "i killed it, sorry [[web:2]]")
    services = SimpleNamespace(store=store, intercom=ic)
    await answer.on_end(services, cid, reply["turn_id"])
    await answer.on_end(services, cid, reply["turn_id"])          # once only
    msgs = await store.read(lambda c: rows.thread(c, first))
    assert [(m["from"], m["to"], m["text"]) for m in msgs] == [
        (f"agent:{cid}", "user", "the download stopped"), ("user", f"agent:{cid}", "why?"),
        (f"agent:{cid}", "user", "i killed it, sorry [[web:2]]")], "as she wrote it"
    assert msgs[2]["reply_to"] == reply["intercom_id"] and msgs[2]["subject"] == "videos"


async def test_only_her_final_answer_goes_in_and_a_later_one_of_hers_does_not_count(store):
    """Words between tool calls are no answer; a message it scheduled for later, or one
    it cancelled, is not its answer; citation marks go."""
    ic = Intercom(store)
    cid = await conversations.create(store, title="downloads")
    first, reply = await _replied(store, ic, cid)
    turn = reply["turn_id"]
    later = await ic.send(f"agent:{cid}", "user", "checking again soon", reply_to=first,
                          due_at="2099-01-01T00:00:00+00:00")
    assert later["intercom_id"]

    def preamble(c):
        leaf = c.execute("SELECT active_leaf_id FROM conversations WHERE id=?",
                         (cid,)).fetchone()[0]
        m = tree.append(c, store, cid, leaf, "assistant", "let me check the logs", "model",
                        None, turn)
        c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, state) "
                  "VALUES('tc', ?, 'shell__run', '{}', 'ok')", (m["id"],))
        c.execute("UPDATE turns SET state='done' WHERE id=?", (turn,))
    await store.write(preamble)
    services = SimpleNamespace(store=store, intercom=ic)
    await answer.on_end(services, cid, turn)
    assert len(await store.read(lambda c: rows.thread(c, first))) == 3, "no preamble"
    await _answers(store, cid, turn, "the disk was full [web:2]【1†a】, fixed")
    await answer.on_end(services, cid, turn)
    assert (await store.read(lambda c: rows.thread(c, first)))[-1]["text"] == \
        "the disk was full [web:2]【1†a】, fixed"


async def test_a_message_she_sends_from_that_turn_answers_in_the_thread(store, cfg):
    """The agent's instructions name the thread; no reply_to needed; one it meant as new starts
    its own; another turn under the same subject starts its own too (no endless thread
    on a word like "update")."""
    from celine.intercom.skill import IntercomSkill
    from celine.skills import ToolContext
    ic = Intercom(store)
    cid = await conversations.create(store, title="downloads")
    first, reply = await _replied(store, ic, cid)
    turn = reply["turn_id"]
    ctx = ToolContext(cid, turn, "x", store, cfg, None)  # type: ignore[arg-type]
    said = await IntercomSkill(cfg).tail_part(ctx)
    assert f'You are in thread #{first} "videos": this turn answers #{reply["intercom_id"]}' \
        in said and 'intercom__send(to="user") answers there by itself' in said
    sent = await ic.send(f"agent:{cid}", "user", "relaunched it", turn=turn)
    other = await ic.send(f"agent:{cid}", "user", "something else", turn=None)
    later = await ic.send(f"agent:{cid}", "user", "all done", subject="videos", turn="t2")
    threads = await store.read(lambda c: {i: c.execute(
        "SELECT thread FROM intercom WHERE id=?", (i,)).fetchone()[0]
        for i in (sent["intercom_id"], other["intercom_id"], later["intercom_id"])})
    assert threads == {sent["intercom_id"]: first, other["intercom_id"]: other["intercom_id"],
                       later["intercom_id"]: later["intercom_id"]}
    await _answers(store, cid, turn, "relaunched it")
    await answer.on_end(SimpleNamespace(store=store, intercom=ic), cid, turn)
    assert len(await store.read(lambda c: rows.thread(c, first))) == 3, \
        "it already answered there: no second answer"


async def test_the_thread_view_holds_the_whole_history_oldest_first(store):
    import httpx
    from fastapi import FastAPI

    from celine.core.services import Services
    from celine.intercom import api as intercom_api
    ic = Intercom(store)
    cid = await conversations.create(store, title="downloads")
    first, reply = await _replied(store, ic, cid)
    await _answers(store, cid, reply["turn_id"], "because the disk was full")
    await answer.on_end(SimpleNamespace(store=store, intercom=ic), cid, reply["turn_id"])
    await ic.send("user", "", "ok", reply_to=first)
    app = FastAPI()
    app.state.services = Services()
    app.state.services.store = store
    app.include_router(intercom_api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as http:
        got = (await http.get(f"/api/intercom?thread={first}")).json()
    assert [m["text"] for m in got["messages"]] == [
        "the download stopped", "why?", "because the disk was full", "ok"]
    chat = {"kind": "chat", "title": "downloads", "link": f"/chat/{cid}"}
    assert got["source"] == chat
    assert [m["source"] for m in got["messages"]] == [chat, None, chat, None]


@pytest.mark.anyio
async def test_bastets_threads_wait_on_their_own_tab_not_waiting_for_you(store):
    from celine.intercom.store import insert
    cid = await conversations.create(store, title="x")

    def fn(c):
        insert(c, rows.GUARD, "user", "notice", "on patrol", store=store)
        insert(c, f"agent:{cid}", "user", "message", "hi", store=store)
        waiting = rows.threads(c, "user", unread=True, reader="user", received=True)
        guard = rows.threads(c, None, reader="user", with_=rows.GUARD)
        return [t["name"] for t in waiting], [t["name"] for t in guard], rows.waiting(c, "user")
    waiting, guard, n = await store.write(fn)
    assert waiting == ["Celine"] and guard == ["Bastet"] and n == 1
