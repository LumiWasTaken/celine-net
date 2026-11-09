"""The agent finds and reads its own things from any conversation: a mission whole, the
runs and triggers, a conversation by run, mission or trigger name, the newest
conversations of a kind, every thread with the user, and its jobs."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.intercom import store as rows
from celine.intercom.skill import IntercomSkill
from celine.missions import store as missions
from celine.missions.skill import Missions
from celine.schedules import Schedule
from celine.schedules.skill import Routines
from celine.skills import ToolContext
from celine.skills.conversations import PastConversations
from celine.triggers import Event

pytestmark = pytest.mark.anyio


def _put(store, frm, to, kind, text, **kw):
    return store.write(lambda c: rows.insert(c, frm, to, kind, text, **kw))


async def _said(store, cid: str, text: str) -> None:
    await store.write(lambda c: tree.append(c, store, cid, None, "assistant", text, "model"))


async def _house(store):
    """A chat here, another chat, a mission with a plan, a sub-agent and talk with the
    user, a run of World and a trigger's background task."""
    here = await conversations.create(store, "Here")
    chat = await conversations.create(store, "Groceries")
    await _said(store, chat, "Milk is on the list.")
    m = await conversations.create(store, "Photos", kind="mission")
    await missions.create(store, m, {
        "name": "Photos", "terminal": "photos", "brief": "Sort the photos by year.\nThen tag.",
        "report_to": chat, "status": "needs_you", "note": "Which year first?"})
    steps = [{"title": "Sort", "state": "done", "checkpoint": "none", "model": None},
             {"title": "Tag", "state": "doing", "checkpoint": "ask", "model": None}]
    await store.write(lambda c: c.execute(
        "INSERT INTO plans(conversation_id, steps, updated_at) VALUES(?,?,'x')",
        (m, json.dumps(steps))))
    await _said(store, m, "Sorting now.")
    sub = await conversations.create(store, title="scout", agent_of=m, agent_name="scout",
                                     access="read")
    report = await _put(store, f"agent:{sub}", f"agent:{m}", "report", "Found 400 photos.")
    asked = await _put(store, f"agent:{m}", "user", "message", "Which year first?")
    hello = await _put(store, f"agent:{chat}", "user", "message", "Milk is 1 euro.")
    answer = await _put(store, "user", "", "message", "2019", reply_to=asked)
    await store.write(lambda c: rows.read(c, [asked]))
    run = await conversations.create(store, "World", trigger_key="run:r1", kind="background")
    await _said(store, run, "Headlines.\nThe rain stops on Friday.")
    await store.write(lambda c: c.execute(
        "INSERT INTO runs(id, schedule, label, slot, night, state, conversation_id, result, "
        "created_at) VALUES ('r1', 'world', 'World', '2026-10-30T01:00:00.000+00:00', "
        "'2026-10-30', 'done', ?, 'Headlines.\nThe rain stops on Friday.\n', 'x'), "
        "('r0', 'world', 'World', '2026-10-29T01:00:00.000+00:00', '2026-10-29', 'failed', "
        "NULL, NULL, 'x')", (run,)))
    await store.write(lambda c: c.execute("UPDATE runs SET error='502' WHERE id='r0'"))
    task = await conversations.for_trigger(store, "trigger:new_mail", "new_mail")
    await _put(store, "system:email", f"agent:{task}", "trigger", "A mail came.",
               data={"trigger": "new_mail"})
    await _said(store, task, "Nothing for them.")
    return SimpleNamespace(here=here, chat=chat, m=m, sub=sub, run=run, task=task,
                           report=report, asked=asked, hello=hello, answer=answer)


def _ctx(h, store, cfg):
    return ToolContext(h.here, "t", "k", store, cfg, None)


async def test_missions_read_gives_one_mission_whole_and_list_its_brief(store):
    cfg = load()
    h = await _house(store)
    skill, ctx, user = Missions(cfg), _ctx(h, store, cfg), cfg.user
    out = (await skill.read(ctx, "photos")).content
    assert out.startswith(f"Mission Photos, from {user} ({h.m}): needs you. Which year first?")
    assert "Brief:\nSort the photos by year.\nThen tag." in out
    assert f'Reports in: "Groceries" ({h.chat}).' in out
    assert f"[x] 1. Sort\n[>] 2. Tag (ask {user} when done)" in out
    assert f"- scout (done, conversation {h.sub[:8]}): report #{h.report}: Found 400 photos." in out
    talk = out.split(f"With {user}, newest last:\n", 1)[1].splitlines()
    assert talk[0].startswith(f"- #{h.asked} ") and talk[0].endswith("you: Which year first?")
    assert talk[1].endswith(f"{user}: 2019")
    assert talk[2] == f'Its conversation: past_conversations__read(conversation="{h.m[:8]}").'
    assert (await skill.read(ctx, h.m[:8])).content == out
    wrong = await skill.read(ctx, "nope")
    assert wrong.is_error and "missions__list gives the names" in wrong.content
    listed = (await skill.list(ctx)).content
    assert "\n  brief: Sort the photos by year. Then tag." in listed


async def test_routines_give_runs_with_their_result_and_the_triggers(store):
    cfg = load()
    cfg.timezone = "UTC"
    cfg.schedules = {"world": Schedule("world", "World", "0 1 * * *", "Go.")}
    h = await _house(store)
    skill = Routines(cfg)
    mail = SimpleNamespace(name="email", triggers=(
        Event("new_mail", "poll", "A mail came.", label="New mail", about="Reads new mail."),))
    skill.app = SimpleNamespace(cfg=cfg, registry=SimpleNamespace(skills={"email": mail}))
    ctx = _ctx(h, store, cfg)
    read = (await skill.read(ctx, "world")).content
    assert "- run:r1 Fri 30 Oct 01:00 done\n  result: The rain stops on Friday." in read
    assert "- run:r0 Thu 29 Oct 01:00 failed: 502" in read
    newest = (await skill.list(ctx, runs=1)).content
    assert newest == "- run:r1 World Fri 30 Oct 01:00 done\n  result: The rain stops on Friday."
    assert (await skill.list(ctx, night="2026-10-29")).content.startswith("- run:r0 World ")
    assert (await skill.list(ctx, night="Friday")).is_error
    listed = (await skill.list(ctx)).content
    assert listed.startswith("- world: World, every day at 01:00, on, last run ")
    fired = listed.split("Triggers:\n", 1)[1]
    assert fired.startswith("- new_mail: New mail, off, last fired ")
    assert fired.endswith(f", conversation {h.task[:8]}")


async def test_past_conversations_read_takes_a_run_a_mission_and_a_trigger(store):
    cfg = load()
    h = await _house(store)
    skill, ctx = PastConversations(cfg), _ctx(h, store, cfg)
    run = (await skill.read(ctx, "run:r1")).content
    assert run.startswith(f"conversation {h.run[:8]}: World") and "run World, done" in run
    assert (await skill.read(ctx, "Photos")).content.startswith(f"conversation {h.m[:8]}: ")
    assert "Nothing for them." in (await skill.read(ctx, "new_mail")).content
    wrong = await skill.read(ctx, "run:zz")
    assert wrong.is_error and "run:<id>" in wrong.content and "trigger's name" in wrong.content


async def test_past_conversations_list_the_newest_of_a_kind(store):
    cfg = load()
    cfg.timezone = "UTC"
    h = await _house(store)
    skill, ctx = PastConversations(cfg), _ctx(h, store, cfg)
    runs = (await skill.list(ctx, kind="run")).content
    assert runs.startswith(f'- {h.run[:8]} run "World": started ') and "run World, done" in runs
    assert "\n  last: The rain stops" not in runs and "\n  last: Headlines." in runs
    assert h.chat[:8] not in runs
    every = (await skill.list(ctx, limit=50)).content
    assert all(x[:8] in every for x in (h.chat, h.m, h.run, h.task))
    assert h.here[:8] not in every and "mission needs you" in every
    assert (await skill.list(ctx, kind="sub-agent")).content == "No sub-agent conversations yet."
    day = await store.read(lambda c: c.execute(
        "SELECT max(created_at) FROM messages").fetchone()[0][:10])
    tasks = (await skill.list_day(ctx, day, kind="task")).content
    assert f'{h.task[:8]} task "new_mail"' in tasks and h.run[:8] not in tasks


async def test_intercom_read_with_user_lists_every_conversations_threads(store):
    cfg = load()
    h = await _house(store)
    skill, ctx, user = IntercomSkill(cfg), _ctx(h, store, cfg), cfg.user
    out = (await skill.read(ctx, with_="user")).content.splitlines()
    assert out[0].startswith(f'#{h.asked} in mission "Photos" ({h.m}): 2 messages, last from '
                             f'{user} ')
    assert out[1].startswith(f'#{h.hello} in chat "Groceries" ({h.chat}): 1 message, waits for '
                             f'{user}, last from you ')
    waiting = (await skill.read(ctx, with_="user", unread=True)).content
    assert waiting.startswith(f"#{h.hello} ") and f"#{h.asked} " not in waiting


async def test_intercom_read_lists_the_jobs_of_every_conversation(store):
    cfg = load()
    h = await _house(store)
    await store.write(lambda c: c.execute(
        "INSERT INTO jobs(id, conversation_id, tool_name, state, result, created_at, "
        "finished_at) VALUES ('J1aaaaaaaa', ?, 'shell__run', 'running', NULL, "
        "'2026-10-30T01:00:00.000+00:00', NULL), ('J2bbbbbbbb', ?, "
        "'image_generator__generate', 'done', 'Made 4 images.\nmore', "
        "'2026-10-30T02:00:00.000+00:00', '2026-10-30T02:05:00.000+00:00')", (h.chat, h.m)))
    said = await _put(store, f"agent:{h.m}", f"agent:{h.m}", "job", "Made 4 images.",
                      data={"jobs": ["J2bbbbbbbb"], "tools": ["image_generator__generate"]})
    out = (await IntercomSkill(cfg).read(_ctx(h, store, cfg), kind="job")).content.splitlines()
    assert out[0].startswith(f'- job J1aaaaaa shell__run in chat "Groceries" ({h.chat[:8]}): '
                             'running since ')
    assert out[1].startswith(f'- job J2bbbbbb image_generator__generate in mission "Photos" '
                             f'({h.m[:8]}): done ')
    assert out[1].endswith(f", result #{said}: Made 4 images.")
