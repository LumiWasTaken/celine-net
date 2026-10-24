"""A background job driven through the real turn runner and Jobs: it carries its call
from start to result message, a failed job still posts, and a job and its conversation
can part ways (delete, Stop, restart): nothing may crash, go quiet, lose a result or lie
to it about it."""
from __future__ import annotations

from chat_fixtures import until, user_says


import anyio
import pytest

from celine.conversations import store as conversations, tree, view
from fake_backend import FakeBackend
from celine.core.bus import EventBus
from celine.jobs.jobs import Jobs
from celine.skills import Registry
from celine.turns import context
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns
from celine.jobs import store as job_rows

from jobs_fixtures import BG, IMAGE, Painter, cfg, tool_texts, two_shot  # noqa: F401  (the fixture)

pytestmark = pytest.mark.anyio
DRAW = "painter__draw"


async def _start(store, cfg, painter, tg, notified):
    """A turn that hands painter__draw to a job; returns (cid, jobs) once it is one."""
    reg = Registry()
    reg.add(painter)
    cid = await conversations.create(store)
    await user_says(store, cid, "draw me")
    turn = await turns.claim(store, "w")
    jobs = Jobs(store, cfg, lambda: notified.append(1), tg)
    await run_turn(turn, store, cfg, two_shot(DRAW, BG), EventBus(), Token(turn["id"]), reg, jobs)
    with anyio.fail_after(3):
        await painter.promoted.wait()
    return cid, jobs


async def _job_messages(store, cid):
    return [m for m in (await view.snapshot(store, cid))["messages"]
            if (m["intercom"] or {}).get("kind") == "job"]


async def _turns(store, cid):
    return await store.read(lambda c: [tuple(r) for r in c.execute(
        "SELECT origin, state FROM turns WHERE conversation_id=? ORDER BY created_at",
        (cid,))])


async def test_an_image_job_carries_its_call_from_start_to_result_and_wakes_her_once(
        store, cfg):
    from celine.intercom import deliver
    painter, notified = Painter(), []
    async with anyio.create_task_group() as tg:
        cid, jobs = await _start(store, cfg, painter, tg, notified)
        call = next(iter((await view.snapshot(store, cid))["tool_calls"].values()))[0]
        assert call["state"] == "detached" and call["started_at"]
        (job,) = await job_rows.list(store, "running", conversation=cid)
        assert job["tool_call_id"] == call["id"] and job["progress"] == "Generating the image"
        painter.release.set()
        await until(lambda: notified, 3)
        tg.cancel_scope.cancel()
    [posted] = await _job_messages(store, cid)
    assert posted["content"].startswith("Made one image.")
    assert posted["intercom"]["data"]["tools"] == [DRAW]
    (job,) = await job_rows.list(store, conversation=cid)
    assert job["state"] == "done" and job["display"] == IMAGE
    assert job["tool_call_id"] == call["id"] and job["job_message_id"] == posted["id"]
    assert job["started_at"] == call["started_at"] and job["finished_at"]
    assert await job_rows.get(store, job["id"]) == job, "one job shape"
    assert await deliver.deliver_idle(store, cid) is None, "nothing left to land"
    assert await _turns(store, cid) == [("user", "done"), ("intercom", "queued")]


async def test_a_failed_job_still_posts_and_reads_failed(store, cfg):
    painter, notified = Painter(fail=True), []
    async with anyio.create_task_group() as tg:
        cid, _ = await _start(store, cfg, painter, tg, notified)
        painter.release.set()
        await until(lambda: notified, 3)
        tg.cancel_scope.cancel()
    [posted] = await _job_messages(store, cid)
    assert posted["content"] == "ComfyUI is offline"
    assert [j["state"] for j in await job_rows.list(store, conversation=cid)] == ["failed"]
    snap = await view.snapshot(store, cid)
    assert next(iter(snap["tool_calls"].values()))[0]["state"] == "error"


async def test_a_conversation_cannot_be_deleted_while_its_job_runs(store, cfg):
    painter, notified = Painter(), []
    async with anyio.create_task_group() as tg:
        cid, jobs = await _start(store, cfg, painter, tg, notified)
        with pytest.raises(tree.Busy):
            await conversations.delete(store, cid)
        painter.release.set()
        await until(lambda: notified, 3)
        await store.write(lambda c: c.execute(
            "UPDATE turns SET state='done' WHERE conversation_id=?", (cid,)))
        assert await conversations.delete(store, cid), "free again once the job landed"
        tg.cancel_scope.cancel()


async def test_a_job_whose_conversation_vanished_does_not_take_the_app_down(store, cfg):
    painter = Painter()
    async with anyio.create_task_group() as tg:
        cid, jobs = await _start(store, cfg, painter, tg, [])
        await store.write(lambda c: c.execute("DELETE FROM conversations WHERE id=?", (cid,)))
        painter.release.set()
        await until(lambda: not jobs.handles, 3)          # the job finished
        assert await view.snapshot(store, await conversations.create(store)), "the writer works"
        tg.cancel_scope.cancel()


async def test_a_stopped_job_tells_her_so_in_one_quiet_job_message(store, cfg):
    async with anyio.create_task_group() as tg:
        cid, jobs = await _start(store, cfg, Painter(), tg, [])
        job_id = next(iter(jobs.handles))
        [placeholder] = await tool_texts(store, cid)
        assert await jobs.cancel(job_id)
        await until(lambda: _job_messages(store, cid), 3)
        assert await tool_texts(store, cid) == [placeholder], "the row it read stays"
        [stopped] = await _job_messages(store, cid)
        assert "stopped before it finished" in stopped["content"]
        assert stopped["intercom"]["data"]["stopped_by"] == "user"
        built = await context.build(store, cfg, FakeBackend(), cid, system="", tools=[])
        assert sum(stopped["content"] in (m["content"] or "") for m in built.messages) == 1
        job = await job_rows.get(store, job_id)
        assert job["state"] == "cancelled" and job["job_message_id"] == stopped["id"]
        assert await store.read(lambda c: c.execute(
            "SELECT count(*) FROM turns WHERE conversation_id=? AND state='queued'",
            (cid,)).fetchone()[0]) == 0, "a Stop wakes no one"
        tg.cancel_scope.cancel()


async def test_a_detached_result_is_in_the_next_request_once_and_the_prefix_holds(
        store, cfg):
    painter, notified = Painter(), []
    reg = Registry()
    reg.add(painter)
    cid = await conversations.create(store)
    await user_says(store, cid, "draw me")
    turn = await turns.claim(store, "w")
    first = two_shot(DRAW, BG)
    async with anyio.create_task_group() as tg:
        jobs = Jobs(store, cfg, lambda: notified.append(1), tg)
        await run_turn(turn, store, cfg, first, EventBus(), Token(turn["id"]), reg, jobs)
        painter.release.set()
        await until(lambda: notified, 3)
        tg.cancel_scope.cancel()
    seen = FakeBackend()
    turn = await turns.claim(store, "w")
    assert turn and turn["conversation_id"] == cid
    await run_turn(turn, store, cfg, seen, EventBus(), Token(turn["id"]), reg)

    def body(msgs):
        # the tail (the time now, built per turn) is never part of the prefix
        def text(c):
            return "\n".join(b["text"] for b in c if not b.get("tail")) \
                if isinstance(c, list) else c
        return [(m["role"], text(m.get("content"))) for m in msgs if m["role"] != "system"]
    earlier, now = body(first.calls[-1]), body(seen.calls[0])
    assert now[:len(earlier)] == earlier, "the earlier request prefix is unchanged"
    assert sum("Made one image." in (t or "") for _, t in now) == 1, "the result once"
    assert "Made one image." not in str(earlier)
