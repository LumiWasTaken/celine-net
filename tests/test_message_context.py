"""A skill's block at the end of a user message: computed once, stored, sent the same
way every time after (turns/message_context.py)."""
from __future__ import annotations

from chat_fixtures import user_says

import pytest

from fake_backend import FakeBackend
from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.skills import Registry
from celine.turns import context, message_context

pytestmark = pytest.mark.anyio


class Scene:
    name = "scene"

    def __init__(self):
        self.text, self.asked = "the ford at dusk", 0

    async def message_context(self, ctx, message):
        self.asked += 1
        return (self.text, {"why": "test"}) if self.text else None


async def test_block_is_stored_once_and_sent_the_same(store):
    cfg = load()
    skill, reg = Scene(), Registry()
    reg.add_specs(skill, [])
    cid = await conversations.create(store)
    mid, tid, _ = await user_says(store, cid, "hello")

    chain = await store.read(lambda c: tree.path(c, cid))
    await message_context.ensure(store, cfg, reg, cid, tid, message_context.unsent(chain))
    built = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[])
    user = [m["content"] for m in built.messages if m["role"] == "user"]
    assert user[0].endswith("hello\n\nthe ford at dusk")

    skill.text = "changed"
    chain = await store.read(lambda c: tree.path(c, cid))
    await message_context.ensure(store, cfg, reg, cid, tid, message_context.unsent(chain))
    again = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[])
    assert again.messages == built.messages and skill.asked == 1

    # a message taken at a step boundary gets its block too
    taken = [{"id": "m2", "role": "user", "content": "later", "created_at": None}]
    await store.write(lambda c: c.execute(
        "INSERT INTO messages(id, conversation_id, parent_id, role, content, created_at) "
        "SELECT 'm2', conversation_id, id, 'user', 'later', created_at FROM messages "
        "WHERE id=?", (mid,)))
    await message_context.ensure(store, cfg, reg, cid, tid, taken)
    assert context.user_wire(cfg, taken, "c1")[0]["content"].endswith("later\n\nchanged")


async def test_a_message_nothing_is_added_to_stays_bare(store):
    cfg = load()
    skill, reg = Scene(), Registry()
    skill.text = ""
    reg.add_specs(skill, [])
    cid = await conversations.create(store)
    await user_says(store, cid, "hello")
    chain = await store.read(lambda c: tree.path(c, cid))
    await message_context.ensure(store, cfg, reg, cid, "t", message_context.unsent(chain))
    skill.text = "later text"
    chain = await store.read(lambda c: tree.path(c, cid))
    await message_context.ensure(store, cfg, reg, cid, "t", message_context.unsent(chain))
    built = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[])
    user = [m["content"] for m in built.messages if m["role"] == "user"]
    assert user[0].endswith("hello") and skill.asked == 1


async def test_a_tail_written_first_never_stands_for_the_block(store):
    cfg = load()
    skill, reg = Scene(), Registry()
    reg.add_specs(skill, [])
    cid = await conversations.create(store)
    mid, tid, _ = await user_says(store, cid, "hello")
    await message_context.put_tail(store, mid, "now")
    chain = await store.read(lambda c: tree.path(c, cid))
    await message_context.ensure(store, cfg, reg, cid, tid, message_context.unsent(chain))
    built = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[])
    user = [m["content"] for m in built.messages if m["role"] == "user"]
    assert user[0].endswith("hello\n\nthe ford at dusk\n\nnow") and skill.asked == 1
