"""Impersonate: the user's next message, written from their last three messages."""
from __future__ import annotations

import pytest

from celine.backends.base import Done, TextDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.conversations import store as conversations, tree

pytestmark = pytest.mark.anyio


async def test_impersonate_writes_as_the_user_from_their_last_three_messages(store):
    from celine.conversations import impersonate
    cfg = load()
    cid = await conversations.create(store)
    leaf = None
    for i, text in enumerate(["an old one about boats", "ok", "where are you right now",
                              "come home soon, the tea is cold"]):
        def add(c, text=text, leaf=leaf):
            m = tree.append(c, store, cid, leaf, "user", text, "user")
            r = tree.append(c, store, cid, m["id"], "assistant", f"reply {i}", "model")
            return r["id"]
        leaf = await store.write(add)
    sent = []

    class Long(FakeBackend):
        async def stream(self, messages, tools, body=None):
            sent.append(messages)
            for _ in range(40):
                yield TextDelta("I am on the way. ")
            yield Done(reason="stop")
    text = await impersonate.write(store, cfg, Long(), cid)
    system = sent[0][0]["content"]
    assert "where are you right now" in system and "the tea is cold" in system
    assert "boats" not in system.split("their last messages:")[1], "the last three only"
    assert "about 4 words long" in system and "never more than 6" in system
    average = (2 + 23 + 31) // 3
    assert len(text) <= average * 3 // 2 and text.endswith(".")
