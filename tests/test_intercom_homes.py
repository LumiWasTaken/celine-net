"""Every message to the user has a home, and the answer goes back there: a chat's message
to the user is its message in that chat (at its time when it has one), a background
task's or a run's starts a chat of its own, a mission's waits on the mission (Waiting
for you lists the mission once), and the app's notices into a mission are its
transcript, no thread."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from chat_fixtures import user_says

from celine import boot, waits
from celine.conversations import store as conversations, view
from celine.intercom import Intercom, deliver
from celine.intercom import store as rows
from celine.intercom.skill import IntercomSkill
from celine.missions import store as missions
from celine.skills import ToolContext
from celine.turns import context
from fake_backend import FakeBackend

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _intercom_clock(clock, monkeypatch):
    """Delivery reads the test's clock: nothing comes due by the wall clock."""
    for mod in (deliver, rows):
        monkeypatch.setattr(mod, "now", clock.iso)


def _skill(store):
    cfg = boot.load()
    skill = IntercomSkill(cfg)
    skill.app = SimpleNamespace(intercom=Intercom(store))
    return cfg, skill


async def _notices(store, kind: str) -> list[dict]:
    return [d for d in await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT data FROM global_events WHERE type='notice' ORDER BY id")])
        if d["kind"] == kind]


async def test_from_a_chat_its_words_there_reach_the_user_and_a_timed_one_lands_there(store, clock):
    cfg, skill = _skill(store)
    chat = await conversations.create(store, "Plans")
    ctx = ToolContext(chat, "t", "k", store, cfg, None)

    now_ = await skill.send(ctx, to="user", text="the flat is free")
    assert not now_.is_error and now_.content.startswith(f"This chat is where {cfg.user} reads you")
    assert await store.read(lambda c: c.execute("SELECT count(*) FROM intercom").fetchone()[0]) == 0

    later = await skill.send(ctx, to="user", text="time to call the landlord", in_="30 min")
    iid = later.display["id"]
    assert later.content.startswith(f"#{iid} lands ") and "in this chat as your message" in later.content
    assert f"#{iid} " in await skill.tail_part(ctx) and f"to {cfg.user}, in this chat" in await skill.tail_part(ctx)
    assert await store.read(lambda c: rows.threads(c, None)) == [], "no Intercom thread"

    # due while a turn of the chat is live: the turn does not take it in
    _, tid, _ = await user_says(store, chat, "and the deposit?")
    await store.write(lambda c: c.execute("UPDATE intercom SET due_at=? WHERE id=?",
                                          (clock.iso(-1), iid)))
    assert await deliver.take(store, chat, tid, None) == []
    await deliver.deliver_due(store)
    assert [m["role"] for m in (await view.snapshot(store, chat))["messages"]] == ["user"]

    await store.write(lambda c: c.execute("UPDATE turns SET state='done' WHERE id=?", (tid,)))
    await deliver.deliver_idle(store, chat)
    last = (await view.snapshot(store, chat))["messages"][-1]
    assert (last["role"], last["author"], last["content"]) == ("assistant", "model",
                                                               "time to call the landlord")
    [n] = await _notices(store, "reply")
    assert (n["body"], n["link"]) == ("time to call the landlord", f"/chat/{chat}")
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM turns WHERE conversation_id=?", (chat,)).fetchone()[0]) == 1, \
        "it lands without a turn"


async def test_a_chat_turn_that_answers_their_thread_reply_writes_into_that_thread(store):
    cfg, skill = _skill(store)
    chat = await conversations.create(store, "Plans")
    asked = await skill.app.intercom.send(f"agent:{chat}", "user", "Shall I book it?")
    reply = await skill.app.intercom.send("user", "", "yes", reply_to=asked["intercom_id"])
    ctx = ToolContext(chat, reply["turn_id"], "k", store, cfg, None)
    out = await skill.send(ctx, to="user", text="Booked.")
    assert not out.is_error and out.content.endswith(f"in thread #{asked['intercom_id']}.")


async def test_from_a_run_it_starts_a_chat_that_names_the_run(store):
    cfg, skill = _skill(store)
    run = await conversations.for_trigger(store, "run:r1", "Radar")
    await store.write(lambda c: c.execute(
        "INSERT INTO runs(id, schedule, label, slot, night, state, conversation_id, "
        "created_at) VALUES('r1', 'radar', 'Radar', 's', '2026-10-22', 'running', ?, "
        "'2026-10-22T23:00:00+00:00')", (run,)))
    ctx = ToolContext(run, "t", "k", store, cfg, None)

    out = await skill.send(ctx, to="user", text="A paper on your topic.", subject="New paper")
    assert not out.is_error and out.content.startswith(f"Started a chat for {cfg.user}")
    new, iid = out.display["chat"], out.display["id"]
    sent = await store.read(lambda c: c.execute(
        "SELECT from_addr, to_addr, subject FROM intercom WHERE id=?", (iid,)).fetchone())
    assert tuple(sent) == (f"agent:{new}", "user", "New paper"), "its home is the new chat"
    assert (await store.read(lambda c: rows.thread_source(c, iid)))["kind"] == "chat"
    built = await context.build(store, cfg, FakeBackend(), new, system="", tools=[])
    first, answer = [m for m in built.messages if m["role"] != "system"][:2]
    assert 'past_conversations__read(conversation="run:r1")' in first["content"]
    assert answer["content"] == "A paper on your topic."

    # the user's answer lands in that chat, not in the run
    reply = await skill.app.intercom.send("user", "", "send me the link", reply_to=iid)
    assert reply["message_id"] and await store.read(lambda c: c.execute(
        "SELECT conversation_id FROM messages WHERE id=?", (reply["message_id"],)).fetchone()[0]) == new

    # for later: the chat starts now, its notice and its wait come at the time, and the
    # result says so
    later = await skill.send(ctx, to="user", text="Your dentist is at 9.", in_="6 h")
    assert "the chat is in their list now, and its notice reaches them" in later.content
    due = await store.read(lambda c: c.execute(
        "SELECT due_at FROM intercom WHERE id=?", (later.display["id"],)).fetchone()[0])
    assert due is not None
    # no subject: the chat and the message it waits with take the run's own title
    title = await store.read(lambda c: c.execute(
        "SELECT title FROM conversations WHERE id=?", (run,)).fetchone()[0])
    assert title and await store.read(lambda c: c.execute(
        "SELECT title FROM conversations WHERE id=?", (later.display["chat"],)).fetchone()[0]) == title
    assert await store.read(lambda c: c.execute(
        "SELECT subject FROM intercom WHERE id=?", (later.display["id"],)).fetchone()[0]) == title


async def test_a_missions_message_waits_on_the_mission_once(store):
    intercom = Intercom(store)
    night = await conversations.create(store, "Night", kind="mission")
    await missions.create(store, night, {"name": "Night", "terminal": "n", "brief": "b"})
    chat = await conversations.create(store, "Plans")
    asked = await intercom.send(f"agent:{night}", "user", "Which hotel, the cheap one?")
    flats = await intercom.send(f"agent:{chat}", "user", "I found three flats.")
    services = SimpleNamespace(store=store, approvals=None, connectors=None)

    got = await waits.items(services)
    assert [(i["kind"], i["line"]) for i in got] == [
        ("mission", "Which hotel, the cheap one?"), ("thread", "I found three flats.")]
    assert (got[0]["title"], got[0]["link"]) == ("Night", f"/waiting/mission/{night}")
    await store.write(lambda c: missions.set_status_sync(c, store, night, "needs_you", "Pick one."))
    again = await waits.items(services)
    assert [i["kind"] for i in again] == ["mission", "thread"], "still once"
    assert again[0]["asked"] == [asked["intercom_id"]], "the mission's page reads what waits"
    assert [t["thread"] for t in await store.read(lambda c: rows.threads(
        c, "user", unread=True, received=True))] == [flats["intercom_id"]], \
        "the Intercom's Waiting for you leaves it to the mission"

    # the page's reply answers it: its thread stops waiting
    await intercom.send("user", "", "the cheap one", reply_to=asked["intercom_id"])
    assert [i["kind"] for i in await waits.items(services)] == ["thread"]


async def test_the_apps_notices_into_a_mission_are_no_threads(store):
    intercom = Intercom(store)
    night = await conversations.create(store, "Night", kind="mission")
    await missions.create(store, night, {"name": "Night", "terminal": "n", "brief": "b"})
    chat = await conversations.create(store, "Plans")
    await intercom.send("system:app", f"agent:{night}", "Do the next step.", kind="notice",
                        data={"mission": night, "keep_going": True}, wake=False)
    kept = await intercom.send("system:app", f"agent:{chat}", "The app is restarting now.",
                               kind="notice", wake=False)
    found = await store.read(lambda c: rows.threads(c, None))
    assert [t["thread"] for t in found] == [kept["intercom_id"]]


async def test_the_users_stop_ends_what_a_mission_asks_and_tells_the_chat_that_started_it(
        store):
    from celine.missions.skill import record
    intercom = Intercom(store)
    chat = await conversations.create(store, "Plans")
    night = await conversations.create(store, "Night", kind="mission")
    await missions.create(store, night, {"name": "Night", "terminal": "n", "brief": "b",
                                         "started_by": chat})
    await intercom.send(f"agent:{night}", "user", "Which hotel, the cheap one?")
    services = SimpleNamespace(store=store, approvals=None, connectors=None)
    assert [i["kind"] for i in await waits.items(services)] == ["mission"]

    await record(SimpleNamespace(intercom=intercom, cfg=boot.load()), store, night,
                 "stopped", None)
    assert await waits.items(services) == [], "a stopped mission waits for nothing"
    [told] = await store.read(lambda c: c.execute(
        "SELECT text FROM intercom WHERE from_addr=? AND kind='report'",
        (f"agent:{night}",)).fetchall())
    assert told[0].startswith("Mission Night stopped by ")


async def test_a_mission_stopped_at_its_time_limit_tells_the_user_and_the_chat_that_started_it(
        store):
    from celine.missions.skill import record
    chat = await conversations.create(store, "Plans")
    night = await conversations.create(store, "Night", kind="mission")
    await missions.create(store, night, {"name": "Night", "terminal": "n", "brief": "b",
                                         "started_by": chat})
    await record(SimpleNamespace(intercom=Intercom(store), cfg=boot.load()), store, night,
                 "stopped", "It reached its time limit of 2 hours.")
    told = await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT text FROM intercom WHERE from_addr=? AND kind='report'", (f"agent:{night}",))])
    assert told == ["Mission Night stopped: It reached its time limit of 2 hours."]
    notices = await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT data FROM global_events WHERE type='notice' ORDER BY id")])
    assert [(n["title"], n["body"]) for n in notices] == [
        ("Night", "It reached its time limit of 2 hours.")]
