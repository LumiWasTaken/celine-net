"""chat_archive__scroll (de-duplicated against what it has in view, compaction aware),
chat_archive__get_messages, a search that stays inside its scope. A synthetic
archive: one WhatsApp chat of 60 messages, one short one; no real data."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from celine.skills import ToolContext
from celine.kb import KBs
from celine.turns import compact, ledger
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


@pytest.fixture(autouse=True)
def led(skill):
    ledger.open_for("ta")
    yield ledger.get("ta")
    ledger.close("ta")


def ids(out) -> list[int]:
    """Message ids shown as citable lines, in order."""
    return [int(m[2]) for m in ledger.MSG_REF.finditer(out.content)]


async def test_scroll_goes_further_each_call_and_stays_in_the_chat(skill):
    out = await skill.scroll(CTX, ref="whatsapp:40", direction="before")
    assert ids(out) == list(range(20, 41))
    out = await skill.scroll(CTX, ref="whatsapp:40", direction="before")
    assert ids(out) == list(range(1, 20)), "what it already has is skipped"
    assert ids(await skill.scroll(CTX, ref="whatsapp:40", direction="before")) == []
    assert ids(await skill.scroll(CTX, ref="whatsapp:55", direction="after")) == \
        list(range(55, 61))
    for ref in ("whatsapp", "discord:4", "whatsapp:9999"):
        assert (await skill.scroll(CTX, ref=ref)).is_error


async def test_compacted_results_count_as_not_shown(skill, led):
    first = await skill.scroll(CTX, ref="whatsapp:40", direction="before", count=5)
    assert ids(first) == [35, 36, 37, 38, 39, 40]
    # the runner rebuilt the context and that result is now a stub
    stub = compact.stub("chat_archive__scroll", '{"ref": "whatsapp:40"}', first.content)
    led.set_view([{"role": "user", "content": "hi"}, {"role": "tool", "content": stub}])
    again = await skill.scroll(CTX, ref="whatsapp:40", direction="before", count=5)
    assert ids(again) == [35, 36, 37, 38, 39, 40]
    led.set_view([{"role": "tool", "content": again.content}])
    third = await skill.scroll(CTX, ref="whatsapp:40", direction="before", count=5)
    assert ids(third) == [30, 31, 32, 33, 34]


async def test_get_fetches_several_refs_in_any_turn(skill):
    out = await skill.get_messages(ToolContext("", "another", "", None, None, None), refs=[
        "whatsapp:61", "[whatsapp:3]", "whatsapp:2", "whatsapp:3", "whatsapp:999", "nonsense"])
    assert ids(out) == [2, 3, 61]
    assert "whatsapp:999" in out.content and "nonsense" in out.content, "the misses are named"


SCOPE_CTX = ToolContext("", "ts", "", None, None, None)  # type: ignore[arg-type]


async def test_a_search_stays_inside_its_scope(tmp_path, monkeypatch):
    skill = archive_skill(monkeypatch, build(tmp_path))
    ledger.open_for("ts")
    try:
        out = await skill.search(SCOPE_CTX, about=["chess"], words=["chess"],
                                 scope=["Discord"])
        assert "[discord:" in out.content and "[whatsapp:" not in out.content
        out = await skill.search(SCOPE_CTX, about=["chess"], words=["chess"],
                                 scope=["whatsapp:2"])
        assert out.display["scope_chats"][0]["ref"] == "whatsapp:2"
        assert "[discord:" not in out.content
        spent = ledger.get("ts").searches
        out = await skill.search(SCOPE_CTX, about=["celine"], words=["celine"],
                                 scope=["discord", "whatsapp:2"])
        assert out.is_error and ledger.get("ts").searches == spent, "refused, not spent"
    finally:
        ledger.close("ts")
