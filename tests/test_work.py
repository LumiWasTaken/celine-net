"""The work queue (work.py) and its pause (hold.py): leases, one try, deferral until a
host is back, and none of the agent's turns claimed while no chat host answers (an agent's
whose own model answers is). Waiting work asks a host known down again (router.revisit)
once RETRY_DOWN has passed; nothing else asks it. Fixed clocks, fake servers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import anyio
import httpx
import pytest

from chat_fixtures import until, user_says

from celine import work
from fake_backend import FakeBackend
from celine.core.ids import iso
from celine.backends.llm import LLM
from celine.backends import router as router_mod
from celine.backends.router import NoModel, Router
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.hold import agent_hold
from celine.turns import dispatcher as dispatcher_mod
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)
PROVIDERS = {
    "gpu": {"kind": "llama-swap", "label": "llama-swap", "base_url": "http://gpu:1/v1",
            "models": {"big": {"caps": ["chat", "tools"]}}},
    "lan": {"kind": "ollama", "label": "gpu-box", "base_url": "http://lan:2/v1",
            "models": {"small": {"caps": ["chat"]}}},
}


def s(n: float) -> timedelta:
    return timedelta(seconds=n)


async def _row(store, wid: int) -> dict:
    return await store.read(lambda c: dict(c.execute(
        "SELECT state, error, lease_worker FROM work WHERE id=?", (wid,)).fetchone()))


def _gateway(tmp_path, monkeypatch, roles, events=None):
    """An LLM over PROVIDERS whose hosts answer only while `up` says so; `later()` ages
    what it knows by RETRY_DOWN, so waiting work asks a host again."""
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=roles)
    cfg = load()
    up = {"gpu": False, "lan": False}

    def net(request: httpx.Request) -> httpx.Response:
        if not up[request.url.host]:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"running": [], "models": []})
    t = httpx.MockTransport(net)
    publish = events.append if events is not None else None
    router = Router(cfg, publish, transport=t, sync_transport=t)
    llm = LLM(cfg, publish, router=router)

    def later() -> None:                    # every state RETRY_DOWN older
        for st in router._providers.values():
            st.checked -= router_mod.RETRY_DOWN
    return cfg, llm, up, later


async def test_no_item_for_a_conversation_purged_meanwhile(store):
    """A turn's tail asks for a title after Delete permanently took its conversation."""
    assert await work.put(store, "title", {"conversation_id": "gone"},
                          conversation_id="gone", unique=True) is None
    cid = await conversations.create(store, "")
    assert await work.put(store, "title", {"conversation_id": cid}, conversation_id=cid)


async def test_a_lease_that_runs_out_puts_the_item_back(store):
    wid = await work.put(store, "x", {"a": 1}, now=T0)
    got = await work.claim(store, "w1", ["x"], now=T0)
    assert got["id"] == wid and got["payload"] == {"a": 1}
    assert await work.claim(store, "w2", ["x"], now=T0) is None, "leased to w1"
    assert await work.heartbeat(store, wid, "w1", now=T0 + s(100))
    assert await work.claim(store, "w2", ["x"], now=T0 + s(200)) is None, "the beat held it"
    again = await work.claim(store, "w2", ["x"], now=T0 + s(100 + work.LEASE_SECONDS + 1))
    assert again["id"] == wid
    assert not await work.heartbeat(store, wid, "w1", now=T0 + s(300)), "no longer w1's"
    await work.finish(store, wid, "w1")
    assert (await _row(store, wid))["lease_worker"] == "w2", "a late worker changes nothing"
    await work.finish(store, wid, "w2")
    assert (await _row(store, wid))["state"] == "done"


async def test_a_real_error_fails_at_once_with_its_reason(store):
    now = [T0]
    pool = work.Pool(store, clock=lambda: now[0])

    async def boom(_p):
        raise RuntimeError("the answer was not JSON")
    pool.handle("x", (), boom)
    wid = await pool.put("x", {})
    item = await work.claim(store, "w", ["x"], now=now[0])
    assert await pool.run_one(item, "w") == "failed"
    row = await _row(store, wid)
    assert row["state"] == "failed"
    assert row["error"] == "RuntimeError: the answer was not JSON"
    assert await work.claim(store, "w", ["x"], now=now[0] + s(3600)) is None


async def test_a_job_whose_host_is_down_waits_and_runs_when_the_host_is_back(
        store, tmp_path, monkeypatch):
    _, llm, up, later = _gateway(tmp_path, monkeypatch, {"small": {"chain": ["lan/small"]}})
    await llm.router.check_all()
    assert llm.router.hosts()["lan"] == "down"
    now = [T0]
    pool = work.Pool(store, llm, clock=lambda: now[0])
    ran = []

    async def job(p):
        ran.append(p)
    pool.handle("title", ("small",), job)
    async with anyio.create_task_group() as tg:
        await pool.start(tg)
        wid = await pool.put("title", {"conversation_id": "c1"})
        await until(async_state(store, wid, "deferred"))
        assert ran == [], "no call to a host that is down"
        up["lan"] = True
        later()
        now[0] += s(work.DEFER_SECONDS)     # due: its run asks the host again
        pool.notify()
        await until(async_state(store, wid, "done"))
        assert ran == [{"conversation_id": "c1"}]
        await pool.stop()
        tg.cancel_scope.cancel()


def async_state(store, wid, state):
    async def check():
        return (await _row(store, wid))["state"] == state
    return check


async def test_with_no_chat_host_the_agent_pauses_and_resumes_by_itself(
        store, tmp_path, monkeypatch):
    events: list[dict] = []
    cfg, llm, up, later = _gateway(tmp_path, monkeypatch, {
        "chat": {"chain": ["gpu/big"]}, "tasks": {"chain": ["gpu/big"]}}, events)
    await llm.router.check_all()
    assert await agent_hold(store, llm) == "paused" and llm.router.hold() == "paused"
    assert [e["hold"] for e in events if e["type"] == "agent.hold"] == ["paused"]
    looked = []

    async def hold(store_, llm_):
        looked.append(1)
        return await agent_hold(store_, llm_)
    monkeypatch.setattr(dispatcher_mod, "agent_hold", hold)
    monkeypatch.setattr(dispatcher_mod, "HOLD_RECHECK", 0.02)
    d = dispatcher_mod.Dispatcher(store, cfg, EventBus(), FakeBackend, None, llm=llm)
    cid = await conversations.create(store)

    async def state():
        return await store.read(lambda c: c.execute(
            "SELECT state FROM turns WHERE conversation_id=?", (cid,)).fetchone()[0])
    async with anyio.create_task_group() as tg:
        await d.start(tg)
        await user_says(store, cid, "hello there")
        d.notify()
        n = len(looked)
        await until(lambda: len(looked) > n + 5)
        assert await state() == "queued", "no turn is claimed while it is paused"
        up["gpu"] = True
        later()                             # the held worker asks it again
        await until(lambda: _done(store, cid))
        assert await agent_hold(store, llm) is None
        assert [e["hold"] for e in events if e["type"] == "agent.hold"][-1] is None
        await until(lambda: _done(store, cid))
        await d.stop()
        tg.cancel_scope.cancel()


async def test_while_the_agent_is_paused_a_sub_agent_whose_model_answers_runs_and_its_turn_waits(
        store, tmp_path, monkeypatch):
    """The main agent's roles' host is down, a sub-agent's own model's host is up: the
    sub-agent's turn runs, its report to the main agent waits in the queue and lands when
    that host is back. A sub-agent on the down host waits with it."""
    from celine.intercom import send
    from celine.settings import conversations as conv
    cfg, llm, up, later = _gateway(tmp_path, monkeypatch, {
        "chat": {"chain": ["gpu/big"]}, "tasks": {"chain": ["gpu/big"]}}, [])
    up["lan"] = True
    await llm.router.check_all()
    assert await agent_hold(store, llm) == "paused"
    assert llm.router.answering() == {"lan/small"}
    monkeypatch.setattr(dispatcher_mod, "HOLD_RECHECK", 0.02)
    d = dispatcher_mod.Dispatcher(store, cfg, EventBus(), FakeBackend, None, llm=llm)
    main = await conversations.create(store)
    agent = await conversations.create(store, title="a", agent_of=main, agent_name="a")
    await conv.apply(store, agent, {"model": "lan/small"})
    stuck = await conversations.create(store, title="b", agent_of=main, agent_name="b")
    await conv.apply(store, stuck, {"model": "gpu/big"})

    async def state(cid):
        return await store.read(lambda c: c.execute(
            "SELECT state FROM turns WHERE conversation_id=?", (cid,)).fetchone()[0])
    async with anyio.create_task_group() as tg:
        await d.start(tg)
        await user_says(store, agent, "build it")
        await user_says(store, stuck, "build that")
        d.notify()
        await until(lambda: _done(store, agent))
        assert (await send(store, f"agent:{agent}", f"agent:{main}", "[a, done]"))["turn_id"]
        d.notify()
        assert await state(main) == "queued" and await state(stuck) == "queued"
        up["gpu"] = True
        later()
        await until(lambda: _done(store, main))
        await until(lambda: _done(store, stuck))
        await d.stop()
        tg.cancel_scope.cancel()


async def test_frozen_wins_over_paused(store, tmp_path, monkeypatch):
    cfg, llm, up, later = _gateway(tmp_path, monkeypatch, {
        "chat": {"chain": ["gpu/big"]}, "tasks": {"chain": ["gpu/big"]}}, [])
    await llm.router.check_all()
    assert await agent_hold(store, llm) == "paused"

    def froze(c):
        rid = c.execute("INSERT INTO freezes(at, by, why) VALUES("
                        "'2026-10-01T10:00:00.000+00:00', 'user', 'x')").lastrowid
        c.execute("INSERT INTO kv(k, v) VALUES('frozen', ?)", (str(rid),))
    await store.write(froze)
    assert await agent_hold(store, llm) == "frozen"
    assert await agent_hold(store, None) == "frozen"


async def _done(store, cid) -> bool:
    return await store.read(lambda c: c.execute(
        "SELECT state FROM turns WHERE conversation_id=?", (cid,)).fetchone()[0]) == "done"


async def test_any_answer_from_a_host_means_it_is_up_and_only_a_rejected_key_is_not(
        tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles={})
    status = {"gpu": 404, "lan": 503}
    t = httpx.MockTransport(lambda r: httpx.Response(status[r.url.host]))
    router = Router(load(), None, transport=t)
    assert (await router.probe("gpu", max_age=0)).reachable is True
    assert (await router.probe("lan", max_age=0)).reachable is True
    status["gpu"] = 401
    st = await router.probe("gpu", max_age=0)
    assert st.reachable is False and st.error.startswith("llama-swap returned 401")


async def test_an_item_of_a_kind_nobody_handles_does_not_wake_the_pool(store):
    await work.put(store, "gone", {}, after=1, now=T0)
    await work.put(store, "x", {}, after=30, now=T0)
    assert await work.next_due(store, ["x"]) == T0 + s(30)
    assert await work.next_due(store, ["y"]) is None


async def _exchange(store, n: int) -> str:
    """A conversation whose first exchange is done: user message, its reply, turn t<n>."""
    cid = await conversations.create(store)

    def fn(c):
        c.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) "
                  "VALUES(?, ?, 'user', 'where did noor go last night', ?)",
                  (f"u{n}", cid, T0.isoformat()))
        c.execute("INSERT INTO turns(id, conversation_id, root_message_id, state, "
                  "created_at) VALUES(?, ?, ?, 'done', ?)",
                  (f"t{n}", cid, f"u{n}", T0.isoformat()))
        c.execute("INSERT INTO messages(id, conversation_id, parent_id, role, content, "
                  "turn_id, created_at) VALUES(?, ?, ?, 'assistant', 'out. again.', ?, ?)",
                  (f"a{n}", cid, f"u{n}", f"t{n}", T0.isoformat()))
        c.execute("UPDATE conversations SET active_leaf_id=? WHERE id=?", (f"a{n}", cid))
    await store.write(fn)
    return cid


class _Raises:
    supports_tools = supports_images = True
    model = "x"

    def __init__(self, e):
        self.e = e

    async def stream(self, messages, tools, body=None):
        raise self.e
        yield


async def _nothing(_roles) -> None:
    pass


def _titler(store, by_role):
    llm = SimpleNamespace(bind=lambda fixed, role, **kw: by_role[role],
                          router=SimpleNamespace(all_down=lambda roles: False,
                                                 revisit=_nothing))
    return dispatcher_mod.Dispatcher(store, load(), EventBus(), None, None, llm=llm)


async def test_a_title_waits_for_a_host_and_stays_empty_on_an_error(
        store, tmp_path, monkeypatch):
    from celine.backends.base import BackendError, Done, TextDelta
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles={})
    none = NoModel("No model for titles and descriptions can answer now.")
    cid = await _exchange(store, 1)
    d = _titler(store, {"small": _Raises(none), "chat": _Raises(none)})
    d.work.clock = lambda: T0
    await d.work.put("title", {"conversation_id": cid})
    item = await work.claim(store, "w", ["title"], now=T0)
    assert await d.work.run_one(item, "w") == "deferred", "no host answered: it waits"
    assert not await conversations.title(store, cid)

    d = _titler(store, {"small": FakeBackend([TextDelta(""), Done(reason="stop")])})
    await d._title(cid)
    assert not await conversations.title(store, cid), "a model answered with nothing"

    cid = await _exchange(store, 2)
    bad = BackendError("bad request", retryable=False)
    d = _titler(store, {"small": _Raises(bad)})
    await d._title(cid)
    assert not await conversations.title(store, cid), "a failure leaves no title"


async def test_the_agents_appraisal_is_a_work_item_and_a_stale_one_makes_no_call(
        store, tmp_path, monkeypatch):
    from celine.skills import mind
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles={})
    cid = await _exchange(store, 1)
    now = [T0]
    pool = work.Pool(store, clock=lambda: now[0])
    app = SimpleNamespace(store=store, cfg=load(), llm=None,
                          dispatcher=SimpleNamespace(work=pool))
    pool.handle("appraisal", (), lambda p: mind._appraise_item(app, p))
    asked = []

    async def appraise(_app, c, t, backend=None):
        asked.append((c, t))
    monkeypatch.setattr(mind, "appraise", appraise)
    await mind._ended(app, cid, "t1")
    await mind._ended(app, cid, "t1")
    item = await work.claim(store, "w", ["appraisal"], now=now[0])
    assert item["payload"]["turn_id"] == "t1" and item["conversation_id"] == cid
    assert await pool.run_one(item, "w") == "done" and asked == [(cid, "t1")]
    now[0] += s(mind.APPRAISE_FRESH + 1)            # the second waited out an outage
    item = await work.claim(store, "w", ["appraisal"], now=now[0])
    assert await pool.run_one(item, "w") == "done" and len(asked) == 1, "no call"


async def test_a_describe_run_with_no_model_leaves_one_waiting_item(store):
    from celine.kb import documents_describe
    pool = work.Pool(store, clock=lambda: T0)
    kb = SimpleNamespace(id="docs", work=pool,
                         tasks=SimpleNamespace(loop=asyncio.get_running_loop()))
    for _ in range(2):
        await anyio.to_thread.run_sync(documents_describe._again, kb)
    rows = await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT kind, state, payload, not_before FROM work")])
    assert rows == [{"kind": "documents.describe", "state": "deferred",
                     "payload": '{"kb": "docs"}',
                     "not_before": iso(T0 + s(work.DEFER_SECONDS))}]


async def test_a_turn_waiting_for_an_answer_takes_no_turn_slot(store):
    """One turn at once, and that turn waits for the user's answer to a request: the
    user's own turn in another chat starts beside it."""
    cfg = load()
    cfg.settings.general["max_concurrent_turns"] = 1
    go = anyio.Event()

    class Blocks(FakeBackend):
        async def stream(self, messages, tools, body=None, model=None):
            if "wait" in str(messages[-1].get("content")):
                await go.wait()
            async for ev in super().stream(messages, tools, body):
                yield ev
    asks = SimpleNamespace(waiting={})
    d = dispatcher_mod.Dispatcher(store, cfg, EventBus(), Blocks, None)
    d.approvals = asks
    first, second = await conversations.create(store), await conversations.create(store)
    async with anyio.create_task_group() as tg:
        await d.start(tg)
        await user_says(store, first, "wait for me")
        d.notify()
        await until(lambda: len(d._active) == 1)
        asks.waiting["a"] = SimpleNamespace(expires=None)   # its tool asks the user
        d.resize()
        await user_says(store, second, "hello")
        d.notify()
        await until(lambda: _done(store, second))
        assert not await _done(store, first), "still waiting for the answer"
        go.set()
        await until(lambda: _done(store, first))
        await d.stop()
        tg.cancel_scope.cancel()
