"""Missions end to end: the API starts one, its tool reports to the report conversation,
and a stop ends the running turn, its job and the wake-up."""
from __future__ import annotations

from chat_fixtures import user_says

from types import SimpleNamespace

import anyio
import httpx
import pytest
from fastapi import FastAPI

from celine.conversations import store as conversations, view
from celine.missions import api as missions_api
from celine.errors import install_error_handler
from celine.boot import load
from celine.core.services import Services
from celine.core.store import Store
from celine.core.bus import EventBus
from celine.intercom import Intercom
from celine.jobs.jobs import Jobs
from celine.missions import store as missions
from celine.skills import ToolContext
from celine.missions.skill import Missions
from celine.turns.dispatcher import Dispatcher
from celine.turns import store as turns

BRIEF = {"name": "RSS feed", "brief": "The feed lists every published post again.\n"
                                       "Work on a branch, never push to master."}


async def _turns(store, cid: str) -> list[tuple[str, str]]:
    return await store.read(lambda c: [tuple(r) for r in c.execute(
        "SELECT origin, state FROM turns WHERE conversation_id=? ORDER BY created_at", (cid,))])


@pytest.mark.anyio
async def test_a_mission_starts_reports_and_stops(db_path):
    async with anyio.create_task_group() as tg:
        bus = EventBus()
        store = Store(db_path, bus=bus)
        await store.start(tg)
        cfg = load()
        notified: list[int] = []
        intercom = Intercom(store, lambda: notified.append(1))
        app = FastAPI()
        app.state.services = Services()
        install_error_handler(app)
        app.include_router(missions_api.router)
        app.state.services.store, app.state.services.cfg = store, cfg
        app.state.services.intercom = intercom
        dispatcher = Dispatcher(store, cfg, bus)
        dispatcher.notify = lambda: notified.append(1)
        dispatcher.jobs = Jobs(store, cfg, dispatcher.notify, tg)
        app.state.services.dispatcher = dispatcher
        report = await conversations.create(store, "Site checks")
        await user_says(store, report, "hi")
        turn = await turns.claim(store, "w")              # the report conversation is busy
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            r = await c.post("/api/missions", json={**BRIEF, "report_to": report,
                                                    "time_limit": "2 h"})
            assert r.status_code == 200, r.text
            cid = r.json()["conversation_id"]
            row = await missions.get(store, cid)
            assert (row["terminal"], row["status"]) == ("rss-feed", "active")
            wake = await store.read(lambda c: c.execute(
                "SELECT kind, from_addr, due_at, cancelled_at FROM intercom WHERE id=?",
                (int(row["wake_id"]),)).fetchone()[:])
            assert wake[:2] == ("notice", "system:app") and wake[3] is None
            first = (await view.snapshot(store, cid))["messages"][0]
            assert (first["role"], first["author"]) == ("user", "intercom")
            assert (first["intercom"]["kind"], first["intercom"]["from"]) == ("brief", "user")
            assert f"Brief:\n{BRIEF['brief']}" in first["content"]
            assert await _turns(store, cid) == [("intercom", "queued")] and notified == [1]

            # its tool: refused elsewhere; a note waits for the report conversation's turn
            skill = Missions(cfg)
            skill.app = Services(intercom=intercom, cfg=cfg)
            other = ToolContext(report, "t", "k", store, cfg, None)
            assert (await skill.update(other, status="Done", note="x")).is_error
            out = await skill.update(ToolContext(cid, "t", "k", store, cfg, None),
                                     status="Needs you", note="The MR is up.")
            assert not out.is_error and (await missions.get(store, cid))["status"] == "needs_you"
            assert len((await view.snapshot(store, report))["messages"]) == 1, "it waits"
            await turns.transition(store, turn["id"], report, "done")
            await dispatcher.deliver_idle(report)        # as at the turn's end
            note = (await view.snapshot(store, report))["messages"][-1]
            assert (note["author"], note["role"]) == ("intercom", "user")
            assert note["content"] == "Mission RSS feed needs you: The MR is up."
            assert (note["intercom"]["kind"], note["intercom"]["data"]) == \
                ("report", {"mission": cid})
            assert await _turns(store, report) == [("user", "done")], "no turn for a note"

            # the mission works: its turn is running and has a step in a job
            work = await turns.claim(store, "w2")
            assert work["conversation_id"] == cid
            token = dispatcher.tokens.open(work["id"], False)
            forever = anyio.Event()

            async def step(ctx):
                await forever.wait()
            job = dispatcher.jobs.begin(
                SimpleNamespace(fn=step), ToolContext(cid, work["id"], "k", store, cfg, None),
                {}, {"id": "c1", "name": "shell__run"},
                {"id": work["id"], "conversation_id": cid})

            assert (await c.post(f"/api/missions/{cid}/stop")).json() == {"ok": True}
            row = await missions.get(store, cid)
            assert (row["status"], row["wake_id"]) == ("stopped", None)
            assert await store.read(lambda c: c.execute(
                "SELECT text FROM intercom WHERE kind='report' AND to_addr=? ORDER BY id DESC",
                (f"agent:{report}",)).fetchone()[0]) == \
                cfg.substitute("Mission RSS feed stopped by {{username}}."), \
                "the chat it reports to hears it stopped"
            assert await store.read(lambda c: c.execute(
                "SELECT cancelled_at IS NOT NULL FROM intercom WHERE kind='notice'"
            ).fetchone()[0]), "the time limit is cancelled, its row stays"
            assert token.cancelled, "the running turn is told to stop"
            with anyio.fail_after(2):
                await job.done.wait()
            assert job.state == "cancelled"
            # a stopped mission takes no change
            assert (await c.patch(f"/api/missions/{cid}", json={"brief": "x"})).status_code == 409
            # a model picked at the start
            bad = await c.post("/api/missions", json={**BRIEF, "model": "nope/x"})
            assert bad.status_code == 400 and "There is no model 'nope/x'" in bad.text
            cfg.settings.model = lambda ref: {"caps": ["chat"]} if ref == "or/glm" else None
            r = await c.post("/api/missions", json={**BRIEF, "model": "or/glm"})
            picked = r.json()["conversation_id"]
            assert await store.read(lambda c: c.execute(
                "SELECT model_id FROM conversations WHERE id=?", (picked,)).fetchone()[0]) \
                == "or/glm", "its first turn runs on the model picked at the start"
        await store.aclose()


@pytest.mark.anyio
async def test_done_cancels_what_the_mission_scheduled_and_stopped_takes_no_retry(store):
    from celine.conversations import tree
    cfg = load()
    intercom = Intercom(store)
    cid = await conversations.create(store, "RSS feed")
    await missions.create(store, cid, {"name": "RSS feed", "terminal": "rss-feed",
                                       "brief": "g"})
    later = "2999-01-01T00:00:00+00:00"
    limit = (await intercom.send("system:app", f"agent:{cid}", "Time limit reached.",
                                 kind="notice", due_at=later))["intercom_id"]
    mine = (await intercom.send(f"agent:{cid}", "me", "check the build",
                                due_at=later))["intercom_id"]
    for_user = (await intercom.send(f"agent:{cid}", "user", "look at the MR",
                                    due_at=later))["intercom_id"]
    skill = Missions(cfg)
    skill.app = Services(intercom=intercom)
    out = await skill.update(ToolContext(cid, "t", "k", store, cfg, None),
                             status="Done", note="The feed works.")
    assert not out.is_error
    gone = await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT id FROM intercom WHERE cancelled_at IS NOT NULL ORDER BY id")])
    assert gone == [limit, mine, for_user], "cancelled, and the rows stay"

    # a stopped mission's conversation runs nothing again
    for text in ("first", "one more"):
        await user_says(store, cid, text)
        await store.write(lambda c: c.execute("UPDATE turns SET state='done'"))
    second = (await view.snapshot(store, cid))["messages"][-1]["id"]
    await store.write(lambda c: missions.set_status_sync(c, store, cid, "stopped", None))
    with pytest.raises(ValueError, match="stopped"):
        await tree.retry(store, cid, second)


@pytest.mark.anyio
async def test_a_reply_to_her_message_resumes_a_stopped_mission(store):
    """The user's reply to what it sent from a stopped mission makes it active again,
    wakes it and puts its time limit back; a message without reply_to wakes nothing."""
    intercom = Intercom(store)
    cid = await conversations.create(store, "RSS feed")
    later = "2999-01-01T00:00:00.000+00:00"
    await missions.create(store, cid, {"name": "RSS feed", "terminal": "rss-feed",
                                       "brief": "g", "until": later})
    asked = (await intercom.send(f"agent:{cid}", "user", "which branch?"))["intercom_id"]
    await missions.update(store, cid, {"status": "stopped", "note": "x", "wake_id": None})
    await intercom.send("user", f"agent:{cid}", "a plain message")
    assert (await missions.get(store, cid))["status"] == "stopped"
    assert await _turns(store, cid) == []
    out = await intercom.send("user", "", "the feed branch", reply_to=asked)
    row = await missions.get(store, cid)
    assert (row["status"], row["note"]) == ("active", None)
    assert out["turn_id"] and await _turns(store, cid) == [("user", "queued")]
    wake = await store.read(lambda c: c.execute(
        "SELECT kind, from_addr, due_at, cancelled_at FROM intercom WHERE id=?",
        (int(row["wake_id"]),)).fetchone()[:])
    assert wake == ("notice", "system:app", "2998-12-31T23:45:00.000+00:00", None), \
        "the time limit is back"


@pytest.mark.anyio
async def test_only_a_mission_carries_missions_update(store):
    from celine.skills import Registry
    from celine.turns import loads
    from celine.turns.prompt import tool_specs as _specs
    cfg = load()
    registry = Registry()
    registry.add(Missions(cfg))

    async def tools(cid):
        carried = await loads.select(store, cfg, registry, cid, None)
        return (await _specs(registry, store, cid, carried))[0]
    plain = await conversations.create(store, "Look around")
    assert "missions__update" not in await tools(plain)
    cid = await conversations.create(store, "Prices", kind="mission")
    await missions.create(store, cid, {"name": "Prices", "terminal": "prices",
                                       "brief": "g"})
    assert "missions__update" in await tools(cid)


@pytest.mark.anyio
async def test_she_plans_then_records_what_she_delivered(store):
    cfg = load()
    cid = await conversations.create(store, "Prices")
    await missions.create(store, cid, {"name": "Prices", "terminal": "prices",
                                       "brief": "Compare the three offers."})
    skill = Missions(cfg)
    ctx = ToolContext(cid, "t", "k", store, cfg, None)
    before = await skill.prompt_fragment(ctx)
    assert "Brief:\nCompare the three offers." in before and "No plan yet." not in before
    assert "No plan yet." in await skill.tail_part(ctx)
    from celine.plans.skill import Plan
    assert not (await Plan(cfg).set(ctx, steps=[{"title": "Read the offers"},
                                                {"title": "A table"}])).is_error
    assert "No plan yet." not in await skill.tail_part(ctx)
    assert await skill.prompt_fragment(ctx) == before, "its state goes in the tail"
    assert (await skill.update(ctx, status="Done", note="x", artifact="a table")).is_error
    out = await skill.update(ctx, status="Done", note="The table is up.",
                             artifact="documents:offers/compare.md")
    row = await missions.get(store, cid)
    assert not out.is_error and (row["status"], row["artifact"]) == (
        "done", "documents:offers/compare.md")


def test_a_mission_without_a_name_takes_the_briefs_first_sentence_cut_at_a_word():
    from celine.missions.api import title_of
    assert title_of("Fix the  backup\nof the NAS. Then the router.") == \
        "Fix the backup of the NAS."
    assert title_of("The feed lists every published post again and again, and the "
                    "old ones too") == \
        "The feed lists every published post again and again, and the..."
    assert title_of("x" * 70) == "x" * 61 + "..."
