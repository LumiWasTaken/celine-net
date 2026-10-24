"""A conversation's home is the server's one answer (homes.py): it follows its root, and
every list row, sub-agent row, search hit and Intercom sender carries the same link. And
the place a turn runs in (skills/places.py)."""
from __future__ import annotations

import sqlite3

import pytest

from celine import homes
from celine.conversations import store as conversations
from celine.db.migrate import migrate
from celine.intercom.store import source_of
from celine.skills.places import offered, place_of
from celine.sub_agents.skill import agents_of

pytestmark = pytest.mark.anyio


async def _tree(store) -> dict[str, str]:
    ids = {"chat": await conversations.create(store, kind="chat"),
           "mission": await conversations.create(store, kind="mission"),
           "run": await conversations.create(store, kind="background", trigger_key="run:r1"),
           "task": await conversations.create(store, kind="background")}
    for parent in ("chat", "mission", "run", "task"):
        ids[f"{parent}-agent"] = await conversations.create(
            store, agent_of=ids[parent], agent_name="helper", kind="agent")
    await store.write(lambda c: c.execute(
        "INSERT INTO runs(id, schedule, label, slot, night, state, conversation_id, "
        "created_at) VALUES('r1', 'diary', 'Diary', 's', '2026-10-22', 'done', ?, "
        "'2026-10-22T23:00:00+00:00')", (ids["run"],)))
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET created_at='2026-10-21T10:00:00+00:00' WHERE id=?",
        (ids["task"],)))
    return ids


async def test_each_conversation_lives_where_its_root_does(store):
    ids = await _tree(store)
    got = await store.read(lambda c: {k: homes.of(c, v) for k, v in ids.items()})
    assert got == {
        "chat": {"home": "her", "link": f"/chat/{ids['chat']}"},
        "chat-agent": {"home": "her", "link": f"/chat/{ids['chat-agent']}"},
        "mission": {"home": "missions", "link": f"/mission/{ids['mission']}"},
        "mission-agent": {"home": "missions", "link": f"/mission/{ids['mission-agent']}"},
        "run": {"home": "routines", "link": "/routines/2026-10-22/run/r1"},
        "run-agent": {"home": "routines",
                      "link": f"/routines/2026-10-22/task/{ids['run-agent']}"},
        "task": {"home": "routines", "link": f"/routines/2026-10-21/task/{ids['task']}"},
        "task-agent": {"home": "routines",
                       "link": f"/routines/2026-10-21/task/{ids['task-agent']}"},
    }
    assert await store.read(lambda c: homes.of(c, "nope")) == {"home": None, "link": None}


async def test_lists_and_senders_carry_the_same_link(store):
    ids = await _tree(store)
    rows = {r["id"]: r for r in await conversations.list(store)}
    assert (rows[ids["mission"]]["home"], rows[ids["mission"]]["link"]) == \
        ("missions", f"/mission/{ids['mission']}")
    agent = rows[ids["chat"]]["agents"][0]
    assert (agent["home"], agent["link"]) == ("her", f"/chat/{ids['chat-agent']}")
    every = {a["id"]: a for a in await store.read(lambda c: agents_of(c))}
    assert every[ids["run-agent"]]["link"] == f"/routines/2026-10-22/task/{ids['run-agent']}"
    for k, v in ids.items():
        src = await store.read(lambda c, v=v: source_of(c, f"agent:{v}"))
        assert src["link"] == (await store.read(lambda c, v=v: homes.of(c, v)))["link"], k


def test_place_of_each_kind(tmp_path):
    db = tmp_path / "celine.db"
    migrate(db)
    c = sqlite3.connect(db)
    for cid, kind, agent_of, key in (
            ("chat", "chat", None, None), ("bridge", "background", None, "bridge:whatsapp"),
            ("run", "background", None, "run:r1:1"), ("trig", "chat", None, "trigger:t"),
            ("mis", "mission", None, None), ("sub", "agent", "mis", None),
            ("subsub", "agent", "sub", None), ("orphan", "agent", None, None)):
        c.execute("INSERT INTO conversations(id, kind, agent_of, trigger_key, created_at, "
                  "updated_at) VALUES(?, ?, ?, ?, 'x', 'x')", (cid, kind, agent_of, key))
    got = {cid: place_of(c, cid) for cid in (
        "chat", "bridge", "run", "trig", "mis", "sub", "subsub", "orphan", "gone")}
    assert got == {"chat": "chat", "bridge": "chat", "run": "background",
                   "trig": "background", "mis": "mission", "sub": "mission",
                   "subsub": "mission", "orphan": "background", "gone": "background"}
    assert offered("notebook", "chat") and offered("notebook", "mission")
    assert offered("appearance", "chat") and not offered("appearance", "mission")
    assert offered("some_mcp_server", "mission") and offered("some_mcp_server", "chat")
