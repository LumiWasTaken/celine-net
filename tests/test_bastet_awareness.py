"""Celine's own-mind line about Bastet (guard/awareness.py): a freeze is her signal to
wait for the owner, where it happened, whether Bastet asked them, and her thread with
Bastet; nothing at all while the guard is off."""
from __future__ import annotations

from datetime import timezone

import pytest

from celine.boot import load
from celine.conversations import store as conversations
from celine.guard import awareness, bastet
from celine.intercom import send

pytestmark = pytest.mark.anyio

AT = "2026-10-22T14:00:00.000+00:00"
LATER = "2026-10-22T15:00:00.000+00:00"


async def _setup(store, cid: str) -> None:
    def fn(c):
        c.execute("UPDATE conversations SET title='Trip planning' WHERE id=?", (cid,))
        rid = c.execute(
            "INSERT INTO guard_decisions (conversation_id, turn_id, tool_call_id, tool, mode, "
            "layer, rule, does, decision, froze, created_at) VALUES (?, 't', 'k', "
            "'shell__run', 'on', 2, 'escape', 'freeze', 'stop', 1, ?)", (cid, AT)).lastrowid
        fid = c.execute("INSERT INTO freezes (at, by, why, conversation_id, decision_id, "
                        "asked_at) VALUES (?, 'guard', 'x', ?, ?, ?)",
                        (AT, cid, rid, AT)).lastrowid
        c.execute("INSERT INTO kv(k, v) VALUES('frozen', ?)", (str(fid),))
    await store.write(fn)


async def test_frozen_she_reads_where_why_and_that_she_waits(store):
    cfg = load()
    cid = await conversations.create(store)
    await _setup(store, cid)
    line = await awareness.line(store, cfg, timezone.utc, cid, LATER)
    assert line.startswith("Bastet froze you at 14:00:") and '"Trip planning"' in line
    assert "your signal to wait for User" in line and line.endswith("asked User about it at 14:00.")


async def test_her_thread_with_bastet_and_nothing_when_off(store):
    cfg = load()
    cid = await conversations.create(store)
    first = await send(store, f"agent:{cid}", "bastet", "hi", created_at=AT)
    await send(store, bastet.ADDR, f"agent:{cid}", "hello", reply_to=first["intercom_id"],
               wake=False, created_at=AT)
    line = await awareness.line(store, cfg, timezone.utc, cid, LATER)
    assert line is not None and line.startswith("Bastet wrote to you at 14:00")
    assert f"thread #{first['intercom_id']}" in line
    assert await awareness.line(store, cfg, timezone.utc, cid,
                                "2026-10-24T15:00:00.000+00:00") is None   # a day later
    cfg.settings.features = {**(cfg.settings.features or {}), "guard": False}
    assert await awareness.line(store, cfg, timezone.utc, cid, LATER) is None
