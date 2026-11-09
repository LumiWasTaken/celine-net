"""The hold (hold.py) as the lists and the snapshot show it: while the agent is paused, a
queued turn of its own waits and is not under Running; a sub-agent whose own model
answers still runs; the snapshot says which hold keeps a conversation's turn aside."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from celine import hold
from celine.conversations import store as conversations, view

pytestmark = pytest.mark.anyio


def _llm(paused: bool, answering=("box/small",)):
    return SimpleNamespace(router=SimpleNamespace(paused=paused,
                                                  answering=lambda: set(answering)))


async def _queued(store, cid: str, tid: str) -> None:
    await store.write(lambda c: c.execute(
        "INSERT INTO turns(id, conversation_id, state, created_at) VALUES(?, ?, 'queued', "
        "'2026-10-01T10:00:00.000+00:00')", (tid, cid)))


async def _rows(store, llm) -> dict[str, dict]:
    rows = await conversations.list(store, models=hold.answering(llm))
    return {r["id"]: r for r in rows} | {a["id"]: a for r in rows for a in r["agents"]}


async def test_a_queued_chat_turn_while_paused_is_not_running_and_its_snapshot_says_paused(store):
    cid = await conversations.create(store, "chat")
    await _queued(store, cid, "t1")
    assert (await _rows(store, _llm(False)))[cid]["rank"] == 2
    paused = _llm(True)
    assert (await _rows(store, paused))[cid]["rank"] == 0
    assert (await view.snapshot(store, cid, paused))["conversation"]["held"] == "paused"
    assert (await view.snapshot(store, cid, _llm(False)))["conversation"]["held"] is None


async def test_a_sub_agent_whose_own_model_answers_keeps_running_while_paused(store):
    top = await conversations.create(store, "chat")
    sub = await conversations.create(store, agent_of=top, agent_name="a", kind="agent")
    await store.write(lambda c: c.execute("UPDATE conversations SET model_id='box/small' "
                                          "WHERE id=?", (sub,)))
    await _queued(store, sub, "t1")
    paused = _llm(True)
    assert (await _rows(store, paused))[top]["rank"] == 2
    assert (await view.snapshot(store, sub, paused))["conversation"]["held"] is None


async def test_frozen_wins_over_paused_in_the_snapshot(store):
    cid = await conversations.create(store, "chat")
    await store.write(lambda c: c.execute("INSERT INTO kv(k, v) VALUES('frozen', '1')"))
    assert (await view.snapshot(store, cid, _llm(True)))["conversation"]["held"] == "frozen"


async def test_while_paused_the_agents_servers_are_asked_only_while_one_of_its_turns_waits(
        store, tmp_path, monkeypatch):
    """No constant probing: a paused worker with nothing held waits for a turn and asks no
    server; a held turn asks again (router.revisit, at most every RETRY_DOWN)."""
    import anyio
    from chat_fixtures import until, user_says
    from celine.core.bus import EventBus
    from celine.turns import dispatcher as dispatcher_mod
    from fake_backend import FakeBackend
    from test_work import _done, _gateway
    cfg, llm, up, later = _gateway(tmp_path, monkeypatch, {
        "chat": {"chain": ["gpu/big"]}, "tasks": {"chain": ["gpu/big"]}})
    await llm.router.check_all()
    assert llm.router.paused
    asked: list[str] = []
    probe = llm.router.probe

    async def counted(pid, **kw):
        asked.append(pid)
        return await probe(pid, **kw)
    monkeypatch.setattr(llm.router, "probe", counted)
    monkeypatch.setattr(dispatcher_mod, "HOLD_RECHECK", 0.02)
    d = dispatcher_mod.Dispatcher(store, cfg, EventBus(), FakeBackend, None, llm=llm)
    waits = d._wait_for_work
    idle: list[int] = []

    async def counted_wait():
        idle.append(1)
        await waits()
    d._wait_for_work = counted_wait
    cid = await conversations.create(store)
    async with anyio.create_task_group() as tg:
        later()                             # a held turn would ask the server now
        await d.start(tg)
        await until(lambda: len(idle) >= len(d._workers))
        assert asked == [], "nothing waits: no server is asked"
        await user_says(store, cid, "hello there")
        d.notify()
        await until(lambda: "gpu" in asked)
        up["gpu"] = True
        later()
        await until(lambda: _done(store, cid))
        await d.stop()
        tg.cancel_scope.cancel()
