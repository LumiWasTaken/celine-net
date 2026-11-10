"""Waiting for you (waits.py): one list of everything that waits on the user, the
server's: a request as its card, a mission in Needs you with its note, a lost login with
its sentence, an Intercom thread that waits; never a run. `waits.changed` follows every
event that changes it."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anyio
import pytest
from chat_fixtures import until

from celine import approvals as ap, waits
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.core.store import Store
from celine.guard.owner import _SEAL, Owner
from celine.intercom import Intercom
from celine.missions import store as missions

pytestmark = pytest.mark.anyio


class Connectors:
    def states(self):
        return [{"kb": "wa", "platform": "whatsapp", "title": "WhatsApp",
                 "connection": {"state": "needs_login"}, "login_lost_at": 1},
                {"kb": "dc", "platform": "discord", "title": "Discord",
                 "connection": {"state": "needs_login"}, "login_lost_at": None}]


async def test_an_ask_a_mission_a_lost_login_and_a_thread_are_four_items(store):
    a = ap.Approvals(SimpleNamespace(user="sam", name="Celine"), store=store)
    chat = await conversations.create(store, "Plans")
    night = await conversations.create(store, "Night", kind="mission")
    await missions.create(store, night, {"name": "Night", "terminal": "n", "brief": "b"})
    await store.write(lambda c: missions.set_status_sync(
        c, store, night, "needs_you", "A question for you at step 3: Book the hotel."))
    run = await conversations.for_trigger(store, "run:r1", "Radar")
    await store.write(lambda c: c.execute(
        "INSERT INTO runs(id, schedule, label, slot, night, state, reason, conversation_id, "
        "created_at) VALUES('r1', 'radar', 'Radar', 's', '2026-10-22', 'failed', 'x', ?, "
        "'2026-10-22T23:00:00+00:00')", (run,)))
    await Intercom(store).send(f"agent:{chat}", "user", "I found three flats.\nmore")
    task = asyncio.create_task(a.ask("agent", chat, "change the Persona"))
    await until(lambda: a.waiting)
    services = SimpleNamespace(store=store, approvals=a, connectors=Connectors())
    got = await waits.items(services)
    assert [i["kind"] for i in got] == ["approval", "mission", "login", "thread"]
    assert [i["group"] for i in got] == ["Approvals", "Missions", "Sources", "Intercom"]
    assert got[0]["approval"]["title"] == "Celine asks to change the Persona."
    assert got[0]["approval"]["where"] == "Chat Plans"
    assert {k: got[1][k] for k in ("title", "why", "text", "asked", "origin", "link",
                                   "home")} == {
        "title": "Night", "why": "A question for you at step 3: Book the hotel.",
        "text": "", "asked": [], "origin": None,
        "link": f"/waiting/mission/{night}", "home": f"/mission/{night}"}
    assert got[2] == {"kind": "login", "id": "wa", "title": "WhatsApp", "group": "Sources",
                      "line": waits.LOST_BY["whatsapp"], "link": "/settings/sources/wa"}
    assert got[3]["line"] == "I found three flats. more"
    assert got[3]["link"] == f"/waiting/thread/{got[3]['id']}"
    assert got[3]["from"] == "Celine" and got[3]["at"]
    assert waits.counts(got) == {"Approvals": 1, "Missions": 1, "Sources": 1, "Intercom": 1}
    [aid] = a.waiting
    a.decide(aid, True, Owner("test", _SEAL))
    await task


async def test_a_bug_report_a_mission_filed_waits_as_its_own_thread(store):
    from celine.bugreports import store as bugs
    from celine.intercom import store as rows
    night = await conversations.create(store, "Night", kind="mission")
    await missions.create(store, night, {"name": "Night", "terminal": "n", "brief": "b"})
    report = await store.write(lambda c: bugs.file(
        c, store, night, "Writes come back short", "high", "A write lost its end.", "", ""))
    got = await waits.items(SimpleNamespace(store=store, approvals=None, connectors=None))
    assert [(i["kind"], i["title"], i["line"]) for i in got] == [
        ("thread", f"Bug #{report['id']}: Writes come back short", "High: A write lost its end.")]
    assert await store.write(lambda c: rows.read_from(c, f"agent:{night}")) == []


async def test_an_archived_mission_waits_for_nobody(store):
    night = await conversations.create(store, "Night", kind="mission")
    await missions.create(store, night, {"name": "Night", "terminal": "n", "brief": "b"})
    await store.write(lambda c: missions.set_status_sync(c, store, night, "needs_you", "A question."))
    services = SimpleNamespace(store=store, approvals=None, connectors=None)
    assert [i["kind"] for i in await waits.items(services)] == ["mission"]
    await conversations.archive(store, night, True)
    assert await waits.items(services) == []
    assert {"conversation.archived", "conversation.unarchived"} <= waits.CHANGES


async def test_nothing_waiting_is_an_empty_list(store):
    services = SimpleNamespace(store=store, approvals=None, connectors=None)
    assert await waits.items(services) == []


async def test_waits_changed_follows_each_event_that_changes_the_list(db_path):
    bus = EventBus()
    async with anyio.create_task_group() as tg:
        store = Store(db_path, bus=bus)
        await store.start(tg)
        tg.start_soon(waits.run, store, bus)
        await until(lambda: bus._subs["global"])
        seen: list[str] = []
        tx, rx = bus.subscribe("global")

        async def watch():
            async for ev in rx:
                seen.append(ev["type"])
        tg.start_soon(watch)
        store.emit_global({"type": "approvals.changed"})
        store.emit_global({"type": "kb.changed"})
        await until(lambda: "waits.changed" in seen)
        assert seen.count("waits.changed") == 1
        tg.cancel_scope.cancel()


async def test_a_mission_says_what_it_asks_and_where_it_was_started(store):
    """What it wrote the user that waits is its text, the newest its reply_to; one
    started from a chat names that chat. The user's reply reads all of it; a stopped
    mission waits for nobody."""
    services = SimpleNamespace(store=store, approvals=None, connectors=None)
    intercom = Intercom(store)
    chat = await conversations.create(store, "Plans")
    night = await conversations.create(store, "Night", kind="mission")
    await missions.create(store, night, {"name": "Night", "terminal": "n", "brief": "b",
                                         "started_by": chat})
    first = (await intercom.send(f"agent:{night}", "user", "Which hotel?"))["intercom_id"]
    second = (await intercom.send(f"agent:{night}", "user", "And which night?"))["intercom_id"]
    [item] = await waits.items(services)
    assert (item["why"], item["text"], item["asked"]) == (
        "", "Which hotel?\n\nAnd which night?", [first, second])
    assert item["origin"] == {"name": "Plans", "link": f"/chat/{chat}"} and item["open"]
    await intercom.send("user", "", "The Adler, Friday.", reply_to=second)
    assert await waits.items(services) == []

    await store.write(lambda c: missions.set_status_sync(c, store, night, "needs_you", "x"))
    assert len(await waits.items(services)) == 1
    await store.write(lambda c: missions.set_status_sync(c, store, night, "done", None))
    await intercom.send(f"agent:{night}", "user", "The table is in the result.")
    [done] = await waits.items(services)
    assert not done["open"], "done, and what it wrote waits: no Mark as done"
    await store.write(lambda c: missions.set_status_sync(c, store, night, "stopped", None))
    assert await waits.items(services) == []

