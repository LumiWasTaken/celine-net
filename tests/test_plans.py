"""The agent's plan (plans/): the rules of a plan, it keeps going while a step is open, an ask
checkpoint ends the turn and your answer lifts it, the transcript gets plan.state; its
Routines tools write and run as its own; a run has an intercom address; where it works."""
from __future__ import annotations

import json

import pytest

from celine import boot
from chat_fixtures import user_says

from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.conversations import here, store as conversations
from celine.core.bus import EventBus
from celine.plans import flow, store as plans
from celine.plans.skill import Plan
from celine.skills import Registry, ToolContext, ToolResult, tool
from celine.skills.meta import Skills
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn

pytestmark = pytest.mark.anyio


def test_one_step_is_doing_and_done_moves_to_the_next():
    steps = plans.clean(["Read", {"title": "Ask", "checkpoint": "ask"}, "Write"])
    assert [s["state"] for s in steps] == ["doing", "todo", "todo"], "the first open one"
    with pytest.raises(plans.Refused, match="Only one step"):
        plans.clean([{"title": "a", "state": "doing"}, {"title": "b", "state": "doing"}])
    with pytest.raises(plans.Refused, match="no title"):
        plans.clean([{"title": " "}])
    plan = {"text": "", "steps": steps, "waiting": False}
    with pytest.raises(plans.Refused, match="Step 1 is doing"):
        plans.move(plan, 3, "doing")
    steps, waiting = plans.move(plan, 1, "done")
    assert [s["state"] for s in steps] == ["done", "doing", "todo"] and not waiting
    plan = {**plan, "steps": steps}
    steps, waiting = plans.move(plan, 2, "done")
    assert [s["state"] for s in steps] == ["done", "done", "todo"] and waiting, \
        "an ask step waits on the user and starts nothing"
    assert flow.keeps_going({**plan, "steps": steps, "waiting": False})
    assert not flow.keeps_going({**plan, "steps": steps, "waiting": True})
    assert not flow.keeps_going(None)


class Replies(FakeBackend):
    def __init__(self, replies):
        super().__init__()
        self.replies = replies

    async def stream(self, messages, tools, body=None):
        self.calls.append(messages)
        for ev in self.replies[min(len(self.calls), len(self.replies)) - 1]:
            yield ev


def _calls(*calls) -> list:
    return [ToolCallDelta(index=i, id=f"c{i}", name=n, arguments=json.dumps(a))
            for i, (n, a) in enumerate(calls)] + [Done(reason="tool_calls")]


class Notebook:
    name = "notebook"
    description = "Her notes."

    @tool(read_only=False, needs_answer=False)
    async def write(self, ctx, path: str, text: str) -> ToolResult:
        """Write a note."""
        return ToolResult(f"wrote {path}")


async def _turn(store, cfg, cid, backend, text="go on") -> str:
    reg = Registry(cfg)
    reg.add(Notebook())
    reg.add(Plan(cfg))
    reg.add(Skills(reg, cfg))
    await user_says(store, cid, text)
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), reg)
    return turn["id"]


def _last_user(messages) -> str:
    content = [m for m in messages if m["role"] == "user"][-1]["content"]
    return content if isinstance(content, str) else " ".join(
        b.get("text", "") for b in content)


async def test_a_note_does_not_end_the_turn_while_a_step_is_open(store):
    cfg = load()
    cid = await conversations.create(store)
    await plans.save(store, cid, "", plans.clean(["Design", "Build"]))
    b = Replies([[TextDelta("Let me lock the designs in a note, then build."),
                  *_calls(("notebook__write", {"path": "a.md", "text": "x"}))],
                 [TextDelta("Built."), Done(reason="stop")]])
    await _turn(store, cfg, cid, b)
    assert len(b.calls) == 2, "it kept going after the note"
    assert "[>] 1. Design" in _last_user(b.calls[0]), "the plan is in its request"


async def test_an_ask_checkpoint_ends_the_turn_and_your_answer_goes_on(store):
    cfg = load()
    cid = await conversations.create(store)
    await plans.save(store, cid, "", plans.clean([{"title": "Compare", "checkpoint": "ask"},
                                                  "Order"]))
    b = Replies([[TextDelta("Here is the table. Shall I order the second one?"),
                  *_calls(("plan__step", {"step": 1, "state": "done"}))],
                 [TextDelta("never reached"), Done(reason="stop")]])
    tid = await _turn(store, cfg, cid, b)
    assert len(b.calls) == 1, "the turn ended at the checkpoint"
    assert await store.read(lambda c: c.execute(
        "SELECT state FROM turns WHERE id=?", (tid,)).fetchone()[0]) == "done"
    plan = await plans.get(store, cid)
    assert plan["waiting"] and plans.doing(plan) is None
    assert "go on when they answer" in flow.block(plan)
    events = await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT data FROM events WHERE conversation_id=? AND type='plan.state' ORDER BY seq",
        (cid,))])
    assert events[-1]["plan"]["waiting"] is True, "the transcript is told"

    b = Replies([[TextDelta("Ordering."), Done(reason="stop")]])
    await _turn(store, cfg, cid, b, "yes, the second")
    assert not (await plans.get(store, cid))["waiting"], "your answer lifts the wait"


async def test_where_she_works_names_the_place_and_its_address(store):
    chat = await conversations.create(store, "Groceries")
    mission = await conversations.create(store, "m", kind="mission")
    await store.write(lambda c: c.execute(
        "INSERT INTO missions(conversation_id, name, terminal, status, created_at, "
        "updated_at) VALUES(?, 'Prices', 't', 'active', '2026-10-22T00:00:00+00:00', "
        "'2026-10-22T00:00:00+00:00')", (mission,)))
    sub = await conversations.create(store, "scout", agent_of=mission, agent_name="scout")
    assert await store.read(lambda c: here.where(c, chat)) == \
        f"You are working in the chat \"Groceries\" (address {chat})."
    assert await store.read(lambda c: here.where(c, sub)) == (
        f"You are working in the sub-agent scout (address {sub}). It reports to the "
        f"mission Prices (address {mission}).")
    assert await store.read(lambda c: here.busy(c, chat)) is None
    await user_says(store, mission, "go")
    assert "the mission Prices" in await store.read(lambda c: here.busy(c, chat))


async def test_her_routine_writes_and_runs_are_hers(tmp_path, monkeypatch, store):
    from celine.intercom.addresses import resolve
    from celine.schedules.runs import Scheduler
    from celine.schedules.skill import Routines
    from settings_fixtures import make_app, scratch
    scratch(tmp_path, monkeypatch)
    app = make_app(boot.load(), store)
    services = app.state.services
    services.schedules = Scheduler(services)
    skill = Routines(services.cfg)
    skill.app = services
    cid = await conversations.create(store)
    ctx = ToolContext(cid, "t", "k", store, services.cfg, None)

    out = await skill.write(ctx, name="morning", label="Morning", prompt="Plan {{date}}.",
                            time="07:30")
    assert not out.is_error and out.content == "Saved Morning (morning), every day at " \
                                               "07:30, on."
    bad = await skill.write(ctx, name="morning", label="Morning", prompt="x", time="25:00")
    assert bad.is_error and bad.content == "Not saved (time): The time is HH:MM, such as 02:00."
    by = await store.read(lambda c: c.execute(
        "SELECT by FROM text_versions WHERE of='routine:morning:prompt'").fetchone()[0])
    assert by == "her", "its change is its own in the routine's history"
    assert "- morning: Morning, every day at 07:30, on, no runs yet" in \
        (await skill.list(ctx)).content

    out = await skill.run(ctx, name="morning")
    rid = out.display["run"]
    row = await store.read(lambda c: dict(c.execute(
        "SELECT started_by, conversation_id FROM runs WHERE id=?", (rid,)).fetchone()))
    assert row["started_by"] == "her"
    with pytest.raises(ValueError, match="has not started"):
        await store.read(lambda c: resolve(c, f"run:{rid}", f"agent:{cid}"))
    run_conv = await conversations.create(store, "Morning", kind="background")
    await store.write(lambda c: c.execute("UPDATE runs SET conversation_id=? WHERE id=?",
                                          (run_conv, rid)))
    for addr in (f"run:{rid}", "routine:morning"):
        _, to = await store.read(lambda c: resolve(c, addr, f"agent:{cid}"))
        assert to == [(f"agent:{run_conv}", run_conv)]
    assert f"run:{rid}" in (await skill.read(ctx, name="morning")).content


async def test_her_routine_change_keeps_what_she_left_out_and_names_a_bad_field(
        tmp_path, monkeypatch, store):
    from celine.schedules.runs import Scheduler
    from celine.schedules.skill import Routines
    from settings_fixtures import make_app, scratch
    scratch(tmp_path, monkeypatch)
    app = make_app(boot.load(), store)
    services = app.state.services
    services.schedules = Scheduler(services)
    skill = Routines(services.cfg)
    skill.app = services
    ctx = ToolContext(await conversations.create(store), "t", "k", store, services.cfg, None)

    new = await skill.write(ctx, name="evening", prompt="Think.")
    assert new.is_error and new.content == (
        "Not saved: there is no routine 'evening' yet (routines__list), and a new one "
        "needs label, time or cron."), "an unknown name names what a new one needs"
    assert "evening" not in services.cfg.schedules
    out = await skill.write(ctx, name="evening", label="Evening", prompt="Think.",
                            time="21:00", on=False, note="evening/{{date}}.md")
    assert not out.is_error, out.content

    out = await skill.write(ctx, name="evening", prompt="Think twice.")
    s = services.cfg.schedules["evening"]
    assert (s.prompt, s.on, s.note, s.time, s.label) == (
        "Think twice.", False, "evening/{{date}}.md", "21:00", "Evening"), \
        "only the prompt changed: it stays off and keeps its note"
    await skill.write(ctx, name="evening", cron="0 22 * * 1")
    assert services.cfg.schedules["evening"].cron == "0 22 * * 1"

    for args, field in (({"cron": "every night"}, "cron"), ({"prompt": " "}, "prompt"),
                        ({"wall_minutes": 999}, "wall_minutes"),
                        ({"time": "07:00", "cron": "0 7 * * *"}, "time")):
        bad = await skill.write(ctx, name="evening", **args)
        assert bad.is_error and bad.content.startswith(f"Not saved ({field}): "), bad.content
        assert services.cfg.schedules["evening"] == s.__class__(**{
            **s.__dict__, "prompt": "Think twice.", "cron": "0 22 * * 1"}), \
            f"{args}: nothing half-saved"


async def test_her_answer_finishes_only_the_last_step(store):
    cfg = load()
    cid = await conversations.create(store)
    await plans.save(store, cid, "", plans.clean([{"title": "Design", "state": "done"},
                                                  "Build"]))
    b = Replies([[TextDelta("Built. Here it is."), Done(reason="stop")]])
    await _turn(store, cfg, cid, b)
    plan = await plans.get(store, cid)
    assert [s["state"] for s in plan["steps"]] == ["done", "done"] and not plan["waiting"]
    events = await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT data FROM events WHERE conversation_id=? AND type='plan.state' ORDER BY seq",
        (cid,))])
    assert not plans.is_open(events[-1]["plan"]), "the transcript is told"

    early = await conversations.create(store)
    await plans.save(store, early, "", plans.clean(["Design", "Build"]))
    await _turn(store, cfg, early, Replies([[TextDelta("A thought."), Done(reason="stop")]]))
    assert [s["state"] for s in (await plans.get(store, early))["steps"]] == ["doing", "todo"], \
        "an earlier step stays open"


def test_a_step_keeps_its_times(monkeypatch):
    T0, T1 = "2026-10-23T08:00:00.000+00:00", "2026-10-23T09:00:00.000+00:00"
    monkeypatch.setattr(plans, "now", lambda: T0)
    steps = plans.clean(["Read", "Write"])
    assert (steps[0]["started_at"], steps[0]["done_at"]) == (T0, None), "doing since now"
    assert (steps[1]["started_at"], steps[1]["done_at"]) == (None, None)
    monkeypatch.setattr(plans, "now", lambda: T1)
    steps, _ = plans.move({"text": "", "steps": steps, "waiting": False}, 1, "done")
    assert (steps[0]["started_at"], steps[0]["done_at"]) == (T0, T1)
    assert (steps[1]["started_at"], steps[1]["done_at"]) == (T1, None), "the next one starts"
    again = plans.clean(["Write", "Check"], steps)
    assert again[0]["started_at"] == T1 and again[1]["started_at"] is None, \
        "the same title keeps its time, a new step has none"
    back, _ = plans.move({"text": "", "steps": steps, "waiting": False}, 1, "todo")
    assert (back[0]["started_at"], back[0]["done_at"]) == (None, None)
