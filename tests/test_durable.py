"""Durable execution: detach and resume from the journal, stop, the wall clock,
and restarts.
The message tree is the journal: a role=tool message is a recorded step result."""
from __future__ import annotations

from chat_fixtures import Scripted, until, user_says

import threading

import anyio
import pytest

from celine.conversations import store as conversations, tree, view
from celine.backends.base import Done
from celine.boot import load
from celine.core.store import Store
from celine.core.bus import EventBus
from celine.jobs.jobs import Jobs
from celine.skills import Registry, ToolResult, tool
from celine.turns.cancel import Token
from celine.turns.dispatcher import Dispatcher
from celine.turns.runner import run_turn
from celine.turns import store as turns
from celine.jobs import store as job_rows

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg(monkeypatch):
    from celine.turns import tools
    monkeypatch.setattr(tools, "BACKGROUND_AFTER", 0.15)
    c = load()
    c.settings.general.update({"tool_timeout_seconds": 0.4,
                 "job_timeout_seconds": 5, "max_concurrent_turns": 1})
    return c


class Slow:
    """A detach tool slower than the background window, asked into the background."""

    name = "slow"
    description = "slow things"

    def __init__(self):
        self.started = self.finished = 0
        self.go = anyio.Event()             # the work ends when the test says so

    async def _work(self, content):
        self.started += 1
        await self.go.wait()
        self.finished += 1
        return ToolResult(content=content)

    @tool(mode="detach", background="background")
    async def detach(self, ctx, background: bool = False) -> ToolResult:
        """Detaches."""
        return await self._work("detached work done")


class Arch:
    """Read-only tools like chat_archive__search, working in an uncancellable thread."""

    name = "arch"
    description = "read-only archive tools"

    def __init__(self, block: bool = False):
        self.find_runs = self.who_runs = 0
        self.started, self.release = threading.Event(), threading.Event()
        self.block = block

    @tool()                                   # blocking, mutating=False
    async def find(self, ctx) -> ToolResult:
        """Finds messages."""
        self.find_runs += 1
        self.started.set()
        if self.block:
            await anyio.to_thread.run_sync(self.release.wait, abandon_on_cancel=False)
        return ToolResult(content="FIND RESULT")

    @tool()
    async def who(self, ctx) -> ToolResult:
        """Finds people."""
        self.who_runs += 1
        return ToolResult(content="WHO RESULT")


BG = ("slow__detach", '{"background": true}')


async def _start(store, skill):
    reg = Registry()
    reg.add(skill)
    cid = await conversations.create(store)
    await user_says(store, cid, "go")
    return reg, cid, await turns.claim(store, "w")


async def _branch(store, cid) -> list[str]:
    def walk(c):
        leaf = c.execute("SELECT active_leaf_id FROM conversations WHERE id=?",
                         (cid,)).fetchone()[0]
        out = []
        while leaf:
            row = c.execute("SELECT parent_id, content FROM messages WHERE id=?",
                            (leaf,)).fetchone()
            out.append(row["content"])
            leaf = row["parent_id"]
        return out[::-1]
    return await store.read(walk)


async def test_detach_lets_the_turn_finish_and_posts_back_later(store, cfg):
    slow = Slow()
    reg, cid, turn = await _start(store, slow)
    notified = []
    async with anyio.create_task_group() as tg:
        await run_turn(turn, store, cfg, Scripted([BG]), EventBus(),
                       Token(turn["id"]), reg, Jobs(store, cfg, lambda: notified.append(1), tg))
        # the turn is done while the step is still running
        snap = await view.snapshot(store, cid)
        assert snap["turn"]["state"] == "done" and slow.finished == 0
        [running] = await job_rows.list(store, "running")
        assert running["tool_call_id"] == next(iter(snap["tool_calls"].values()))[0]["id"]
        slow.go.set()
        await until(lambda: notified)
        tg.cancel_scope.cancel()

    # one run, not one aborted in the foreground and one again in the job
    assert (slow.started, slow.finished) == (1, 1)
    [done] = await job_rows.list(store, "done")
    assert done["job_message_id"], "the notice must deep-link to its node"
    # it came back as a new queued turn, through the same path as everything else
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM turns WHERE state='queued' AND origin='intercom'"
    ).fetchone()[0]) == 1
    assert any((m["intercom"] or {}).get("kind") == "job"
               for m in (await view.snapshot(store, cid))["messages"])


async def test_unasked_a_detach_tool_runs_in_the_turn_and_is_stopped_at_its_time(store, cfg):
    """Nothing is put in the background on its own: without background=true the step
    runs in its turn up to the tool's time, is stopped then, and no job row exists."""
    slow = Slow()
    reg, cid, turn = await _start(store, slow)
    async with anyio.create_task_group() as tg:
        jobs = Jobs(store, cfg, lambda: None, tg)
        await run_turn(turn, store, cfg, Scripted(["slow__detach"]), EventBus(),
                       Token(turn["id"]), reg, jobs)
        assert not jobs.handles
        tg.cancel_scope.cancel()
    assert (slow.started, slow.finished) == (1, 0)
    assert await job_rows.list(store) == []
    [text] = await store.read(lambda c: [r["content"] for r in c.execute(
        "SELECT content FROM messages WHERE conversation_id=? AND role='tool'", (cid,))])
    assert text.startswith("timed out after")
    assert (await view.snapshot(store, cid))["turn"]["state"] == "done"


async def test_stop_also_cancels_the_step_it_handed_to_a_job(store, cfg):
    """Else `Stopped.` shows while the step runs on, with nothing left to stop it. The
    turn's own end stops nothing it handed off; the conversation's Stop does."""
    slow = Slow()
    reg, cid, turn = await _start(store, slow)
    async with anyio.create_task_group() as tg:
        jobs = Jobs(store, cfg, lambda: None, tg)
        await run_turn(turn, store, cfg, Scripted([BG]), EventBus(),
                       Token(turn["id"]), reg, jobs)
        assert await jobs.cancel_for_turn(turn["id"]) == 0, "handed off: not the turn's any more"
        assert await jobs.cancel_for_conversation(cid) == 1
        await until(lambda: not jobs.handles)
        tg.cancel_scope.cancel()
    assert slow.finished == 0, "the step kept running after Stop"
    assert len(await job_rows.list(store, "cancelled")) == 1


async def ended(store, cid) -> bool:
    return (await view.snapshot(store, cid))["turn"]["state"] in ("failed", "done")


async def test_wall_clock_releases_a_wedged_turn(store, cfg):
    """A backend that never answers must not hold the conversation forever."""
    class Stuck:
        name, model, supports_tools, supports_images = "stuck", "m", False, False

        async def stream(self, messages, tools):
            await anyio.sleep_forever()
            yield Done(reason="stop")

    cfg.settings.general.update({"turn_wall_clock_seconds": 0.2, "last_step_seconds": 0.1})
    cid = await conversations.create(store)
    await user_says(store, cid, "hang")
    async with anyio.create_task_group() as tg:
        d = Dispatcher(store, cfg, EventBus(), Stuck, Registry())
        await d.start(tg)
        d.notify()
        await until(lambda: ended(store, cid), 5)
        await d.stop()
        tg.cancel_scope.cancel()
    turn = (await view.snapshot(store, cid))["turn"]
    assert turn["state"] == "failed" and "wall_clock" in turn["reason"]
    await user_says(store, cid, "again")
    assert (await turns.claim(store, "w2"))["conversation_id"] == cid


async def test_a_restart_during_a_read_only_find_ends_it_for_regenerate(db_path, cfg):
    """The turn ends `interrupted` with its reason at the stop; nothing resumes it on the
    next start; the snapshot names the message Regenerate answers, and Regenerate does."""
    arch = Arch(block=True)
    reg = Registry()
    reg.add(arch)

    async def state(store):
        return await store.read(lambda c: c.execute("SELECT state FROM turns").fetchone()[0])

    # first process: the turn is inside `find` when the server shuts down; nothing raises
    async with anyio.create_task_group() as outer:
        store = Store(db_path)
        await store.start(outer)
        cid = await conversations.create(store)
        await user_says(store, cid, "find it")
        async with anyio.create_task_group() as tg:
            d = Dispatcher(store, cfg, EventBus(), lambda: Scripted(["arch__find"]), reg)
            await d.start(tg)
            d.notify()
            await until(arch.started.is_set, 5)
            # exactly what __main__.lifespan's finally does on a restart
            await d.stop()
            await store.aclose()
            tg.cancel_scope.cancel()
            arch.release.set()          # the thread finishes after the close
        outer.cancel_scope.cancel()

    # second process: the turn stays as the stop left it until the user's Regenerate
    async with anyio.create_task_group() as outer:
        store = Store(db_path)
        await store.start(outer)
        assert (await turns.sweep(store, registry=reg))["total"] == 0
        assert await state(store) == "interrupted"
        snap = await view.snapshot(store, cid)
        assert snap["turn"]["reason_code"] == "restart"
        mine = [m for m in snap["messages"] if m["role"] == "user"][-1]
        assert snap["unanswered"] == {"message_id": mine["id"], "state": "interrupted",
                                      "reason": "The server restarted during the answer."}
        arch.block = False
        async with anyio.create_task_group() as tg:
            d = Dispatcher(store, cfg, EventBus(), lambda: Scripted(["arch__find"]), reg)
            await d.start(tg)
            tid, _ = await tree.regenerate(store, cid)
            d.notify()

            async def answered():
                return await store.read(lambda c: c.execute(
                    "SELECT state FROM turns WHERE id=?", (tid,)).fetchone()[0]) == "done"
            await until(answered, 5)
            await d.stop()
            tg.cancel_scope.cancel()
        assert (await _branch(store, cid))[-1] == "the answer"
        assert (await view.snapshot(store, cid))["unanswered"] is None
        await store.aclose()
        outer.cancel_scope.cancel()


async def test_a_resumed_turn_keeps_its_branch_and_call_ids(store, cfg):
    """A turn that died between steps: the next step must not reuse call id `turn:0:0`
    (the journal would answer it with the old step's result), and the new messages
    must not fork off the user message."""
    arch = Arch()
    reg, cid, turn = await _start(store, arch)
    with pytest.raises(RuntimeError):     # step 0 (who) settles, then the process dies
        await run_turn(turn, store, cfg, Scripted(["arch__who", "arch__find"], crash_on_reply=2),
                       EventBus(), Token(turn["id"]), reg, None)
    assert arch.who_runs == 1
    assert (await turns.sweep(store, registry=reg))["interrupted"] == 1
    await turns.continue_turn(store, cid, turn["id"], reg)     # the user's Continue
    again = await turns.claim(store, "w2")
    assert again is not None and again["id"] == turn["id"]
    backend = Scripted(["arch__who", "arch__find"])
    await run_turn(again, store, cfg, backend, EventBus(), Token(again["id"]), reg, None)
    assert arch.find_runs == 1, "find was answered from who's journal entry"
    assert "FIND RESULT" in [m["content"] for m in backend.calls[-1] if m.get("role") == "tool"]
    branch = await _branch(store, cid)
    assert "WHO RESULT" in branch, "the resumed turn forked from the user message"
    assert branch[-1] == "the answer"


def test_a_cut_result_keeps_its_images():
    from celine.turns import tools
    out = tools._capped(ToolResult(content="word " * 5000, images=["a1"]),
                        {"max_output_tokens": 100})
    assert "[cut:" in out.content and out.images == ["a1"]


async def test_a_store_error_before_a_turn_runs_fails_that_turn_only(store, cfg, monkeypatch):
    """The dispatcher's worker outlives a turn whose start raised (here: the role lookup)."""
    import sqlite3
    from celine.settings import roles
    real = roles.resolve
    bad = await conversations.create(store)
    good = await conversations.create(store)

    async def resolve(st, c, cid):
        if cid == bad:
            raise sqlite3.OperationalError("database is locked")
        return await real(st, c, cid)
    monkeypatch.setattr(roles, "resolve", resolve)
    await user_says(store, bad, "hi")
    await user_says(store, good, "hi")
    async with anyio.create_task_group() as tg:
        d = Dispatcher(store, cfg, EventBus(), lambda: Scripted([]),
                       Registry())
        await d.start(tg)
        d.notify()
        await until(lambda: ended(store, bad), 5)
        await until(lambda: ended(store, good), 5)
        await d.stop()
        tg.cancel_scope.cancel()
    assert (await view.snapshot(store, bad))["turn"]["state"] == "failed"
    assert (await view.snapshot(store, good))["turn"]["state"] == "done"


@pytest.mark.parametrize("where", ["hold", "claim"])
async def test_a_store_error_in_the_worker_loop_leaves_the_worker_alive(
        store, cfg, monkeypatch, where):
    """A store error before a turn is claimed (the hold check, the claim) is logged; the
    worker looks again and runs the turn, the server stays up."""
    import sqlite3
    from celine.turns import dispatcher
    monkeypatch.setattr(dispatcher, "HOLD_RECHECK", 0.05)
    real = {"hold": dispatcher.her_hold, "claim": turns.claim}[where]
    failed = []

    async def flaky(*a, **kw):
        if not failed:
            failed.append(1)
            raise sqlite3.OperationalError("database is locked")
        return await real(*a, **kw)
    if where == "hold":
        monkeypatch.setattr(dispatcher, "her_hold", flaky)
    else:
        monkeypatch.setattr(turns, "claim", flaky)
    cid = await conversations.create(store)
    await user_says(store, cid, "hi")
    async with anyio.create_task_group() as tg:
        d = Dispatcher(store, cfg, EventBus(), lambda: Scripted([]), Registry())
        await d.start(tg)
        d.notify()
        await until(lambda: ended(store, cid), 5)
        await d.stop()
        tg.cancel_scope.cancel()
    assert failed
    assert (await view.snapshot(store, cid))["turn"]["state"] == "done"


async def test_a_turn_that_ended_during_shutdown_runs_its_end_hooks_at_the_next_start(
        store, cfg):
    cid = await conversations.create(store)
    await user_says(store, cid, "hi")
    seen = []

    async def hook(c, t):
        seen.append((c, t))
    async with anyio.create_task_group() as tg:
        d = Dispatcher(store, cfg, EventBus(), lambda: Scripted([]), Registry())
        d.on_end(hook)
        await d.start(tg)
        d.notify()
        await until(lambda: ended(store, cid), 5)
        await d.stop()                       # may cut the hooks off
        tg.cancel_scope.cancel()
    tid = (await view.snapshot(store, cid))["turn"]["id"]
    await store.write(lambda c: c.execute("UPDATE turns SET ended_hooks=0 WHERE id=?", (tid,)))
    seen.clear()
    async with anyio.create_task_group() as tg:
        d = Dispatcher(store, cfg, EventBus(), lambda: Scripted([]), Registry())
        d.on_end(hook)
        await d.start(tg)
        await until(lambda: (cid, tid) in seen, 5)
        flag = lambda c: c.execute("SELECT ended_hooks FROM turns WHERE id=?", (tid,)).fetchone()[0]
        await until(lambda: store.read(flag), 5)
        await d.stop()
        tg.cancel_scope.cancel()
