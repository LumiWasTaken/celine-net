"""Config reload in place, event pruning."""
from __future__ import annotations

import time

import pytest

from celine.conversations import store as conversations, view
from celine import reload
from celine.config import Problem
from celine.boot import load
from celine.core.services import Services
from celine.skills import build as build_registry

pytestmark = pytest.mark.anyio


async def test_a_skill_rebuilt_by_a_reload_reaches_the_running_dispatcher(store, monkeypatch):
    cfg = load()
    cfg.skills = {**cfg.skills, "sub_agents": {"enabled": True}}
    app = Services(cfg=cfg, store=store)
    app.registry = build_registry(app)
    old = app.registry.skills["sub_agents"]

    fresh = load()
    fresh.settings.general.update({"max_tool_iterations": 3})
    fresh.skills = {**fresh.skills, "sub_agents": {"enabled": True},
                    "web": {"enabled": False}}
    monkeypatch.setattr(reload.boot, "load", lambda: fresh)
    assert reload.apply(app)
    assert cfg.r("max_tool_iterations") == 3, "the same object, changed in place"
    assert "web__search" not in app.registry.tools
    assert "notebook__write" in app.registry.tools
    rebuilt = app.registry.skills["sub_agents"]
    assert rebuilt is not old
    app.dispatcher = object()           # started after the reload, handed to no one
    assert rebuilt.dispatcher is app.dispatcher

    broken = load()
    broken.problems.append(Problem("config/celine.toml", "boom", fatal=True))
    broken.settings.general.update({"max_tool_iterations": 99})
    monkeypatch.setattr(reload.boot, "load", lambda: broken)
    assert not reload.apply(app)
    assert cfg.r("max_tool_iterations") == 3, "a broken file keeps the last good values"


async def test_prune_keeps_the_tail_and_forces_a_resync_below_it(store):
    cid = await conversations.create(store)
    for i in range(30):
        await store.append_event(cid, "note", {"i": i})
    assert await conversations.prune_events(store, keep=10) == 20
    assert (await view.snapshot(store, cid))["seq"] == 30
    assert (await view.resume(store, cid, after_seq=5)).get("resync") == "history_trimmed"
    assert len((await view.resume(store, cid, after_seq=25))["events"]) == 5


async def test_a_reload_keeps_what_the_shell_told_her_and_moves_the_clock(store, monkeypatch):
    cfg = load()
    app = Services(cfg=cfg, store=store)
    app.registry = build_registry(app)
    app.registry.skills["shell"].told.add(("c", "job"))
    fresh = load()
    fresh.timezone = "Asia/Tokyo"
    monkeypatch.setattr(reload.boot, "load", lambda: fresh)
    assert reload.apply(app)
    assert ("c", "job") in app.registry.skills["shell"].told
    assert time.strftime("%Z") == "JST", "the archive's clock is the app's"


async def test_turns_at_once_follows_a_save(store):
    """Settings, Advanced: more workers when it is raised; lowered, the extra ones stay
    and claim nothing (the worker loop checks its place against the number)."""
    import anyio
    from celine.core.bus import EventBus
    from celine.skills import Registry
    from celine.turns.dispatcher import Dispatcher
    cfg = load()
    cfg.settings.general["max_concurrent_turns"] = 1
    d = Dispatcher(store, cfg, EventBus(), lambda: None, Registry())
    async with anyio.create_task_group() as tg:
        await d.start(tg)
        assert len(d._workers) == 1
        cfg.settings.general["max_concurrent_turns"] = 3
        d.resize()
        assert len(d._workers) == 3
        cfg.settings.general["max_concurrent_turns"] = 2
        d.resize()
        assert len(d._workers) == 3 and d._cap() == 2
        tg.cancel_scope.cancel()
