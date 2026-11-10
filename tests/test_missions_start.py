"""The agent's missions__start (the user's start, from it) and a mission's minimum time: Done
before it takes force."""
from __future__ import annotations

import pytest

from celine.boot import load
from celine.conversations import store as conversations, view
from celine.core import ids
from celine.core.services import Services
from celine.intercom import Intercom
from celine.missions import store as missions
from celine.missions.skill import Missions
from celine.skills import ToolContext

pytestmark = pytest.mark.anyio


def _skill(store, cfg) -> Missions:
    skill = Missions(cfg)
    skill.app = Services(store=store, cfg=cfg, intercom=Intercom(store))
    return skill


async def test_the_agent_starts_a_mission_with_what_the_form_takes(store):
    cfg = load()
    skill = _skill(store, cfg)
    chat = await conversations.create(store, "Plans")
    ctx = ToolContext(chat, "t", "k", store, cfg, None)
    out = await skill.start(ctx, brief="Sort the photos by year.", name="Photos",
                            time_limit="3 h", min_time="2 h", check_in="30 min",
                            report_to=chat)
    assert not out.is_error, out.content
    cid = out.display["mission"]
    assert out.content == f"Started the mission Photos ({cid})."
    row = await missions.get(store, cid)
    assert (row["started_by"], row["report_to"], row["check_in"]) == (chat, chat, 30)
    assert row["min_until"] < row["until"]
    first = (await view.snapshot(store, cid))["messages"][0]
    assert first["intercom"]["from"] == f"agent:{chat}", "the brief is the agent's"
    assert first["content"].startswith("Mission Photos, from Celine.")
    assert "Minimum time: until " in first["content"]

    bad = await skill.start(ctx, brief="x", time_limit="1 h", min_time="2 h")
    assert bad.is_error and bad.content == "Minimum time ends after the time limit."
    assert (await skill.start(ctx, brief=" ")).is_error


async def test_start_is_offered_everywhere_but_in_a_sub_agent(store):
    skill = _skill(store, load())
    chat = await conversations.create(store, "Plans")
    sub = await conversations.create(store, "Helper")
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET agent_of=? WHERE id=?", (chat, sub)))
    assert await skill.offered_in(store, chat) == {"list", "read", "start"}
    assert await skill.offered_in(store, sub) == {"list", "read"}


async def test_done_before_the_minimum_time_takes_force(store, clock, monkeypatch):
    monkeypatch.setattr(ids, "now", clock.iso)
    cfg = load()
    skill = _skill(store, cfg)
    cid = await conversations.create(store, "Night", kind="mission")
    await missions.create(store, cid, {"name": "Night", "terminal": "night", "brief": "b",
                                       "min_until": clock.iso(5400)})
    ctx = ToolContext(cid, "t", "k", store, cfg, None)
    out = await skill.update(ctx, status="Done", note="all of it")
    local = ids.clock(clock.at.fromisoformat(clock.iso(5400)), skill.tz, clock.at)
    assert out.is_error and out.content == (
        f"The minimum time is until {local}, 1 h 30 min from now. Done before it takes "
        "force=true.")
    assert (await missions.get(store, cid))["status"] == "active"
    assert not (await skill.update(ctx, status="Needs you", note="a question")).is_error
    clock.advance(5400)
    assert not (await skill.update(ctx, status="Done", note="all of it")).is_error

    other = await conversations.create(store, "Day", kind="mission")
    await missions.create(store, other, {"name": "Day", "terminal": "day", "brief": "b",
                                         "min_until": clock.iso(60)})
    out = await skill.update(ToolContext(other, "t", "k", store, cfg, None), status="Done",
                             note="done early", force=True)
    assert not out.is_error and (await missions.get(store, other))["status"] == "done"


async def test_a_mission_on_a_cloud_model_the_agent_picked_asks_first(tmp_path, monkeypatch, store):
    from settings_fixtures import PROVIDERS, ROLES, scratch
    scratch(tmp_path, monkeypatch, providers={**PROVIDERS, "or": {
        "kind": "openai", "label": "Far", "base_url": "https://api.example.com/v1",
        "models": {"glm": {"label": "GLM", "caps": ["chat", "tools"]}}}},
        roles={**ROLES, "tasks": {"chain": ["main/big"]}})
    cfg = load()
    asked, answer = [], ["Lumi denied it; nothing was done."]

    class Approvals:
        async def ask(self, kind, conversation, action, *, facts=(), detail=None, **kw):
            asked.append((action, list(facts), detail))
            return answer[0]
    skill = Missions(cfg)
    skill.app = Services(store=store, cfg=cfg, intercom=Intercom(store),
                         approvals=Approvals())
    chat = await conversations.create(store, "Plans")
    ctx = ToolContext(chat, "t", "k", store, cfg, None)
    out = await skill.start(ctx, brief="Map the city.", model="or/glm")
    assert out.is_error and out.content == answer[0]
    assert asked == [("start a mission on a cloud model",
                      [("Mission", "Map the city."), ("Model", "GLM"), ("Provider", "Far")],
                      ("Instructions", "Map the city."))]
    assert await missions.list(store, "all", False, False) == [], "nothing started"
    out = await skill.start(ctx, brief="Map the town.", model="main/big")
    assert not out.is_error and len(asked) == 1, "a local model asks nothing"
