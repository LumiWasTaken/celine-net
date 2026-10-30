"""The tail of a turn is stored once on the message it answers (turns/message_context.py
put_tail): the next turn's request carries the earlier turn's message byte for byte, and
starts with every byte of the request before it."""
from __future__ import annotations

import json

import pytest

from chat_fixtures import user_says
from fake_backend import FakeBackend

from celine import clock
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn

pytestmark = pytest.mark.anyio


async def _turn(store, cfg, cid, backend, text):
    await user_says(store, cid, text)
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), None)


async def test_an_earlier_tail_is_byte_identical_in_the_next_request(store, monkeypatch):
    cfg = load()
    cid = await conversations.create(store)
    backend = FakeBackend()
    monkeypatch.setattr(clock, "now", lambda: "2026-10-23T10:38:00.000+00:00")
    await _turn(store, cfg, cid, backend, "first")
    monkeypatch.setattr(clock, "now", lambda: "2026-10-23T11:52:00.000+00:00")
    await _turn(store, cfg, cid, backend, "second")
    one, two = backend.calls
    assert "Now it is [Fri 23 Oct 10:38]" in one[-1]["content"]
    assert two[:len(one)] == one, "the second request starts with the first, byte for byte"
    assert "Now it is [Fri 23 Oct 11:52]" in two[-1]["content"]
    assert json.dumps(two).count("Now it is [") == 2, "one tail per turn, none moved"


async def test_a_regenerate_writes_a_fresh_tail(store, monkeypatch):
    from celine.conversations import tree
    cfg = load()
    cid = await conversations.create(store)
    backend = FakeBackend()
    monkeypatch.setattr(clock, "now", lambda: "2026-10-23T10:38:00.000+00:00")
    await _turn(store, cfg, cid, backend, "first")
    reply = await store.read(lambda c: c.execute(
        "SELECT id FROM messages WHERE role='assistant'").fetchone()[0])
    monkeypatch.setattr(clock, "now", lambda: "2026-10-23T11:52:00.000+00:00")
    await tree.retry(store, cid, reply)
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), None)
    again = backend.calls[-1][-1]["content"]
    assert "Now it is [Fri 23 Oct 11:52]" in again and "10:38]. Every" not in again
