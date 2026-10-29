"""chat_archive__scroll, chat_archive__get_messages, a search that stays inside its scope. A synthetic
archive: one WhatsApp chat of 60 messages, one short one; no real data."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from celine.skills import ToolContext
from celine.kb import KBs
from celine.turns.verify import MSG_REF
from kb_fixtures import archive_skill, build

pytestmark = pytest.mark.anyio

T0 = int(datetime(2022, 5, 1, 12, 0).timestamp() * 1000)
CTX = ToolContext("", "ta", "", None, None, None)  # type: ignore[arg-type]


@pytest.fixture
def skill(tmp_path, monkeypatch):
    from celine.skills import archive
    monkeypatch.setattr(archive, "_chat_counts", {})
    kbs = KBs(SimpleNamespace(data_dir=tmp_path, problems=[], kb={
        "whatsapp": {"kind": "chat", "title": "WhatsApp", "platform": "whatsapp"}}),
        lambda ev: None)
    s = kbs.get("whatsapp").sink()
    noor = s.person("491700000005@s.whatsapp.net", push_name="Noor")
    dm = s.chat("491700000005@s.whatsapp.net", person_key="491700000005@s.whatsapp.net")
    for i in range(60):                        # ids 1..60, one a minute
        s.message(dm, f"m{i}", ts=T0 + i * 60_000, text=f"line {i}", from_me=i % 2 == 1,
                  sender=None if i % 2 else noor)
    other = s.chat("123-456@g.us", kind="group", name="Chess club")
    s.message(other, "o1", ts=T0 + 300_000, text="other chat", sender=noor)   # id 61
    s.close()
    return archive_skill(monkeypatch, kbs)


def ids(out) -> list[int]:
    """Message ids shown as citable lines, in order."""
    return [int(m[2]) for m in MSG_REF.finditer(out.content)]


async def test_scroll_returns_the_same_messages_each_call_and_stays_in_the_chat(skill):
    out = await skill.scroll(CTX, ref="whatsapp:40", direction="before")
    assert ids(out) == list(range(20, 41))
    again = await skill.scroll(CTX, ref="whatsapp:40", direction="before")
    assert ids(again) == ids(out), "the same call shows the same messages"
    assert 'older: chat_archive__scroll(ref="whatsapp:20", direction="before")' in out.content
    assert ids(await skill.scroll(CTX, ref="whatsapp:20", direction="before")) == \
        list(range(1, 21))
    edge = await skill.scroll(CTX, ref="whatsapp:55", direction="after")
    assert ids(edge) == list(range(55, 61)) and "newest message reached" in edge.content
    for ref in ("whatsapp", "discord:4", "whatsapp:9999"):
        assert (await skill.scroll(CTX, ref=ref)).is_error


async def test_scroll_both_sides_with_a_count(skill):
    out = await skill.scroll(CTX, ref="whatsapp:40", direction="both", count=3)
    assert ids(out) == [37, 38, 39, 40, 41, 42, 43]
    assert out.display["messages"] == 6


async def test_get_fetches_several_refs_in_any_turn(skill):
    out = await skill.get_messages(ToolContext("", "another", "", None, None, None), refs=[
        "whatsapp:61", "[whatsapp:3]", "whatsapp:2", "whatsapp:3", "whatsapp:999", "nonsense"])
    assert ids(out) == [2, 3, 61]
    assert "whatsapp:999" in out.content and "nonsense" in out.content, "the misses are named"


SCOPE_CTX = ToolContext("", "ts", "", None, None, None)  # type: ignore[arg-type]


async def test_a_search_stays_inside_its_scope(tmp_path, monkeypatch):
    skill = archive_skill(monkeypatch, build(tmp_path))
    out = await skill.search(SCOPE_CTX, about=["chess"], words=["chess"], scope=["Discord"])
    assert "[discord:" in out.content and "[whatsapp:" not in out.content
    out = await skill.search(SCOPE_CTX, about=["chess"], words=["chess"],
                             scope=["whatsapp:2"])
    assert out.display["scope_chats"][0]["ref"] == "whatsapp:2"
    assert "[discord:" not in out.content
    out = await skill.search(SCOPE_CTX, about=["celine"], words=["celine"],
                             scope=["discord", "whatsapp:2"])
    assert out.is_error
