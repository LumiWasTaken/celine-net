"""The agent's self: me__read and me__edit
(skills/me.py), its talk and voice in History with a real revert (her.py,
api/texts.py), the `her` source of Events, the slop phrases of
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
from celine.skills.me import Me, replaced
from celine.turns import slop

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


def _ctx(store, cfg, call="x"):
    return ToolContext(conversation_id="c1", turn_id="t1", tool_call_id=call, store=store,
                       config=cfg, emit=None)  # type: ignore[arg-type]


class Yes:
    """The approvals of a user who says yes to everything, every request kept."""

    def __init__(self):
        self.asked: list[tuple] = []

    async def ask(self, action, facts, conversation, why="", short="", kind="vault",
                  ref=None, call=None):
        self.asked.append((kind, action, facts, call))


def _me(cfg, approvals=None):
    me = Me(cfg)
    me.app = Services(cfg=cfg, approvals=Yes() if approvals is None else approvals)
    return me


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
    me = _me(cfg)
    seen = []
    store.emit_global = seen.append
    read = (await me.read(_ctx(store, cfg))).content
    assert "## persona" in read and "## talk" in read and "## slop list" in read
    assert "Joe" in read and "{{" not in read
    got = await me.edit(_ctx(store, cfg), what="phrases", add=["my call was right"])
    assert not got.is_error and "1 added" in got.content
    assert "my call was right" in cfg.her.voice["phrases"], "cfg follows the row"
    placeholders = cfg.her.talk_text.count("{{username}}")
    got = await me.edit(_ctx(store, cfg), what="talk", old="write to Joe,",
                        new="write to Joe, plainly,")
    assert not got.is_error, got.content
    assert "write to Joe, plainly," in cfg.her.talk, "stored as it wrote it"
    assert cfg.her.talk.count("{{username}}") == placeholders - 1, \
        "only the edited one is gone; the rest stay placeholders"
    got = await me.edit(_ctx(store, cfg), what="talk", new="## how I sound\nshort.")
    assert cfg.her.talk.rstrip().endswith("## how I sound\nshort.")
    got = await me.edit(_ctx(store, cfg), what="persona", new="I keep a balcony garden.")
    assert not got.is_error and got.content == "Added to your persona. Tell Joe in one line."
    assert cfg.her.persona.rstrip().endswith("I keep a balcony garden.")
    assert [b for b, _ in await _versions(store, "her:persona")] == ["", "her"]
    assert {"type": "her.changed"} in seen
    talk = await _versions(store, "her:talk")
    assert [b for b, _ in talk] == ["", "her", "her"]
    rows = await store.read(lambda c: events.listing(c, cfg, kinds=["her"]))
    assert [e["title"] for e in rows["events"]] == [
        "Changed the Persona", "Changed the Example lines", "Changed the Example lines",
        "Changed the Banned words"]
    assert rows["events"][0]["links"]["open"] == "/settings/agent/edit/persona/history"


async def test_an_edit_keeps_every_placeholder_it_does_not_touch(cfg):
    kept = "Hi {{username}}. Bye {{username}}."
    assert replaced(cfg, kept, "Bye", "Ciao") == "Hi {{username}}. Ciao {{username}}."
    assert replaced(cfg, kept, "Hi Joe.", "Hey Joe!") == "Hey Joe! Bye {{username}}."
    assert replaced(cfg, kept, "Bye J", "See y") == "Hi {{username}}. See yoe.", \
        "a placeholder cut by the edit keeps its outside part as it reads"


async def test_a_wrong_edit_says_why_and_writes_nothing(store, cfg):
    me = _me(cfg)
    for kw, why in (({"what": "talk", "old": "nowhere", "new": "x"}, "0 times"),
                    ({"what": "talk", "add": ["x"]}, "old and new"),
                    ({"what": "phrases", "remove": ["not there"]}, "Not in your phrases"),
                    ({"what": "patterns", "add": ["(unclosed"]}, "does not read"),
                    ({"what": "banned", "add": [" "]}, "one line")):
        got = await me.edit(_ctx(store, cfg), **kw)
        assert got.is_error and why in got.content, (kw, got.content)
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM text_versions").fetchone()[0]) == 0


async def test_every_edit_of_herself_waits_for_the_owner_guard_or_not(store, cfg,
                                                                      monkeypatch):
    """me__edit asks the owner (celine/approvals.py) before it writes, whatever the guard
    is; denied, unanswered or with nobody to ask, nothing changes. A call the owner
    already approved on Bastet's ask is not asked again."""
    from celine import approvals as ap
    from celine.guard.owner import _SEAL, Owner
    from chat_fixtures import until
    import asyncio
    yes = Yes()
    await _me(cfg, yes).edit(_ctx(store, cfg), what="persona", old="", new="I garden.")
    [(kind, action, facts, call)] = yes.asked
    assert (kind, action, facts, call) == ("her", "change the Persona", ["new: I garden."],
                                           "x")
    before = cfg.her.persona
    monkeypatch.setattr(ap, "ASK_FOR", 5)
    a = ap.Approvals(cfg)

    async def answer(ok, kind="her"):
        await until(lambda: a.waiting)
        [ask] = a.view()
        assert ask["kind"] == kind and (kind != "her" or "every chat" in ask["why"])
        assert a.decide(ask["id"], ok, Owner("test", _SEAL))
    got, _ = await asyncio.gather(
        _me(cfg, a).edit(_ctx(store, cfg, "k1"), what="talk", new="short."), answer(False))
    assert got.is_error and got.content == "Joe denied it; nothing was done."
    assert cfg.her.persona == before and "short." not in cfg.her.talk_text
    monkeypatch.setattr(ap, "ASK_FOR", 0)
    got = await _me(cfg, a).edit(_ctx(store, cfg, "k2"), what="phrases", add=["x y"])
    assert got.is_error and got.content.startswith("Joe did not answer")
    got = await Me(cfg).edit(_ctx(store, cfg, "k3"), what="phrases", add=["x y"])
    assert got.is_error and "nobody" in got.content
    assert "x y" not in cfg.her.voice["phrases"]
    # Bastet's confirm ask for this very call was approved: it is not asked again
    monkeypatch.setattr(ap, "ASK_FOR", 5)
    gate, _ = await asyncio.gather(a.ask("run me__edit", [], "c1", kind="gate", call="k4"),
                                   answer(True, "gate"))
    assert gate is None
    got = await _me(cfg, a).edit(_ctx(store, cfg, "k4"), what="phrases", add=["x y"])
    assert not got.is_error and "x y" in cfg.her.voice["phrases"] and not a.waiting


def test_she_knows_an_edit_of_herself_waits_for_the_owner():
    assert "approve" in Me.edit.__doc__ and "approve" in Me.guide.body


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


# ---- the legend --------------------------------------------------------------------------

async def test_the_legend_names_the_marks_the_code_writes(cfg):
    from zoneinfo import ZoneInfo

    from celine.intercom.render import header
    from celine.skills import recall
    from celine.skills import mind
    from celine.skills.notebook import NOTES_HEAD
    from celine.turns import compact, legend
    from celine.turns.context import time_line
    text = legend.legend(cfg, guarded=False)
    stamp = time_line({"created_at": T0}, ZoneInfo("UTC")).strip()
    assert stamp == "[Thu 22 Oct 18:00]"
    iv = {"kind": "report", "from": "agent:x", "from_name": "scout", "id": 4, "data": {}}
    for mark in (mind.HEAD, recall.HEAD, NOTES_HEAD, compact.STUB_HEAD, "[cut: ", "[the screen from ",
                 "[Report from "):
        assert mark.rstrip() in text, mark
    assert header(iv, "Joe", None) == "[Report from scout (#4)]\n"
    assert "<outside>" not in text and "<outside>" in legend.legend(cfg, guarded=True)
    assert "Joe" in text and "—" not in text


# ---- me__whoami ----------------------------------------------------------------------------

async def test_whoami_says_the_fallback_with_its_reason_and_a_sub_agent_its_parent(
        tmp_path, monkeypatch, store, clock):
    """Every line is read when it is called: the turn's running event and the router
    for the model, the probes for the devices, the conversation row for where it is."""
    import httpx
    from chat_fixtures import user_says
    from settings_fixtures import scratch

    from celine.backends.llm import LLM
    from celine.backends.router import Router
    from celine.conversations import store as conversations
    from celine.core.event_types import LLM_QUEUE
    from celine.settings import features

    scratch(tmp_path, monkeypatch, roles={"chat": {"chain": ["main/big", "lan/small"]}})
    cfg = load()
    cfg.user = "Joe"

    def reply(request):
        if request.url.host == "127.0.0.1":           # main is down, lan answers
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"running": [], "models": []})
    llm = LLM(cfg, router=Router(cfg, None, transport=httpx.MockTransport(reply)))
    probes = {"shellbox": {"state": "missing",
                           "detail": "The shellbox at box:8700 did not answer."},
              "web": {"state": "ready", "detail": "SearXNG answers."}}

    async def probe_all(cfg, services, *, cache=True):
        return {f.id: probes.get(f.id, {"state": "none", "detail": ""})
                for f in features.catalog()}
    monkeypatch.setattr(features, "probe_all", probe_all)
    me = Me(cfg)
    me.app = Services(cfg=cfg, store=store, llm=llm)

    def ctx(cid, tid):
        return ToolContext(conversation_id=cid, turn_id=tid, tool_call_id="x", store=store,
                           config=cfg, emit=None)  # type: ignore[arg-type]

    parent = await conversations.create(store, "plans")
    _, tid, _ = await user_says(store, parent, "hi")
    await store.append_event(parent, LLM_QUEUE, {
        "turn_id": tid, "state": "running", "role": "chat", "backend": "lan/small",
        "model": "small", "endpoint": "gpu-box", "fallback": True, "instead_of": "big",
        "why": "llama-swap does not answer."})
    out = (await me.whoami(ctx(parent, tid))).content
    assert "Model: lan/small for chat, a fallback. main/big: llama-swap does not answer." in out
    assert 'You are working in the chat "plans"' in out and "This turn: Joe's message." in out
    assert "Shellbox: The shellbox at box:8700 did not answer." in out
    assert "Web search: SearXNG answers." in out and "Guard: off" in out
    assert "[image]" not in out and "—" not in out

    sub = await conversations.create(store, "s", agent_of=parent, agent_name="scout")
    _, stid, _ = await user_says(store, sub, "go")
    out = (await me.whoami(ctx(sub, stid))).content
    assert "You are working in the sub-agent scout" in out
    assert 'It reports to the chat "plans"' in out
