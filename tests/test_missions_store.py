"""Mission rows, their terminal names and the Missions page's counts (docs/ARCHITECTURE.md)."""
from __future__ import annotations

import pytest

from celine.conversations import store as conversations
from celine.missions import store as missions
from celine.terminals import store as terminals

pytestmark = pytest.mark.anyio

BRIEF = {"name": "RSS feed", "terminal": "rss-feed",
         "brief": "Build the feed. It validates. Use python, no npm. The site is in ~/site."}
AT = "2026-10-02T01:00:00.000+00:00"


async def test_terminal_name_skips_open_rows_and_live_missions(store):
    await terminals.upsert(store, "r00001", "rss-feed", "celine", AT)
    assert await missions.terminal_name(store, "rss-feed") == "rss-feed-2"
    cid = await conversations.create(store, "RSS feed")
    await missions.create(store, cid, {**BRIEF, "terminal": "rss-feed-2"})
    assert await missions.terminal_name(store, "rss-feed") == "rss-feed-3"
    await terminals.upsert(store, "x00001", "sixteen-letters", "celine", AT)
    assert await missions.terminal_name(store, "sixteen-letters") == "sixteen-letter-2", \
        "the suffix never pushes the name past the daemon's 16 characters"


async def test_create_update_status(store):
    cid = await conversations.create(store, "RSS feed")
    await missions.create(store, cid, BRIEF)
    row = await missions.get(store, cid)
    assert {k: row[k] for k in BRIEF} == BRIEF and row["status"] == "active"
    report = await conversations.create(store, "Her site checks")
    await missions.update(store, cid, {"report_to": report})
    await store.write(lambda c: missions.set_status_sync(
        c, store, cid, "needs_you", "The feed needs a key."))
    row = await missions.get(store, cid)
    assert (row["plan"], row["steps"], row["step"]) == ("", [], None), "no plan yet"
    assert row["report_to"] == report
    assert row["brief"] == BRIEF["brief"], "the other fields stay"
    assert row["status"] == "needs_you" and row["note"] == "The feed needs a key."
    with pytest.raises(ValueError):
        await missions.update(store, cid, {"colour": "pink"})
    rows = {r["id"]: r for r in await conversations.list(store)}
    assert rows[cid]["mission_status"] == "needs_you"


async def _mission(store, name: str, status: str) -> str:
    cid = await conversations.create(store, title=name, kind="mission")
    await missions.create(store, cid, {"name": name, "terminal": name, "brief": "Go.", "status": status})
    return cid


async def test_the_counts_split_the_list_by_status_and_count_the_shelves_apart(store):
    for name, status in [("a", "active"), ("b", "active"), ("c", "needs_you"), ("d", "done"), ("e", "stopped")]:
        await _mission(store, name, status)
    archived = await _mission(store, "f", "done")
    trashed = await _mission(store, "g", "stopped")
    await store.write(lambda c: c.execute("UPDATE conversations SET archived_at='2026-10-01T00:00:00Z' WHERE id=?", (archived,)))
    await store.write(lambda c: c.execute("UPDATE conversations SET trashed_at='2026-10-01T00:00:00Z' WHERE id=?", (trashed,)))
    assert await missions.counts(store) == {"all": 5, "active": 2, "needs_you": 1, "done": 1, "stopped": 1,
                                            "archived": 1, "trashed": 1}
