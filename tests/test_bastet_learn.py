"""Bastet learns from past freezes (guard/learn.py): she lifts a freeze herself only on
the owner's own unfreezes of the same kind, never after they kept one, and else asks
them in one intercom message."""
from __future__ import annotations

import json

import pytest

from celine.boot import load
from celine.core.services import Services
from celine.guard import awareness, banners, freeze, owner, passes, rules
from celine.guard import store as decisions

pytestmark = pytest.mark.anyio

TURN = {"id": "t1", "conversation_id": "c1"}


async def _decision(store, n: int, tool="shell__run", rule="escape") -> int:
    return await decisions.record(store, turn=TURN, call={"id": f"k{n}", "name": tool},
                                  mode="on", rule=rule, does="freeze", decision="stop")


async def _guard_freeze(app, rid: int) -> dict:
    return await freeze.freeze(app, by="guard", why="a way out", conversation_id="c1",
                               turn_id="t1", decision_id=rid)


async def _owner_notes(store) -> list[str]:
    return await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT text FROM intercom WHERE from_addr='system:guard' AND to_addr='user' "
        "ORDER BY id")])


async def _past(app, n: int, tool="shell__run") -> list[int]:
    """n freezes of one kind, each unfrozen by the owner's session."""
    ids = []
    for i in range(n):
        row = await _guard_freeze(app, await _decision(app.store, 100 + i, tool))
        ids.append(row["id"])
        await freeze.unfreeze(app, by=owner.Owner("session", owner._SEAL))
    return ids


async def test_three_of_his_own_unfreezes_and_she_lifts_the_fourth(store):
    app = Services(cfg=load(), store=store)
    ids = await _past(app, 2)
    row = await _guard_freeze(app, await _decision(store, 1))
    assert (await freeze.frozen(store))["id"] == row["id"], "two are not enough: it asks"
    assert "Before: #" in (await _owner_notes(store))[-1]
    got = await store.read(lambda c: c.execute("SELECT asked_id FROM freezes WHERE id=?",
                                               (row["id"],)).fetchone()[0])
    assert got is not None
    await freeze.unfreeze(app, by=owner.Owner("session", owner._SEAL))
    ids.append(row["id"])
    row = await _guard_freeze(app, await _decision(store, 2))
    assert await freeze.frozen(store) is None, "the fourth is lifted"
    done = await store.read(lambda c: dict(c.execute(
        "SELECT unfrozen_by, precedents FROM freezes WHERE id=?", (row["id"],)).fetchone()))
    assert done["unfrozen_by"] == "bastet" and json.loads(done["precedents"]) == ids
    assert "Bastet lifted the freeze herself" in (await _owner_notes(store))[-1]
    line = await awareness.unfrozen_line(store, app.cfg, __import__("datetime").timezone.utc)
    assert "She lifted it herself" in line


async def test_a_kept_freeze_or_another_kind_never_counts(store):
    app = Services(cfg=load(), store=store)
    await _past(app, 3, tool="browser__open")          # another kind
    await _past(app, 3)
    first = await store.read(lambda c: c.execute(
        "SELECT decision_id FROM freezes WHERE by='guard' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0])
    await store.write(lambda c: c.execute(
        "INSERT INTO kv(k, v) VALUES(?, ?)", (f"{decisions.THREAD}{first}", json.dumps(
            [{"by": "guard", "text": "Kept.", "grant": {"grant": "keep"}}]))))
    await _guard_freeze(app, await _decision(store, 9))
    assert await freeze.frozen(store) is not None, "they kept one of them: it asks"
    note = (await _owner_notes(store))[-1]
    assert "you kept it" in note and "you unfroze it" in note


async def test_ask_in_guard_toml_always_asks(store):
    cfg = load()
    cfg.settings.guard = rules.validate({"learn": {"lift": "ask"}})
    app = Services(cfg=cfg, store=store)
    await _past(app, 4)
    await _guard_freeze(app, await _decision(store, 9))
    assert await freeze.frozen(store) is not None


async def test_the_precedent_holds_only_his_own_unfreezes_and_only_unfreezes(store):
    app = Services(cfg=load(), store=store)
    ids = await _past(app, 3)
    with pytest.raises(TypeError):
        await store.read(lambda c: owner.precedent(c, ids[:2], 3))
    await store.write(lambda c: c.execute(
        "UPDATE freezes SET unfrozen_by='bastet' WHERE id=?", (ids[0],)))
    with pytest.raises(TypeError):
        await store.read(lambda c: owner.precedent(c, ids, 3))
    by = await store.read(lambda c: owner.precedent(c, ids[1:], 2))
    with pytest.raises(TypeError):
        await passes.make(store, by=by, rule="all", minutes=5)
    with pytest.raises(TypeError):
        await banners.answer(app, 1, by=by, said="ok", decided={"grant": "unfreeze"})

