"""Schedules and runs (schedules/): the variables come from the run's night, missed slots
are caught up, a run waits for the runs it follows, for a model of its role and for the
user's chat, a run has one try (it waits for its model, fails once, resumes after a
restart), a partner and it alternate, the answer is written to the note and read by the
next run of the night."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import anyio
import pytest

from celine import boot
from celine.backends.base import BackendError, Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.conversations import store as conversations, view
from celine.core.bus import EventBus
from celine.core.ids import iso
from celine.core.services import Services
from celine.intercom import Intercom
from celine.intercom.skill import IntercomSkill
from celine.schedules import Schedule, fill, runs as runs_mod
from celine.schedules.runs import CHECKED, MISSED, Scheduler
from celine.skills import Registry
from celine.skills.conversations import PastConversations
from celine.skills.notebook import Notebook
from celine.turns.dispatcher import Dispatcher
from chat_fixtures import until, user_says

pytestmark = pytest.mark.anyio
UTC = timezone.utc


@pytest.fixture
def cfg():
    c = load()
    c.timezone = "UTC"
    c.settings.general.update({"max_concurrent_turns": 2})
    c.user = "Joe"
    return c


def _s(name, cron="0 2 * * *", prompt="Go.", **kw) -> Schedule:
    return Schedule(name, kw.pop("label", name.title()), cron, prompt, **kw)


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _app(store, cfg, backend=None, llm=None):
    reg = Registry()
    reg.add(Notebook(cfg))
    reg.add(PastConversations(cfg))
    reg.add(IntercomSkill(cfg))
    app = Services(cfg=cfg, store=store, registry=reg, llm=llm)
    app.dispatcher = SimpleNamespace(backend_factory=lambda: backend)
    app.intercom = Intercom(store)
    for s in reg.skills.values():
        s.app = app
    return app


@asynccontextmanager
async def _scheduler(store, cfg, app, backend, clock, routed=False):
    """A dispatcher and a scheduler on `clock`; `routed`: the run's backend comes from the
    router, not from the dispatcher's factory."""
    async with anyio.create_task_group() as tg:
        d = Dispatcher(store, cfg, EventBus(), lambda: backend, app.registry)
        await d.start(tg)
        app.dispatcher = SimpleNamespace(backend_factory=None, notify=d.notify,
                                         on_end=d.on_end) if routed else d
        app.intercom = Intercom(store, d.notify)
        sched = Scheduler(app, clock)
        sched._tg = tg
        yield sched
        await d.stop()
        tg.cancel_scope.cancel()


async def _runs(store) -> list[dict]:
    return await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT * FROM runs ORDER BY night, slot")])


async def test_the_variables_come_from_the_night_not_the_clock(cfg):
    text = ("{{date}} {{day_before}} {{weekday}} {{username}} {{name}} {{she}} "
            "[{{result:world}}] [{{result:nope}}]")
    assert fill(cfg, text, date(2026, 10, 10), {"world": "Rain."}) == (
        f"2026-10-10 2026-10-09 Saturday Joe {cfg.name} she [Rain.] [(none)]")


async def test_missed_slots_are_made_oldest_first_and_older_than_a_week_skipped(
        store, cfg):
    cfg.schedules = {"world": _s("world"), "off": _s("off", on=False)}
    clock = Clock(datetime(2026, 10, 10, 11, tzinfo=UTC))
    sched = Scheduler(_app(store, cfg), clock)
    await sched.make_due()
    assert [(r["night"], r["state"]) for r in await _runs(store)] == [
        ("2026-10-10", "waiting")], "the first look makes tonight's passed slots, no more"
    await store.kv_set(CHECKED, iso(clock.at - timedelta(days=9)))
    await sched.make_due()
    got = [(r["night"], r["state"], r["reason"]) for r in await _runs(store)]
    assert got == [("2026-10-02", "skipped", MISSED), ("2026-10-03", "skipped", MISSED)] + [
        (f"2026-10-{d:02d}", "waiting", "") for d in range(4, 11)]
    assert all(r["finished_at"] for r in await _runs(store) if r["state"] == "skipped"), \
        "a missed slot is finished"
    await sched.make_due()
    assert len(await _runs(store)) == 9, "a slot has one run"


async def test_slots_past_max_slots_are_made_by_the_next_look(store, cfg, monkeypatch):
    monkeypatch.setattr(runs_mod, "MAX_SLOTS", 3)
    cfg.schedules = {"tick": _s("tick", "0 * * * *")}
    clock = Clock(datetime(2026, 10, 10, 11, 30, tzinfo=UTC))
    sched = Scheduler(_app(store, cfg), clock)
    await store.kv_set(CHECKED, iso(clock.at - timedelta(hours=5)))
    await sched.make_due()
    assert len(await _runs(store)) == 3
    await sched.make_due()
    assert len(await _runs(store)) == 5, "no slot is dropped"


async def test_the_night_the_clocks_go_back_runs_two_am_once(store, cfg):
    cfg.timezone = "Europe/Berlin"
    cfg.schedules = {"tidy": _s("tidy", "0 2 * * *")}
    clock = Clock(datetime(2026, 10, 25, 3, tzinfo=UTC))
    await store.kv_set(CHECKED, iso(datetime(2026, 10, 24, 12, tzinfo=UTC)))
    await Scheduler(_app(store, cfg), clock).make_due()
    assert len(await _runs(store)) == 1


class Router:
    def __init__(self):
        self.why: str | None = "llama-swap does not answer"

    async def can_answer(self, role):
        return self.why

    async def wait_change(self, timeout):
        await anyio.sleep(timeout)


async def test_a_run_waits_for_a_model_and_for_what_it_follows_then_runs_its_own_night(
        store, cfg):
    cfg.schedules = {"world": _s("world", prompt="News of {{day_before}}?"),
                     "day": _s("day", "0 3 * * *", "Day after [{{result:world}}]",
                               after=("world",))}
    router = Router()
    clock = Clock(datetime(2026, 10, 9, 1, tzinfo=UTC))
    backend = FakeBackend(echo=True)
    app = _app(store, cfg, backend, llm=SimpleNamespace(router=router))
    async with _scheduler(store, cfg, app, backend, clock, routed=True) as sched:
        await sched.make_due()
        clock.at = datetime(2026, 10, 10, 1, tzinfo=UTC)   # the host was off all night
        await sched.tick()
        rows = await _runs(store)
        assert [(r["night"], r["schedule"], r["state"]) for r in rows] == [
            ("2026-10-09", "world", "waiting"), ("2026-10-09", "day", "waiting")]
        assert rows[0]["reason"] == "Waiting for a model: llama-swap does not answer"
        assert rows[1]["reason"] == "Runs after World"

        router.why = None                                 # the next day, Nix is on
        await sched.tick()
        assert sched.current == rows[0]["id"]
        await until(lambda: _state(store, rows[0]["id"], "done"))
        await sched.tick()
        await until(lambda: _state(store, rows[1]["id"], "done"))
    world, day = await _runs(store)
    assert "News of 2026-10-08?" in world["result"], "its own night, not today"
    assert f"[{world['result']}]" in day["result"], "the result of the same night"
    cid = world["conversation_id"]
    assert cid not in [r["id"] for r in await conversations.background(
        store)], "a run is in Nights, not under Background tasks"
    title = await store.read(lambda c: c.execute("SELECT title FROM conversations WHERE id = ?",
                                                 (cid,)).fetchone()[0])
    assert title == "World, Fri 9 Oct", "the night in words"
    assert cid not in [c["id"] for c in await conversations.list(store)]


async def _state(store, rid, state) -> bool:
    r = await store.read(lambda c: c.execute("SELECT state FROM runs WHERE id = ?",
                                             (rid,)).fetchone())
    return r["state"] == state


async def test_a_moment_before_noon_is_that_dates_night_from_noon_the_next(store, cfg):
    from zoneinfo import ZoneInfo

    from celine.schedules import night_of, night_start, run_date
    berlin = ZoneInfo("Europe/Berlin")
    at17 = datetime(2026, 10, 10, 17, tzinfo=berlin)
    assert run_date(_s("radar", daytime=True), at17, berlin) == date(2026, 10, 10), \
        "a daytime job writes under today"
    assert run_date(_s("watch"), at17, berlin) == date(2026, 10, 11), "a night job its night"
    assert night_of(datetime(2026, 10, 10, 11, 59, tzinfo=berlin), berlin) == date(2026, 10, 10)
    assert night_of(datetime(2026, 10, 10, 12, 0, tzinfo=berlin), berlin) == date(2026, 10, 11)
    assert night_of(datetime(2026, 10, 10, 10, 30, tzinfo=UTC), berlin) == date(2026, 10, 11)
    assert night_start(date(2026, 10, 11), berlin) == datetime(2026, 10, 10, 12, tzinfo=berlin)
    cfg.schedules = {"world": _s("world", "0 23 * * *")}
    sched = Scheduler(_app(store, cfg), Clock(datetime(2026, 10, 10, 14, tzinfo=UTC)))
    assert (await sched.run_now("world"))["night"] == "2026-10-11", "Run now: tonight"
    await store.kv_set(CHECKED, iso(datetime(2026, 10, 10, 14, tzinfo=UTC)))
    sched.now = Clock(datetime(2026, 10, 10, 23, 30, tzinfo=UTC))
    await sched.make_due()
    assert {r["night"] for r in await _runs(store)} == {"2026-10-11"}, "23:00 too"
    sched.now = Clock(datetime(2026, 10, 10, 22, 59, 30, tzinfo=UTC))
    assert sched.until_next() == 30, "it wakes at the next slot"


class Hanging(FakeBackend):
    async def stream(self, messages, tools, body=None):
        await anyio.sleep_forever()
        yield  # unreachable: makes this a generator


async def test_stop_ends_a_waiting_run_and_aborts_a_running_one(
        store, cfg):
    cfg.schedules = {"world": _s("world")}
    clock = Clock(datetime(2026, 10, 10, 2, tzinfo=UTC))
    backend = Hanging()
    app = _app(store, cfg, backend)
    await store.kv_set(CHECKED, iso(clock.at))
    async with _scheduler(store, cfg, app, backend, clock) as sched:
        waiting = await sched.run_now("world")
        stopped = await sched.end(waiting["id"], "stopped", cfg.substitute(runs_mod.STOPPED))
        assert (stopped["state"], stopped["reason"]) == ("stopped", "Stopped by Joe")
        assert await sched.end(waiting["id"], "stopped", "again") is None, "it has finished"

        clock.at += timedelta(minutes=1)
        run = await sched.run_now("world")
        await sched.tick()
        await until(lambda: _live(store, run["id"]))
        out = await sched.end(run["id"], "stopped", "Stopped by Joe")
        assert (out["state"], out["reason"]) == ("stopped", "Stopped by Joe")
        cid = out["conversation_id"]
        await until(lambda: _quiet(store, cid))
        assert (await store.read(lambda c: runs_mod.outcome(c, cid)))[0] == "aborted"
        await until(lambda: _idle(sched))
        [row] = [r for r in await _runs(store) if r["id"] == run["id"]]
        assert row["state"] == "stopped", "the cancelled try writes nothing after"


async def _live(store, rid) -> bool:
    r = await store.read(lambda c: c.execute(
        "SELECT state, conversation_id FROM runs WHERE id = ?", (rid,)).fetchone())
    return r["state"] == "running" and bool(r["conversation_id"]) and \
        not await _quiet(store, r["conversation_id"])


async def _quiet(store, cid) -> bool:
    from celine.sub_agents.report import quiet
    return await store.read(lambda c: quiet(c, cid))


async def _idle(sched) -> bool:
    return sched.current is None


class Failing(FakeBackend):
    async def stream(self, messages, tools, body=None):
        raise BackendError("the model broke")
        yield  # unreachable: makes this a generator


async def test_a_model_error_fails_the_run_once(store, cfg):
    cfg.schedules = {"world": _s("world")}
    backend = Failing()
    app = _app(store, cfg, backend)
    async with _scheduler(store, cfg, app, backend,
                          Clock(datetime(2026, 10, 10, 2, tzinfo=UTC))) as sched:
        run = await sched.run_now("world")
        await sched.tick()
        await until(lambda: _state(store, run["id"], "failed"))
        await until(lambda: _idle(sched))
        await sched.tick()
    [row] = await _runs(store)
    assert row["state"] == "failed" and "the model broke" in row["error"]
    assert await _turns(store, row["conversation_id"]) == 1, "one try, no second turn"


async def _turns(store, cid) -> int:
    return await store.read(lambda c: c.execute(
        "SELECT count(*) FROM turns WHERE conversation_id = ?", (cid,)).fetchone()[0])


class Unreachable(FakeBackend):
    """The model server is down for the first call (the Router notes it), up after."""

    def __init__(self, router):
        super().__init__(echo=True)
        self.router, self.down = router, True

    async def stream(self, messages, tools, body=None):
        if self.down:
            self.down = False
            self.router.why = "llama-swap does not answer"
            e = BackendError("llama-swap refused the connection")
            e.unreachable = True
            raise e
        async for ev in super().stream(messages, tools, body):
            yield ev


async def test_an_unreachable_model_leaves_the_run_waiting_and_its_turn_goes_on_once_it_answers(
        store, cfg):
    cfg.schedules = {"world": _s("world", prompt="News of {{day_before}}?")}
    router = Router()
    router.why = None
    backend = Unreachable(router)
    app = _app(store, cfg, backend, llm=SimpleNamespace(router=router))
    async with _scheduler(store, cfg, app, backend, Clock(datetime(2026, 10, 10, 2, tzinfo=UTC)),
                          routed=True) as sched:
        run = await sched.run_now("world")
        await sched.tick()
        await until(lambda: _state(store, run["id"], "waiting"))
        await until(lambda: _idle(sched))
        [row] = await _runs(store)
        assert row["reason"] == "Waiting for a model: llama-swap does not answer"
        cid = row["conversation_id"]
        assert cid and await _turns(store, cid) == 1
        await sched.tick()
        [row] = await _runs(store)
        assert row["state"] == "waiting" and sched.current is None, "still down"
        router.why = None                        # the Router sees it answer
        await sched.tick()
        await until(lambda: _state(store, run["id"], "done"))
    [row] = await _runs(store)
    assert row["conversation_id"] == cid, "the same conversation"
    assert await _turns(store, cid) == 1, "the failed turn was continued, not asked again"
    assert "News of 2026-10-09?" in row["result"]


async def test_a_run_is_over_only_once_its_sub_agents_have_reported(store):
    """The agent may hand conversations to sub-agents and end its turn: the run waits for
    their reports (each wakes it) before it takes its answer."""
    from celine.intercom import send
    from celine.turns import store as turns
    run = await conversations.for_trigger(store, "run:r:1", "Self-review")
    sub = await conversations.create(store, "scout", agent_of=run, agent_name="scout")
    assert await store.read(lambda c: runs_mod.finished(c, run))
    out = await send(store, f"agent:{run}", f"agent:{sub}", "Review the groceries chat.",
                     kind="brief")
    assert not await store.read(lambda c: runs_mod.finished(c, run)), "it works"
    await turns.claim(store, "w")
    await turns.transition(store, out["turn_id"], sub, "done")
    assert not await store.read(lambda c: runs_mod.finished(c, run)), "not reported yet"
    rep = await send(store, f"agent:{sub}", f"agent:{run}", "It went fine.", kind="report")
    assert not await store.read(lambda c: runs_mod.finished(c, run)), "the report wakes her"
    await turns.claim(store, "w")
    await turns.transition(store, rep["turn_id"], run, "done")
    assert await store.read(lambda c: runs_mod.finished(c, run))


async def test_a_run_that_was_running_at_the_stop_runs_on_in_its_conversation(store, cfg):
    """A graceful stop ends its turn `interrupted`; the next start's scheduler picks the
    run up where it was and continues that turn: the same conversation, the same turn."""
    class Hang(FakeBackend):
        """Hangs until the restart; answers after it."""
        def __init__(self):
            super().__init__(echo=True)
            self.hang = True
            self.asked = anyio.Event()

        async def stream(self, messages, tools, body=None):
            if self.hang:
                self.asked.set()
                await anyio.sleep_forever()
            async for ev in super().stream(messages, tools, body):
                yield ev

    from celine.turns import store as turns
    cfg.schedules = {"world": _s("world", prompt="News of {{day_before}}?")}
    clock = Clock(datetime(2026, 10, 10, 2, 30, tzinfo=UTC))
    backend = Hang()
    app = _app(store, cfg, backend)
    await store.kv_set(CHECKED, iso(clock.at))
    async with anyio.create_task_group() as tg:
        d = Dispatcher(store, cfg, EventBus(), lambda: backend, app.registry)
        app.dispatcher = d
        app.intercom = d.intercom = Intercom(store, d.notify)
        await d.start(tg)
        sched = Scheduler(app, clock)
        sched._tg = tg
        run = await sched.run_now("world")
        await sched.tick()
        await until(lambda: _live(store, run["id"]))
        await backend.asked.wait()                  # its turn runs
        await d.stop(grace=0.2)
        tg.cancel_scope.cancel()
    [row] = await _runs(store)
    cid = row["conversation_id"]
    assert row["state"] == "running" and cid
    assert (await store.read(lambda c: runs_mod.outcome(c, cid)))[0] == "interrupted"

    backend.hang = False                            # the next start
    await turns.sweep(store, app.registry)
    assert await store.write(sched._interrupted) == [row], "the run runs on"
    async with anyio.create_task_group() as tg:
        d = Dispatcher(store, cfg, EventBus(), lambda: backend, app.registry)
        app.dispatcher = d
        app.intercom = d.intercom = Intercom(store, d.notify)
        await d.start(tg)
        sched = Scheduler(app, clock)
        await sched.run(tg)
        assert sched.current == run["id"]
        await until(lambda: _state(store, run["id"], "done"))
        await d.stop()
        tg.cancel_scope.cancel()
    [row] = await _runs(store)
    assert row["conversation_id"] == cid, "the same conversation, no new one"
    assert await _turns(store, cid) == 1, "the same turn, continued"
    assert "News of 2026-10-09?" in row["result"]
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM conversations WHERE trigger_key LIKE 'run:%'").fetchone()[0]) == 1


def _calls(*calls):
    return [ToolCallDelta(index=i, id=f"c{i}", name=n, arguments=json.dumps(a))
            for i, (n, a) in enumerate(calls)] + [Done(reason="stop")]


class Session(FakeBackend):
    """The partner reads the day first, then asks "Question <n>."; it answers each
    line, and its final answer is two lines: The entry. The end."""

    def __init__(self):
        super().__init__()
        self.partner: list[tuple[str, list[str]]] = []

    async def stream(self, messages, tools, body=None):
        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        if system.startswith("You are the mentor"):
            self.partner.append((system, [t["function"]["name"] for t in tools]))
            lines = [m for m in messages if m["role"] == "assistant"
                     and not m.get("tool_calls")]
            out = _calls(("past_conversations__list_day", {"date": "2026-10-09"})) \
                if len(self.partner) == 1 else \
                [TextDelta(f"Question {len(lines) + 1}."), Done(reason="stop")]
        else:
            last = str([m for m in messages if m["role"] == "user"][-1]["content"])
            out = [TextDelta("The entry.\nThe end." if "The talk with mentor is over" in last
                             else f"Answer to {last.strip().splitlines()[-1]}"),
                   Done(reason="stop")]
        for ev in out:
            yield ev


async def test_a_partner_and_she_alternate_up_to_the_rounds_then_her_answer_is_the_note(
        store, cfg):
    cfg.schedules = {
        "review": _s("review", "0 4 * * *"),
        "reflection": _s("reflection", "0 5 * * *", "Session about {{day_before}}.",
                      label="Reflection", after=("review",), partner="mentor.md",
                      partner_prompt="You are the mentor. Review: {{result:review}}",
                      rounds=3, note="diary/{{day_before}}-session.md")}
    night = "2026-10-10"
    await store.write(lambda c: c.execute(
        "INSERT INTO runs(id, schedule, label, slot, night, state, result, created_at) VALUES "
        "('r', 'review', 'Review', '2026-10-10T04:00:00.000+00:00', ?, 'done', 'A good day.', "
        "'2026-10-10T04:00:00.000+00:00')", (night,)))
    backend = Session()
    async with _scheduler(store, cfg, _app(store, cfg, backend), backend,
                          Clock(datetime(2026, 10, 10, 5, tzinfo=UTC))) as sched:
        run = await sched.run_now("reflection")
        await sched.tick()
        await until(lambda: _state(store, run["id"], "done"))
    row = next(r for r in await _runs(store) if r["schedule"] == "reflection")
    assert row["result"] == "The entry.\nThe end."
    cid = row["conversation_id"]
    said = [((m["intercom"] or {}).get("from") or m["role"], m["content"])
            for m in (await view.snapshot(store, cid))["messages"]
            if m["role"] in ("user", "assistant") and m["content"]]
    assert said[0][0] == "system:schedules" and said[0][1].startswith(
        "Session about 2026-10-09.")
    assert [w for w, _ in said[1:]] == ["assistant"] + [
        "system:mentor", "assistant"] * 3 + ["system:schedules", "assistant"]
    assert [t for w, t in said if w == "system:mentor"] == [
        "Question 1.", "Question 2.", "Question 3."]
    first, tools = backend.partner[0]
    assert "Review: A good day." in first and "This is line 1 of at most 3." in first
    assert "past_conversations__list_day" in tools and "notebook__write" not in tools
    assert runs_mod.LAST in backend.partner[-1][0]
    note = cfg.her.notebook / "diary" / "2026-10-09-session.md"
    assert note.read_text().endswith("The entry.\nThe end.")


async def test_the_api_ships_four_schedules_and_edits_previews_runs_and_deletes(
        tmp_path, monkeypatch, store):
    import tomllib

    from celine.schedules import api as schedules_api
    from settings_fixtures import client, make_app, scratch
    var = scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(schedules_api.router)
    services = app.state.services
    services.schedules = Scheduler(services)
    async with client(app) as api:
        got = (await api.get("/api/schedules")).json()
        by_name = {s["name"]: s for s in got["schedules"]}
        reflection = by_name["reflection"]
        assert (reflection["time"], reflection["after"]) == ("05:00", ["self-review"])
        assert reflection["every"] == "Every day at 05:00" and reflection["rounds"] == 8
        assert reflection["next_at"] == "05:00" and reflection["next_night"] >= got["tonight"]
        assert reflection["partner"] == "mentor.md" and "neutral and direct" in \
            reflection["partner_prompt"]
        day = by_name["user-day"]
        assert (day["label"], day["title"]) == ("{{username}}'s day", f"{cfg.user}'s day"), \
            "the label as written, the title filled"
        assert "result:self-review" in got["variables"]
        assert [r["name"] for r in got["roles"]] == ["chat", "tasks", "small"], \
            "the roles a schedule may run on"
        for s in got["schedules"]:
            for text in (s["prompt"], s["partner_prompt"]):
                assert "—" not in text and " · " not in text

        body = {"label": "Morning", "time": "07:30", "prompt": "Plan {{date}}.",
                "after": [], "partner_prompt": "", "rounds": 3, "note": "", "role": "chat",
                "on": True}
        r = await api.put("/api/schedules/morning", json=body)
        assert r.status_code == 200 and r.json()["cron"] == "30 7 * * *"
        saved = tomllib.loads((var / "settings" / "schedules" / "morning.toml").read_text())
        assert saved["prompt"] == "Plan {{date}}." and saved["partner"] == ""
        assert await store.read(lambda c: c.execute(
            "SELECT by FROM text_versions WHERE of='routine:morning:prompt'").fetchone()[0]) \
            == "user", "the prompt goes into its history"
        for bad, field in (({"time": "25:00"}, "time"), ({"after": ["nope"]}, "after"),
                           ({"note": "../x.md"}, "note"), ({"rounds": 0}, "rounds"),
                           ({"role": "roleplay"}, "role")):
            r = await api.put("/api/schedules/morning", json={**body, **bad})
            assert r.status_code == 400 and r.json()["error"]["detail"]["field"] == field
        r = await api.put("/api/schedules/user-day", json={**body, "label": day["label"],
                                                          "after": ["world"]})
        assert (r.json()["label"], r.json()["title"]) == (day["label"], day["title"])
        assert tomllib.loads((var / "settings" / "schedules" / "user-day.toml").read_text()
                             )["label"] == "{{username}}'s day", "the placeholder is kept"
        weekly = {k: v for k, v in body.items() if k != "time"} | {"cron": "30 9 * * 1"}
        r = await api.put("/api/schedules/morning", json=weekly)
        assert r.json()["every"] == "Next: Mon 09:30", "the next time, not the cron line"
        r = await api.put("/api/schedules/world", json={**body, "after": ["reflection"]})
        assert r.status_code == 400 and r.json()["error"]["message"] == \
            "Reflection already runs after Morning.", "the circle in words"

        r = await api.post("/api/schedules/preview", json={
            "night": "2026-10-10", "text": "{{day_before}} [{{result:world}}]"})
        assert r.json() == {"text": "2026-10-09 [(none)]"}

        run = (await api.post("/api/schedules/morning/run")).json()["run"]
        assert run["state"] == "waiting" and run["label"] == "Morning"
        listed = (await api.get(f"/api/runs?night={run['night']}")).json()
        assert [x["id"] for x in listed["runs"]] == [run["id"]]
        assert len(listed["nights"]) == 14
        earlier = (await api.get("/api/runs?until=2026-01-20")).json()["nights"]
        assert [earlier[0]["night"], earlier[-1]["night"]] == ["2026-01-20", "2026-01-07"]
        one = (await api.get(f"/api/runs/{run['id']}")).json()
        assert one["prompt"] == f"Plan {run['night']}."

        r = await api.delete("/api/schedules/world")
        assert r.status_code == 400 and "runs after" in r.json()["error"]["message"]
        assert (await api.delete("/api/schedules/morning")).json()["deleted"] == "morning"
        assert "morning" not in [s["name"] for s in (await api.get(
            "/api/schedules")).json()["schedules"]]
        gone = (await api.get(f"/api/runs/{run['id']}")).json()["run"]
        assert (gone["label"], gone["state"], gone["reason"]) == (
            "Morning", "skipped", runs_mod.DELETED), "deleting stops its runs, the label stays"
        r = await api.post(f"/api/runs/{run['id']}/stop")
        assert r.status_code == 400, "it has finished"
        assert (await api.post("/api/runs/nope/stop")).status_code == 404


class Writer(FakeBackend):
    """A real run: a lead-in line beside the notebook__write of
    the whole note, which ends its turn (the runner's quiet end). `script` maps words of
    the newest message to its step."""

    def __init__(self, script: dict):
        super().__init__()
        self.script = script

    async def stream(self, messages, tools, body=None):
        self.calls.append(messages)
        last = json.dumps(messages[-1]["content"])
        step = next(v for k, v in self.script.items() if k in last)
        if isinstance(step, Exception):
            raise step
        for ev in step:
            yield ev


def _write(note: str, content: str) -> list:
    return [ToolCallDelta(index=0, id="w", name="notebook__write", arguments=json.dumps(
        {"path": note, "content": content, "summary": "The review."})),
        Done(reason="tool_calls")]


NOTE = "diary/2026-10-10-review.md"     # {{date}} on _one_run's clock


async def _one_run(store, cfg, backend) -> dict:
    """One run of a self-review with a note."""
    cfg.schedules = {"review": _s("review", "0 4 * * *", "Review the night.",
                                  label="Self-review", note="diary/{{date}}-review.md")}
    async with _scheduler(store, cfg, _app(store, cfg, backend), backend,
                          Clock(datetime(2026, 10, 10, 4, tzinfo=UTC))) as sched:
        run = await sched.run_now("review")
        await sched.tick()
        await until(lambda: _state(store, run["id"], "done"))
    [row] = await _runs(store)
    return row


async def test_a_note_she_wrote_is_the_result_not_the_line_beside_the_write(store, cfg):
    review = "# Self-review\n\nWent well: the search.\nDid not: the mail."
    backend = Writer({"Review the night": [TextDelta("Writing the review now."),
                                           *_write(NOTE, review)]})
    row = await _one_run(store, cfg, backend)
    note = (cfg.her.notebook / NOTE).read_text()
    assert note.endswith(review), "nothing is written over the note it wrote"
    assert "Writing the review now" not in note
    assert row["result"] == review, "the note is the result the next run reads"
    assert len(backend.calls) == 1, "its turn ended with the write, no step more"


async def test_a_new_note_gets_her_answer_whole(store, cfg):
    backend = Writer({"Review the night": [TextDelta("Went well: the search.\nDid not: the mail."),
                                           Done(reason="stop")]})
    row = await _one_run(store, cfg, backend)
    from celine.skills.notebook import unstamped
    note = unstamped((cfg.her.notebook / NOTE).read_text())
    assert note.strip() == "Went well: the search.\nDid not: the mail." == row["result"]
    assert len(backend.calls) == 1, "its first final answer is the result: no second ask"


async def test_a_note_she_wrote_again_keeps_its_text_not_the_closing_line(store, cfg):
    """The note was there with the same text she writes: her write still makes it hers,
    her closing line is not appended."""
    review = "# Self-review\n\nWent well: the search."
    note = cfg.her.notebook / NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(review)
    backend = Writer({"Review the night": [TextDelta("Got the whole picture now."),
                                           *_write(NOTE, review)]})
    row = await _one_run(store, cfg, backend)
    from celine.skills.notebook import unstamped
    assert unstamped(note.read_text()) == review, "the note she wrote stays as she wrote it"
    assert row["result"] == review


async def test_a_note_she_did_not_write_gets_her_answer_appended(store, cfg):
    note = cfg.her.notebook / NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Reviews\n")
    backend = Writer({"Review the night": [TextDelta("Went well: the search."),
                                           Done(reason="stop")]})
    row = await _one_run(store, cfg, backend)
    assert note.read_text().endswith("Went well: the search.\n")
    assert row["result"] == "Went well: the search."


async def test_a_log_note_she_adds_to_gives_only_what_she_added(store, cfg):
    note = cfg.her.notebook / NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Reviews\n\nThe night before.\n")
    added = "\n## Tonight\n\nWent well: the search.\n"
    backend = Writer({"Review the night": [TextDelta("Adding tonight."), *[
        ToolCallDelta(index=0, id="w", name="notebook__write", arguments=json.dumps(
            {"path": NOTE, "content": added, "mode": "append",
             "summary": "Reviews."})), Done(reason="tool_calls")]]})
    row = await _one_run(store, cfg, backend)
    assert note.read_text().endswith("The night before.\n" + added), "nothing more appended"
    assert row["result"] == added.strip(), "the result is tonight's part"


async def test_a_stop_of_a_turn_that_never_ran_still_tells_its_end(store, cfg):
    """A run is over when its conversation is quiet, looked at after every turn end: a
    queued turn a Stop ends at once tells its end too, no worker would."""
    d = Dispatcher(store, cfg, EventBus())
    heard = []

    async def listen(cid, turn):
        heard.append((cid, turn))
    d.on_end(listen)
    cid = await conversations.create(store, "Run")
    _, tid, _ = await user_says(store, cid, "go")
    assert await d.abort_conversation(cid) == tid
    assert heard == [(cid, tid)]


async def test_a_run_stopped_before_it_begins_stays_stopped_and_nothing_takes_the_app_down(
        store, cfg):
    """The scheduler's `running` write only takes a run still waiting or running: a Stop
    that landed between the run's start and that write leaves no zombie row, and a
    second running run fails instead of raising into the lifespan."""
    cfg.schedules = {"world": _s("world"), "other": _s("other")}
    app = _app(store, cfg, FakeBackend(echo=True))
    sched = Scheduler(app, Clock(datetime(2026, 10, 10, 2, tzinfo=UTC)))
    run = await sched.run_now("world")
    stale = await store.read(lambda c: dict(c.execute(
        "SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone()))
    await sched.end(run["id"], "stopped", "Stopped by Joe")
    with anyio.move_on_after(3):          # a run taken anyway waits for a turn forever
        await sched._run_as(run["id"], stale)
    [row] = await _runs(store)
    assert row["state"] == "stopped", "no running row without a task"

    other = await sched.run_now("other")
    await store.write(lambda c: c.execute(
        "UPDATE runs SET state = 'running' WHERE id = ?", (run["id"],)))
    taken = await store.read(lambda c: dict(c.execute(
        "SELECT * FROM runs WHERE id = ?", (other["id"],)).fetchone()))
    with anyio.fail_after(3):
        await sched._run_as(other["id"], taken)
    got = await store.read(lambda c: c.execute(
        "SELECT state FROM runs WHERE id = ?", (other["id"],)).fetchone()[0])
    assert got == "failed", "one running run at most, and no exception"


class HangsOnce(FakeBackend):
    """The first call waits until it is stopped; every later one answers."""

    def __init__(self):
        super().__init__(echo=True)
        self.hung = False

    async def stream(self, messages, tools, body=None):
        if not self.hung:
            self.hung = True
            await anyio.sleep_forever()
        async for ev in super().stream(messages, tools, body):
            yield ev


async def test_a_freeze_leaves_the_run_in_flight_waiting_and_the_unfreeze_runs_it_on(
        store, cfg):
    from celine.guard import freeze
    from test_bastet import OWNER
    cfg.schedules = {"world": _s("world")}
    backend = HangsOnce()
    app = _app(store, cfg, backend)
    async with _scheduler(store, cfg, app, backend,
                          Clock(datetime(2026, 10, 10, 2, tzinfo=UTC))) as sched:
        run = await sched.run_now("world")
        await sched.tick()
        await until(lambda: backend.hung)          # its turn streams
        await freeze.freeze(app, by="user", why="a test")
        await until(lambda: _state(store, run["id"], "waiting"))
        await until(lambda: _idle(sched))
        await sched.tick()
        [row] = await _runs(store)
        assert (row["state"], row["reason"]) == ("waiting", runs_mod.FROZEN), \
            "it waits, not failed, and does not start while frozen"
        await freeze.unfreeze(app, by=OWNER)
        await sched.tick()
        await until(lambda: _state(store, run["id"], "done"))
    [row] = await _runs(store)
    assert await _turns(store, row["conversation_id"]) == 1, "the cut turn went on"


async def test_routines_list_and_read_tell_a_run_in_the_app_zone(store, cfg):
    from celine.schedules.skill import Routines
    from celine.skills import ToolContext
    cfg.timezone = "Europe/Berlin"
    cfg.schedules = {"world": _s("world")}
    run_cid = await conversations.create(store, "run")
    await store.write(lambda c: c.execute(
        "INSERT INTO runs(id, schedule, label, slot, night, state, conversation_id, "
        "created_at) VALUES('r', 'world', 'World', '2026-10-30T01:00:00.000+00:00', "
        "'2026-10-30', 'waiting', ?, 'x')", (run_cid,)))
    skill = Routines(cfg)
    skill.app = SimpleNamespace(cfg=cfg)
    ctx = ToolContext(conversation_id="c", turn_id="t", tool_call_id="x", store=store,
                      config=cfg, emit=None)  # type: ignore[arg-type]
    listed = (await skill.list(ctx)).content
    assert listed.endswith(", last run Fri 30 Oct 02:00 waiting"), listed
    assert "- run:r Fri 30 Oct 02:00 waiting" in (await skill.read(ctx, "world")).content
