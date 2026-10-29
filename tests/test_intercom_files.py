"""intercom__send carries files by their short ids, and `at` reads a day with a time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from chat_fixtures import user_says

from celine import attachments
from celine.boot import load
from celine.conversations import store as conversations, tree, view
from celine.core.ids import iso, parse_when
from celine.core.services import Services
from celine.intercom import Intercom
from celine.intercom import store as rows
from celine.intercom.skill import IntercomSkill
from celine.skills import ToolContext

pytestmark = pytest.mark.anyio


def test_at_reads_a_day_with_a_time_in_the_app_zone():
    tz = ZoneInfo("Europe/Berlin")
    now_ = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    due = parse_when(None, "2026-11-02 09:00", tz, now_)
    assert due == datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc), "CET, UTC+1"
    assert parse_when(None, "2026-10-28 09:00", tz, now_) is None, "passed"
    assert parse_when(None, "2026-13-02 09:00", tz, now_) is None
    assert parse_when(None, "09:00", tz, now_) == datetime(2026, 10, 30, 8, 0,
                                                            tzinfo=timezone.utc)


async def _her_file(store, cid: str) -> str:
    """A voice note on her message in `cid`. -> its attachment id."""
    _, tid, _ = await user_says(store, cid, "say it")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    row = await attachments.add(store, "ab" * 32, 1200, "audio/mpeg", "voice-note.mp3",
                                "audio")
    await store.write(lambda c: c.execute("UPDATE attachments SET message_id=? WHERE id=?",
                                          (mid, row["id"])))
    return row["id"]


async def test_she_sends_a_file_of_hers_along(store):
    cfg = load()
    skill = IntercomSkill(cfg)
    skill.app = Services(intercom=Intercom(store))
    chat, other = await conversations.create(store), await conversations.create(store)
    aid = await _her_file(store, chat)
    ctx = ToolContext(chat, "t", "k", store, cfg, None)

    out = await skill.send(ctx, to="user", text="listen", attachments=[aid[:8]])
    assert not out.is_error, out.content
    msg = await store.read(lambda c: rows.view(c, out.display["id"]))
    [f] = msg["attachments"]
    assert f["id"] != aid and (f["filename"], f["kind"]) == ("voice-note.mp3", "audio")
    assert await attachments.gc(cfg, store, datetime.now(timezone.utc) +
                                timedelta(days=60)) == 0
    assert await attachments.get(store, f["id"]), "a message to the user keeps its file"

    out = await skill.send(ctx, to=other, text="for you", attachments=[aid[:8]])
    assert not out.is_error
    [m] = (await view.snapshot(store, other))["messages"]
    assert [a["filename"] for a in m["attachments"]] == ["voice-note.mp3"]

    bad = await skill.send(ctx, to="user", text="x", attachments=["nothing!"])
    assert bad.is_error and "short id" in bad.content
    elsewhere = await conversations.create(store)
    mine = await _her_file(store, elsewhere)
    await store.write(lambda c: c.execute(
        "UPDATE messages SET role='user' WHERE id=(SELECT message_id FROM attachments "
        "WHERE id=?)", (mine,)))
    refused = await skill.send(ctx, to="user", text="x", attachments=[mine[:8]])
    assert refused.is_error and refused.content.startswith("There is no file")


async def test_a_message_lands_on_a_day(store):
    cfg = load()
    skill = IntercomSkill(cfg)
    skill.app = Services(intercom=Intercom(store))
    cid = await conversations.create(store)
    ctx = ToolContext(cid, "t", "k", store, cfg, None)
    day = (datetime.now(skill.tz) + timedelta(days=3)).replace(hour=9, minute=0, second=0,
                                                              microsecond=0)
    out = await skill.send(ctx, to="me", text="the visa", at=day.strftime("%Y-%m-%d %H:%M"))
    assert not out.is_error and out.display["due_at"] == iso(day)
    assert (await skill.send(ctx, to="me", text="x", at="2001-01-01 09:00")).is_error
