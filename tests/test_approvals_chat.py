"""An approval in its chat (celine/approvals.py): the request lands in its conversation's
feed and snapshot after the message that made the call, Approve runs it, Deny gives the
tool its denial, a Stop or an answer takes it away everywhere, a restart closes what the
last run left open. In a turn the user waits on it waits for them and holds the turn's
clock; anywhere else it keeps its time."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import anyio
import pytest
from chat_fixtures import until

from celine import approvals as ap
from celine.conversations import store as conversations, tree, view
from celine.guard.owner import _SEAL, Owner
from celine.turns import waiting
from celine.turns.cancel import Token, until_stop

pytestmark = pytest.mark.anyio
OWNER = Owner("test", _SEAL)


async def _chat(store):
    """A conversation with one reply that made call k1; -> (cid, the reply's id)."""
    cid = await conversations.create(store)

    def fn(c):
        m = tree.append(c, store, cid, None, "assistant", "on it", "model", None, "t1")
        c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, state) "
                  "VALUES('k1', ?, 'me__edit', '{}', 'running')", (m["id"],))
        return m["id"]
    return cid, await store.write(fn)


async def _events(store, cid):
    return await store.read(lambda c: [(r[0], json.loads(r[1])) for r in c.execute(
        "SELECT type, data FROM events WHERE conversation_id=? AND type LIKE 'approval.%' "
        "ORDER BY seq", (cid,))])


def _approvals(store, monkeypatch, ask_for=5):
    monkeypatch.setattr(ap, "ASK_FOR", ask_for)
    return ap.Approvals(SimpleNamespace(user="sam", name="Celine"), store=store)


async def _open(store, cid):
    return (await view.snapshot(store, cid))["approvals"]


async def test_an_ask_lands_in_its_chat_and_approve_takes_it_away(store, monkeypatch):
    a = _approvals(store, monkeypatch)
    cid, mid = await _chat(store)
    task = asyncio.create_task(a.ask("change the Persona", ["new: I garden.", "old: x"], cid,
                                     "why", "change the Persona", kind="her", call="k1"))
    await until(lambda: a.waiting)
    [ask] = await _open(store, cid)
    [aid] = a.waiting
    assert ask == {"id": aid, "kind": "her", "label": "Persona", "tool_call_id": "k1",
                   "message_id": mid, "line": "Change the Persona. new: I garden.; old: x"}
    assert await _events(store, cid) == [("approval.asked", {"approval": ask})]
    assert a.decide(aid, True, OWNER)
    assert await task is None
    assert await _open(store, cid) == []
    assert (await _events(store, cid))[-1] == ("approval.closed", {"id": aid})


async def test_deny_is_the_tools_plain_denial_and_takes_it_away(store, monkeypatch):
    a = _approvals(store, monkeypatch)
    cid, _ = await _chat(store)
    task = asyncio.create_task(a.ask("change the Persona", [], cid, kind="her", call="k1"))
    await until(lambda: a.waiting)
    [aid] = a.waiting
    assert a.decide(aid, False, OWNER)
    assert await task == "sam denied it; nothing was done."
    assert await _open(store, cid) == []


async def test_a_stop_ends_the_wait_and_takes_it_away(store, monkeypatch):
    a = _approvals(store, monkeypatch)
    cid, _ = await _chat(store)
    token = Token("t1")
    clock = anyio.CancelScope(deadline=anyio.current_time() + 600)
    waits = waiting.TURN.set(waiting.Turn(interactive=True, clock=clock, token=token))
    try:
        stop = asyncio.create_task(until_stop(
            lambda: a.ask("change the Persona", [], cid, kind="her", call="k1"), token))
        await until(lambda: a.waiting)
        token.cancel()
        stopped, out = await stop
    finally:
        waiting.TURN.reset(waits)
    assert stopped and out is None and not a.waiting
    assert await _open(store, cid) == []


async def test_in_a_turn_the_user_waits_on_it_has_no_time_and_holds_the_clock(store,
                                                                            monkeypatch):
    """ASK_FOR 0 would end it at once; the user's own turn waits for them, and the
    turn's wall clock does not run while it does."""
    a = _approvals(store, monkeypatch, ask_for=0)
    cid, _ = await _chat(store)
    clock = anyio.CancelScope(deadline=anyio.current_time() + 600)
    before = clock.deadline
    waits = waiting.TURN.set(waiting.Turn(interactive=True, clock=clock, token=Token("t1")))
    try:
        task = asyncio.create_task(a.ask("change the Persona", [], cid, kind="her"))
        await until(lambda: a.waiting)
        await until(lambda: clock.deadline == float("inf"))
        [view_] = a.view()
        assert view_["expires_in"] is None
        assert a.decide(view_["id"], True, OWNER)
        assert await task is None
    finally:
        waiting.TURN.reset(waits)
    assert before <= clock.deadline < float("inf")


async def test_a_background_turn_keeps_its_time(store, monkeypatch):
    a = _approvals(store, monkeypatch, ask_for=0)
    cid, _ = await _chat(store)
    clock = anyio.CancelScope(deadline=anyio.current_time() + 600)
    waits = waiting.TURN.set(waiting.Turn(interactive=False, clock=clock, token=Token("t1")))
    try:
        got = await a.ask("change the Persona", [], cid, kind="her")
    finally:
        waiting.TURN.reset(waits)
    assert got == "sam did not answer in 1 minute; nothing was done."
    assert await _open(store, cid) == []


async def test_a_gate_is_asked_on_bastets_banner_not_twice(store, monkeypatch):
    a = _approvals(store, monkeypatch)
    cid, _ = await _chat(store)
    task = asyncio.create_task(a.ask("run me__edit", [], cid, kind="gate", call="k1"))
    await until(lambda: a.waiting)
    assert await _open(store, cid) == [] and await _events(store, cid) == []
    [aid] = a.waiting
    a.decide(aid, True, OWNER)
    assert await task is None


async def test_asks_in_two_chats_wait_side_by_side(store, monkeypatch):
    a = _approvals(store, monkeypatch)
    one, _ = await _chat(store)
    two = await conversations.create(store)
    first = asyncio.create_task(a.ask("a", [], one, kind="her"))
    await until(lambda: a.waiting)
    second = asyncio.create_task(a.ask("b", [], two, kind="her"))
    await until(lambda: len(a.waiting) == 2)
    assert (await a.ask("c", [], one, kind="her")).startswith("Another request waits")
    for aid in list(a.waiting):
        a.decide(aid, True, OWNER)
    assert await first is None and await second is None


async def test_a_restart_closes_what_the_last_run_left_open(store, monkeypatch):
    a = _approvals(store, monkeypatch)
    cid, _ = await _chat(store)
    task = asyncio.create_task(a.ask("change the Persona", [], cid, kind="her", call="k1"))
    await until(lambda: a.waiting)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # a crash: the closing event never landed
    await store.write(lambda c: c.execute(
        "DELETE FROM events WHERE type='approval.closed'"))
    assert len(await _open(store, cid)) == 1
    assert await ap.close_left(store) == 1
    assert await _open(store, cid) == []
