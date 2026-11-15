"""Who may see a conversation (guard/classify.py): classed after each turn, only ever
raised, sensitive until classed, never classed over the owner's own class; its past
chats results carry it (skills/conversations.py) for the leak rule and its gates."""
from __future__ import annotations

from types import SimpleNamespace

from chat_fixtures import user_says

import pytest

from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.guard import classify
from celine.settings import conversations as conv_settings
from celine.skills import ToolContext
from celine.skills.conversations import PastConversations
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


async def _turn(store, text: str, reply: str, cid: str | None = None):
    cid = cid or await conversations.create(store)
    mid, tid, _ = await user_says(store, cid, text)
    await store.write(lambda c: tree.append(c, store, cid, mid, "assistant", reply, "model",
                                            turn_id=tid))
    await turns.transition(store, tid, cid, "done")
    return cid, tid


def _app(store, monkeypatch, *, first=None, said=None):
    """An app whose First look answers `first` {question: p}, else whose guard role
    writes `said`."""
    asked = {}

    async def decide(llm, state, qs, **kw):
        asked["state"], asked["background"] = state, kw["background"]
        return {n: {"noul": first[n]} for n in qs}, "d3-mini"

    async def complete(llm, text, body, **kw):
        asked["prompt"] = text
        return said, None, "qwen"
    monkeypatch.setattr(classify, "decider", lambda llm: first is not None)
    monkeypatch.setattr(classify, "decide", decide)
    monkeypatch.setattr(classify, "complete", complete)
    return SimpleNamespace(store=store, cfg=load(), llm=object()), asked


async def _privacy(store, cid):
    return await store.read(lambda c: c.execute(
        "SELECT privacy FROM conversations WHERE id=?", (cid,)).fetchone()[0])


async def test_a_conversation_is_sensitive_until_classed(store):
    cid = await conversations.create(store)
    assert await store.read(lambda c: classify.of(c, [cid])) == "sensitive"


async def test_the_first_look_classes_an_exchange_in_the_background(store, monkeypatch):
    app, asked = _app(store, monkeypatch, first={"private": 0.1, "intimate": 0.9})
    cid, tid = await _turn(store, "we planned a date night", "Sounds lovely.")
    assert await classify.classify(app, cid, tid) == "nsfw"
    assert "date night" in asked["state"] and "Sounds lovely." in asked["state"]
    assert asked["background"] is True


async def test_a_class_only_rises(store, monkeypatch):
    app, _ = _app(store, monkeypatch, first={"private": 0.8, "intimate": 0.0})
    cid, tid = await _turn(store, "my doctor called", "Hope it went well.")
    assert await classify.classify(app, cid, tid) == "sensitive"
    app, _ = _app(store, monkeypatch, first={"private": 0.0, "intimate": 0.0})
    _, tid = await _turn(store, "what is 2+2", "4", cid)
    assert await classify.classify(app, cid, tid) == "sensitive"
    assert await _privacy(store, cid) == "sensitive"


async def test_without_a_first_look_the_guard_role_answers_one_word(store, monkeypatch):
    app, asked = _app(store, monkeypatch, said="Shareable.")
    cid, tid = await _turn(store, "what is the weather", "Sunny.")
    assert await classify.classify(app, cid, tid) == "informal"
    assert "what is the weather" in asked["prompt"]
    app, _ = _app(store, monkeypatch, said="I cannot tell")
    cid, tid = await _turn(store, "hi", "Hi.")
    assert await classify.classify(app, cid, tid) is None
    assert await store.read(lambda c: classify.of(c, [cid])) == "sensitive"


async def test_the_owners_class_is_never_classed_over_and_null_hands_it_back(
        store, monkeypatch):
    app, _ = _app(store, monkeypatch, first={"private": 0.9, "intimate": 0.9})
    cid, tid = await _turn(store, "hi", "Hi.")
    with pytest.raises(Exception, match="Privacy is one of"):
        conv_settings.validate(None, None, {"privacy": "secret"})
    await conv_settings.apply(store, cid, conv_settings.validate(None, None,
                                                                 {"privacy": "public"}))
    assert await classify.classify(app, cid, tid) is None
    assert (await conv_settings.get(store, cid))["privacy"] == "public"
    await conv_settings.apply(store, cid, {"privacy": None})
    assert (await conv_settings.get(store, cid))["privacy"] is None
    assert await classify.classify(app, cid, tid) == "nsfw"


async def test_past_chats_results_carry_their_conversations_class(store):
    cfg = load()
    a = await conversations.create(store, "Plans")
    b = await conversations.create(store, "Groceries")
    await user_says(store, a, "pelican trip plans")
    await user_says(store, b, "pelican shaped pasta")
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET privacy='informal' WHERE id=?", (b,)))
    here = await conversations.create(store)
    ctx = ToolContext(conversation_id=here, turn_id="t", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    skill = PastConversations(cfg)
    assert (await skill.read(ctx, b[:8])).display["class"] == "informal"
    assert (await skill.read(ctx, a[:8])).display["class"] == "sensitive"
    assert (await skill.search(ctx, "pelican")).display["class"] == "sensitive"
