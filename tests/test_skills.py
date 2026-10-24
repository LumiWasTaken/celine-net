"""The skill layer: what is declared mutating, the tools of a conversation's mode, tools
that are off or down shown as stubs and refused, and skills__read_guide."""
from __future__ import annotations

from chat_fixtures import user_says

import json

import pytest

from celine.conversations import store as conversations
from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.boot import load
from celine.core.bus import EventBus
from celine.core.services import Services
from celine.skills import Registry, ToolContext, ToolResult, build, guide, tool
from celine.skills.gating import set_override
from celine.skills.meta import Skills
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("CELINE_SHELL_URL", "http://shellbox:8770")   # the shell is up
    c = load()
    c.skills = {n: {**s, "enabled": True} for n, s in c.skills.items() if isinstance(s, dict)}
    return c


def test_mutating_tools_are_declared(cfg):
    """Mutating calls never run side by side, and read-access sub-agents get none."""
    reg = build(Services(cfg=cfg))
    writes = {n for n, s in reg.tools.items() if s.mutating}
    assert {"shell__type", "notebook__write", "notebook__delete",
            "chat_archive__mark_same_person"} <= writes
    assert not {"chat_archive__search", "shell__read", "skills__read_guide"} & writes


class Recorder(FakeBackend):
    """Replies in order and records the tools each request offered."""

    def __init__(self, replies):
        super().__init__()
        self.replies = replies
        self.offered: list[dict[str, dict]] = []

    async def stream(self, messages, tools):
        self.calls.append(messages)
        self.offered.append({t["function"]["name"]: t["function"] for t in tools or []})
        for ev in self.replies[min(len(self.calls), len(self.replies)) - 1]:
            yield ev


class Shellish:
    name = "shell"
    description = "Runs commands."
    status = "up"

    @tool(read_only=False)
    async def type(self, ctx, text: str) -> ToolResult:
        """Type it into {{username}}'s terminal. Then read."""
        return ToolResult(f"typed {text}")

    @tool()
    async def read(self, ctx) -> ToolResult:
        """Read the terminal."""
        return ToolResult("screen")


class Painter:
    name = "image_generator"
    description = "Makes images."
    guide = guide("image_generator")      # its words: "picture", ...

    @tool()
    async def generate_image(self, ctx, prompt: str) -> ToolResult:
        """Make an image."""
        return ToolResult("made")


def _call(name, args=None):
    return [ToolCallDelta(index=0, id="c0", name=name, arguments=json.dumps(args or {})),
            Done(reason="tool_calls")]


async def _turn(store, cfg, reg, cid, replies) -> tuple[Recorder, list[dict]]:
    b = Recorder(replies)
    await user_says(store, cid, "go")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, b, EventBus(), Token(turn["id"]), reg)
    results = await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT tc.name, tc.state, m.content FROM tool_calls tc JOIN messages m ON "
        "m.id = tc.result_message_id ORDER BY tc.rowid")])
    return b, results


def _registry(cfg):
    reg = Registry(cfg)
    for s in (Shellish(), Painter()):
        reg.add(s)
    reg.add(Skills(reg, cfg))
    return reg


async def test_tools_load_by_need_and_a_load_counts_from_the_next_step(store, cfg):
    """A chat starts with the core skills; a picture in the message loads the images; a
    call to a skill that is not loaded is refused with the way to load it; skills__load
    puts its tools in the next step's list. Descriptions are filled in once."""
    from celine.skills import CORE
    reg = _registry(cfg)
    chat = await conversations.create(store)
    b, _ = await _turn(store, cfg, reg, chat, [[TextDelta("hi"), Done(reason="stop")]])
    assert set(b.offered[0]) == {"skills__read_guide", "skills__load"}
    assert "shell" not in CORE

    await user_says(store, chat, "draw me a picture of the sea")
    b, results = await _turn(store, cfg, reg, chat, [
        _call("shell__type", {"text": "ls"}),
        _call("skills__load", {"name": "shell"}),
        [TextDelta("ok"), Done(reason="stop")]])
    assert "image_generator__generate_image" in b.offered[0]
    assert "shell__type" not in b.offered[1] and "shell__type" in b.offered[2]
    assert "User's terminal" in b.offered[2]["shell__type"]["description"]
    assert results[-2] == {"name": "shell__type", "state": "gated",
                           "content": "shell is not loaded. skills__load(name=\"shell\") "
                                      "loads it for your next step."}
    assert results[-1]["content"] == ("Loaded shell. Its tools are in your list from "
                                      "your next step.")


async def test_a_skill_a_tool_touched_loads_from_the_next_step(store, cfg, monkeypatch):
    """A tool that touches a skill (the browser at a password field touches the vault):
    its tools from its next step. A skill that stays out (every tool off) leaves its rows
    at that rebuild, so the steps after it do not rebuild the request again."""
    from celine.turns import loads

    class Toucher:
        name, description = "toucher", "Touches."

        @tool()
        async def touch(self, ctx, name: str) -> ToolResult:
            """Touch a skill."""
            await loads.touch(ctx.store, ctx.conversation_id, "skill", [name])
            return ToolResult("touched")

    reg = _registry(cfg)
    reg.add(Toucher())
    cfg.settings.tools = {**cfg.settings.tools, "image_generator__generate_image": "off"}
    cid = await conversations.create(store)
    await loads.touch(store, cid, "skill", ["toucher"])
    steps = []
    select = loads.select

    async def counted(*a, **kw):
        steps.append(kw.get("when"))
        return await select(*a, **kw)
    monkeypatch.setattr(loads, "select", counted)
    b, _ = await _turn(store, cfg, reg, cid, [
        _call("toucher__touch", {"name": "shell"}),
        _call("toucher__touch", {"name": "image_generator"}),
        _call("toucher__touch", {"name": "toucher"}),
        _call("toucher__touch", {"name": "toucher"}),
        [TextDelta("ok"), Done(reason="stop")]])
    assert "shell__type" not in b.offered[0] and "shell__type" in b.offered[1]
    assert "image_generator__generate_image" not in b.offered[2]
    assert steps.count("step") == 2


async def test_a_tool_that_is_off_or_down_is_not_sent_and_refused(store, cfg):
    from celine.turns import loads
    reg = _registry(cfg)
    cid = await conversations.create(store)
    await loads.touch(store, cid, "skill", ["shell"])
    await set_override(store, cid, "shell__type", False, 1)
    b, results = await _turn(store, cfg, reg, cid, [
        _call("shell__type", {"text": "ls"}), [TextDelta("ok"), Done(reason="stop")]])
    assert "shell__type" not in b.offered[0]
    assert b.offered[0]["shell__read"]["description"] == "Read the terminal."
    assert results[-1] == {"name": "shell__type", "state": "gated",
                           "content": "shell__type is off here."}

    # the whole skill off in Settings: none of it is sent, the call says the skill
    await set_override(store, cid, "shell__type", True, 2)
    cfg.settings.tools = {**cfg.settings.tools, "shell__read": "off", "shell__type": "off"}
    b, results = await _turn(store, cfg, reg, cid, [
        _call("shell__read"), [TextDelta("ok"), Done(reason="stop")]])
    assert "shell__read" not in b.offered[0]
    # a skill that is down: test_turn_loop


async def test_read_guide_shows_the_guide_and_her_notes_loaded_or_not(store, cfg):
    from celine.turns import loads
    reg = build(Services(cfg=cfg))
    meta = reg.skills["skills"]
    cid = await conversations.create(store)
    ctx = ToolContext(conversation_id=cid, turn_id="t", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    (cfg.her.notebook / "skills").mkdir(parents=True)
    (cfg.her.notebook / "skills" / "web.md").write_text("searx is slow at night")
    out = await meta.read_guide(ctx, name="web")
    assert out.content.startswith("# web\n\n") and "searx is slow at night" in out.content
    assert reg.skills["web"].guide.body[:40] in out.content
    mission = await conversations.create(store, kind="mission")
    out = await meta.read_guide(ToolContext(conversation_id=mission, turn_id="t",
                                            tool_call_id="x", store=store, config=cfg,
                                            emit=None), name="appearance")
    assert out.is_error and out.content.startswith("No skill called 'appearance' here")
    carried = await loads.select(store, cfg, reg, cid, None)
    assert "[notes]" in reg.index(cfg, carried).split("- web")[1].split("\n")[0]
    out = await meta.read_guide(ctx, name="skills")
    assert not out.is_error and out.content == reg.index(cfg, carried)


async def test_read_guide_brings_the_live_presets_and_the_guide_names_none(store, cfg,
                                                                          monkeypatch):
    """The image guide promises each preset's prompt rules: read_guide brings the
    skill's docs, the presets chosen in Settings as they are now, after the guide. The
    guide itself names no preset: the user picks and drops them."""
    from celine.comfyui import presets as presets_mod
    from celine.skills import images
    shipped = presets_mod.of("images")
    live = shipped["krea2-turbo"]
    monkeypatch.setattr(images, "role_presets", lambda cfg, role: [live])
    reg = build(Services(cfg=cfg))
    cid = await conversations.create(store)
    ctx = ToolContext(conversation_id=cid, turn_id="t", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    out = await reg.skills["skills"].read_guide(ctx, name="image_generator")
    assert not out.is_error and presets_mod.docs(live, True) in out.content
    assert "### `anima-turbo`" not in out.content
    body = reg.skills["image_generator"].guide.body
    assert [p for p in shipped if p in body] == []


def test_a_source_without_its_account_is_not_connected():
    from types import SimpleNamespace as NS

    from celine.skills.catalog import status_of
    conn = NS(state="needs_login")
    connector = NS(connection=lambda: {"state": conn.state})
    services = NS(cfg=NS(mcp={"whatsapp": NS(kb="wa")}),
                 connectors=NS(get={"wa": connector}.get))
    up = NS(status="up")
    assert status_of(services, "whatsapp", up) == "unconnected"
    conn.state = "connected"
    assert status_of(services, "whatsapp", up) == "up"
    assert status_of(services, "whatsapp", NS(status="dead")) == "dead"
    assert status_of(services, "shell", up) == "up"
