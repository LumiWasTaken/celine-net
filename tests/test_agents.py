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
                # a reply that starts with blank lines, as a thinking model's often does
                out = [TextDelta(f"\n\n\n{who.group(1)} done: {task}"), Done(reason="stop")]
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
    [start] = [tc for tcs in snap["tool_calls"].values() for tc in tcs]
    assert start["display"]["link"] == f"/chat/{sub['id']}", "its Open goes where it lives"
    [landed] = [m for m in snap["messages"] if (m["intercom"] or {}).get("kind") == "report"]
    assert landed["created_at"] > tool_msg["created_at"], "the call did not wait for it"
    top, answer = landed["content"].split("\n\n", 1)
    assert top.startswith("[car scout, done") and top.endswith("; changed agents/cars/found.md]"), \
        "its head: name, state, work, the notes it changed"
    assert answer.startswith("car scout done: "), "one blank line after its head"
    assert "[Brief from Celine (#2)]\nfind the red cars\n\nToday is" in answer, \
        "its brief, under its header, then the tail of its turn (the test model echoes it)"
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
    assert {"notebook__read", "intercom__send", "sub_agents__start"} <= set(tools), \
        "starting a sub-agent is no write in itself"
    assert not {"notebook__write", "notebook__delete"} & set(tools)
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
    cfg.settings.general["sub_agents.max_running"] = 1
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


async def test_a_mission_starts_no_more_sub_agents_than_advanced_allows(store, cfg):
    """Sub-agents per mission caps the names a mission starts; one that is done still
    takes a new brief; a chat has no such cap; 0 is none."""
    cfg.settings.general["mission_max_sub_agents"] = 1
    mission = await conversations.create(store, "m", kind="mission")
    chat = await conversations.create(store, "p")
    d = Dispatcher(store, cfg, EventBus(), lambda: None, Registry())
    app = Services(cfg=cfg, store=store, dispatcher=d, intercom=Intercom(store, d.notify))
    skill = SubAgents(cfg)
    skill.app = app
    first, refusal, _, _ = await skill.begin(store, mission, "review the night", "night-rvw")
    assert refusal is None
    second, refusal, _, _ = await skill.begin(store, mission, "review the day", "day-rvw")
    assert second is None and refusal and "1 sub-agent" in refusal
    await store.write(lambda c: c.execute("UPDATE turns SET state='done'"))
    await report.on_end(app, first)
    again, refusal, resumed, _ = await skill.begin(store, mission, "and the day", "night-rvw")
    assert (again, refusal, resumed) == (first, None, True), "a done one takes a new brief"
    for name in ("chat-one", "chat-two"):
        assert (await skill.begin(store, chat, "look", name))[1] is None, "a chat: no cap"
    cfg.settings.general["mission_max_sub_agents"] = 0
    assert (await skill.begin(store, mission, "look", "day-rvw"))[1] is None, "0: no cap"


async def test_stopping_a_queued_sub_agent_drops_its_brief(store, cfg):
    cfg.settings.general["sub_agents.max_running"] = 0
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


async def test_a_folder_outside_the_notebook_is_refused(store, cfg):
    agents = _registry(cfg).skills["sub_agents"]
    agents.app = Services(dispatcher=object())     # a refusal never reaches it
    parent = await conversations.create(store)

    async def emit(note, **data):
        pass
    ctx = ToolContext(parent, "t", "x", store, cfg, emit)
    for bad in ("../out", "../x", ".", "tools/x", "/etc", "agents/../../x"):
        for access in ("read", "work"):
            assert (await agents.start(ctx, brief="x", name="valid name", folder=bad,
                                       access=access)).is_error, (bad, access)
    assert await _subs_of(store, parent) == []


async def test_a_read_sub_agent_starts_only_read_children_inside_its_folder(store, cfg):
    from celine.sub_agents.skill import READ_PARENT
    from celine.turns.loads import offered
    reg = _registry(cfg)
    agents = reg.skills["sub_agents"]
    agents.app = Services(dispatcher=object())     # a refusal never reaches it
    parent = await conversations.create(store)
    scout = await conversations.create(store, "scout", agent_of=parent, agent_name="scout",
                                       access="read", notebook_folder="agents/scout")
    here = await offered(reg, store, scout)
    assert [s.wire_name for s in here["notebook"]] == [
        "notebook__list", "notebook__read", "notebook__search"], "what it may call"
    assert "sub_agents__start" in {s.wire_name for s in here["sub_agents"]}

    async def emit(note, **data):
        pass
    ctx = ToolContext(scout, "t", "x", store, cfg, emit)
    from celine.skills.meta import Skills
    loaded, *_ = await Skills(reg, cfg).loaded(ctx, ["notebook"])
    assert "notebook__read" in loaded and "notebook__write" not in loaded, \
        "a load hands it only what it may call"
    for more in ({"access": "work"}, {"cli": "opencode"}):
        out = await agents.start(ctx, brief="x", name="a helper", **more)
        assert out.is_error and out.content == READ_PARENT, more
    for wider in ("agents/other", "agents/scout/../other", "agents/scout/.."):
        out = await agents.start(ctx, brief="x", name="a helper", folder=wider)
        assert out.is_error and ("agents/scout" in out.content or "not a folder" in
                                 out.content), (wider, "no folder above its own")
    assert await _subs_of(store, scout) == []

    # read, stop and a resume reach only its own sub-agents
    other = await conversations.create(store, "other", agent_of=parent, agent_name="other",
                                       access="work")
    await conversations.create(store, "deep", agent_of=other, agent_name="deep one")
    for call in (agents.read(ctx, "other"), agents.read(ctx, "deep one"),
                 agents.stop(ctx, "other"), agents.stop(ctx, "deep one")):
        out = await call
        assert out.is_error and out.content.startswith("No sub-agent called"), out.content
    worker = await conversations.create(store, "w", agent_of=scout, agent_name="old worker",
                                        notebook_folder="agents/scout", access="work")
    cli = await conversations.create(store, "c", agent_of=scout, agent_name="old cli",
                                     notebook_folder="agents/scout", access="read",
                                     agent_cli="opencode")
    assert (await agents.start(ctx, brief="again", name="old worker")).is_error
    out = await agents.start(ctx, brief="again", name="old cli")
    assert out.is_error and out.content == READ_PARENT, "a resume gives no more than read"
    assert {s["id"] for s in await _subs_of(store, scout)} == {worker, cli}


async def test_an_inherited_folder_leaves_the_default_access_read(store, cfg):
    from types import SimpleNamespace
    agents = _registry(cfg).skills["sub_agents"]
    agents.app = Services(cfg=cfg, store=store, intercom=Intercom(store),
                          dispatcher=SimpleNamespace(notify=lambda: None))
    parent = await conversations.create(store)
    scout = await conversations.create(store, "scout", agent_of=parent, agent_name="scout",
                                       access="work", notebook_folder="agents/scout")
    (cfg.her.notebook / "agents/scout").mkdir(parents=True)
    kid, refusal, _, _ = await agents.begin(store, scout, "look around", "a looker")
    assert refusal is None
    [row] = await store.read(lambda c: agents_of(c, agent=kid))
    assert (row["access"], row["folder"]) == ("read", "agents/scout")
    kid, refusal, _, _ = await agents.begin(store, scout, "write it", "a writer",
                                            folder="agents/scout/out")
    [row] = await store.read(lambda c: agents_of(c, agent=kid))
    assert (row["access"], row["folder"]) == ("work", "agents/scout/out"), "a folder given"


async def test_sub_agents_start_their_own_down_to_three_levels(store, cfg):
    agents = _registry(cfg).skills["sub_agents"]
    agents.app = Services(dispatcher=object())     # a refusal never reaches it
    chain = [await conversations.create(store)]
    for n in ("agent", "sub-agent", "sub-sub-agent"):
        chain.append(await conversations.create(store, n, agent_of=chain[-1], agent_name=n))
    assert [await store.read(lambda c, x=x: conversations.depth(c, x)) for x in chain] == \
        [0, 1, 2, 3]
    assert [await agents.offered_in(store, x) for x in chain] == [True, True, True, False], \
        "depth 3 is the deepest: it is not offered sub-agents"

    async def emit(note, **data):
        pass
    out = await agents.start(ToolContext(chain[3], "t", "x", store, cfg, emit),
                             brief="x", name="a fourth level")
    assert out.is_error and out.content == cfg.substitute(
        "Sub-agents go at most 3 levels below {{name}}, and this conversation is 3 below: "
        "it starts none.")
    assert await _subs_of(store, chain[3]) == []


async def test_a_cloud_model_asks_the_user_first_and_a_local_one_does_not(store, cfg,
                                                                          monkeypatch):
    s = cfg.settings
    monkeypatch.setattr(s, "providers", {
        "or": {"kind": "openrouter", "label": "OpenRouter",
               "models": {"big": {"caps": ["chat", "tools"]}}},
        "gpu": {"kind": "llama-server", "label": "gpu", "base_url": "http://gpu:8080/v1",
                "models": {"small": {"caps": ["chat", "tools"]}}}})
    monkeypatch.setattr(s, "key_missing", lambda pid: False)
    monkeypatch.setattr(s, "roles", {"tasks": {"chain": ["gpu/small"]}})
    asked = []

    class Approvals:
        async def ask(self, action, facts, conversation, why="", short="", kind="vault",
                      **kw):
            asked.append((kind, action, facts, conversation))
            return "denied; nothing was done."
    from types import SimpleNamespace
    agents = _registry(cfg).skills["sub_agents"]
    agents.app = Services(cfg=cfg, store=store, intercom=Intercom(store), approvals=Approvals(),
                          dispatcher=SimpleNamespace(notify=lambda: None))   # nothing runs
    parent = await conversations.create(store)

    async def emit(note, **data):
        pass
    ctx = ToolContext(parent, "t", "x", store, cfg, emit)
    assert (await agents.start(ctx, brief="x", name="abc", model="or/big")).is_error
    out = await agents.start(ctx, brief="Read the whole site.", name="site reader",
                             model="or/big")
    assert out.is_error and out.content == "denied; nothing was done."
    assert asked == [("model", "start the sub-agent site reader on the cloud model big on "
                               "OpenRouter", ["brief: Read the whole site."], parent)]
    out = await agents.start(ctx, brief="Read the whole site.", name="site reader",
                             model="gpu/small")
    assert len(asked) == 1, "a local model starts without asking"
    assert out.content.startswith("site reader started"), out.content
    for model in ("or/big", None):
        out = await agents.start(ctx, brief="again", name="site reader", model=model)
        assert out.is_error and ("still working" in out.content or "exists with" in out.content)
    assert len(asked) == 1, "a start refused anyway asks nothing"
    monkeypatch.setattr(s, "roles", {"tasks": {"chain": ["or/big", "gpu/small"]}})
    out = await agents.start(ctx, brief="Count the pages.", name="page counter")
    assert out.is_error and asked[1][:2] == (
        "model", "start the sub-agent page counter on the cloud model big on OpenRouter"), \
        "no model given: the one its role runs it on"
    from celine.backends.router import ProviderState
    states = {"or": ProviderState(reachable=False, error="OpenRouter refused the connection.")}
    agents.app.llm = SimpleNamespace(router=SimpleNamespace(
        provider_state=lambda pid: states.get(pid, ProviderState())))
    out = await agents.start(ctx, brief="Count the pages.", name="page counter 2",
                             model="or/big")
    assert out.is_error and out.content == (
        "page counter 2 did not start: big on OpenRouter does not answer. OpenRouter "
        "refused the connection."), out.content
    assert len(asked) == 2, "a model known down is not offered to the owner"
    monkeypatch.setattr(s, "roles", {"tasks": {"chain": ["gpu/small", "or/big"]}})
    out = await agents.start(ctx, brief="Count the pages.", name="page counter 3")
    assert out.display["model"] == "gpu/small", "the one model it runs on, kept as its own"


async def test_a_stop_drops_the_queued_briefs_all_the_way_down(store, cfg):
    from celine.sub_agents.skill import release, waiting

    reg = _registry(cfg)
    agents = reg.skills["sub_agents"]
    dispatcher = Dispatcher(store, cfg, EventBus(), lambda: None, reg)
    dispatcher.notify = lambda: None
    app = agents.app = Services(cfg=cfg, store=store, intercom=Intercom(store),
                                dispatcher=dispatcher)
    parent = await conversations.create(store)
    scout = await conversations.create(store, "scout", agent_of=parent, agent_name="scout")
    kid = await conversations.create(store, "kid", agent_of=scout, agent_name="kid")
    await app.intercom.send(f"agent:{scout}", f"agent:{kid}", "dig", kind="brief", wake=False)
    assert await store.read(lambda c: waiting(c, kid)), "queued"
    await agents.stop_agent(scout)
    assert await store.read(lambda c: waiting(c, kid)) is None, "the kid's brief went too"
    await release(app, scout)
    assert not await store.read(lambda c: c.execute(
        "SELECT 1 FROM turns WHERE conversation_id=?", (kid,)).fetchone()), "nothing starts"
    sibling = await conversations.create(store, "sib", agent_of=parent, agent_name="sib")
    await app.intercom.send(f"agent:{parent}", f"agent:{sibling}", "go", kind="brief",
                            wake=False)
    await dispatcher.abort_conversation(parent)
    assert await store.read(lambda c: waiting(c, sibling)) is None, \
        "a Stop on the parent drops its children's queued briefs"
    await release(app, parent)
    assert not await store.read(lambda c: c.execute(
        "SELECT 1 FROM turns WHERE conversation_id=?", (sibling,)).fetchone())


async def test_a_long_report_is_cut_from_the_parents_window_and_its_tail_says_so(
        store, cfg):
    from celine.conversations import tree
    app = Services(store=store, intercom=Intercom(store))
    parent = await conversations.create(store)
    sub = await conversations.create(store, "scout", agent_of=parent, agent_name="scout")
    await store.write(lambda c: c.execute(
        "INSERT INTO turns(id, conversation_id, state, created_at) VALUES('pt', ?, 'done', "
        "'2026-10-30T10:00:00.000+00:00')", (parent,)))
    await store.write(lambda c: c.execute(
        "INSERT INTO requests(message_id, step, turn_id, provider, model, body, window, "
        "created_at) VALUES('pm', 0, 'pt', 'gpu/big', 'big', x'', 10000, "
        "'2026-10-30T10:00:01.000+00:00')"))
    assert await store.read(lambda c: report.cap(c, parent)) == 300, "3 % of its window"
    await app.intercom.send(f"agent:{parent}", f"agent:{sub}", "read it all", kind="brief")
    await store.write(lambda c: c.execute("UPDATE turns SET state='done'"))
    await store.write(lambda c: tree.append(c, store, sub, None, "assistant",
                                            "line of the answer\n" * 400, "model"))
    await report.on_end(app, sub)
    text = await store.read(lambda c: c.execute(
        "SELECT text FROM intercom WHERE kind='report'").fetchone()[0])
    assert text.startswith("[scout, done]\n\nline of the answer\n")
    assert text.endswith("\n\n" + report.CUT) and len(text) < 2000
    ctx = ToolContext(sub, "t", "x", store, cfg, None)
    frag = await SubAgents(cfg).prompt_fragment(ctx)
    assert "Long material goes in files, and your report names them." in frag
    assert "cut at about" not in frag, "the room depends on the window now: the tail"
    assert await SubAgents(cfg).tail_part(ctx) == "Your report reaches " + \
        cfg.substitute("{{name}}") + " cut at about 300 tokens; the rest stays in this " \
        "conversation."


async def test_a_report_goes_to_the_one_that_started_it_once_its_own_are_done(store):
    from celine.intercom.addresses import REACH, Refused
    app = Services(store=store, intercom=Intercom(store))
    parent = await conversations.create(store)
    scout, other = [await conversations.create(store, title=n, agent_of=parent,
                                               agent_name=n, access="read")
                    for n in ("scout", "other")]
    kid = await conversations.create(store, "kid", agent_of=scout, agent_name="kid")

    async def ended(cid):
        await store.write(lambda c: c.execute(
            "UPDATE turns SET state='done' WHERE conversation_id=?", (cid,)))
        await report.on_end(app, cid)
        return await store.read(lambda c: [tuple(r) for r in c.execute(
            "SELECT from_addr, to_addr FROM intercom WHERE kind='report' ORDER BY id")])

    up = [(f"agent:{kid}", f"agent:{scout}"), (f"agent:{scout}", f"agent:{parent}")]
    await app.intercom.send(f"agent:{parent}", f"agent:{scout}", "look", kind="brief")
    await app.intercom.send(f"agent:{scout}", "kid", "look deeper", kind="brief")
    from celine import metrics
    counted = metrics.SUB_AGENTS_FINISHED.labels("done")._value.get

    start = counted()
    assert await ended(scout) == [], "its own sub-agent works: it is not done"
    assert counted() == start, "nothing sent: nothing counted"
    assert await ended(kid) == up[:1], "the kid reports to scout, not to the top"
    assert counted() == start + 1, "scout, woken, is checked and not counted"
    assert await ended(scout) == up, "woken by it, scout reports to its parent"
    assert counted() == start + 2
    await app.intercom.send(f"agent:{kid}", "parent", "psst")
    assert await ended(scout) == up, "a message from its own sub-agent: no report"
    for frm, to in ((other, f"agent:{scout}"), (kid, f"agent:{parent}"), (kid, "all"),
                    (kid, "user"), (kid, "bastet"), (kid, "system:guard")):
        with pytest.raises(Refused, match=REACH[:20]):
            await app.intercom.send(f"agent:{frm}", to, "hi")
    await app.intercom.send(f"agent:{parent}", "user", "hi")    # the top: as before
    await app.intercom.send(f"agent:{parent}", f"agent:{scout}", "and now?")
    assert len(await ended(scout)) == 3, "the parent's follow-up"


async def test_a_sub_agent_is_addressed_by_its_exact_name(store):
    from celine.intercom import addresses
    parent = await conversations.create(store, "p")
    sub = await conversations.create(store, "s", agent_of=parent, agent_name="Scout")
    found = await store.read(lambda c: addresses.resolve(c, "Scout", f"agent:{parent}"))
    assert found[1] == [(f"agent:{sub}", sub)]
    try:
        await store.read(lambda c: addresses.resolve(c, "scout", f"agent:{parent}"))
    except (ValueError, KeyError):
        pass
    else:
        raise AssertionError("a name in another case reaches nobody")


async def test_a_name_is_looked_up_among_the_senders_own_sub_agents_only(store):
    from celine.intercom import addresses
    parent = await conversations.create(store, "p")
    scout = await conversations.create(store, "s", agent_of=parent, agent_name="scout")
    kid = await conversations.create(store, "k", agent_of=scout, agent_name="digger")
    await conversations.create(store, "d", agent_of=parent, agent_name="digger")
    found = await store.read(lambda c: addresses.resolve(c, "digger", f"agent:{scout}"))
    assert found[1] == [(f"agent:{kid}", kid)], "a newer sibling never shadows its own"


async def test_a_failed_sub_agent_reports_the_reason_its_turn_stored(store):
    """A turn that failed before a model answered: its reason is the report's body; one
    that failed after some words: the reason after them."""
    from celine.conversations import tree
    app = Services(store=store, intercom=Intercom(store))
    parent = await conversations.create(store)
    why = ("qwen3:4b has room for 4,096 tokens, and Celine's instructions and tools alone "
           "take about 8,228.")

    async def failed(name, words):
        sub = await conversations.create(store, name, agent_of=parent, agent_name=name)
        await app.intercom.send(f"agent:{parent}", f"agent:{sub}", "go", kind="brief")
        await store.write(lambda c: c.execute(
            "UPDATE turns SET state='failed', reason=?, reason_code='context_too_small' "
            "WHERE conversation_id=?", (why, sub)))
        if words:
            await store.write(lambda c: tree.append(c, store, sub, None, "assistant",
                                                    words, "model"))
        await report.on_end(app, sub)
        return await store.read(lambda c: c.execute(
            "SELECT text FROM intercom WHERE kind='report' AND from_addr=?",
            (f"agent:{sub}",)).fetchone()[0])

    assert await failed("quiet", "") == f"[quiet, failed]\n\n{why}"
    assert await failed("said", "half of it\n") == f"[said, failed]\n\nhalf of it\n\n{why}"
