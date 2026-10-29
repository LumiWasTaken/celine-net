"""The agent's mind (skills/mind.py): the mood's decay, its own block stored once per message
and only where its mind runs, with no line of what comes to mind, its thoughts, the
appraisal after a chat turn, and index builds in the LLM queue."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from chat_fixtures import user_says

from celine.backends.base import Done, TextDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.skills import Registry, ToolContext
from celine.skills import mind
from celine.turns import context, message_context

pytestmark = pytest.mark.anyio
T0 = datetime(2026, 10, 16, 7, 12, tzinfo=timezone.utc)


def test_a_mood_fades_halfway_back_in_one_half_life_and_names_its_event():
    base = (0.0, 0.0, 0.0)
    rows = [{"at": T0.isoformat(), "emotion": "angry", "strength": 1.0}]
    at_once = mind.current_mood(rows, base, 6, T0)
    later = mind.current_mood(rows, base, 6, T0 + timedelta(hours=6))
    assert at_once == pytest.approx(mind.PAD["angry"])
    assert later == pytest.approx(tuple(x / 2 for x in mind.PAD["angry"]))
    # a weak event pulls only part of the way
    half = mind.current_mood([{**rows[0], "strength": 0.5}], base, 6, T0)
    assert half == pytest.approx(tuple(x / 2 for x in mind.PAD["angry"]))
    assert mind.mood_words(at_once, base) == ("hostile", "strong")
    assert mind.mood_words(base, base)[1] == "slight"
    rows = [{"at": T0.isoformat(), "emotion": "annoyed", "strength": 0.8,
             "why": "the build broke again"}]
    line = mind.describe_mood(rows, timezone.utc, T0 + timedelta(hours=1))
    assert line.startswith("mood: ") and "annoyed since 07:12: the build broke again" in line
    assert mind.describe_mood([], timezone.utc, T0) == ""


async def _her(store, cfg):
    skill = mind.Mind(cfg)
    skill.app = SimpleNamespace(store=store, cfg=cfg, llm=None, registry=None)
    reg = Registry()
    reg.add_specs(skill, [])
    return skill, reg


async def test_the_block_has_her_mood_and_no_comes_to_mind_line(store):
    cfg = load()
    _, reg = await _her(store, cfg)
    cid = await conversations.create(store)
    await mind.feel(store, "happy", 0.8, "a good morning", "her")
    _, tid, _ = await user_says(store, cid, "hello")
    chain = await store.read(lambda c: tree.path(c, cid))
    await message_context.ensure(store, cfg, reg, cid, tid, message_context.unsent(chain))
    built = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[])
    user = [m["content"] for m in built.messages if m["role"] == "user"][0]
    assert "[your mind]\nmood: " in user and "\nnow: " not in user
    assert "comes to mind" not in user
    report = json.loads(await store.read(lambda c: c.execute(
        "SELECT report FROM message_context").fetchone()[0]))
    assert set(report["mind"]) == {"mood", "gap", "state"}


async def test_no_block_with_automatic_context_off(store):
    cfg = load()
    _, reg = await _her(store, cfg)
    off = await conversations.create(store)
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET auto_context=0 WHERE id=?", (off,)))
    await store.write(lambda c: c.execute(
        "INSERT INTO messages(id, conversation_id, role, content, created_at) "
        "VALUES('m1', ?, 'user', 'hi', ?)", (off, T0.isoformat())))
    ctx = ToolContext(off, "t", "", store, cfg, None)  # type: ignore[arg-type]
    assert await reg.message_context(ctx, {"id": "m1", "role": "user", "content": "hi"}) is None


async def test_the_gap_line_shows_after_an_hour(store):
    cfg = load()
    skill, _ = await _her(store, cfg)
    cid = await conversations.create(store)
    early, later = (T0 - timedelta(hours=9)).isoformat(), T0.isoformat()

    def rows(c):
        c.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) "
                  "VALUES('a', ?, 'user', 'night', ?)", (cid, early))
        c.execute("INSERT INTO messages(id, conversation_id, parent_id, role, content, "
                  "created_at) VALUES('b', ?, 'a', 'user', 'morning', ?)", (cid, later))
    await store.write(rows)
    ctx = ToolContext(cid, "t", "", store, cfg, None)  # type: ignore[arg-type]
    text, report = await skill.message_context(
        ctx, {"id": "b", "role": "user", "content": "morning", "created_at": later})
    assert "last message here 9 hours ago; last talk with " in text
    assert report["gap"].endswith("anywhere 9 hours ago")


async def test_a_thought_goes_into_todays_thoughts_with_the_time(store):
    cfg = load()
    skill, _ = await _her(store, cfg)
    cid = await conversations.create(store)
    ctx = ToolContext(cid, "t", "x", store, cfg, None)  # type: ignore[arg-type]
    first = await skill.think(ctx, "the build keeps failing on the same test")
    await skill.think(ctx, "maybe he forgot")
    rel = first.content.removeprefix("Saved ").removesuffix(".")
    assert rel.startswith("thoughts/") and rel.endswith(".md")
    text = (cfg.her.notebook / rel).read_text()
    lines = text.splitlines()
    assert lines[0].startswith("Last changed:")
    assert lines[1].endswith(" the build keeps failing on the same test") and lines[1].startswith("- ")
    assert lines[2].endswith(" maybe he forgot") and len(lines) == 3
    assert (cfg.her.notebook / ".versions" / rel).is_dir()


async def _chat_turn(store, cid, *, feel=False):
    def fn(c):
        c.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) "
                  "VALUES('u1', ?, 'user', 'the build broke again tonight', ?)",
                  (cid, T0.isoformat()))
        c.execute("INSERT INTO turns(id, conversation_id, root_message_id, state, "
                  "created_at) VALUES('t1', ?, 'u1', 'done', ?)", (cid, T0.isoformat()))
        c.execute("INSERT INTO messages(id, conversation_id, parent_id, role, content, "
                  "turn_id, created_at) VALUES('a1', ?, 'u1', 'assistant', 'oh. fun.', "
                  "'t1', ?)", (cid, T0.isoformat()))
        if feel:
            c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, state) "
                      "VALUES('tc', 'a1', 'mind__feel', '{}', 'ok')")
    await store.write(fn)


async def test_the_appraisal_after_a_chat_turn_sets_her_mood(store):
    from celine.backends.llm import LLM
    cfg = load()
    cid = await conversations.create(store)
    await _chat_turn(store, cid)
    answer = FakeBackend([TextDelta('{"emotion": "annoyed", "strength": 0.6, '
                                    '"why": "the build again"}'), Done(reason="stop")])
    app = SimpleNamespace(store=store, cfg=cfg, llm=LLM(cfg))
    got = await mind.appraise(app, cid, "t1", backend=answer)
    assert got == {"emotion": "annoyed", "strength": 0.6, "why": "the build again"}
    row = await store.read(lambda c: dict(c.execute(
        "SELECT emotion, strength, why, by, message_id FROM moods").fetchone()))
    assert row == {"emotion": "annoyed", "strength": 0.6, "why": "the build again",
                   "by": "appraisal", "message_id": "a1"}
    system = answer.calls[0][0]["content"]
    assert "jealous" in system and "—" not in system
    assert mind.parse_appraisal('ok {"emotion": "sad", "strength": 2, "why": "x"}') == \
        {"emotion": "sad", "strength": 1.0, "why": "x"}
    long = "because " * 100
    assert mind.parse_appraisal(json.dumps({"emotion": "sad", "strength": 0.5,
                                            "why": long}))["why"] == long, "taken whole"
    for bad in ('{"emotion": null}', '{"emotion": "meh", "strength": 0.5}', "no json"):
        assert mind.parse_appraisal(bad) is None, bad


async def test_her_own_feeling_wins_over_the_appraisal(store):
    from celine.backends.llm import LLM
    cfg = load()
    cid = await conversations.create(store)
    await _chat_turn(store, cid, feel=True)
    answer = FakeBackend([TextDelta('{"emotion": "happy", "strength": 1}'), Done(reason="stop")])
    app = SimpleNamespace(store=store, cfg=cfg, llm=LLM(cfg))
    assert await mind.appraise(app, cid, "t1", backend=answer) is None
    assert answer.calls == []


async def test_a_build_batch_takes_a_background_ticket(monkeypatch):
    import asyncio

    import anyio

    from celine.backends.queue import Priority, Scheduler
    from celine.kb import embed

    class Limits:
        def slots(self, endpoint, model):
            return 1

        def endpoint_max(self, endpoint):
            return 0

        def exclusive(self, endpoint):
            return False

    class Sched(Scheduler):
        def __init__(self):
            super().__init__(Limits())
            self.got, self.freed = [], []

        async def acquire(self, t, **kw):
            self.got.append(t)
            return await super().acquire(t, **kw)

        def release(self, t):
            self.freed.append(t)
            super().release(t)
    sched = Sched()
    monkeypatch.setattr(embed, "_queue", (sched, asyncio.get_running_loop()))
    sent = []
    pool = embed.Pool({"url": "http://gpu:8080/v1/embeddings", "model": "qwen3-embedding",
                       "workers": 1}, lambda s, t: sent.append(t) or [[1.0] for _ in t])
    await anyio.to_thread.run_sync(lambda: pool.run([["a"], ["b"]], lambda i, v: None,
                                                    lambda: None))
    assert sent == [["a"], ["b"]]
    t = sched.got[0]
    assert (t.endpoint, t.model, t.priority, t.label) == (
        "http://gpu:8080/v1", "qwen3-embedding", Priority.BACKGROUND, "Search index build")
    with anyio.fail_after(5):           # the releases come through the loop
        while len(sched.freed) < 2:
            await anyio.sleep(0)
