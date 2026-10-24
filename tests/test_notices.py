"""The `notice` events the app shows as system notifications (events.notice): one per
item, with the server's link and a tag per item."""
from __future__ import annotations

import json

import pytest

from chat_fixtures import Script, user_says
from celine.backends.base import Done, TextDelta
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.intercom import Intercom
from celine.missions import store as missions
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.skills import Registry

from jobs_fixtures import cfg  # noqa: F401  (the fixture)

pytestmark = pytest.mark.anyio


async def _notices(store) -> list[dict]:
    return await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT data FROM global_events WHERE type='notice' ORDER BY id")])


async def _reply(store, cfg, cid: str, text: str) -> None:
    await user_says(store, cid, "how was it?")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, Script([[TextDelta(text), Done(reason="stop")]]),
                   EventBus(), Token(turn["id"]), Registry())


async def test_her_finished_reply_in_a_chat_is_one_notice_and_a_missions_none(store, cfg):
    cid = await conversations.create(store)
    await _reply(store, cfg, cid, "\nit went fine\nthe rest of it")
    [n] = await _notices(store)
    assert n == {"kind": "reply", "title": n["title"], "body": "it went fine",
                 "link": f"/chat/{cid}", "tag": f"reply:{cid}"}
    mission = await conversations.create(store, kind="mission")
    await _reply(store, cfg, mission, "working on it")
    assert len(await _notices(store)) == 1, "a mission's turn is no reply notice"


async def test_an_intercom_message_to_you_is_a_notice_yours_and_later_ones_not(store):
    ic = Intercom(store)
    cid = await conversations.create(store)
    sent = await ic.send(f"agent:{cid}", "user", "the cars are in\nmore", subject=None)
    await ic.send("user", f"agent:{cid}", "thanks")
    await ic.send(f"agent:{cid}", "user", "tomorrow", due_at="2999-01-01T00:00:00+00:00")
    [n] = await _notices(store)
    assert n["kind"] == "intercom" and n["body"] == "the cars are in"
    assert n["link"] == f"/intercom/{sent['intercom_id']}" == "/intercom/" + n["tag"].split(":")[1]


async def test_a_mission_that_waits_for_you_is_a_notice_with_its_link(store):
    cid = await conversations.create(store, "RSS feed", kind="mission")
    await missions.create(store, cid, {"name": "RSS feed", "terminal": "rss", "brief": "b"})
    await store.write(lambda c: missions.set_status_sync(c, store, cid, "needs_you", "A key."))
    await store.write(lambda c: missions.set_status_sync(c, store, cid, "active", None))
    assert await _notices(store) == [{"kind": "mission", "title": "RSS feed", "body": "A key.",
                                      "link": f"/mission/{cid}", "tag": f"mission:{cid}"}]


async def test_an_approval_opens_where_it_is_answered(store):
    import asyncio
    from types import SimpleNamespace

    from celine.approvals import Approvals, Ask
    cid = await conversations.create(store)
    a = Approvals(SimpleNamespace(user="sam", name="Celine"), store=store)
    fut = asyncio.get_running_loop().create_future()
    for kind in ("vault", "gate"):
        await a._notice(Ask(id=kind, kind=kind, action="do it", facts=[], why="", short="it",
                            conversation=cid, read=None, expires=0, answer=fut))
    assert [(n["title"], n["link"], n["tag"]) for n in await _notices(store)] == [
        ("Vault", "/settings/vault", "ask:vault"), ("Bastet", f"/chat/{cid}", "ask:gate")]
