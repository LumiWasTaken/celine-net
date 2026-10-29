"""The mission loop (missions/watch.py, missions__wait, the time limit, check-ins): a night
mission neither ends early nor burns time."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_plans import Replies, _calls
from chat_fixtures import user_says

from celine.backends.base import Done, TextDelta
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.core.ids import iso
from celine.intercom import Intercom
from celine.missions import store as missions, watch
from celine.missions.store import book_time_limit
from celine.missions.skill import Missions
from celine.plans import store as plans
from celine.skills import Registry, ToolContext
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn

pytestmark = pytest.mark.anyio
LATER = "2999-01-01T00:00:00.000+00:00"
PAST = "2000-01-01T00:00:00.000+00:00"


async def _mission(store, intercom, until=None, report_to=None, check_in=None) -> str:
    cid = await conversations.create(store, "Night", kind="mission")
    await missions.create(store, cid, {"name": "Night", "terminal": "night", "brief": "b",
                                       "until": until, "report_to": report_to,
                                       "check_in": check_in})
    if until:
        wake = await book_time_limit(intercom, cid, datetime.fromisoformat(until))
        await missions.update(store, cid, {"wake_id": wake})
    return cid


async def _ended_turn(store, intercom, cid: str, tool: bool = False) -> str:
    """A turn of `cid` started by a message and ended done; with `tool` it called one."""
    tid = (await intercom.send("user", f"agent:{cid}", "go", kind="brief"))["turn_id"]
    await _end(store, tid, tool)
    return tid


async def _end(store, tid: str, tool: bool = False) -> None:
    def fn(c):
        c.execute("UPDATE turns SET state='done' WHERE id=?", (tid,))
        if tool:
            cid = c.execute("SELECT conversation_id FROM turns WHERE id=?",
                            (tid,)).fetchone()[0]
            c.execute("INSERT INTO messages(id, conversation_id, role, content, turn_id, "
                      "created_at) VALUES(?, ?, 'assistant', '', ?, ?)",
                      (f"a-{tid}", cid, tid, PAST))
            c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, state) "
                      "VALUES(?, ?, 'shell__run', '{}', 'ok')", (f"tc-{tid}", f"a-{tid}"))
    await store.write(fn)


async def _sent(store, cid: str) -> list[tuple[str, dict]]:
    """What the app said to mission `cid`, oldest first, the time limit aside."""
    return await store.read(lambda c: [(r[0], json.loads(r[1])) for r in c.execute(
        "SELECT text, data FROM intercom WHERE to_addr=? AND from_addr='system:app' AND "
        "json_extract(data, '$.time_limit') IS NULL ORDER BY id", (f"agent:{cid}",))])


async def test_a_mission_with_a_time_limit_still_goes_on_when_she_stops(store):
    """The time limit's notice waits hours away: it is no wake that keeps it quiet."""
    intercom = Intercom(store)
    app = SimpleNamespace(store=store, intercom=intercom)
    cid = await _mission(store, intercom, until=LATER)
    await plans.save(store, cid, "", plans.clean(["Read", "Write"]))
    tid = await _ended_turn(store, intercom, cid)
    assert await watch.check(app, cid, tid) == "line"
    assert [t for t, _ in await _sent(store, cid)] == [watch.KEEP_GOING]
    assert (await missions.get(store, cid))["status"] == "active"


async def test_the_time_limit_tells_her_to_wrap_up_before_it(store):
    intercom = Intercom(store)
    until = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
    cid = await _mission(store, intercom, until=iso(until))
    row = await missions.get(store, cid)
    due, text = await store.read(lambda c: tuple(c.execute(
        "SELECT due_at, text FROM intercom WHERE id=?", (int(row["wake_id"]),)).fetchone()))
    assert due == iso(until - timedelta(minutes=15))
    assert text.startswith("The time limit is in 15 min. Wrap up now")

    short = await _mission(store, intercom, until=iso(datetime.now(timezone.utc)
                                                       + timedelta(minutes=5)))
    row = await missions.get(store, short)
    text = await store.read(lambda c: c.execute(
        "SELECT text FROM intercom WHERE id=?", (int(row["wake_id"]),)).fetchone()[0])
    assert text.startswith("The time limit is in 1 min."), "a fifth of a short limit"


async def test_past_the_time_limit_one_line_says_close_it_then_it_needs_you(store):
    intercom = Intercom(store)
    app = SimpleNamespace(store=store, intercom=intercom)
    cid = await _mission(store, intercom)
    await missions.update(store, cid, {"until": PAST})
    await plans.save(store, cid, "", plans.clean([{"title": "Read", "state": "done"}]))
    tid = await _ended_turn(store, intercom, cid, tool=True)
    assert await watch.check(app, cid, tid) == "line", "even with every step done"
    (text, data), = await _sent(store, cid)
    assert text == watch.OVER and data["over"] and data["keep_going"]

    nxt = await store.read(lambda c: c.execute(
        "SELECT turn_id FROM intercom_deliveries d JOIN intercom i ON i.id = d.intercom_id "
        "WHERE i.text=?", (watch.OVER,)).fetchone()[0])
    await _end(store, nxt, tool=True)              # it worked on instead of closing it
    assert await watch.check(app, cid, nxt) == "needs_you"
    row = await missions.get(store, cid)
    assert (row["status"], row["note"]) == ("needs_you", watch.PAST)
    assert len(await _sent(store, cid)) == 1, "no second line past the limit"


async def test_she_waits_until_a_time_and_nothing_wakes_her_before(store):
    cfg = load()
    intercom = Intercom(store)
    app = SimpleNamespace(store=store, intercom=intercom)
    cid = await _mission(store, intercom, until=LATER)
    await plans.save(store, cid, "", plans.clean(["Download", "Train"]))
    skill = Missions(cfg)
    skill.app = app
    ctx = ToolContext(cid, "t", "k", store, cfg, None)
    assert (await skill.wait(ctx, why="the download")).is_error, "in or at"
    before = datetime.now(timezone.utc)
    out = await skill.wait(ctx, why="the download ends, start the training", in_="20 min")
    after = datetime.now(timezone.utc)
    assert not out.is_error and out.content.startswith("You wake ")
    assert "time limit" not in out.content, "the limit is later"
    first = out.display["due_at"]
    assert iso(before + timedelta(minutes=20)) <= first <= iso(after + timedelta(minutes=20))
    out = await skill.wait(ctx, why="the download, again", in_="40 min")
    rows = await store.read(lambda c: [tuple(r) for r in c.execute(
        "SELECT from_addr, text, cancelled_at IS NOT NULL FROM intercom WHERE "
        "json_extract(data, '$.wait') ORDER BY id")])
    assert rows == [(f"agent:{cid}", "the download ends, start the training", 1),
                    (f"agent:{cid}", "the download, again", 0)], "a new wait replaces it"

    tid = await _ended_turn(store, intercom, cid)
    assert await watch.check(app, cid, tid) is None, "its wake is set: no line"
    assert await _sent(store, cid) == []

    soon = await _mission(store, intercom)
    await missions.update(store, soon, {"until": iso(datetime.now(timezone.utc)
                                                     + timedelta(hours=1))})
    row = await missions.get(store, soon)
    wake = await book_time_limit(intercom, soon, datetime.fromisoformat(row["until"]))
    await missions.update(store, soon, {"wake_id": wake})
    out = await skill.wait(ToolContext(soon, "t", "k", store, cfg, None), why="x",
                           in_="3 h")
    assert "The time limit's notice wakes you first" in out.content


async def test_a_wait_ends_her_turn(store):
    cfg = load()
    intercom = Intercom(store)
    cid = await _mission(store, intercom)
    await plans.save(store, cid, "", plans.clean(["Download", "Train"]))
    skill = Missions(cfg)
    skill.app = SimpleNamespace(store=store, intercom=intercom)
    reg = Registry(cfg)
    reg.add(skill)
    b = Replies([[TextDelta("The download takes a while."),
                  *_calls(("missions__wait", {"why": "the download", "in": "30 min"}))],
                 [TextDelta("never reached"), Done(reason="stop")]])
    await user_says(store, cid, "go")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, b, EventBus(), Token(turn["id"]), reg)
    assert len(b.calls) == 1, "the wait ended the turn though a step is open"
    assert await store.read(lambda c: c.execute(
        "SELECT state FROM turns WHERE id=?", (turn["id"],)).fetchone()[0]) == "done"


async def test_done_with_an_open_step_needs_a_note(store):
    cfg = load()
    intercom = Intercom(store)
    cid = await _mission(store, intercom)
    await plans.save(store, cid, "", plans.clean(["Read", "Write", "Check"]))
    skill = Missions(cfg)
    skill.app = SimpleNamespace(store=store, intercom=intercom)
    ctx = ToolContext(cid, "t", "k", store, cfg, None)
    out = await skill.update(ctx, status="Done")
    assert out.is_error and out.content.startswith("Step 1, 2, 3 of your plan is still open")
    assert (await missions.get(store, cid))["status"] == "active"
    out = await skill.update(ctx, status="Done", note="the brief changed, only Read counts")
    assert not out.is_error and (await missions.get(store, cid))["status"] == "done"


async def test_check_ins_go_where_it_reports_on_its_cadence(store):
    intercom = Intercom(store)
    app = SimpleNamespace(store=store, intercom=intercom)
    chat = await conversations.create(store, "Her chat")
    cid = await _mission(store, intercom, report_to=chat, check_in=30)
    quiet = await _mission(store, intercom)                    # no check-in: none
    await plans.save(store, cid, "", plans.clean(["Read the docs", "Build"]))
    await missions.update(store, cid, {"note": "Two docs left."})
    start = datetime.fromisoformat((await missions.get(store, cid))["created_at"])

    assert await watch.check_ins(app, start + timedelta(minutes=29)) == []
    assert await watch.check_ins(app, start + timedelta(minutes=30)) == [cid]
    assert await watch.check_ins(app, start + timedelta(minutes=45)) == []
    await plans.save(store, cid, "", plans.clean([{"title": "Read the docs",
                                                    "state": "done"}, "Build"]))
    assert await watch.check_ins(app, start + timedelta(minutes=61)) == [cid]
    said = await store.read(lambda c: [tuple(r) for r in c.execute(
        "SELECT from_addr, to_addr, kind, wake, text FROM intercom WHERE "
        "json_extract(data, '$.check_in') ORDER BY id")])
    assert said == [
        (f"agent:{cid}", f"agent:{chat}", "report", 0,
         "Mission Night: step 1 of 2, Read the docs. Two docs left."),
        (f"agent:{cid}", f"agent:{chat}", "report", 0,
         "Mission Night: step 2 of 2, Build. Two docs left.")]
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM turns WHERE conversation_id=?", (chat,)).fetchone()[0]) == 0

    await store.write(lambda c: missions.set_status_sync(c, store, cid, "needs_you", "x"))
    assert await watch.check_ins(app, start + timedelta(hours=5)) == [], "active only"
    assert quiet not in await watch.check_ins(app, start + timedelta(hours=5))


async def test_past_the_mission_time_limit_of_advanced_it_stops_with_the_reason(store):
    intercom = Intercom(store)
    cfg = load()
    cfg.settings.general["mission_max_hours"] = 2
    aborted = []

    async def abort(cid):
        aborted.append(cid)
    app = SimpleNamespace(store=store, intercom=intercom, cfg=cfg, terminals=None,
                          dispatcher=SimpleNamespace(abort_conversation=abort))
    cid = await _mission(store, intercom)
    start = datetime.fromisoformat((await missions.get(store, cid))["created_at"])
    assert await watch.ceilings(app, start + timedelta(hours=2)) == []
    assert await watch.ceilings(app, start + timedelta(hours=2, seconds=1)) == [cid]
    row = await missions.get(store, cid)
    assert (row["status"], row["note"]) == ("stopped", "Ran over 2 hours")
    assert aborted == [cid], "its turn and its sub-agents are stopped"
    assert await watch.ceilings(app, start + timedelta(hours=3)) == [], "once"

    cfg.settings.general["mission_max_hours"] = 0
    await _mission(store, intercom)
    assert await watch.ceilings(app, start + timedelta(days=999)) == [], "0: no ceiling"


async def test_a_mission_a_freeze_cut_goes_on_after_the_unfreeze(store):
    from celine.core.services import Services
    from celine.guard import freeze
    from test_bastet import OWNER
    intercom = Intercom(store)
    cfg = load()
    woke = []
    app = SimpleNamespace(store=store, intercom=intercom, cfg=cfg, registry=None,
                          dispatcher=SimpleNamespace(notify=lambda: woke.append(1)))
    cid = await _mission(store, intercom)
    await plans.save(store, cid, "", plans.clean(["Read", "Write"]))
    tid = await _ended_turn(store, intercom, cid, tool=True)
    await store.write(lambda c: c.execute(
        "UPDATE turns SET state='aborted', reason='frozen', reason_code='frozen' WHERE id=?",
        (tid,)))
    await freeze.freeze(Services(cfg=cfg, store=store), by="user", why="a test")
    assert await watch.check(app, cid, tid) is None, "left active, not Needs you"
    assert (await missions.get(store, cid))["status"] == "active"
    assert await watch.thaw(app) == [], "still frozen"
    await freeze.unfreeze(Services(cfg=cfg, store=store), by=OWNER)
    assert await watch.thaw(app) == [cid]
    assert await store.read(lambda c: c.execute(
        "SELECT state FROM turns WHERE id=?", (tid,)).fetchone()[0]) == "queued", \
        "the cut turn goes on"
    assert woke and await watch.thaw(app) == [], "once"

    cfg.settings.general["missions_resume_after_unfreeze"] = False
    other = await _mission(store, intercom)
    tid = await _ended_turn(store, intercom, other, tool=True)
    await store.write(lambda c: c.execute(
        "UPDATE turns SET state='aborted', reason='frozen', reason_code='frozen' WHERE id=?",
        (tid,)))
    assert await watch.check(app, other, tid) == "needs_you", "switched off: as a Stop"
    assert await watch.thaw(app) == []


async def test_a_reply_of_the_user_makes_a_waiting_mission_active_again(store):
    intercom = Intercom(store)
    app = SimpleNamespace(store=store, intercom=intercom)
    cid = await _mission(store, intercom)
    await store.write(lambda c: missions.set_status_sync(c, store, cid, "needs_you", "x"))
    tid = await _ended_turn(store, intercom, cid, tool=True)
    await store.write(lambda c: c.execute("UPDATE turns SET origin='user' WHERE id=?", (tid,)))
    assert await watch.check(app, cid, tid) == "line", "active again, so it goes on"
    assert (await missions.get(store, cid))["status"] == "active"


async def test_a_stopped_sub_agent_lets_the_watch_move_its_mission_on(store):
    """The mission's own turn ended while its sub-agent still ran (not stuck). When the
    sub-agent's turn ends, the watch looks at the mission again."""
    intercom = Intercom(store)
    hooks = []
    app = SimpleNamespace(cfg=None, store=store, intercom=intercom,
                          dispatcher=SimpleNamespace(on_end=hooks.append))
    watch.start(app)
    cid = await _mission(store, intercom)
    await plans.save(store, cid, "", plans.clean([{"title": "Read", "state": "done"}]))
    sub = await conversations.create(store, "Helper", agent_of=cid)
    stid = (await intercom.send("user", f"agent:{sub}", "go", kind="brief"))["turn_id"]
    await _ended_turn(store, intercom, cid, tool=True)
    assert (await missions.get(store, cid))["status"] == "active"
    await store.write(lambda c: c.execute("UPDATE turns SET state='aborted' WHERE id=?",
                                          (stid,)))
    await hooks[0](sub, stid)                       # the Stop's turn end
    row = await missions.get(store, cid)
    assert (row["status"], row["note"]) == ("needs_you", watch.NOTE)
