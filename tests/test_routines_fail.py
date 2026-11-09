"""When a run fails (schedules/runs.py _fail): a Skip routine ends skipped and quiet and
runs once after days off, a Retry routine's failed run runs again up to three times for
the same night and then tells, the run page's words (nights failed, Waiting while its work waits
for a model, reason_why), and the routes that go with them."""
from __future__ import annotations

import importlib.util
import tomllib
from datetime import datetime, timedelta, timezone
import pytest

from celine import boot
from celine.backends.base import BackendError
from celine.config import CONFIG_DIR
from celine.core.ids import iso, new_id, now
from celine.db.migrate import MIGRATIONS_DIR
from celine.schedules import Schedule, parse, runs as runs_mod
from celine.schedules.runs import CHECKED, RunClock, Scheduler, view, why_alone
from celine.settings.files import Invalid
from chat_fixtures import until
from fake_backend import FakeBackend
from test_schedules import Clock, Failing, Hanging, _app, _idle, _notices, _runs, \
    _scheduler, _state

pytestmark = pytest.mark.anyio
UTC = timezone.utc


@pytest.fixture
def cfg():
    c = boot.load()
    c.timezone = "UTC"
    c.settings.general.update({"max_concurrent_turns": 2})
    c.settings.features["done"] = True
    c.user = "Joe"
    return c


def _s(name, cron="0 2 * * *", prompt="Go.", **kw) -> Schedule:
    return Schedule(name, kw.pop("label", name.title()), cron, prompt, **kw)


class FailsOnce(FakeBackend):
    """The first request fails with a model error, the next ones echo."""

    def __init__(self):
        super().__init__(echo=True)
        self.failed = False

    async def stream(self, messages, tools, body=None, model=None):
        if not self.failed:
            self.failed = True
            raise BackendError("the model broke")
        async for ev in super().stream(messages, tools, body, model):
            yield ev


# ---- When a run fails: Retry or Skip ---------------------------------------------------

def test_when_a_run_fails_is_retry_without_the_key_and_only_retry_or_skip():
    base = {"label": "World", "cron": "0 2 * * *", "prompt": "Go."}
    assert parse("world", base, None).on_fail == "retry"
    assert parse("world", {**base, "on_fail": "skip"}, None).raw()["on_fail"] == "skip"
    with pytest.raises(Invalid) as e:
        parse("world", {**base, "on_fail": "later"}, None)
    assert e.value.field == "on_fail"


def test_the_shipped_skip_routines_are_daydream_followed_sites_and_watch_videos():
    folder = CONFIG_DIR / "schedules"
    got = {p.stem: tomllib.loads(p.read_text())["on_fail"] for p in folder.glob("*.toml")}
    assert {k for k, v in got.items() if v == "skip"} == {"daydream", "radar", "watch"}
    assert set(got.values()) == {"retry", "skip"}, "every shipped file states it"


def test_the_migration_makes_the_three_saved_routines_skip_once(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "m0021", next(MIGRATIONS_DIR.glob("0021_*.py")))
    step = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step)
    db = tmp_path / "celine.db"
    step.up(None, db)                                 # no schedules: nothing to do
    folder = tmp_path / "settings" / "schedules"
    folder.mkdir(parents=True)
    plain = 'label = "X"\ncron = "0 2 * * *"\nprompt = "Go."\n'
    (folder / "daydream.toml").write_text(plain)
    (folder / "radar.toml").write_text(plain + 'on_fail = "retry"\n')
    (folder / "world.toml").write_text(plain)
    (folder / "watch.toml").write_text("not toml [")
    step.up(None, db)
    assert tomllib.loads((folder / "daydream.toml").read_text())["on_fail"] == "skip"
    assert tomllib.loads((folder / "radar.toml").read_text())["on_fail"] == "retry", \
        "a choice the user made stays"
    assert (folder / "world.toml").read_text() == plain, "a Retry routine needs no key"
    assert (folder / "watch.toml").read_text() == "not toml ["
    before = (folder / "daydream.toml").read_bytes()
    step.up(None, db)
    assert (folder / "daydream.toml").read_bytes() == before, "run again, it writes nothing"


async def test_a_skip_routine_that_fails_ends_skipped_with_no_notice_and_no_run_again(
        store, cfg):
    cfg.schedules = {"daydream": _s("daydream", on_fail="skip")}
    backend = Failing()
    app = _app(store, cfg, backend)
    async with _scheduler(store, cfg, app, backend,
                          Clock(datetime(2026, 10, 10, 2, tzinfo=UTC))) as sched:
        run = await sched.run_now("daydream")
        await sched.tick()
        await until(lambda: _state(store, run["id"], "skipped"))
        await until(lambda: _idle(sched))
    [row] = await _runs(store)
    assert "the model broke" in row["reason"] and row["error"] is None
    assert await _notices(store) == 0
    from celine.skills.conversations import _ended
    assert await store.read(lambda c: _ended(c, row["conversation_id"], f"run:{row['id']}")) \
        == "run Daydream, skipped", "what the agent reads when it looks back"


async def test_a_skip_routine_after_days_off_waits_once_for_its_newest_slot(store, cfg):
    cfg.schedules = {"daydream": _s("daydream", on_fail="skip"), "world": _s("world")}
    clock = Clock(datetime(2026, 10, 10, 3, tzinfo=UTC))
    sched = Scheduler(_app(store, cfg), clock)
    await store.kv_set(CHECKED, iso(clock.at - timedelta(days=7)))
    await sched.make_due()
    rows = await _runs(store)
    mine = [r for r in rows if r["schedule"] == "daydream"]
    assert [r["state"] for r in mine] == ["skipped"] * 6 + ["waiting"]
    assert {r["reason"] for r in mine[:-1]} == {runs_mod.NEXT_DUE}
    assert [r["state"] for r in rows if r["schedule"] == "world"] == ["waiting"] * 7, \
        "a Retry routine catches up every slot"


# ---- Retry runs again ---------------------------------------------------------------------

async def test_no_run_again_when_it_would_fall_past_the_next_slot(store, cfg):
    cfg.schedules = {"hourly": _s("hourly", "0 * * * *")}
    backend = Failing()
    async with _scheduler(store, cfg, _app(store, cfg, backend), backend,
                          Clock(datetime(2026, 10, 10, 2, 50, tzinfo=UTC))) as sched:
        run = await sched.run_now("hourly")
        await sched.tick()
        await until(lambda: _state(store, run["id"], "failed"))
        await until(lambda: _idle(sched))
    assert len(await _runs(store)) == 1 and await _notices(store) == 1


async def test_no_run_again_after_the_time_limit(store, cfg):
    cfg.schedules = {"world": _s("world", wall_minutes=1)}
    backend = Hanging()
    clock = Clock(datetime(2026, 10, 10, 2, tzinfo=UTC))
    async with _scheduler(store, cfg, _app(store, cfg, backend), backend, clock) as sched:
        run = await sched.run_now("world")
        await sched.tick()
        await until(lambda: _state(store, run["id"], "running"))
        clock.at += timedelta(minutes=2)
        await sched.tick()
        await until(lambda: _state(store, run["id"], "failed"))
        await until(lambda: _idle(sched))
    [row] = await _runs(store)
    assert row["error"] == "Stopped at its time limit of 1 minute." and await _notices(store) == 1


async def test_a_run_again_fills_the_date_of_its_first_try(store, cfg):
    cfg.schedules = {"late": _s("late", "50 23 * * *", "About {{date}}.", daytime=True)}
    backend = FailsOnce()
    clock = Clock(datetime(2026, 10, 10, 23, 50, tzinfo=UTC))
    async with _scheduler(store, cfg, _app(store, cfg, backend), backend, clock) as sched:
        await store.kv_set(CHECKED, iso(clock.at - timedelta(minutes=1)))
        await sched.tick()
        [first] = await _runs(store)
        await until(lambda: _state(store, first["id"], "failed"))
        await until(lambda: _idle(sched))
        clock.at += runs_mod.RUN_AGAIN[0]                     # 00:05 the next day
        await sched.tick()
        again = next(r for r in await _runs(store) if r["again_of"] == first["id"])
        await until(lambda: _state(store, again["id"], "done"))
    again = next(r for r in await _runs(store) if r["id"] == again["id"])
    assert "About 2026-10-10." in again["result"] and again["night"] == first["night"]
    assert await _notices(store) == 0, "it landed: nothing to tell"


async def _api(tmp_path, monkeypatch, store):
    from celine.schedules import api as schedules_api
    from settings_fixtures import make_app, scratch
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(schedules_api.router)
    app.state.services.schedules = Scheduler(app.state.services)
    return app, cfg


async def _row(store, schedule="world", state="failed", night="2026-10-10", slot="02:00",
               again_of=None) -> str:
    rid = new_id()
    await store.write(lambda c: c.execute(
        "INSERT INTO runs(id, schedule, label, slot, night, state, created_at, again_of) "
        "VALUES(?,?,?,?,?,?,?,?)", (rid, schedule, schedule.title(),
                                    f"{night}T{slot}:00+00:00", night, state, now(),
                                    again_of)))
    return rid


async def test_retry_runs_a_failed_run_again_once_and_refuses_the_rest(
        tmp_path, monkeypatch, store):
    from settings_fixtures import client
    app, cfg = await _api(tmp_path, monkeypatch, store)
    failed = await _row(store)
    waiting = await _row(store, state="waiting", slot="03:00")
    async with client(app) as api:
        assert (await api.post("/api/runs/nope/retry")).status_code == 404
        assert (await api.post(f"/api/runs/{waiting}/retry")).status_code == 400
        r = await api.post(f"/api/runs/{failed}/retry")
        again = r.json()["run"]
        before = (await api.get(f"/api/runs/{failed}")).json()["run"]
        assert (again["state"], again["again_of"], again["night"]) == (
            "waiting", {"id": failed, "at": before["at"]}, "2026-10-10")
        assert again["schedule"] == "world" and again["retried"] is None
        r = await api.post(f"/api/runs/{failed}/retry")
        assert r.status_code == 400 and r.json()["error"]["message"] == \
            "This run was retried already."
        one = (await api.get(f"/api/runs/{failed}")).json()["run"]
        assert one["retried"] == {"id": again["id"], "at": again["at"]}


async def test_retry_and_run_now_wait_while_the_app_is_in_use_and_end_no_other_run(
        store, cfg):
    """A run the user started (Retry, Run now) of an idle_only Skip routine is not skipped
    while the app is in use, and ends none of its other waiting runs (NEXT_DUE); the
    clock's run keeps the idle rule."""
    from chat_fixtures import user_says
    from celine.conversations import store as conversations
    cfg.schedules = {"daydream": _s("daydream", "0 3 * * *", on_fail="skip", idle_only=True)}
    clock = Clock(datetime(2026, 10, 10, 14, tzinfo=UTC))
    sched = Scheduler(_app(store, cfg), clock)
    await user_says(store, await conversations.create(store, "talk"), "hi")  # a live chat
    skipped = await _row(store, "daydream", "skipped", slot="13:50")
    late = await _row(store, "daydream", "waiting", slot="13:55")
    again = await sched.retry(skipped)
    clock.at += timedelta(seconds=1)
    fresh = await sched.run_now("daydream")
    assert await _state(store, late, "waiting"), "nothing the user started ends it"
    await sched.tick()
    got = {r["id"]: (r["state"], r["reason"]) for r in await _runs(store)}
    assert got[again["id"]] == got[fresh["id"]] == ("waiting",
                                                    "Waiting until your chat is done")
    assert got[late] == ("skipped", runs_mod.IN_USE), "the clock's run keeps the rule"
    assert again["again_of"]["id"] == skipped


async def test_the_schedule_list_gives_its_choices_and_help_and_a_write_keeps_on_fail(
        tmp_path, monkeypatch, store):
    from settings_fixtures import client
    app, cfg = await _api(tmp_path, monkeypatch, store)
    async with client(app) as api:
        got = (await api.get("/api/schedules")).json()
        assert got["choices"]["on_fail"] == [{"value": "retry", "label": "Retry"},
                                             {"value": "skip", "label": "Skip"}]
        assert set(got["help"]) == {"on_fail", "idle_only", "wall_minutes"}
        for text in got["help"].values():
            assert text.endswith(".") and len(text) <= 220 and "{{" not in text
        body = {"label": "Morning", "time": "07:30", "prompt": "Go.", "after": [],
                "reviewer_prompt": "", "turns": 3, "note": "", "role": "chat", "on": True,
                "on_fail": "skip"}
        assert (await api.put("/api/schedules/morning", json=body)).json()["on_fail"] == "skip"
        rest = {k: v for k, v in body.items() if k != "on_fail"} | {"on": False}
        assert (await api.put("/api/schedules/morning", json=rest)).json()["on_fail"] == \
            "skip", "a write that leaves it out keeps it"
        r = await api.put("/api/schedules/morning", json={**body, "on_fail": "never"})
        assert r.status_code == 400 and r.json()["error"]["detail"]["field"] == "on_fail"


# ---- the run page in words --------------------------------------------------------------

async def test_a_night_is_failed_only_when_a_newest_try_failed(tmp_path, monkeypatch, store):
    from settings_fixtures import client
    app, cfg = await _api(tmp_path, monkeypatch, store)
    tonight = runs_mod.night_of(datetime.now(UTC), runs_mod.zone(cfg))
    a, b = (tonight - timedelta(days=1)).isoformat(), (tonight - timedelta(days=2)).isoformat()
    first = await _row(store, night=a)
    await _row(store, state="done", night=a, slot="02:15", again_of=first)
    await _row(store, state="done", night=b)
    await _row(store, schedule="day", night=b, slot="03:00")
    async with client(app) as api:
        nights = {n["night"]: n["failed"] for n in (await api.get("/api/runs")).json()["nights"]}
    assert (nights[a], nights[b], nights[tonight.isoformat()]) == (False, True, False)


def test_reason_why_leaves_out_the_waiting_its_state_word_says():
    assert why_alone("Waiting for Qwen3.8 Big. Main returned 503: The model server is "
                     "paused.") == "Main returned 503: The model server is paused."
    assert why_alone("Waiting until your chat is done") == "Until your chat is done"
    assert why_alone("Runs after World news") == "Runs after World news"
    assert why_alone("") == ""


async def test_a_running_run_reads_waiting_while_its_work_waits_for_a_model(store, cfg):
    cfg.schedules = {"world": _s("world")}
    sched = Scheduler(_app(store, cfg), Clock(datetime(2026, 10, 10, 2, tzinfo=UTC)))
    rid = await _row(store, state="running", night="2026-10-10")
    sched.current = rid
    sched._clock = clock = RunClock(None, sched.now, lambda: sched._tell(rid))

    async def told() -> list[str]:
        import json
        return await store.read(lambda c: [json.loads(d)["run"]["state"] for (d,) in c.execute(
            "SELECT data FROM global_events WHERE type = 'run.state' ORDER BY id")])
    clock.hold()
    await until(lambda: _len_is(told, 1))
    assert await told() == ["waiting"] and sched.held == rid
    clock.release()
    await until(lambda: _len_is(told, 2))
    assert await told() == ["waiting", "running"] and sched.held is None
    assert clock.left() is None, "no limit: nothing runs out"
    row = await store.read(lambda c: c.execute(f"{runs_mod.ROW} WHERE r.id = ?",
                                               (rid,)).fetchone())
    held = view(row, cfg, held=True)
    assert (held["state"], held["reason"], held["reason_why"]) == (
        "waiting", runs_mod.HELD, "For the model"), "the row says what it waits for"
    assert (view(row, cfg)["state"], view(row, cfg)["reason"]) == ("running", "")


async def _len_is(fn, n) -> bool:
    return len(await fn()) == n
