"""A detached shell call across a restart: its job carries the terminal, and the sweep
words each job by its tool. No shellbox here: a fake `shell` skill stands in for it."""
from __future__ import annotations

from chat_fixtures import until, user_says

import json

import anyio
import pytest

from celine.conversations import store as conversations, view
from celine.core.bus import EventBus
from celine.jobs.jobs import Jobs
from celine.skills import Registry, ToolResult, tool
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns
from celine.jobs import store as job_rows

from jobs_fixtures import BG, Painter, cfg, tool_texts, two_shot  # noqa: F401  (the fixture)

pytestmark = pytest.mark.anyio


class Box(Painter):
    """A detach tool named like the shell's: it waits for the test, then answers."""

    name = "shell"

    @tool(mode="detach", read_only=False, destructive=True, background="background")
    async def run(self, ctx, terminal: str = "main", command: str = "",
                  background: bool = False) -> ToolResult:
        """Runs."""
        await until(lambda: job_rows.get(ctx.store, ctx.turn_id), 3)
        self.promoted.set()
        await self.release.wait()
        return ToolResult(content="terminal build ~: exit 0 after 0.1 s")


async def _start(store, cfg, skill, tg):  # noqa: F811
    reg = Registry()
    reg.add(skill)
    from celine.turns import loads
    cid = await conversations.create(store)
    await loads.touch(store, cid, "skill", [skill.name])
    await user_says(store, cid, "build it")
    turn = await turns.claim(store, "w")
    jobs = Jobs(store, cfg, lambda: None, tg)
    backend = (two_shot("shell__run", json.dumps({"terminal": "build", "command": "make",
                                                   "background": True}))
               if skill.name == "shell" else two_shot("painter__draw", BG))
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), reg, jobs)
    with anyio.fail_after(3):
        await skill.promoted.wait()
    return cid, next(iter(jobs.handles))


async def _notice(store, cid):
    """The one notice in the conversation, with the queued turn it started."""
    msgs = (await view.snapshot(store, cid))["messages"]
    [notice] = [m for m in msgs if (m["intercom"] or {}).get("kind") == "notice"]
    assert await store.read(lambda c: c.execute(
        "SELECT origin FROM turns WHERE conversation_id=? AND state='queued' AND "
        "root_message_id=?", (cid, notice["id"])).fetchall()[0][0]) == "intercom"
    return notice["content"]


async def test_the_restart_sweep_settles_every_job_by_its_tool(store, cfg):  # noqa: F811
    async with anyio.create_task_group() as tg:
        shell_cid, shell_job = await _start(store, cfg, Box(), tg)
        paint_cid, _ = await _start(store, cfg, Painter(), tg)
        job = await job_rows.get(store, shell_job)
        assert (job["terminal"], job["prompt"]) == ("build", "make"), "the UI opens it live"
        tg.cancel_scope.cancel()                       # the process dies here
    before = await tool_texts(store, shell_cid) + await tool_texts(store, paint_cid)
    assert (await turns.sweep(store))["jobs"] == 2
    assert await tool_texts(store, shell_cid) + await tool_texts(store, paint_cid) == \
        before, "the rows it read stay as written"
    shell, paint = [await _notice(store, cid) for cid in (shell_cid, paint_cid)]
    assert "(shell__run): the app restarted while watching" in shell and "shell__read" in shell
    assert "(painter__draw): the server restarted" in paint
    assert (await turns.sweep(store))["total"] == 2, "the two queued turns"
    assert await _notice(store, shell_cid) == shell, "one notice each"
    job = await job_rows.get(store, shell_job)
    assert job["result"] in shell and job["terminal"] == "build", "the jobs row says it too"
