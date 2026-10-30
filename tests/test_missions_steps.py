"""Missions with a model per step, keep going (missions/watch.py), and the trash (missions/)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from settings_fixtures import scratch

from celine import boot
from celine.conversations import store as conversations
from celine.intercom import Intercom
from celine.missions import store as missions, watch
from celine.missions.skill import Missions
from celine.plans import store as plans
from celine.plans.skill import Plan
from celine.settings import conversations as conv
from celine.skills import ToolContext

pytestmark = pytest.mark.anyio


async def _mission(store, name="Learn Blender", report_to=None) -> str:
    cid = await conversations.create(store, name, kind="mission")
    await missions.create(store, cid, {"name": name, "terminal": "blender", "brief": "b",
                                       "report_to": report_to})
    return cid


async def test_a_step_with_a_model_switches_the_mission_to_it(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    cid = await _mission(store)
    skill, plan = Missions(cfg), Plan(cfg)
    ctx = ToolContext(cid, "t", "k", store, cfg, None)
    out = await plan.set(ctx, text="read, then build", steps=[
        {"title": "Read the docs", "model": "lan/small"}, {"title": "Build it"},
        {"title": "Write it up", "model": "main/big"}])
    assert not out.is_error, out.content
    assert "[>] 1. Read the docs (model lan/small)" in out.content
    row = await missions.get(store, cid)
    assert row["steps"][1] == {"title": "Build it", "state": "todo", "checkpoint": "none",
                               "model": None, "started_at": None, "done_at": None} \
        and row["step"] == 1
    assert row["plan"] == "read, then build"
    assert (await conv.get(store, cid))["model"] == "lan/small", "step 1 runs on its model"
    fragment = await skill.prompt_fragment(ctx)
    assert "Small (lan/small)" in fragment and "Emb" not in fragment, "chat models only"

    out = await plan.step(ctx, step=1, state="done")
    assert out.content.startswith("Step 1 is done.") and "[>] 2. Build it" in out.content
    assert (await conv.get(store, cid))["model"] == "lan/small", "no model: it stays"
    await plan.step(ctx, step=2, state="done")
    assert (await conv.get(store, cid))["model"] == "main/big"
    assert (await plan.step(ctx, step=9, state="done")).is_error
    bad = await plan.set(ctx, steps=[{"title": "x", "model": "main/emb"}])
    assert bad.is_error and "no chat model" in bad.content
    event = missions.state_event(await missions.get(store, cid))
    assert (event["step"], len(event["steps"]), event["auto_context"]) == (3, 3, True)

    # a sub-agent started now runs on the step's model, with the mission's switch
    from celine.sub_agents.skill import SubAgents
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET auto_context=0 WHERE id=?", (cid,)))
    sub = await conversations.create(store, "scout", agent_of=cid, agent_name="scout")
    agents = SubAgents(cfg)
    await agents._settings(store, cid, sub, await agents._model(store, cid, None, None))
    assert (await conv.get(store, sub))["model"] == "main/big"
    assert await store.read(lambda c: c.execute(
        "SELECT auto_context FROM conversations WHERE id=?", (sub,)).fetchone()[0]) == 0


async def _ended(store, tid: str, state: str = "done", tool: bool = False) -> None:
    """Turn `tid` over in `state`; with `tool` it called one."""
    def fn(c):
        c.execute("UPDATE turns SET state=? WHERE id=?", (state, tid))
        if tool:
            cid = c.execute("SELECT conversation_id FROM turns WHERE id=?",
                            (tid,)).fetchone()[0]
            c.execute("INSERT INTO messages(id, conversation_id, role, content, turn_id, "
                      "created_at) VALUES(?, ?, 'assistant', '', ?, "
                      "'2026-10-22T00:00:00+00:00')", (f"a-{tid}", cid, tid))
            c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, state) "
                      "VALUES(?, ?, 'shell__run', '{}', 'ok')", (f"tc-{tid}", f"a-{tid}"))
    await store.write(fn)


async def _lines(store, cid: str) -> list[int]:
    return [r[0] for r in await store.read(lambda c: c.execute(
        "SELECT id FROM intercom WHERE to_addr=? AND text=?",
        (f"agent:{cid}", watch.KEEP_GOING)).fetchall())]


async def _turn_of(store, iid: int) -> str | None:
    """The turn intercom message `iid` started."""
    return await store.read(lambda c: c.execute(
        "SELECT turn_id FROM intercom_deliveries WHERE intercom_id=?", (iid,)).fetchone()[0])


async def _started(store, cid: str) -> str:
    """The turn the brief starts in mission `cid`."""
    out = await Intercom(store).send("user", f"agent:{cid}", "go", kind="brief")
    return out["turn_id"]


async def test_a_mission_turn_that_ends_with_nothing_to_wake_it_goes_on_once(store):
    app = SimpleNamespace(store=store, intercom=Intercom(store), cfg=boot.load())
    report = await conversations.create(store, "Her chat")
    cid = await _mission(store, report_to=report)
    first = await _started(store, cid)
    await _ended(store, first)
    assert await watch.check(app, cid, first) == "line"
    assert len(await _lines(store, cid)) == 1
    # the line started its next turn: while it runs nothing more is sent
    nxt = await _turn_of(store, (await _lines(store, cid))[0])
    assert nxt and await watch.check(app, cid, first) is None

    # it did a step (a tool call) and stopped again: one more line
    await _ended(store, nxt, tool=True)
    assert await watch.check(app, cid, nxt) == "line"
    lines = await _lines(store, cid)
    assert len(lines) == 2
    empty = await _turn_of(store, lines[-1])

    # a continuation with no tool call: no further line, the mission needs the user
    await _ended(store, empty)
    assert await watch.check(app, cid, empty) == "needs_you"
    assert len(await _lines(store, cid)) == 2
    row = await missions.get(store, cid)
    assert (row["status"], row["note"]) == ("needs_you", watch.NOTE)
    said = await store.read(lambda c: c.execute(
        "SELECT text FROM intercom WHERE kind='report'").fetchone()[0])
    assert said == f"Mission Learn Blender needs you: {watch.NOTE}"


async def test_a_mission_with_a_wake_set_or_done_gets_no_line(store):
    intercom = Intercom(store)
    app = SimpleNamespace(store=store, intercom=intercom)
    waits = await _mission(store, "Watch prices")
    tid = await _started(store, waits)
    await _ended(store, tid)
    await intercom.send(f"agent:{waits}", f"agent:{waits}", "check again",
                        due_at="2999-01-01T00:00:00+00:00")
    assert await watch.check(app, waits, tid) is None

    done = await _mission(store, "Done one")
    tid = await _started(store, done)
    await _ended(store, tid)
    await store.write(lambda c: missions.set_status_sync(c, store, done, "done", "delivered"))
    assert await watch.check(app, done, tid) is None

    stopped = []
    for state in ("aborted", "interrupted"):
        cid = await _mission(store, f"Ended {state}")
        tid = await _started(store, cid)
        await _ended(store, tid, state)
        assert await watch.check(app, cid, tid) == "needs_you", f"{state} is not overridden"
        stopped.append(cid)
    assert await watch.check(app, await _mission(store, "Third"), None) is None, "a job end"
    for cid in (waits, done, *stopped):
        assert await _lines(store, cid) == []


async def test_an_unnamed_mission_is_named_by_its_title(store):
    cid = await conversations.create(store, "", auto=True, kind="mission")
    await missions.create(store, cid, {"name": "Fix the NAS.", "terminal": "m", "brief": "b"})
    assert (await missions.get(store, cid))["name"] == "Fix the NAS."
    await conversations.update(store, cid, title="NAS backups", auto=True)
    assert (await missions.get(store, cid))["name"] == "NAS backups"


T0 = "2026-10-23T08:00:00.000+00:00"


async def test_the_user_edits_the_steps_and_moves_the_step(tmp_path, monkeypatch, store):
    monkeypatch.setattr(plans, "now", lambda: T0)
    import httpx
    from fastapi import FastAPI

    from celine.core.services import Services
    from celine.errors import install_error_handler
    from celine.missions import api as missions_api
    scratch(tmp_path, monkeypatch)
    app = FastAPI()
    install_error_handler(app)
    app.include_router(missions_api.router)
    app.state.services = Services(cfg=boot.load(), store=store,
                                  intercom=Intercom(store))
    cid = await _mission(store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.patch(f"/api/missions/{cid}", json={
            "steps": [{"title": "Read", "state": "done"},
                      {"title": "Build", "state": "doing", "model": "main/big"}]})
        assert r.json() == {"ok": True}, r.text
        assert (await conv.get(store, cid))["model"] == "main/big"
        two = {"steps": [{"title": "a", "state": "doing"}, {"title": "b", "state": "doing"}]}
        assert (await c.patch(f"/api/missions/{cid}", json=two)).status_code == 400
        listed = (await c.get("/api/missions")).json()["missions"]
        assert (listed[0]["step"], listed[0]["steps"][0]["done_at"]) == (2, T0)
        assert (await c.get("/api/missions?trashed=1")).json()["missions"] == []
        await conversations.delete(store, cid)
        assert (await c.get("/api/missions")).json()["missions"] == []
        assert [m["conversation_id"] for m in (await c.get("/api/missions?trashed=1")
                                               ).json()["missions"]] == [cid]
