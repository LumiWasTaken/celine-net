"""api/bulk.py: one action on many goes through each one's own route. Ids and an age
pick them; a conversation with a live turn is skipped with its reason, a scene is never
archived, and only what is in the Trash is deleted for good."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from celine.api import bulk as bulk_api
from celine.boot import load
from celine.conversations import api as conversations_api, store as conversations
from celine.core.services import Services
from celine.errors import install_error_handler

pytestmark = pytest.mark.anyio


@pytest.fixture
async def c(store):
    app = FastAPI()
    app.state.services = Services()
    install_error_handler(app)
    app.include_router(conversations_api.router)
    app.include_router(bulk_api.router)
    app.state.services.store, app.state.services.cfg = store, load()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        yield client


async def bulk(c, **body) -> dict:
    r = await c.post("/api/bulk-actions", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def listed(c, which: str = "") -> list[str]:
    q = f"?{which}=true" if which else ""
    return [x["id"] for x in (await c.get(f"/api/conversations{q}")).json()["conversations"]]


async def test_ids_archive_restore_trash_and_purge_each_by_its_rules(c, store):
    from chat_fixtures import user_says
    a = await conversations.create(store, "a")
    b = await conversations.create(store, "b")
    busy = await conversations.create(store, "busy")
    await user_says(store, busy, "hello")                   # a live turn of the user's

    out = await bulk(c, what="conversations", action="archive", ids=[a, b, busy, "nope"])
    assert out["done"] == [a, b]
    assert {s["id"] for s in out["skipped"]} == {busy, "nope"}
    assert all(s["reason"] for s in out["skipped"])
    assert set(await listed(c, "archived")) == {a, b} and await listed(c) == [busy]

    out = await bulk(c, what="conversations", action="unarchive", ids=[a])
    assert out["done"] == [a] and set(await listed(c)) == {a, busy}

    out = await bulk(c, what="conversations", action="purge", ids=[a])
    assert out["done"] == [] and out["skipped"][0]["id"] == a, "not in the Trash: kept"
    out = await bulk(c, what="conversations", action="trash", ids=[a, b, busy])
    assert out["done"] == [a, b] and [s["id"] for s in out["skipped"]] == [busy]
    assert set(await listed(c, "trashed")) == {a, b}
    out = await bulk(c, what="conversations", action="restore", ids=[b])
    assert out["done"] == [b] and b in await listed(c, "archived"), "back where it was"
    out = await bulk(c, what="conversations", action="purge", ids=[a])
    assert out["done"] == [a] and a not in await listed(c, "trashed")
    assert (await c.get(f"/api/conversations/{a}")).status_code == 404


async def test_older_than_takes_the_old_unpinned_chats_of_a_kind(c, store):
    old = await conversations.create(store, "old")
    pinned = await conversations.create(store, "pinned")
    await conversations.create(store, "new")
    mission = await conversations.create(store, "m", kind="mission")
    await store.write(lambda db: db.execute(
        "UPDATE conversations SET updated_at='2020-01-01T00:00:00.000+00:00' WHERE id IN (?,?,?)",
        (old, pinned, mission)))
    await store.write(lambda db: db.execute("UPDATE conversations SET pinned=1 WHERE id=?", (pinned,)))

    seen = await bulk(c, what="conversations", action="archive", kind="chat", older_than=90,
                      dry_run=True)
    assert seen == {"ids": [old]}
    assert old in await listed(c), "a dry run does nothing"
    out = await bulk(c, what="conversations", action="archive", kind="chat", older_than=90)
    assert out == {"done": [old], "skipped": []}
    assert (await bulk(c, what="conversations", action="archive", kind="mission",
                       older_than=30, dry_run=True)) == {"ids": [mission]}
    r = await c.post("/api/bulk-actions", json={"what": "conversations", "action": "trash",
                                        "kind": "chat", "older_than": 90})
    assert r.status_code == 400, "an age only archives"

