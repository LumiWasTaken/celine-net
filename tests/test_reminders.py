"""Scheduled messages (a reminder is a message to itself with a time): one lands at its
time, also one that came due while the app was down; one due during a live turn joins
that turn at its next step; a cancelled one never lands and stays listed."""
from __future__ import annotations

from chat_fixtures import until, user_says

import anyio
import pytest

from celine.conversations import store as conversations, view
from celine.boot import load
from celine.core.bus import EventBus
from celine.core.services import Services
from celine.core.store import Store
from celine.intercom import Intercom, deliver
from celine.intercom import store as rows
from celine.intercom.skill import IntercomSkill
from celine.skills import ToolContext
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _intercom_clock(clock, monkeypatch):
    """Delivery reads the test's clock: nothing comes due by the wall clock."""
    for mod in (deliver, rows):
        monkeypatch.setattr(mod, "now", clock.iso)


async def _origins(store, cid: str) -> list[str]:
    return await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT origin FROM turns WHERE conversation_id=? ORDER BY created_at", (cid,))])


async def snapshot_has(store, cid: str) -> bool:
    return bool((await view.snapshot(store, cid))["messages"])


async def test_one_lands_at_its_time_and_a_past_due_one_at_start(db_path, clock):
    async with anyio.create_task_group() as tg:
        store = Store(db_path, bus=EventBus())
        await store.start(tg)
        intercom = Intercom(store)
        a, b = await conversations.create(store), await conversations.create(store)
        # written before the timer runs: due while the app was down
        await store.write(lambda c: rows.insert(c, f"agent:{a}", "me", "message",
                                                "missed while down", due_at=clock.iso(-7200)))
        await intercom.send(f"agent:{b}", "me", "check the server log", due_at=clock.iso(60))
        await intercom.run(tg)
        await until(lambda: snapshot_has(store, a))
        assert not await snapshot_has(store, b), "not before its time"
        clock.advance(60)
        # a new message with a due time wakes the timer, as its own sleep would at 60 s
        await intercom.send(f"agent:{b}", "me", "not yet", due_at=clock.iso(86400))
        await until(lambda: snapshot_has(store, b))
        for cid, text in ((a, "missed while down"), (b, "check the server log")):
            [m] = (await view.snapshot(store, cid))["messages"]
            assert m["author"] == "intercom" and m["content"] == text
            assert m["intercom"]["kind"] == "message" and m["intercom"]["mine"]
            assert await _origins(store, cid) == ["intercom"], "it starts its turn"
        assert await deliver.next_due(store), "tomorrow's is still waiting"
        tg.cancel_scope.cancel()
        await store.aclose()


async def test_the_timer_tries_once_and_runs_again_on_the_next_message(store):
    from celine.intercom import service
    calls = []

    async def due(_store):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(deliver, "deliver_due", due)
        async with anyio.create_task_group() as tg:
            ic = Intercom(store)
            await ic.run(tg)
            await until(lambda: len(calls) == 1, 2)
            assert not hasattr(service, "RETRY")
            ic._wake.set()                   # a message with a due time
            await until(lambda: len(calls) >= 2, 2)
            tg.cancel_scope.cancel()


async def test_one_due_during_a_turn_joins_it_at_the_next_step(store, clock):
    intercom = Intercom(store)
    cid = await user_conversation(store)
    turn = await turns.claim(store, "w")
    await intercom.send(f"agent:{cid}", "me", "later", due_at=clock.iso(60))
    clock.advance(60)
    assert await deliver.deliver_due(store) == [], "the turn is live: the tree is its own"
    leaf = (await view.snapshot(store, cid))["messages"][-1]["id"]
    [m] = await deliver.take(store, cid, turn["id"], leaf)       # the runner's next step
    assert (m["content"], m["turn_id"], m["parent_id"]) == ("later", turn["id"], leaf)
    assert await _origins(store, cid) == ["user"], "no second turn"


async def user_conversation(store) -> str:
    cid = await conversations.create(store)
    await user_says(store, cid, "hi")
    return cid


async def test_a_cancelled_one_never_lands_and_stays_listed(store, clock):
    cfg = load()
    skill = IntercomSkill(cfg)
    skill.app = Services(intercom=Intercom(store))
    cid = await conversations.create(store)
    ctx = ToolContext(cid, "t", "k", store, cfg, None)
    assert (await skill.send(ctx, to="me", text="x", in_="soon")).is_error
    out = await skill.send(ctx, to="me", text="look at the build", in_="30 min")
    iid = out.display["id"]
    assert f"#{iid}" in await skill.prompt_fragment(ctx)
    assert not (await skill.cancel(ctx, id=iid)).is_error
    assert (await skill.cancel(ctx, id=iid)).is_error, "once"
    assert await skill.prompt_fragment(ctx) is None
    await store.write(lambda c: c.execute("UPDATE intercom SET due_at=? WHERE id=?",
                                          (clock.iso(-1), iid)))
    assert await deliver.deliver_due(store) == []
    assert (await view.snapshot(store, cid))["messages"] == [], "it never lands"
    [row] = await store.read(lambda c: rows.listed(c, to=f"agent:{cid}", scheduled=True))
    assert (row["id"], row["state"], row["text"]) == (iid, "cancelled", "look at the build")


async def test_one_to_the_user_gives_its_notice_at_its_time_once(store, clock):
    async def notices() -> int:
        return await store.read(lambda c: c.execute(
            "SELECT count(*) FROM global_events WHERE type='notice'").fetchone()[0])
    await store.write(lambda c: rows.insert(c, "agent:x", "user", "message", "stretch",
                                            due_at=clock.iso(60), store=store))
    assert await deliver.next_due(store) == clock.iso(60)
    await deliver.deliver_due(store)
    assert await notices() == 0, "not before its time"
    clock.advance(60)
    await deliver.deliver_due(store)
    await deliver.deliver_due(store)
    assert await notices() == 1
    assert await deliver.next_due(store) is None
