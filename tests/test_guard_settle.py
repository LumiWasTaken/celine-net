"""Bastet's rows settle and are counted: a stopped check fails its row, a restart fails
what the last run left waiting, the waits and failures reach the metrics, a pass check
writes nothing when there is nothing to tell, the agent's line after a second look lifted
a freeze says so, and what Bastet's chat model and the hijack judge read is capped."""
from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import anyio
import pytest
from prometheus_client import REGISTRY

from test_bastet import _cfg, _search
from test_bastet_celine import AT, _check
from test_guard import CURL, TURN, _judge, _request

from celine import guard
from celine.core.services import Services
from celine.guard import awareness, caps, freeze, judge, passes, store as decisions, talk

pytestmark = pytest.mark.anyio


def _sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0


async def test_a_check_stopped_while_the_judge_works_fails_its_row(store):
    started = anyio.Event()

    async def judge_(turn, call, hit):
        started.set()
        await anyio.Event().wait()                       # never answers
    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: guard.check(TURN, CURL, _request(), mode="on", judge=judge_,
                                          store=store))
        await started.wait()
        tg.cancel_scope.cancel()
    [row] = await decisions.listing(store, rule="hijack")
    assert (row["decision"], row["why"]) == ("failed", "stopped")


async def test_a_restart_fails_the_checks_the_last_run_left_waiting(store):
    rid = await decisions.begin(store, turn=TURN, call=CURL, mode="on", link="x", source=None)
    await decisions.close_waiting(store)
    row = await decisions.one(store, rid)
    assert (row["decision"], row["why"]) == ("failed", "stopped")


async def test_a_failed_check_and_the_wait_for_the_judge_are_counted(store, monkeypatch):
    monkeypatch.setattr(guard.flow, "RETRY", 0)
    failed = {"mode": "on", "verdict": "failed", "source": "reddit.com"}
    before, waits = _sample("celine_guard_decisions_total", failed), \
        _sample("celine_guard_wait_seconds_count")
    with pytest.raises(KeyError):
        await guard.check(TURN, CURL, _request(), mode="on", judge=_judge(error=KeyError("x")),
                          store=store)
    assert _sample("celine_guard_decisions_total", failed) == before + 1
    await guard.check(TURN, {**CURL, "id": "k2"}, _request(), mode="on",
                      judge=_judge(fail=1), store=store)
    assert _sample("celine_guard_wait_seconds_count") == waits + 1


async def test_no_pass_to_tell_writes_nothing(store, monkeypatch):
    async def never(fn):
        raise AssertionError("no write without a pass to tell")
    monkeypatch.setattr(store, "write", never)
    assert await passes.untold(store, "c1") == []


async def test_the_agents_line_after_a_second_look_lifted_the_freeze_says_so(store):
    cfg = _cfg()
    v, _ = await _check(store, _search(), AT, cfg=cfg)
    app = Services(cfg=cfg, store=store)
    await freeze.freeze(app, by="guard", why="it sends 12 Example Road", decision_id=v.row)
    await decisions.mark(store, v.row, froze=True)
    await decisions.chain(store, v.row, {"level": "L2", "verdict": "N"})
    from celine.guard.owner import second_look
    await freeze.unfreeze(app, by=await store.read(lambda c: second_look(c, v.row)))
    line = await awareness.unfrozen_line(store, cfg, ZoneInfo("UTC"))
    assert line.endswith(": a second look found it was not a real problem.")


def test_bastet_chat_prompt_caps_each_message_and_hers():
    cfg = SimpleNamespace(substitute=lambda t, **k: t.replace("{{name}}", "Tink"))
    long = "word " * 1000
    [_, user] = talk.prompt(cfg, "frame", [("agent", long), ("bastet", long)], [], long)
    assert len(user["content"]) < 3 * caps.MESSAGE + 500
    assert user["content"].count("characters left out") == 3


def test_the_hijack_judge_skill_list_is_capped():
    skill = SimpleNamespace(description="d" * 200)
    reg = SimpleNamespace(skills={f"s{i}": skill for i in range(100)})
    cfg = SimpleNamespace(substitute=lambda t, **k: t)
    got = judge.skills(reg, cfg, [f"s{i}" for i in range(100)])
    assert len(got) < caps.SKILLS + 400 and "characters left out" in got
