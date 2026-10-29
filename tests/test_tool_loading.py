"""Loads append (skills/meta.py, turns/prompt.py wire_tools): every tool offered here is
in the list, a core skill's with its parameters, every other by name with open
parameters; skills__load answers with the parameters and the skill's part of the prompt
as its result, and the system prompt and the tools never change for it."""
from __future__ import annotations

import json

import pytest

from notes_fixtures import notes_cfg
from test_turn_loop import Replies, _calls, _results, _run

from celine.backends.anthropic import AnthropicBackend
from celine.backends.base import Done, TextDelta
from celine.backends.ollama import OllamaBackend
from celine.backends.openai import build
from celine.boot import load
from celine.config import BackendConfig
from celine.conversations import store as conversations
from celine.skills import Registry, ToolContext, ToolResult, tool
from celine.skills.meta import Skills
from celine.turns import loads, prompt

pytestmark = pytest.mark.anyio

RAN: list[dict] = []


def _mac():
    """A skill with large parameters, like an MCP server's: forty tools, and its own part
    of the prompt."""
    class Mac:
        name = "mac"
        description = "Her Mac."

        async def prompt_fragment(self, ctx) -> str:
            return "The Mac is in the living room."

    def make(i: int):
        async def run(self, ctx, command: str, cwd: str = "", timeout_ms: int = 0,
                      shell: str = "") -> ToolResult:
            RAN.append({"tool": i, "command": command})
            return ToolResult(f"ran {command}")
        run.__doc__ = f"Run thing number {i} on the Mac. " + "It explains a lot. " * 60
        return tool(read_only=False)(run)
    for i in range(40):
        setattr(Mac, f"tool{i:02d}", make(i))

    async def version(self, ctx) -> ToolResult:
        RAN.append({"tool": "version"})
        return ToolResult("26.7")
    version.__doc__ = "The macOS version. " + "It explains a lot. " * 60
    Mac.version = tool()(version)
    return Mac()


class Notebook:
    name = "notebook"
    description = "Her notes."

    @tool()
    async def read(self, ctx, path: str) -> ToolResult:
        """Read a note. It returns the note's text."""
        return ToolResult("note")


def _registry(cfg) -> Registry:
    reg = Registry(cfg)
    for s in (Notebook(), _mac()):
        reg.add(s)
    reg.add(Skills(reg, cfg))
    return reg


def _ctx(store, cfg, cid, room=None) -> ToolContext:
    return ToolContext(conversation_id=cid, turn_id="t", tool_call_id="x", store=store,
                       config=cfg, emit=None, max_output_tokens=room)


async def test_every_tool_is_sent_core_in_full_others_by_name(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    reg = _registry(cfg)
    cid = await conversations.create(store)
    got = await loads.select(store, cfg, reg, cid, None)
    specs, _ = await prompt.tool_specs(reg, store, cid, got)
    wire = {t["function"]["name"]: t for t in prompt.wire_tools(specs, reg, {}, cfg)}
    assert wire["mac__tool07"]["function"] == {
        "name": "mac__tool07", "description": "Run thing number 7 on the Mac.",
        "parameters": {"type": "object"}}
    assert wire["notebook__read"] == reg.tools["notebook__read"].as_wire(), \
        "a core skill keeps its parameters"
    assert wire["mac__version"] == reg.tools["mac__version"].as_wire(), \
        "a tool without parameters goes out in full"
    index = reg.index(cfg, got)
    assert "- notebook [core]: Her notes." in index and "- mac: Her Mac." in index


async def test_a_load_appends_and_the_prefix_stays_byte_for_byte(store):
    cfg = load()
    RAN.clear()
    reg = _registry(cfg)
    cid = await conversations.create(store)
    b = Replies([_calls(("skills__load", {"names": ["mac__tool02"]})),
                 _calls(("mac__tool02", {"command": "sw_vers"})),
                 [TextDelta("done"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    rows = await _results(store)
    spec = reg.tools["mac__tool02"]
    assert rows[0][:2] == ("skills__load", "ok")
    assert rows[0][2] == ("mac__tool02, with its parameters:\n"
                          + json.dumps(spec.as_wire()["function"], ensure_ascii=False))
    assert rows[1][:2] == ("mac__tool02", "ok") and RAN == [{"tool": 2, "command": "sw_vers"}]
    systems = {json.dumps(c[0]) for c in b.calls}
    tools = {json.dumps(o) for o in b.offered}
    assert len(b.calls) == 3 and len(systems) == 1 and len(tools) == 1, \
        "system prompt and tools are the same bytes in every step"


async def test_a_call_by_name_is_checked_against_the_real_schema(store):
    cfg = load()
    RAN.clear()
    reg = _registry(cfg)
    cid = await conversations.create(store)
    b = Replies([_calls(("mac__tool02", {"cmd": "sw_vers"})),
                 _calls(("mac__tool02", {"command": "sw_vers"})),
                 [TextDelta("done"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    rows = await _results(store)
    spec = reg.tools["mac__tool02"]
    assert rows[0][:2] == ("mac__tool02", "error")
    assert rows[0][2] == ("invalid arguments: missing command; unexpected cmd\nThe tool: "
                          + json.dumps(spec.as_wire()["function"], ensure_ascii=False))
    assert rows[1][:2] == ("mac__tool02", "ok") and RAN == [{"tool": 2, "command": "sw_vers"}]


async def test_a_skill_loads_with_its_part_and_note_and_a_big_one_by_name(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    reg = _registry(cfg)
    cid = await conversations.create(store)
    (cfg.her.notebook / "skills").mkdir(parents=True)
    (cfg.her.notebook / "skills" / "mac.md").write_text("The Mac sleeps at night.")
    meta = reg.skills["skills"]
    out = await meta.load(_ctx(store, cfg, cid), names=["mac", "nope", "mac__x"])
    lines = out.content.split("\n\n")
    assert not out.is_error and lines[:3] == [
        "# mac", "The Mac is in the living room.",
        "Your notes about mac (from your notebook, skills/mac.md):"]
    assert json.dumps(reg.tools["mac__tool39"].as_wire()["function"]) in out.content
    assert "No skill called 'nope' here. The skills are: notebook, mac." in lines
    assert out.display == {"skills": ["mac"], "tools": []}

    out = await meta.load(_ctx(store, cfg, cid, room=500), names="mac")
    assert out.content.startswith("# mac\n\nThe Mac is in the living room.")
    assert out.content.endswith("more than one result holds (500): load the tools you "
                                "need by name, such as mac__tool00.")
    assert '"name": "mac__tool00"' not in out.content


async def test_a_message_that_asks_for_a_skill_gets_its_load_once(store, tmp_path):
    from chat_fixtures import user_says
    from celine.conversations import tree
    from celine.skills import guide as guide_of
    from celine.skills.meta import HEAD
    from celine.turns import message_context

    class Painter:
        name = "image_generator"
        description = "Makes images."
        guide = guide_of("image_generator")

        @tool()
        async def generate_image(self, ctx, prompt: str) -> ToolResult:
            """Make an image."""
            return ToolResult("made")
    cfg = load()
    reg = _registry(cfg)
    reg.add(Painter())
    cid = await conversations.create(store)

    async def says(text):
        mid, tid, _ = await user_says(store, cid, text)
        [m] = await store.read(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM messages WHERE id=?", (mid,))])
        await message_context.ensure(store, cfg, reg, cid, tid, [m], 5000)
        reply = await tree.start_assistant_message(store, cid, mid, tid)
        await tree.finish_assistant_message(store, cid, reply, "ok", None)
        await store.write(lambda c: c.execute("UPDATE turns SET state='done'"))
        return m.get("context") or ""
    first = await says("draw me a picture of the sea")
    assert first.startswith(HEAD + "\n# image_generator\n\n")
    assert '"name": "image_generator__generate_image"' in first
    assert await says("another picture") == "", "brought once on its branch"
    await store.write(lambda c: c.execute("UPDATE conversations SET auto_context=0"))
    other = await conversations.create(store)
    await store.write(lambda c: c.execute("UPDATE conversations SET auto_context=0"))
    cid = other
    assert await says("a picture please") == "", "nothing by need while it is off"


def test_the_name_form_goes_through_every_backend():
    entry = {"type": "function", "function": {
        "name": "mac__run", "description": "Run a command.",
        "parameters": {"type": "object"}}}
    bc = dict(base_url="http://x", model="m")
    openai = build(BackendConfig(id="x", kind="llama-server", label="x", raw={}, **bc))
    assert openai.request_body([], [entry])["tools"] == [entry]
    ollama = OllamaBackend(BackendConfig(id="o", kind="ollama", label="o", raw={}, **bc))
    assert ollama.request_body([], [entry])["tools"] == [entry]
    anthropic = AnthropicBackend(BackendConfig(
        id="a", kind="anthropic", label="a", params={"max_tokens": 10},
        raw={"provider_kind": "anthropic", "tools": True}, **bc))
    assert [{k: t[k] for k in ("name", "description", "input_schema")}
            for t in anthropic.request_body([], [entry])["tools"]] == [
        {"name": "mac__run", "description": "Run a command.",
         "input_schema": {"type": "object"}}]
