"""The work queue (work.py) and its pause (hold.py): leases, tries, deferral until a
host is back, and no turn claimed while no chat host answers. Fixed clocks, fake
servers."""
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
from celine.backends.router import NoModel, Router
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.hold import her_hold
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
        "SELECT state, tries, error, lease_worker FROM work WHERE id=?", (wid,)).fetchone()))


def _gateway(tmp_path, monkeypatch, roles, events=None):
    """An LLM over PROVIDERS whose hosts answer only while `up` says so; pinged."""
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
    router._pinging = True                  # as the app's ping does

    async def ping() -> None:               # the next ping, PING_EVERY later
        for st in router._providers.values():
            st.checked -= 3600
        await router.ping_once()
    return cfg, llm, up, ping


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


async def test_a_real_error_fails_after_three_tries_with_its_reason(store):
    now = [T0]
    pool = work.Pool(store, clock=lambda: now[0])

    async def boom(_p):
        raise RuntimeError("the answer was not JSON")
    pool.handle("x", (), boom)
    wid = await pool.put("x", {})
    states = []
    for _ in range(work.MAX_TRIES):
        item = await work.claim(store, "w", ["x"], now=now[0])
        states.append(await pool.run_one(item, "w"))
        assert await work.claim(store, "w", ["x"], now=now[0] + s(1)) is None, "not at once"
        now[0] += s(work.RETRY_SECONDS)
    assert states == ["queued", "queued", "failed"]
    row = await _row(store, wid)
    assert (row["state"], row["tries"]) == ("failed", 3)
    assert row["error"] == "RuntimeError: the answer was not JSON"
    assert await work.claim(store, "w", ["x"], now=now[0]) is None


async def test_a_job_whose_host_is_down_waits_and_runs_when_the_host_is_back(
        store, tmp_path, monkeypatch):
    _, llm, up, ping = _gateway(tmp_path, monkeypatch, {"small": {"chain": ["lan/small"]}})
    await ping()
    assert llm.router.hosts()["lan"]["state"] == "down"
    pool = work.Pool(store, llm, clock=lambda: T0)      # the clock never moves
    ran = []

    async def job(p):
        ran.append(p)
    pool.handle("title", ("small",), job)
    async with anyio.create_task_group() as tg:
        await pool.start(tg)
        wid = await pool.put("title", {"conversation_id": "c1"})
        await until(async_state(store, wid, "deferred"))
        assert ran == [], "no call to a host that is down"
        assert (await _row(store, wid))["tries"] == 0, "waiting spends no try"
        up["lan"] = True
        await ping()
        await until(async_state(store, wid, "done"))
        assert ran == [{"conversation_id": "c1"}]
        await pool.stop()
        tg.cancel_scope.cancel()


def async_state(store, wid, state):
    async def check():
        return (await _row(store, wid))["state"] == state
    return check


async def test_with_no_chat_host_she_pauses_and_resumes_by_herself(
        store, tmp_path, monkeypatch):
    events: list[dict] = []
    cfg, llm, up, ping = _gateway(tmp_path, monkeypatch, {
        "chat": {"chain": ["gpu/big"]}, "tasks": {"chain": ["gpu/big"]}}, events)
    await ping()
    assert await her_hold(store, llm) == "paused" and llm.router.hold()["state"] == "paused"
    assert [e["hold"]["state"] for e in events if e["type"] == "her.hold"] == ["paused"]
    looked = []

    async def hold(store_, llm_):
        looked.append(1)
        return await her_hold(store_, llm_)
    monkeypatch.setattr(dispatcher_mod, "her_hold", hold)
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
        await ping()
        assert await her_hold(store, llm) is None
        assert [e["hold"] for e in events if e["type"] == "her.hold"][-1] is None
        await until(lambda: _done(store, cid))
        await d.stop()
        tg.cancel_scope.cancel()


async def test_frozen_wins_over_paused(store, tmp_path, monkeypatch):
    cfg, llm, up, ping = _gateway(tmp_path, monkeypatch, {
        "chat": {"chain": ["gpu/big"]}, "tasks": {"chain": ["gpu/big"]}}, [])
    await ping()
    assert await her_hold(store, llm) == "paused"

    def froze(c):
        rid = c.execute("INSERT INTO freezes(at, by, why) VALUES("
                        "'2026-10-01T10:00:00.000+00:00', 'user', 'x')").lastrowid
        c.execute("INSERT INTO kv(k, v) VALUES('frozen', ?)", (str(rid),))
    await store.write(froze)
    assert await her_hold(store, llm) == "frozen"
    assert await her_hold(store, None) == "frozen"


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
    assert st.reachable is False and "rejected the API key" in st.error


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


def _titler(store, by_role):
    llm = SimpleNamespace(bind=lambda fixed, role, **kw: by_role[role],
                          router=SimpleNamespace(all_down=lambda roles: False))
    return dispatcher_mod.Dispatcher(store, load(), EventBus(), None, None, llm=llm)


async def test_a_title_waits_for_a_host_and_falls_back_to_the_first_line_on_an_error(
        store, tmp_path, monkeypatch):
    from celine.backends.base import BackendError, Done, TextDelta
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles={})
    none = NoModel("No model for titles and descriptions can answer now.")
    cid = await _exchange(store, 1)
    d = _titler(store, {"small": _Raises(none), "chat": _Raises(none)})
    d.work.clock = lambda: T0
    wid = await d.work.put("title", {"conversation_id": cid})
    item = await work.claim(store, "w", ["title"], now=T0)
    assert await d.work.run_one(item, "w") == "deferred", "no host answered: it waits"
    assert not await conversations.title(store, cid)
    assert (await _row(store, wid))["tries"] == 0

    d = _titler(store, {"small": _Raises(none),
                        "chat": FakeBackend([TextDelta(""), Done(reason="stop")])})
    await d._title(cid)
    assert await conversations.title(store, cid) == "where did noor go", \
        "a model answered with nothing: the first line, not another wait"

    cid = await _exchange(store, 2)
    bad = BackendError("bad request", retryable=False)
    d = _titler(store, {"small": _Raises(bad), "chat": _Raises(bad)})
    await d._title(cid)
    assert await conversations.title(store, cid) == "where did noor go"


async def test_her_appraisal_is_a_work_item_and_a_stale_one_makes_no_call(
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
