"""Terminals and tabs remember who opened them: the shell
skill's opener and its listing, the idle sweeps (a fake daemon's
quiet_for, tabs on a fixed clock), and what closes when a mission or a sub-agent ends."""
from __future__ import annotations

import json

import httpx
import pytest

from celine import leftovers
from celine.browser import sync as browser_sync
from celine.browser import tabs
from celine.boot import load
from celine.conversations import store as conversations
from celine.conversations import tree
from celine.core.services import Services
from celine.missions import store as missions
from celine.terminals import store as terminals
from celine.terminals.sync import IDLE, TerminalSync
from celine.turns import store as turns
from chat_fixtures import user_says
from test_shell import FakeBox, ctx, ran, screen
from test_terminals_api import OPENED_ISO, Daemon, term

pytestmark = pytest.mark.anyio

T0 = "2026-10-22T03:00:00.000+00:00"
DAY_LATER = "2026-10-23T03:00:00.001+00:00"


async def test_the_call_that_opened_a_terminal_names_her_conversation(store, monkeypatch):
    from celine.skills.shell import Shell
    box = FakeBox()
    monkeypatch.setenv("CELINE_SHELL_URL", "http://shellbox:8770")
    sh = Shell(load(), transport=httpx.MockTransport(box.handle))
    sh.cfg.user = "Joe"
    await store.write(lambda c: c.execute(
        "INSERT INTO conversations(id, title, next_seq, created_at, updated_at) "
        "VALUES('c', 'Lighthouse', 1, ?, ?)", (T0, T0)))
    # the sync listed it first, without an opener; then its call answers
    await terminals.upsert(store, "m1m1m1", "main", "celine", T0)
    box.answers = [ran(opened=True)]
    await sh.run(ctx(store=store), terminal="main", command="ls")
    assert (await terminals.get(store, "m1m1m1"))["opened_by"] == "c"
    box.answers = [ran(id="b2b2b2", terminal="build", opened=False)]
    await sh.run(ctx(store=store), command="ls", terminal="build")
    assert (await terminals.get(store, "b2b2b2"))["opened_by"] is None, \
        "a call that did not open it claims nothing"
    box.answers = [screen(id="lu0001", terminal="sam", owner="sam", opened=True)]
    await terminals.upsert(store, "lu0001", "sam", "sam", T0)
    await terminals.set_may_type(store, "lu0001", True)
    await sh.type(ctx(store=store), text="ls", terminal="sam")
    assert (await terminals.get(store, "lu0001"))["opened_by"] is None, "the user's own"

    mission = await conversations.create(store, "Car scout")
    await missions.create(store, mission, {"name": "Car scout", "terminal": "cars",
                                           "brief": "b"})
    await terminals.upsert(store, "c3c3c3", "cars", "celine", T0, mission)
    box.answers = [{"terminals": [
        term("main", "m1m1m1"), term("cars", "c3c3c3"), term("build", "b2b2b2"),
        term("sam", "lu0001", owner="sam")], "notes": []}]
    out = await sh.read(ctx(store=store))
    assert out.content.split("\n") == [
        "main, opened here: bash ~, at the prompt",
        'cars, opened in mission "Car scout": bash ~, at the prompt',
        "build: bash ~, at the prompt",
        "sam, Joe's terminal, you may type: bash ~, at the prompt"]
    row = await terminals.opener(store, "c3c3c3")
    assert (row["id"], row["kind"]) == (mission, "mission")


def _sync(store, daemon: Daemon) -> TerminalSync:
    cfg = load()
    cfg.data_dir = store.db_path.parent
    cfg.settings.services = {"shellbox": {"url": "http://shellbox:8770"}}
    return TerminalSync(cfg, store, transport=httpx.MockTransport(daemon.handle))


async def test_her_terminal_quiet_a_day_with_nothing_running_closes(store):
    daemon = Daemon(term("old", "o1o1o1", quiet_for=IDLE),
                    term("fresh", "f1f1f1", quiet_for=IDLE - 1),
                    term("build", "b1b1b1", quiet_for=IDLE * 2, busy=True, program="make"),
                    term("paused", "p1p1p1", quiet_for=IDLE * 2, paused=True),
                    term("sam", "s1s1s1", owner="sam", quiet_for=IDLE * 2))
    sync = _sync(store, daemon)
    await sync.refresh()
    assert daemon.calls("/control") == [{"terminal": "old", "action": "close"}]
    rows = {t["name"]: t for t in await terminals.list_terminals(store)}
    assert rows["old"]["ended_by"] == "idle" and rows["old"]["ended_at"]
    assert all(rows[n]["ended_at"] is None for n in ("fresh", "build", "paused", "sam"))
    assert "o1o1o1" not in sync.live
    assert (store.db_path.parent / "terminals" / "o1o1o1.raw.gz").exists(), "its history"


async def test_a_conversations_terminals_close_when_it_ends_and_no_one_elses(store):
    sub = await conversations.create(store, "s", agent_of=await conversations.create(store),
                                     agent_name="s")
    other = await conversations.create(store)
    daemon = Daemon(term("s-ab12", "a1a1a1", busy=True, program="python3"),
                    term("mine", "b1b1b1"))
    sync = _sync(store, daemon)
    await sync.refresh()
    await terminals.upsert(store, "a1a1a1", "s-ab12", "celine", OPENED_ISO, sub)
    await terminals.upsert(store, "b1b1b1", "mine", "celine", OPENED_ISO, other)
    await sync.close_opened_by(sub)
    assert daemon.calls("/control") == [{"terminal": "s-ab12", "action": "close"}], \
        "closed whatever runs in it"
    assert (await terminals.get(store, "a1a1a1"))["ended_by"] == "done"
    assert (await terminals.get(store, "b1b1b1"))["ended_at"] is None
    assert (store.db_path.parent / "terminals" / "a1a1a1.raw.gz").exists(), "its history"


async def test_a_refused_close_leaves_the_row_open(store):
    daemon = Daemon(term("old", "o1o1o1", quiet_for=IDLE))
    real = daemon.handle

    def handle(request):
        if request.url.path == "/control":
            daemon.sent.append(("/control", json.loads(request.content), {}))
            return httpx.Response(400, json={"error": "No terminal named old."})
        return real(request)
    sync = TerminalSync(load(), store, transport=httpx.MockTransport(handle))
    sync.cfg.settings.services = {"shellbox": {"url": "http://shellbox:8770"}}
    sync.cfg.data_dir = store.db_path.parent
    await sync.refresh()
    assert daemon.calls("/control") and \
        (await terminals.get(store, "o1o1o1"))["ended_at"] is None


async def _call(store, cid: str, tab: str, at: str, i: int) -> None:
    _, tid, _ = await user_says(store, cid, "look")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [
        {"id": f"c{i}", "name": "browser__open", "arguments": json.dumps({"tab": tab})}])
    await store.write(lambda c: c.execute(
        "UPDATE tool_calls SET started_at=? WHERE id=?", (at, f"c{i}")))
    await store.write(lambda c: c.execute("UPDATE turns SET state='done' WHERE id=?", (tid,)))


async def _tabs(store) -> dict[str, dict]:
    return await store.read(lambda c: {r["name"]: dict(r) for r in c.execute(
        "SELECT * FROM tabs WHERE closed_at IS NULL")})


@pytest.fixture
def closed(monkeypatch):
    out: list[str] = []

    async def close_tab(cfg, tab):
        out.append(tab)
        return True
    monkeypatch.setattr(browser_sync, "close_tab", close_tab)
    return out


def up(*tabs_: tuple[str, str], taken=None) -> dict:
    return {"state": "up", "taken": taken,
            "tabs": [{"name": n, "url": u, "title": "", "busy": False} for n, u in tabs_]}


async def test_a_tab_belongs_to_the_newest_call_naming_it_and_closes_unused_a_day(
        store, closed):
    a = await conversations.create(store)
    b = await conversations.create(store)
    await _call(store, a, "jobs", "2026-10-21T01:00:00.000+00:00", 1)
    await _call(store, b, "jobs", "2026-10-22T02:59:00.000+00:00", 2)
    await _call(store, a, "console", "2026-10-21T01:00:00.000+00:00", 3)
    await tabs.follow(None, store, up(("jobs", "https://a.example"), ("console", "about:blank")),
                      T0)
    rows = await _tabs(store)
    assert (rows["jobs"]["opened_by"], rows["console"]["opened_by"]) == (b, None), \
        "a call from long before did not open the user's console"
    assert rows["jobs"]["used_at"] == T0
    by = await tabs.openers(store)
    assert set(by) == {"jobs"} and (by["jobs"]["id"], by["jobs"]["kind"]) == (b, "conversation")

    # the user browses by hand in console: its address changed, so it was used
    await tabs.follow(None, store, up(("jobs", "https://a.example"), ("console", "https://x.org")),
                      "2026-10-22T12:00:00.000+00:00")
    assert (await _tabs(store))["console"]["used_at"] == "2026-10-22T12:00:00.000+00:00"
    await tabs.follow(None, store, up(("jobs", "https://a.example"), ("console", "https://x.org")),
                      DAY_LATER)
    assert closed == ["jobs"], "unused a day; console was used since"
    assert set(await _tabs(store)) == {"console"}


async def test_tabs_stay_while_the_browser_is_down_or_taken_and_end_once_gone(store, closed):
    await tabs.follow(None, store, up(("jobs", "https://a.example")), T0)
    await tabs.follow(None, store, {"state": "starting", "tabs": []}, DAY_LATER)
    await tabs.follow(None, store, up(("jobs", "https://a.example"), taken=1), DAY_LATER)
    assert closed == [] and set(await _tabs(store)) == {"jobs"}
    assert (await _tabs(store))["jobs"]["used_at"] == DAY_LATER, "the user had it"
    await tabs.follow(None, store, up(("jobs", "https://a.example")),
                      "2026-10-23T03:01:00.000+00:00")
    assert closed == [], "given back: not unused a day"
    await tabs.follow(None, store, up(), "2026-10-22T03:00:05.000+00:00")
    assert await _tabs(store) == {}, "closed by hand: its row ends"


async def _quiet_mission(store, status: str) -> str:
    cid = await conversations.create(store, "Car scout")
    await missions.create(store, cid, {"name": "Car scout", "terminal": "cars",
                                       "brief": "b", "status": status})
    return cid


class Terminals:
    def __init__(self):
        self.closed: list[str] = []

    async def close_opened_by(self, cid):
        self.closed.append(cid)


async def test_a_done_or_stopped_mission_and_a_quiet_sub_agent_close_what_they_opened(
        store, closed, monkeypatch):
    monkeypatch.setattr(tabs, "now", lambda: T0)
    sub = await conversations.create(store, "s", agent_of=await conversations.create(store),
                                     agent_name="s")
    done = await _quiet_mission(store, "done")
    stopped = await _quiet_mission(store, "stopped")
    active = await _quiet_mission(store, "active")
    chat = await conversations.create(store)
    for i, (cid, tab) in enumerate([(sub, "s-ab12"), (done, "cars"), (stopped, "c2"),
                                    (active, "c3"), (chat, "c4")]):
        await _call(store, cid, tab, T0, i)

    looks: list[int] = []

    async def status(cfg):
        looks.append(1)
        return up(*((t, "about:blank") for t in ("s-ab12", "cars", "c2", "c3", "c4")))
    monkeypatch.setattr(browser_sync, "status", status)
    cfg = load()
    cfg.settings.services = {"browser": {"url": "http://browser:8771"}}
    services = Services(cfg=cfg, store=store, terminals=Terminals())
    for cid in (sub, done, stopped, active, chat):
        await leftovers.on_end(services, cid)
    assert services.terminals.closed == [sub, done, stopped]
    assert closed == ["s-ab12", "cars", "c2"]
    assert len(looks) == 3, "one look per done one that used the browser"

    # a sub-agent still at work keeps them
    busy = await conversations.create(store, "t", agent_of=chat, agent_name="t")
    await user_says(store, busy, "go")
    await leftovers.on_end(services, busy)
    assert services.terminals.closed == [sub, done, stopped]

    # a done mission that never called the browser asks the box nothing
    plain = await _quiet_mission(store, "done")
    await leftovers.on_end(services, plain)
    assert len(looks) == 3 and services.terminals.closed[-1] == plain


async def test_her_next_result_says_the_app_closed_it_not_the_user(store, monkeypatch):
    from datetime import datetime

    from celine.clock import zone
    from celine.skills.shell import Shell
    box = FakeBox()
    monkeypatch.setenv("CELINE_SHELL_URL", "http://shellbox:8770")
    sh = Shell(load(), transport=httpx.MockTransport(box.handle))
    sh.cfg.user = "Joe"
    sh.boots["c"] = ran()["boot_id"]
    await store.write(lambda c: c.execute(
        "INSERT INTO conversations(id, title, next_seq, created_at, updated_at) "
        "VALUES('c', '', 1, ?, ?)", (T0, T0)))
    epoch = 1792638000                  # 2026-10-22 03:00 UTC
    for ended_by, said in [("idle", "Terminal main closed at {}: nothing ran in it for a day."),
                           ("user", "Joe closed terminal main at {}.")]:
        await terminals.upsert(store, f"x{ended_by}", "main", "celine", T0)
        await terminals.end(store, f"x{ended_by}", ended_by, None)
        box.answers = [ran(opened=True, notes=[f"closed:{epoch}"])]
        out = await sh.run(ctx(store=store), terminal="main", command="ls")
        at = datetime.fromtimestamp(epoch, zone(sh.cfg)).strftime("%H:%M")
        assert said.format(at) in out.content.split("\n")
