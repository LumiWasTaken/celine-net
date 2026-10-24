"""Lock archived chats (conversations/store.locked): an archived conversation takes
nothing it does on its own, its reviews leave it out, the user's own message opens it
again; a deleted one is always locked."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from chat_fixtures import user_says

from celine.boot import load
from celine.conversations import api as conversations_api, store as conversations, tree
from celine.core.services import Services
from celine.errors import install_error_handler
from celine.intercom import Intercom
from celine.intercom.addresses import Refused
from celine.missions import store as missions, watch
from celine.schedules.variables import last_exchange
from celine.skills import ToolContext
from celine.skills.conversations import PastConversations
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


async def _turns(store, cid):
    return await store.read(lambda c: [tuple(r) for r in c.execute(
        "SELECT origin, state, reason FROM turns WHERE conversation_id=? "
        "ORDER BY created_at", (cid,))])


async def _archived(store, title="Download all videos from this:"):
    cid = await conversations.create(store, title)
    _, tid, _ = await user_says(store, cid, "download all videos from this page")
    await turns.transition(store, tid, cid, "done")
    await conversations.archive(store, cid)
    return cid


class Stops:
    """The dispatcher's Stop, recorded."""
    def __init__(self):
        self.stopped = []

    async def abort_conversation(self, cid):
        self.stopped.append(cid)

    def notify(self):
        pass


@pytest.fixture
async def api(store):
    app = FastAPI()
    app.state.services = Services(store=store, cfg=load(), dispatcher=Stops())
    install_error_handler(app)
    app.include_router(conversations_api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        client.services = app.state.services
        yield client


async def test_archiving_stops_her_work_and_drops_what_waits_there(api, store):
    cid = await conversations.create(store, "videos")
    sub = await conversations.create(store, "fetcher", agent_of=cid, agent_name="fetcher")
    ic = Intercom(store)
    _, tid, _ = await user_says(store, cid, "download them")
    await turns.transition(store, tid, cid, "done")
    await ic.send(f"agent:{cid}", "me", "check the downloads", due_at="2999-01-01T00:00:00+00:00")
    woke = await ic.send("system:app", f"agent:{sub}", "go on")     # its turn, queued
    assert woke["turn_id"]

    r = await api.post(f"/api/conversations/{sub}/archive")
    assert r.status_code == 200, "only a turn of the user's refuses it"
    assert api.services.dispatcher.stopped == [cid], "its work in the family stops"
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM intercom_deliveries WHERE state='pending' "
        "AND conversation_id IS NOT NULL").fetchone()[0]) == 0, "the reminder is dropped"
    assert await turns.claim(store, "w") is None, "the queued turn does not start"
    assert await _turns(store, sub) == [("intercom", "aborted", "That chat is archived.")]

    # what comes later from the app (a job's result, a restart notice) never lands
    out = await ic.send(f"agent:{cid}", f"agent:{cid}", "Made one video.", kind="job")
    assert out["turn_id"] is None and out["message_id"] is None
    # and its own writing there is refused with the reason
    review = await conversations.create(store, "Self-review", kind="background")
    with pytest.raises(Refused, match="^That chat is archived.$"):
        await ic.send(f"agent:{review}", f"agent:{cid}", "pick the downloads up again")
    assert (await conversations.archive(store, cid, True))["archived_at"], "still archived"


async def test_a_mission_of_an_archived_chat_is_not_woken_or_watched(store):
    ic = Intercom(store)
    app = SimpleNamespace(store=store, intercom=ic)
    cid = await conversations.create(store, "Videos", kind="mission")
    await missions.create(store, cid, {"name": "Videos", "terminal": "videos", "brief": "b"})
    await conversations.archive(store, cid)
    out = await ic.send("system:app", f"agent:{cid}", "Time is up.", kind="notice")
    assert out["turn_id"] is None, "its time limit does not resume it"
    assert not await watch.check(app, cid, "t1")
    assert (await missions.get(store, cid))["status"] == "active", "left as it stands"
    delivered = lambda: store.read(lambda c: c.execute(  # noqa: E731
        "SELECT count(*) FROM intercom_deliveries WHERE conversation_id=?",
        (cid,)).fetchone()[0])
    before = await delivered()
    await ic.send("system:app", "all", "the night is over")
    assert await delivered() == before, "everyone working is not the archived mission"


async def test_her_reviews_leave_an_archived_chat_out_and_her_search_marks_it(store):
    cfg = load()
    cfg.timezone = "UTC"
    skill = PastConversations(cfg)
    gone = await _archived(store)
    open_ = await conversations.create(store, "Groceries")
    _, tid, _ = await user_says(store, open_, "buy milk")
    await turns.transition(store, tid, open_, "done")
    here = await conversations.create(store, "Self-review", kind="background")
    ctx = ToolContext(conversation_id=here, turn_id="t", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    today = (await store.read(lambda c: c.execute(        # the day the store stamped
        "SELECT max(created_at) FROM messages WHERE conversation_id = ?",
        (open_,)).fetchone()[0]))[:10]

    day = (await skill.list_day(ctx, today)).content
    assert open_[:8] in day and gone[:8] not in day
    found = (await skill.search(ctx, "videos")).content
    assert f"(conversation {gone[:8]}, archived)" in found, "found on purpose, marked"
    assert (await skill.read(ctx, gone[:8])).content.startswith(
        f"conversation {gone[:8]}, archived:")
    assert await store.read(last_exchange) == ["buy milk"], "the routine's {{on_mind}} skips it"


async def test_the_users_message_opens_it_again_and_she_answers(api, store):
    gone = await _archived(store)
    _, tid, _ = await user_says(store, gone, "one more: the last video")
    assert tid and (await turns.claim(store, "w"))["id"] == tid
    row = await store.read(lambda c: c.execute(
        "SELECT archived_at FROM conversations WHERE id=?", (gone,)).fetchone())
    assert row[0] is None
    await turns.transition(store, tid, gone, "done")

    # any write of the user's there opens it: here an answer again
    await conversations.archive(store, gone)
    r = await api.post(f"/api/conversations/{gone}/regenerate")
    assert r.status_code == 200, r.text
    assert (await turns.claim(store, "w"))["id"] == r.json()["turn_id"]
    assert (await api.get(f"/api/conversations/{gone}")).json()["conversation"][
        "archived_at"] is None


async def test_with_the_setting_off_an_archived_chat_wakes_as_before(store, monkeypatch):
    await store.write(lambda c: conversations.set_lock_archived(c, False))
    ic = Intercom(store)
    gone = await _archived(store)
    review = await conversations.create(store, "Self-review", kind="background")
    out = await ic.send(f"agent:{review}", f"agent:{gone}", "go on")
    assert out["turn_id"] and (await turns.claim(store, "w"))["id"] == out["turn_id"]
    assert (await store.read(lambda c: c.execute(
        "SELECT archived_at FROM conversations WHERE id=?", (gone,)).fetchone()))[0] is None


async def test_a_deleted_chat_is_always_locked(store, monkeypatch):
    await store.write(lambda c: conversations.set_lock_archived(c, False))
    ic = Intercom(store)
    cid = await conversations.create(store, "Videos")
    _, tid, _ = await user_says(store, cid, "download them")
    await turns.transition(store, tid, cid, "done")
    await ic.send("user", f"agent:{cid}", "and later", due_at="2999-01-01T00:00:00+00:00")
    assert await conversations.delete(store, cid)
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM intercom_deliveries WHERE state='pending' AND "
        "conversation_id=?", (cid,)).fetchone()[0]) == 0, "nothing waits there"
    review = await conversations.create(store, "Self-review", kind="background")
    with pytest.raises(Refused, match="^That chat is deleted.$"):
        await ic.send(f"agent:{review}", f"agent:{cid}", "go on")
    # a turn queued there anyway (a restart's resume) ends at once
    await store.write(lambda c: tree.queue_turn(c, store, cid, None, origin="intercom"))
    assert await turns.claim(store, "w") is None
    assert (await _turns(store, cid))[-1] == ("intercom", "aborted", "That chat is deleted.")


async def test_the_setting_reaches_the_database_where_the_locks_read_it(store):
    """Settings > General writes var/settings; every load puts the switch into kv
    (keep_lock), and the SQL of every reader follows it."""
    cfg = load()
    assert await store.read(conversations.lock_archived), "on until a load says"
    cfg.settings.general = {**cfg.settings.general, "lock_archived": False}
    conversations.keep_lock(store, cfg)
    gone = await _archived(store)
    assert await store.read(lambda c: (conversations.lock_archived(c),
                                       conversations.locked(c, gone))) == (False, None)
    cfg.settings.general = {**cfg.settings.general, "lock_archived": True}
    conversations.keep_lock(store, cfg)
    await store.write(lambda c: None)            # the writer is in order: it landed
    assert await store.read(lambda c: c.execute(
        f"SELECT {conversations.locked_sql()} FROM conversations c WHERE id=?",
        (gone,)).fetchone()[0]) == 1
