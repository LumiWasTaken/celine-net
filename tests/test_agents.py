"""Sub-agents (sub_agents/): a task in a conversation of its own, the notebook folder
limit and read mode enforced in code, the report back to the parent, inline or later as
an intercom `report` once its jobs are done too."""
from __future__ import annotations

from chat_fixtures import until, user_says

import json
import re

import anyio
import pytest

from celine.conversations import store as conversations, view
from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.core.bus import EventBus
from celine.core.services import Services
from celine.skills import Registry, ToolContext
from celine.intercom import Intercom
from celine.intercom.skill import IntercomSkill
from celine.skills.gating import READ_ONLY_REFUSAL
from celine.sub_agents import report
from celine.sub_agents.skill import SubAgents, agents_of
from celine.skills.notebook import Notebook
from celine.turns.dispatcher import Dispatcher
from celine.skills.gating import overrides_for
from celine.skills.gating import set_override

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg():
    c = load()
    c.skills = dict(c.skills)
    c.settings.general.update({"job_timeout_seconds": 10, "max_concurrent_turns": 4})
    return c


def _registry(cfg) -> Registry:
    reg = Registry()
    reg.add(Notebook(cfg))
    reg.add(SubAgents(cfg))
    reg.add(IntercomSkill(cfg))
    return reg


def _calls(*calls: tuple[str, dict]):
    return [ToolCallDelta(index=i, id=f"c{i}", name=n, arguments=json.dumps(a))
            for i, (n, a) in enumerate(calls)] + [Done(reason="tool_calls")]


class Script(FakeBackend):
    """The parent makes `parent_calls` once (and `then_calls` in its second step, when
    given), then answers "ok". Each sub-agent writes the notebook entry `note` (when
    given), waits for `go` (when given), and reports "<name> done: <its task>". Every sub-agent request
    is kept in `seen` as its tool names."""

    def __init__(self, parent_calls, note=None, go=None, then_calls=None):
        super().__init__()
        self.parent_calls, self.then_calls, self.go = parent_calls, then_calls, go
        self.note, self.seen = note, []
        self.systems: list[str] = []

    async def stream(self, messages, tools):
        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        who = re.search(r"You are ([^,\n]+), a sub-agent \w+ started", system)
        last = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        content = messages[last]["content"]
        text = content[0]["text"] if isinstance(content, list) else content   # before the tail
        task = text.split("\n", 1)[1]        # after the time line
        done = [m for m in messages[last + 1:] if m.get("role") == "tool"]
        steps = [m for m in messages[last + 1:] if m.get("role") == "assistant"]
        if who:
            self.seen.append([t["function"]["name"] for t in tools])
            self.systems.append(system)
            if self.note and not done:
                out = _calls(("notebook__write", {"path": self.note, "summary": "cars",
                                                  "content": "red, red, red"}))
            else:
                if self.go is not None:
                    await self.go.wait()
                out = [TextDelta(f"{who.group(1)} done: {task}"), Done(reason="stop")]
        elif task.startswith(("[Job ", "[Report from")) or done and not (
                self.then_calls and len(steps) == 1):
            out = [TextDelta("ok"), Done(reason="stop")]
        else:
            out = _calls(*(self.then_calls if done else self.parent_calls))
        for ev in out:
            yield ev


async def _subs_of(store, parent):
    return await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT * FROM conversations WHERE agent_of=? ORDER BY created_at", (parent,))])


async def _run_parent(store, cfg, reg, backend, parent, wait_for_report=False):
    async def ended():
        return (await view.snapshot(store, parent))["turn"]["state"] in ("done", "failed", "aborted")

    async def report_landed():
        return await store.read(lambda c: c.execute(
            "SELECT 1 FROM messages m JOIN intercom i ON i.id = m.intercom_id "
            "WHERE m.conversation_id=? AND i.kind='report'",
            (parent,)).fetchone())
    async with anyio.create_task_group() as tg:
        d = Dispatcher(store, cfg, EventBus(), lambda: backend, reg)
        await d.start(tg)
        app = Services(cfg=cfg, store=store, dispatcher=d, intercom=Intercom(store, d.notify))
        for skill in reg.skills.values():
            skill.app = app
        d.on_end(lambda cid, _turn: report.on_end(app, cid))
        d.notify()
        await until(report_landed if wait_for_report else ended)
        await until(ended)
        await d.stop()
        tg.cancel_scope.cancel()


async def test_start_returns_at_once_and_the_report_comes_later(store, cfg):
    reg = _registry(cfg)
    args = {"brief": "find the red cars", "name": "car scout", "folder": "agents/cars"}
    backend = Script([("sub_agents__start", args)], note="agents/cars/found.md",
                     go=anyio.Event())
    parent = await conversations.create(store)
    await set_override(store, parent, "shell", False, 1)
    await user_says(store, parent, "go")

    async def answered():                # the start call's result is in: the sub may report
        await until(lambda: store.read(lambda c: c.execute(
            "SELECT 1 FROM messages WHERE conversation_id=? AND role='tool'",
            (parent,)).fetchone()))
        backend.go.set()
    async with anyio.create_task_group() as tg:
        tg.start_soon(answered)
        await _run_parent(store, cfg, reg, backend, parent, wait_for_report=True)

    [sub] = await _subs_of(store, parent)
    assert (sub["agent_name"], sub["notebook_folder"], sub["access"]) == \
        ("car scout", "agents/cars", "work")
    assert await overrides_for(store, sub["id"]) == {"shell": False}, \
        "a tool off in the parent is off for its sub-agent"
    snap = await view.snapshot(store, parent)
    tool_msg = [m for m in snap["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"].startswith("car scout started (brief #")
    [landed] = [m for m in snap["messages"] if (m["intercom"] or {}).get("kind") == "report"]
    assert landed["created_at"] > tool_msg["created_at"], "the call did not wait for it"
    assert landed["content"].startswith("car scout done: ")
    assert landed["content"].endswith("\nfind the red cars"), "its brief, under its header"
    assert (cfg.her.notebook / "agents/cars/found.md").read_text().endswith("\nred, red, red")


async def test_a_read_sub_agent_has_only_read_tools_and_writes_nothing(store, cfg):
    reg = _registry(cfg)
    backend = Script([("sub_agents__start", {"brief": "look around", "name": "looker"})],
                     note="stolen.md")
    parent = await conversations.create(store)
    await user_says(store, parent, "go")
    await _run_parent(store, cfg, reg, backend, parent, wait_for_report=True)

    [sub] = await _subs_of(store, parent)
    assert sub["access"] == "read", "no folder, no mode: read"
    tools = backend.seen[0]
    assert "notebook__read" in tools and "intercom__send" in tools
    assert not {"notebook__write", "notebook__delete", "sub_agents__start"} & set(tools)
    # a write it calls anyway is refused at the chokepoint
    assert not (cfg.her.notebook / "stolen.md").exists()
    refused = await store.read(lambda c: c.execute(
        "SELECT tc.state, m.content FROM tool_calls tc JOIN messages m "
        "ON m.id = tc.result_message_id JOIN messages a ON a.id = tc.message_id "
        "WHERE a.conversation_id = ?", (sub["id"],)).fetchone())
    assert refused["state"] == "gated" and refused["content"] == READ_ONLY_REFUSAL.format(
        skill="notebook",
        reads="notebook__list, notebook__read, notebook__search")


async def test_a_stop_sticks_a_stopped_sub_agent_reports_quietly_and_its_schedule_is_cancelled(
        store, cfg):
    """The parent's Stop cascades; the sub-agent's report then must not start a new
    parent turn, and nothing the sub-agent scheduled wakes it again."""
    from datetime import datetime, timedelta, timezone
    from celine.intercom import Intercom
    parent = await conversations.create(store, "p")
    sub = await conversations.create(store, "s", agent_of=parent, agent_name="s")
    d = Dispatcher(store, cfg, EventBus(), lambda: None, Registry())
    intercom = Intercom(store, d.notify)
    d.intercom = intercom
    await intercom.send(f"agent:{parent}", f"agent:{sub}", "find the cars", kind="brief")
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sched = await intercom.send(f"agent:{sub}", f"agent:{sub}", "check again", due_at=later)
    assert await _turns(store, sub) == [("intercom", "queued")]

    await d.abort_conversation(parent)
    assert await _turns(store, sub) == [("intercom", "aborted")]
    assert await store.read(lambda c: c.execute(
        "SELECT cancelled_at FROM intercom WHERE id=?",
        (sched["intercom_id"],)).fetchone()[0]) is not None, "its schedule is cancelled"

    app = Services(cfg=cfg, store=store, dispatcher=d, intercom=intercom)
    await report.on_end(app, sub)
    rep = await store.read(lambda c: c.execute(
        "SELECT wake, data FROM intercom WHERE kind='report' AND from_addr=?",
        (f"agent:{sub}",)).fetchone())
    assert rep["wake"] == 0 and '"aborted"' in rep["data"]
    assert await _turns(store, parent) == [], "the report started no parent turn"


async def test_a_start_over_max_running_is_queued_and_starts_when_one_is_done(store, cfg):
    """More sub-agents than the limit are queued, not refused."""
    cfg.skills["sub_agents"] = {**cfg.skills.get("sub_agents", {}), "max_running": 1}
    parent = await conversations.create(store, "p")
    d = Dispatcher(store, cfg, EventBus(), lambda: None, Registry())
    app = Services(cfg=cfg, store=store, dispatcher=d, intercom=Intercom(store, d.notify))
    skill = SubAgents(cfg)
    skill.app = app
    first, refusal, _, _ = await skill.begin(store, parent, "review the night", "night-rvw")
    second, refusal2, _, _ = await skill.begin(store, parent, "review the day", "day-rvw")
    assert refusal is refusal2 is None
    assert await _turns(store, first) == [("intercom", "queued")]
    assert await _turns(store, second) == [], "over the cap it waits"
    [row] = await store.read(lambda c: agents_of(c, agent=second))
    assert row["queued"] and row["working"]

    await store.write(lambda c: c.execute("UPDATE turns SET state='done' WHERE "
                                          "conversation_id=?", (first,)))
    await report.on_end(app, first)
    assert await _turns(store, second) == [("intercom", "queued")]
    [row] = await store.read(lambda c: agents_of(c, agent=second))
    assert not row["queued"]
    reply = await store.read(lambda c: c.execute(
        "SELECT d.turn_id FROM intercom_deliveries d WHERE d.conversation_id=?",
        (second,)).fetchone()[0])
    assert reply == (await store.read(lambda c: c.execute(
        "SELECT id FROM turns WHERE conversation_id=?", (second,)).fetchone()[0])), \
        "its report answers the brief"


async def test_stopping_a_queued_sub_agent_drops_its_brief(store, cfg):
    cfg.skills["sub_agents"] = {**cfg.skills.get("sub_agents", {}), "max_running": 0}
    parent = await conversations.create(store, "p")
    d = Dispatcher(store, cfg, EventBus(), lambda: None, Registry())
    app = Services(cfg=cfg, store=store, dispatcher=d, intercom=Intercom(store, d.notify))
    skill = SubAgents(cfg)
    skill.app = app
    sub, _, _, _ = await skill.begin(store, parent, "review the night", "night-rvw")
    await skill.stop_agent(sub)
    [row] = await store.read(lambda c: agents_of(c, agent=sub))
    assert not row["queued"] and not row["working"]


async def test_a_turn_with_no_new_brief_still_announces_its_state(store, monkeypatch):
    """A sibling's message woke it: no report is due, its overview still updates."""
    parent = await conversations.create(store, "p")
    sub = await conversations.create(store, "s", agent_of=parent, agent_name="s")
    seen: list[dict] = []
    monkeypatch.setattr(store, "emit_global", seen.append)
    await report.on_end(Services(store=store, intercom=Intercom(store)), sub)
    assert [e["type"] for e in seen] == ["agent.state"]


async def _turns(store, cid):
    return await store.read(lambda c: [tuple(r) for r in c.execute(
        "SELECT origin, state FROM turns WHERE conversation_id=? ORDER BY created_at",
        (cid,))])


async def test_deleting_a_parent_takes_its_sub_agents_and_waits_for_them(store):
    """A sub-agent never becomes a conversation of its own: deleting the parent deletes
    the whole family, and is refused while any of them works."""
    from celine.conversations import tree
    parent = await conversations.create(store, "p")
    sub = await conversations.create(store, "s", agent_of=parent, agent_name="s")
    grand = await conversations.create(store, "g", agent_of=sub, agent_name="g")
    await user_says(store, grand, "work")           # a queued turn: it works
    with pytest.raises(tree.Busy):
        await conversations.delete(store, parent)
    await store.write(lambda c: c.execute("UPDATE turns SET state='done'"))
    assert await conversations.delete(store, parent)
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM conversations WHERE id IN (?,?,?) AND trashed_at IS NULL",
        (parent, sub, grand)).fetchone()[0]) == 0


async def test_a_folder_outside_the_notebook_and_a_sub_agent_of_a_sub_agent_are_refused(
        store, cfg):
    agents = _registry(cfg).skills["sub_agents"]
    agents.app = Services(dispatcher=object())     # a refusal never reaches it
    parent = await conversations.create(store)

    async def emit(note, **data):
        pass
    ctx = ToolContext(parent, "t", "x", store, cfg, emit)
    for bad in ("../out", ".", "tools/x"):
        assert (await agents.start(ctx, brief="x", name="valid name", folder=bad)).is_error, bad
    sub = await conversations.create(store, title="kid", agent_of=parent, agent_name="kiddo",
                                          access="work")
    assert not await agents.offered_in(store, sub), "a sub-agent is not offered the tools"
    assert len(await _subs_of(store, parent)) == 1


async def test_a_message_from_a_sibling_gets_no_report_to_the_parent(store):
    app = Services(store=store, intercom=Intercom(store))
    parent = await conversations.create(store)
    scout, other = [await conversations.create(store, title=n, agent_of=parent,
                                               agent_name=n, access="read")
                    for n in ("scout", "other")]

    async def worked(frm, text, kind="message"):
        """`frm` writes to scout, scout's turn ends, the end rule runs."""
        await app.intercom.send(f"agent:{frm}", f"agent:{scout}", text, kind=kind)
        await store.write(lambda c: c.execute(
            "UPDATE turns SET state='done' WHERE conversation_id=?", (scout,)))
        await report.on_end(app, scout)
        return await store.read(lambda c: [r[0] for r in c.execute(
            "SELECT reply_to FROM intercom WHERE kind='report' ORDER BY id")])

    assert len(await worked(parent, "look", kind="brief")) == 1, "the brief's report"
    assert len(await worked(other, "psst")) == 1, "a sibling's message: no report"
    assert len(await worked(parent, "and now?")) == 2, "the parent's follow-up"
