"""The tail of a turn is stored once on the message it answers (turns/message_context.py
put_tail): the next turn's request carries the earlier turn's message byte for byte, and
starts with every byte of the request before it."""
from __future__ import annotations

import json

import pytest

from chat_fixtures import user_says
from fake_backend import FakeBackend

from celine import clock
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn

pytestmark = pytest.mark.anyio


async def _turn(store, cfg, cid, backend, text):
    await user_says(store, cid, text)
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), None)


async def test_an_earlier_tail_is_byte_identical_in_the_next_request(store, monkeypatch):
    cfg = load()
    cid = await conversations.create(store)
    backend = FakeBackend()
    monkeypatch.setattr(clock, "now", lambda: "2026-10-23T10:38:00.000+00:00")
    await _turn(store, cfg, cid, backend, "first")
    monkeypatch.setattr(clock, "now", lambda: "2026-10-23T11:52:00.000+00:00")
    await _turn(store, cfg, cid, backend, "second")
    one, two = backend.calls
    assert "Now it is [Fri 23 Oct 10:38]" in one[-1]["content"]
    assert two[:len(one)] == one, "the second request starts with the first, byte for byte"
    assert "Now it is [Fri 23 Oct 11:52]" in two[-1]["content"]
    assert json.dumps(two).count("Now it is [") == 2, "one tail per turn, none moved"


async def test_a_regenerate_writes_a_fresh_tail(store, monkeypatch):
    from celine.conversations import tree
    cfg = load()
    cid = await conversations.create(store)
    backend = FakeBackend()
    monkeypatch.setattr(clock, "now", lambda: "2026-10-23T10:38:00.000+00:00")
    await _turn(store, cfg, cid, backend, "first")
    reply = await store.read(lambda c: c.execute(
        "SELECT id FROM messages WHERE role='assistant'").fetchone()[0])
    monkeypatch.setattr(clock, "now", lambda: "2026-10-23T11:52:00.000+00:00")
    await tree.retry(store, cid, reply)
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), None)
    again = backend.calls[-1][-1]["content"]
    assert "Now it is [Fri 23 Oct 11:52]" in again and "10:38]. Every" not in again


class Tools(FakeBackend):
    """Records the tools each request carried too."""

    def __init__(self):
        super().__init__()
        self.tools: list[list[dict]] = []

    async def stream(self, messages, tools, body=None, model=None):
        self.tools.append(list(tools or []))
        async for ev in super().stream(messages, tools, body, model):
            yield ev


async def test_live_state_between_two_turns_leaves_the_prefix_as_it_was(store):
    """An announcement lands, a sub-agent starts working and the mission's status moves
    between two turns of a mission: the system prompt and the tools are the same bytes,
    and the second turn's tail says what changed."""
    from datetime import datetime, timedelta, timezone

    from celine.core.ids import iso
    from celine.intercom import Intercom
    from celine.intercom.skill import IntercomSkill
    from celine.missions import store as missions
    from celine.missions.skill import Missions
    from celine.skills import Registry
    from celine.skills.meta import Skills
    from celine.sub_agents.skill import SubAgents
    cfg = load()
    reg = Registry(cfg)
    for s in (IntercomSkill(cfg), Missions(cfg), SubAgents(cfg)):
        reg.add(s)
    reg.add(Skills(reg, cfg))
    cid = await conversations.create(store, "Prices", kind="mission")
    await missions.create(store, cid, {"name": "Prices", "terminal": "prices",
                                       "brief": "Compare the three offers."})
    scout = await conversations.create(store, "s", agent_of=cid, agent_name="scout")
    backend = Tools()

    async def turn(text):
        await user_says(store, cid, text)
        t = await turns.claim(store, "w")
        await run_turn(t, store, cfg, backend, EventBus(), Token(t["id"]), reg)
    await turn("go")
    until = iso(datetime.now(timezone.utc) + timedelta(hours=1))
    await Intercom(store).send("system:updater", "all", "the server restarts soon.",
                               kind="notice", until_at=until)
    await user_says(store, scout, "work")
    await turns.claim(store, "w2")                  # the sub-agent's turn runs: working
    await store.write(lambda c: missions.set_status_sync(c, store, cid, "needs_you", "?"))
    await turn("and now?")

    one, two = backend.calls
    assert json.dumps(two[0]) == json.dumps(one[0]), "the system prompt"
    assert backend.tools[0] and json.dumps(backend.tools[1]) == json.dumps(backend.tools[0])
    before, now_ = one[-1]["content"], two[-1]["content"]
    assert "scout (done)" in before and "scout (working)" in now_
    assert "the server restarts soon." in now_ and "the server restarts soon." not in before
    assert "Status: " in before and "Status: " in now_
    assert before.split("Status: ")[1].split(".")[0] != now_.split("Status: ")[1].split(".")[0]
