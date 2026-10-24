"""The agent's self: me__read and me__edit
(skills/me.py), its talk and voice in History with a real revert (her.py,
api/texts.py), the `her` source of Events, the voice cue from its talk (prompt.voice_line), the slop phrases of
validation talk and owning up (config/her/voice.toml), and the one marking
style the legend names (turns/legend.py)."""
from __future__ import annotations

import tomllib

import httpx
import pytest
from fastapi import FastAPI

from celine import events, her, texts
from celine.api import texts as texts_api
from celine.boot import load
from celine.core import ids
from celine.core.services import Services
from celine.errors import install_error_handler
from celine.settings.files import Invalid
from celine.skills import ToolContext
from celine.skills.me import Me
from celine.turns import slop
from celine.turns.prompt import VOICE_MAX, voice_line

pytestmark = pytest.mark.anyio
T0 = "2026-10-22T18:00:00.000+00:00"


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(ids, "now", lambda: T0)


@pytest.fixture
async def cfg(store, clock):
    cfg = load()
    cfg.user = "Joe"
    return cfg


def _ctx(store, cfg):
    return ToolContext(conversation_id="c1", turn_id="t1", tool_call_id="x", store=store,
                       config=cfg, emit=None)  # type: ignore[arg-type]


async def _versions(store, of: str) -> list[tuple[str, str]]:
    return await store.read(lambda c: [(r[0], r[1]) for r in c.execute(
        "SELECT by, text FROM text_versions WHERE of=? ORDER BY id", (of,))])


# ---- the slop list -----------------------------------------------------------------------

MORNING = [
    "You caught me and I'm not gonna lie about it. Let me check what actually ran overnight "
    "before I answer, so I'm not wrong about being wrong.",
    "Confirmed, and yeah. That's all I did... That's a real miss and I'm not gonna dress "
    "it up.",
    "That's the best mission you could've given me. Let me get to work.",
    "You did the backup first and my call was right. You were right to ask.",
    "I apologize, my apologies, turns out you were right.",
]


def test_validation_talk_and_owning_up_are_slop_and_her_own_lines_are_not():
    voice = tomllib.loads((her.BASE / "voice.toml").read_text())
    for line in MORNING:
        assert len(slop.find(line, voice)) >= 2, line
    for line in ("ugh. yeah.", "you did good", "i was right there when it broke"):
        assert slop.find(line, voice) == [], line


# ---- its voice as text, and History ------------------------------------------------------

def test_her_voice_as_text_reads_back_the_same():
    v = {"banned": [" delve", "Moreover"], "bias": -12.5, "on": False,
         "phrases": ["you caught me"], "patterns": [r"\bheh\b"]}
    text = her.voice_text(v)
    assert text.splitlines()[:3] == ["on: false", "bias: -12.5", "banned:  delve"]
    assert her.voice_from_text(text) == v
    with pytest.raises(Invalid):
        her.voice_from_text("on: true\nwords: delve")


# ---- me__read, me__edit ------------------------------------------------------------------

async def test_she_reads_herself_and_edits_her_talk_and_slop_list(store, cfg):
    me = Me(cfg)
    seen = []
    store.emit_global = seen.append
    read = (await me.read(_ctx(store, cfg))).content
    assert "## persona" in read and "## talk" in read and "## slop list" in read
    assert "Joe" in read and "{{" not in read
    got = await me.edit(_ctx(store, cfg), what="phrases", add=["my call was right"])
    assert not got.is_error and "1 added" in got.content
    assert "my call was right" in cfg.her.voice["phrases"], "cfg follows the row"
    got = await me.edit(_ctx(store, cfg), what="talk", old="write to Joe,",
                        new="write to Joe, plainly,")
    assert not got.is_error, got.content
    assert "{{username}}, plainly," in cfg.her.talk, "kept with the placeholder"
    got = await me.edit(_ctx(store, cfg), what="talk", new="## how I sound\nshort.")
    assert cfg.her.talk.rstrip().endswith("## how I sound\nshort.")
    got = await me.edit(_ctx(store, cfg), what="persona", new="I keep a balcony garden.")
    assert not got.is_error and got.content == "Added to your persona. Tell Joe in one line."
    assert cfg.her.persona.rstrip().endswith("I keep a balcony garden.")
    assert [b for b, _ in await _versions(store, "her:persona")] == ["", "her"]
    assert voice_line(cfg) == "[how you sound] short."
    assert {"type": "her.changed"} in seen
    talk = await _versions(store, "her:talk")
    assert [b for b, _ in talk] == ["", "her", "her"]
    rows = await store.read(lambda c: events.listing(c, cfg, kinds=["her"]))
    assert [e["title"] for e in rows["events"]] == [
        "Changed the Persona", "Changed the Example lines", "Changed the Example lines",
        "Changed the Banned words"]
    assert rows["events"][0]["links"]["open"] == "/settings/agent/edit/persona/history"


async def test_a_wrong_edit_says_why_and_writes_nothing(store, cfg):
    me = Me(cfg)
    for kw, why in (({"what": "talk", "old": "nowhere", "new": "x"}, "0 times"),
                    ({"what": "talk", "add": ["x"]}, "old and new"),
                    ({"what": "phrases", "remove": ["not there"]}, "Not in your phrases"),
                    ({"what": "patterns", "add": ["(unclosed"]}, "does not read"),
                    ({"what": "banned", "add": [" "]}, "one line")):
        got = await me.edit(_ctx(store, cfg), **kw)
        assert got.is_error and why in got.content, (kw, got.content)
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM text_versions").fetchone()[0]) == 0


# ---- revert ------------------------------------------------------------------------------

@pytest.fixture
async def client(store, cfg):
    app = FastAPI()
    app.state.services = s = Services()
    install_error_handler(app)
    app.include_router(texts_api.router)
    s.store, s.cfg = store, cfg
    store.emit_global = lambda ev: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        yield c


async def test_revert_makes_an_old_version_hers_again_as_a_new_one(store, cfg, client):
    first = cfg.her.voice
    await her.write(store, cfg, {"voice": {**first, "banned": [" delve", " ozone"]}}, "her")
    vid = await store.read(lambda c: c.execute(
        "SELECT min(id) FROM text_versions WHERE of='her:voice'").fetchone()[0])
    r = await client.post(f"/api/text-versions/{vid}/revert")
    assert r.status_code == 200, r.text
    assert cfg.her.voice == first
    assert [b for b, _ in await _versions(store, "her:voice")] == ["", "her", "user"]
    await store.write(lambda c: texts.record(c, "routine:x:prompt", "a", "user"))
    other = await store.read(lambda c: c.execute(
        "SELECT id FROM text_versions WHERE of='routine:x:prompt'").fetchone()[0])
    assert (await client.post(f"/api/text-versions/{other}/revert")).status_code == 400
    assert (await client.post("/api/text-versions/999999/revert")).status_code == 404


async def test_the_cue_is_cut_at_a_sentence_or_a_word(cfg):
    first = "short. lowercase most of the time. " + "x" * 120 + "."
    cfg.her.talk = f"intro\n\n## shape\n{first} trailing off with dots when done arguing, and more after it.\n"
    assert voice_line(cfg) == f"[how you sound] {first}"
    cfg.her.talk = "## shape\n" + "word " * 80
    got = voice_line(cfg)
    assert got.endswith("word ...") and len(got) <= len("[how you sound] ") + VOICE_MAX + 4
    cfg.her.talk = "no heading at all"
    assert voice_line(cfg) is None


# ---- the legend --------------------------------------------------------------------------

async def test_the_legend_names_the_marks_the_code_writes(cfg):
    from zoneinfo import ZoneInfo

    from celine.intercom.render import header
    from celine.skills import recall
    from celine.skills import mind
    from celine.skills.notebook import NOTES_HEAD
    from celine.turns import compact, legend
    from celine.turns.context import time_line
    text = legend.legend(cfg, guarded=False, voiced=True)
    stamp = time_line({"created_at": T0}, ZoneInfo("UTC")).strip()
    assert stamp == "[Thu 22 Oct 18:00]"
    iv = {"kind": "report", "from": "agent:x", "from_name": "scout", "id": 4, "data": {}}
    for mark in (mind.HEAD, recall.HEAD, NOTES_HEAD, compact.STUB_HEAD, "[cut: ", "[the screen from ",
                 "[how you sound]", "[Report from "):
        assert mark.rstrip() in text, mark
    assert header(iv, "Joe", None) == "[Report from scout (#4)]\n"
    assert "<outside>" not in text and "<outside>" in legend.legend(cfg, guarded=True, voiced=True)
    assert "[how you sound]" not in legend.legend(cfg, guarded=False, voiced=False)
    assert "Joe" in text and "—" not in text
