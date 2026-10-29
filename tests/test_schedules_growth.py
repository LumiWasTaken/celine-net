"""Schedules that make room for the user, append instead of overwrite, keep backups,
and the growth jobs that ship with a fresh install (schedules/)."""
from __future__ import annotations

import tomllib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chat_fixtures import user_says

from celine.backends.queue import Priority
from celine.config import ROOT
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.services import Services
from celine.intercom import Intercom
from celine.schedules import Schedule, parse, read_partner, runs as runs_mod
from celine.schedules.runs import IN_USE, PASSED, Scheduler, binding, waited_on
from celine.schedules.variables import tool_failures
from celine.skills import Registry
from celine.skills.notebook import Notebook

pytestmark = pytest.mark.anyio
UTC = timezone.utc
CONFIG = ROOT / "config" / "schedules"


@pytest.fixture
def cfg():
    c = load()
    c.timezone = "UTC"
    c.user = "Joe"
    return c


def _app(store, cfg, llm=None):
    reg = Registry()
    reg.add(Notebook(cfg))
    app = Services(cfg=cfg, store=store, registry=reg, llm=llm)
    app.dispatcher = SimpleNamespace(backend_factory=lambda: None)
    app.intercom = Intercom(store)
    for s in reg.skills.values():
        s.app = app
    return app


def test_every_shipped_schedule_loads_through_the_parser():
    names = sorted(p.stem for p in CONFIG.glob("*.toml"))
    assert {"daydream", "consolidate", "radar", "watch", "practice", "tidy",
            "growth"} <= set(names)
    for name in names:
        raw = tomllib.loads((CONFIG / f"{name}.toml").read_text())
        s = parse(name, raw, read_partner(CONFIG, str(raw.get("partner") or "")))
        assert "—" not in s.prompt and " · " not in s.prompt
    from celine.errors import CelineError
    with pytest.raises(CelineError):
        parse("x", {"label": "X", "cron": "0 2 * * *", "prompt": "p", "wall_minutes": 0}, None)


async def test_the_partner_binds_behind_the_user_with_the_schedules_label(store, cfg):
    bound = {}
    llm = SimpleNamespace(bind=lambda backend, **kw: bound.update(kw) or "b")
    sched = Scheduler(_app(store, cfg, llm))
    s = Schedule("reflection", "Reflection", "0 5 * * *", "p")
    assert sched._partner_backend("cid", s) == "b"
    assert (bound["priority"], bound["label"], bound["fallback"]) == \
        (Priority.BACKGROUND, "Reflection", False), "on the role's own model only"

    cfg.schedules = {"reflection": s}
    run_cid = await conversations.create(store, "run")
    sub = await conversations.create(store, "scout", agent_of=run_cid, agent_name="scout")
    await store.write(lambda c: c.execute(
        "INSERT INTO runs(id, schedule, label, slot, night, state, conversation_id, "
        "created_at) VALUES('r', 'reflection', 'Reflection', 'x', '2026-10-16', 'running', ?, "
        "'x')", (run_cid,)))
    for cid in (run_cid, sub):
        assert await store.read(lambda c: binding(c, cid, cfg)) == {
            "priority": Priority.BACKGROUND, "label": "Reflection"}
    chat = await conversations.create(store, "talk")
    assert await store.read(lambda c: binding(c, chat, cfg)) is None


async def test_the_start_gate_sees_a_live_mission(store):
    scene = await conversations.create(store, "mission", kind="mission")
    run = await conversations.create(store, "run", trigger_key="run:r")
    await store.write(lambda c: c.execute("UPDATE conversations SET kind='background' "
                                          "WHERE id=?", (run,)))
    await user_says(store, run, "go")
    assert not await store.read(waited_on), "a run's own turn is nobody's wait"
    await user_says(store, scene, "hello")
    assert await store.read(waited_on)


async def test_a_mission_that_goes_on_by_itself_holds_no_routine_unless_asked(store, cfg):
    from celine.intercom import send
    scene = await conversations.create(store, "mission", kind="mission")
    await send(store, "system:app", f"agent:{scene}", "Do the next step.", kind="notice")
    assert not await store.read(waited_on), "its own turn: nobody waits on it"
    assert await store.read(lambda c: waited_on(c, missions=True))

    cfg.schedules = {"world": Schedule("world", "World", "0 2 1 1 *", "p")}
    sched = Scheduler(_app(store, cfg))
    run = await sched.run_now("world")
    cfg.settings.general["routines_wait_for_missions"] = True
    await sched.tick()
    assert await store.read(lambda c: c.execute(
        "SELECT reason FROM runs WHERE id=?", (run["id"],)).fetchone()[0]) == \
        "Waiting until your chat is done", "Advanced: routines wait for missions"


async def test_an_idle_only_run_is_skipped_while_the_app_is_in_use(store, cfg):
    clock = SimpleNamespace(at=datetime.now(UTC))
    # its own time is next month, so only the runs started here wait
    cfg.schedules = {"daydream": Schedule(
        "daydream", "Daydream", f"0 10 1 {clock.at.month % 12 + 1} *", "p", idle_only=True)}
    sched = Scheduler(_app(store, cfg), lambda: clock.at)
    chat = await conversations.create(store, "talk")
    await user_says(store, chat, "hi")
    await store.write(lambda c: c.execute("UPDATE turns SET state='done'"))
    run = await sched.run_now("daydream")
    await sched.tick()
    got = await store.read(lambda c: dict(c.execute(
        "SELECT state, reason FROM runs WHERE id=?", (run["id"],)).fetchone()))
    assert got == {"state": "skipped", "reason": IN_USE}, "a message came a moment ago"
    clock.at += timedelta(seconds=1)
    later = await sched.run_now("daydream")
    clock.at += timedelta(hours=1)
    await sched.tick()
    assert await store.read(lambda c: c.execute(
        "SELECT reason FROM runs WHERE id=?", (later["id"],)).fetchone()[0]) == PASSED, \
        "never caught up"


async def test_a_note_that_is_there_gets_the_answer_under_a_heading(store, cfg):
    sched = Scheduler(_app(store, cfg))
    s = Schedule("review", "Self-review", "0 4 * * *", "p", note="diary/x.md")
    cid = await conversations.create(store, "run")
    note = cfg.her.notebook / "diary" / "x.md"
    assert await sched._write_note(cid, "diary/x.md", "First.", s, "2026-10-16") is None
    assert note.read_text().strip().endswith("\nFirst.")
    await sched._write_note(cid, "diary/x.md", "Second.", s, "2026-10-17")
    assert note.read_text().strip().endswith("First.\n\n## 2026-10-17 Self-review\n\nSecond.")


async def test_a_note_she_touched_since_the_run_began_is_hers_whole(store, cfg):
    """A run that goes on after a restart: what it wrote before it is the result."""
    import os
    sched = Scheduler(_app(store, cfg))
    note = cfg.her.notebook / "diary" / "w.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# World\n\nThe news.\n")
    at = datetime(2026, 10, 17, 2, tzinfo=UTC)
    os.utime(note, (at.timestamp(), at.timestamp()))
    assert not sched._note_touched("diary/w.md", "2026-10-17T02:30:00+00:00")
    assert sched._note_touched("diary/w.md", "2026-10-17T01:30:00+00:00")
    assert sched._take("diary/w.md", None, "Done.", True) == ("# World\n\nThe news.", True)
    assert sched._take("diary/w.md", None, "Done.", False) == ("Done.", False)


async def test_tool_failures_are_one_line_per_tool(store):
    cid = await conversations.create(store, "Searching")

    def fn(c):
        for i, (name, state, at) in enumerate((
                ("web__search", "error", "2026-10-15T20:00:00.000+00:00"),
                ("web__search", "error", "2026-10-15T21:00:00.000+00:00"),
                ("shell__run", "error", "2026-10-16T01:00:00.000+00:00"),
                ("web__search", "ok", "2026-10-15T22:00:00.000+00:00"),
                ("web__search", "error", "2026-10-14T20:00:00.000+00:00"))):
            c.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) "
                      "VALUES(?, ?, 'assistant', '', ?)", (f"m{i}", cid, at))
            c.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) "
                      "VALUES(?, ?, 'tool', ?, ?)", (f"r{i}", cid, f"SearXNG is down {i}", at))
            c.execute("INSERT INTO tool_calls(id, message_id, result_message_id, name, "
                      "arguments, state, started_at) VALUES(?, ?, ?, ?, '{}', ?, ?)",
                      (f"t{i}", f"m{i}", f"r{i}", name, state, at))
    await store.write(fn)
    got = await store.read(lambda c: tool_failures(c, date(2026, 10, 16), UTC))
    assert got == ("- web__search: 2 failed. First error: SearXNG is down 0 In: Searching.\n"
                   "- shell__run: 1 failed. First error: SearXNG is down 2 In: Searching.")
    assert await store.read(lambda c: tool_failures(c, date(2026, 10, 1), UTC)) == \
        runs_mod.NONE


async def test_voice_counts_compare_her_replies_with_the_users(store):
    from celine.schedules.variables import voice_counts
    cid = await conversations.create(store, "talk")
    now = datetime.now(UTC)

    def fn(c):
        rows = [("u1", "user", "user", "keep it short pls"),
                ("a1", "assistant", "model", "ok pal, one two three."),
                ("u2", "user", "user", "and now?"),
                ("a2", "assistant", "model", "Sure. Here is a long answer")]
        for i, (mid, role, author, text) in enumerate(rows):
            c.execute("INSERT INTO messages(id, conversation_id, role, author, content, "
                      "created_at) VALUES(?, ?, ?, ?, ?, ?)",
                      (mid, cid, role, author, text, (now - timedelta(minutes=10 - i))
                       .isoformat(timespec="milliseconds")))
    await store.write(fn)
    got = await store.read(lambda c: voice_counts(c, now, {}))
    assert "Your chat replies of the last 7 days: 2" in got
    assert "Replies that start lowercase: 50%." in got
    assert "your reply was 5 words (median of 1)" in got


async def test_the_clock_starts_with_the_run_and_its_ceiling_wakes_the_scheduler(
        store, cfg):
    clock = SimpleNamespace(at=datetime(2026, 10, 10, 2, tzinfo=UTC))
    cfg.schedules = {"world": Schedule("world", "World", "0 2 * * *", "p", wall_minutes=10)}
    sched = Scheduler(_app(store, cfg), lambda: clock.at)
    run = await sched.run_now("world")
    sched._tg = SimpleNamespace(start_soon=lambda *a: None)
    sched._start(run)
    await store.write(lambda c: c.execute("UPDATE runs SET state='running' WHERE id=?",
                                          (run["id"],)))
    assert sched._began == clock.at, "the clock starts with the run"
    clock.at += timedelta(minutes=9, seconds=30)
    assert sched.until_next() == 30, "the scheduler looks again at its ceiling"
    clock.at += timedelta(seconds=30)
    await sched.tick()
    assert await store.read(lambda c: c.execute(
        "SELECT state FROM runs WHERE id=?", (run["id"],)).fetchone()[0]) == "running", \
        "at its ceiling, not past it"
    clock.at += timedelta(seconds=1)
    await sched.tick()
    assert await store.read(lambda c: tuple(c.execute(
        "SELECT state, error FROM runs WHERE id=?", (run["id"],)).fetchone())) == \
        ("failed", "Ran over 10 minutes")


async def test_a_routine_time_limit_of_0_lets_a_run_go_on(store, cfg):
    clock = SimpleNamespace(at=datetime(2026, 10, 10, 2, tzinfo=UTC))
    cfg.settings.general["routine_max_minutes"] = 0
    cfg.schedules = {"world": Schedule("world", "World", "0 2 * * *", "p")}
    sched = Scheduler(_app(store, cfg), lambda: clock.at)
    run = await sched.run_now("world")
    sched._tg = SimpleNamespace(start_soon=lambda *a: None)
    sched._start(run)
    await store.write(lambda c: c.execute("UPDATE runs SET state='running' WHERE id=?",
                                          (run["id"],)))
    clock.at += timedelta(days=2)
    await sched.tick()
    assert await store.read(lambda c: c.execute(
        "SELECT state FROM runs WHERE id=?", (run["id"],)).fetchone()[0]) == "running"
