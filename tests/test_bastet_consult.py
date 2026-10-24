"""The owner answers Bastet's consult in her intercom thread (guard/consult.py): their
words are their answer on that decision's banner, and its line comes back in the thread."""
from __future__ import annotations

import pytest

from celine.boot import load
from celine.core.services import Services
from celine.guard import banners, consult, freeze, owner
from celine.guard import store as decisions
from celine.guard.small_role import Unavailable
from celine.intercom import send

pytestmark = pytest.mark.anyio

TURN = {"id": "t1", "conversation_id": "c1"}
HIM = owner.Owner("session", owner._SEAL)


async def _his_reply(app, monkeypatch, said) -> tuple[int, str | None]:
    """A freeze it asks about, and their reply in its thread, read as `said`."""
    async def reader(app, text, banner):
        if isinstance(said, Exception):
            raise said
        return said
    monkeypatch.setattr(banners, "read", reader)
    rid = await decisions.record(app.store, turn=TURN, call={"id": "k1", "name": "shell__run"},
                                 mode="on", rule="escape", does="freeze", decision="stop")
    row = await freeze.freeze(app, by="guard", why="a way out", conversation_id="c1",
                              turn_id="t1", decision_id=rid)
    asked = await app.store.read(lambda c: c.execute(
        "SELECT asked_id FROM freezes WHERE id=?", (row["id"],)).fetchone()[0])
    out = await send(app.store, "user", "", "go on, that was me", reply_to=asked)
    return out["intercom_id"], await consult.replied(app, out["intercom_id"],
                                                     "go on, that was me", by=HIM)


async def test_his_reply_in_her_thread_answers_her_and_she_says_so_there(store, monkeypatch):
    app = Services(cfg=load(), store=store)
    iid, line = await _his_reply(app, monkeypatch, {"grant": "unfreeze"})
    assert await freeze.frozen(store) is None, "their words in its thread unfroze it"
    back = await store.read(lambda c: dict(c.execute(
        "SELECT text, reply_to, to_addr FROM intercom WHERE from_addr='system:guard' "
        "ORDER BY id DESC LIMIT 1").fetchone()))
    assert back["reply_to"] == iid and back["to_addr"] == "user" and back["text"] == line
    by = await store.read(lambda c: c.execute(
        "SELECT unfrozen_by FROM freezes ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert by == "user", "a precedent it reads next time"


async def test_a_reply_she_cannot_read_changes_nothing_and_she_says_so(store, monkeypatch):
    app = Services(cfg=load(), store=store)
    _, line = await _his_reply(app, monkeypatch, Unavailable("no model."))
    assert await freeze.frozen(store) is not None
    assert line.startswith("I could not read that: no model.")


async def test_a_reply_in_a_thread_without_her_is_only_a_message(store):
    app = Services(cfg=load(), store=store)
    first = await send(store, "user", "user", "a note to self")
    out = await send(store, "user", "", "more", reply_to=first["intercom_id"])
    assert await consult.replied(app, out["intercom_id"], "more", by=HIM) is None
