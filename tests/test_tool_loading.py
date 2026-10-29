"""Tools load one by one (turns/loads.py): a loaded skill puts its tools in the list by
name and one line, a loaded or called tool carries its parameters, the tools budget
counts parameters only, a call without them returns the tool and does not run, and
skills__unload frees the room. The core skills keep their parameters."""
from __future__ import annotations

import json

import pytest

from notes_fixtures import BACKEND, notes_cfg, select_turn
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
from celine.turns import loads, prompt, tokens

pytestmark = pytest.mark.anyio

RAN: list[dict] = []


def _mac():
    """A skill with large parameters, like an MCP server's: forty tools."""
    class Mac:
        name = "mac"
        description = "Her Mac."

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


def _ctx(store, cfg, cid) -> ToolContext:
    return ToolContext(conversation_id=cid, turn_id="t", tool_call_id="x", store=store,
                       config=cfg, emit=None)


async def test_a_skill_loads_as_names_and_costs_its_names(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    reg = _registry(cfg)
    cid = await conversations.create(store)
    await loads.touch(store, cid, "skill", ["mac"])
    got = await select_turn(store, cfg, reg, cid, 1)
    counter = tokens.counter(BACKEND)
    specs = reg.by_skill()["mac"]
    full = loads.schema_cost(counter, specs)
    assert full > got.budgets["tools"], "its parameters alone are over the budget"
    assert "mac" in got.skills and got.tools == []
    assert got.costs["mac"] == loads.names_cost(counter, specs) < full / 5
    assert got.counted == got.costs["notebook"] + got.costs["skills"], "names not counted"
    specs_here, _ = await prompt.tool_specs(reg, store, cid, got)
    wire = {t["function"]["name"]: t for t in prompt.wire_tools(specs_here, reg, {}, cfg, got)}
    assert wire["mac__tool07"]["function"] == {
        "name": "mac__tool07", "description": "Run thing number 7 on the Mac.",
        "parameters": {"type": "object", "properties": {}}}
    assert wire["notebook__read"] == reg.tools["notebook__read"].as_wire(), \
        "a core skill keeps its parameters"
    assert wire["mac__version"] == reg.tools["mac__version"].as_wire(), \
        "a tool without parameters goes out in full"


async def test_loading_one_tool_carries_just_its_parameters(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    reg = _registry(cfg)
    cid = await conversations.create(store)
    await select_turn(store, cfg, reg, cid, 1)
    meta = reg.skills["skills"]
    out = await meta.load(_ctx(store, cfg, cid), names=["mac__tool03", "nope", "mac__x"])
    assert not out.is_error and out.content.splitlines() == [
        "Loaded mac__tool03: its parameters are in your list from your next step.",
        "No skill called 'nope' here. The skills are: notebook, mac.",
        "There is no tool mac__x. The mac skill has "
        + ", ".join(f"mac__tool{i:02d}" for i in range(40)) + ", mac__version."]
    got = await loads.select(store, cfg, reg, cid, BACKEND, when="step")
    counter = tokens.counter(BACKEND)
    assert "mac" in got.skills and got.tools == ["mac__tool03"]
    assert got.costs["mac__tool03"] == loads.schema_cost(counter, [reg.tools["mac__tool03"]])
    specs_here, _ = await prompt.tool_specs(reg, store, cid, got)
    wire = prompt.wire_tools(specs_here, reg, {}, cfg, got)
    full = {t["function"]["name"] for t in wire if t["function"]["parameters"].get("required")}
    assert {n for n in full if n.startswith("mac")} == {"mac__tool03"}
    assert "notebook__read" in full

    # over the budget: not loaded, and it says what the list holds
    cfg.context["tools"] = got.counted + 10
    out = await meta.load(_ctx(store, cfg, cid), names="mac__tool04")
    cost = loads.schema_cost(tokens.counter(), [reg.tools["mac__tool04"]])
    assert out.is_error and out.content == (
        f"mac__tool04 is not loaded: its parameters take {cost} tokens, the tools budget "
        f"is {got.counted + 10}, and the parameters in your list take {got.counted}: "
        f"notebook ({got.costs['notebook']}), skills ({got.costs['skills']}), "
        f"mac__tool03 ({got.costs['mac__tool03']}).")
    got = await loads.select(store, cfg, reg, cid, BACKEND, when="step")
    assert got.tools == ["mac__tool03"]


async def test_unload_frees_the_budget(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    reg = _registry(cfg)
    cid = await conversations.create(store)
    await select_turn(store, cfg, reg, cid, 1)
    meta, ctx = reg.skills["skills"], _ctx(store, cfg, cid)
    await meta.load(ctx, names=["mac__tool01", "mac__tool02"])
    got = await loads.select(store, cfg, reg, cid, BACKEND, when="step")
    before = got.counted
    out = await meta.unload(ctx, names=["mac__tool01", "notebook", "mac__tool09"])
    assert out.content.splitlines() == ["notebook is always in your list.",
                                        "Unloaded mac__tool01.", "mac__tool09 was not loaded."]
    got = await loads.select(store, cfg, reg, cid, BACKEND, when="step")
    assert got.tools == ["mac__tool02"] and got.counted < before
    assert (await meta.unload(ctx, names="mac")).content == "Unloaded mac."
    got = await loads.select(store, cfg, reg, cid, BACKEND, when="step")
    assert "mac" not in got.skills and got.tools == []
    assert got.counted == got.costs["notebook"] + got.costs["skills"]


async def test_a_tool_idle_for_more_than_six_turns_unloads(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    reg = _registry(cfg)
    cid = await conversations.create(store)
    await select_turn(store, cfg, reg, cid, 1)
    await reg.skills["skills"].load(_ctx(store, cfg, cid), names=["mac__tool05"])
    await loads.touch(store, cid, "skill", ["mac"])
    for n in range(2, 9):
        got = await select_turn(store, cfg, reg, cid, n)
        if n == 6:
            await loads.touch(store, cid, "skill", ["mac"])   # the skill is used, not the tool
    assert got.tools == [] and "mac" in got.skills


async def test_a_call_without_its_parameters_returns_the_tool_and_does_not_run(store):
    cfg = load()
    RAN.clear()
    reg = _registry(cfg)
    cid = await conversations.create(store)
    await loads.touch(store, cid, "skill", ["mac"])
    b = Replies([_calls(("mac__tool02", {"cmd": "sw_vers"})),
                 _calls(("mac__tool02", {"command": "sw_vers"})),
                 [TextDelta("done"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    rows = await _results(store)
    spec = reg.tools["mac__tool02"]
    assert rows[0][:2] == ("mac__tool02", "gated")
    assert rows[0][2] == ("mac__tool02 did not run: its parameters were not in your list. "
                          "They are from your next step. The tool:\n"
                          + json.dumps(spec.as_wire()["function"], ensure_ascii=False))
    assert rows[1][:2] == ("mac__tool02", "ok") and RAN == [{"tool": 2, "command": "sw_vers"}]
    first, second = ({t["function"]["name"]: t["function"]["parameters"] for t in o}
                     for o in b.offered[:2])
    assert first["mac__tool02"] == {"type": "object", "properties": {}}
    assert second["mac__tool02"] == spec.schema


async def test_a_tool_without_parameters_runs_with_no_load_step(store):
    cfg = load()
    RAN.clear()
    reg = _registry(cfg)
    cid = await conversations.create(store)
    await loads.touch(store, cid, "skill", ["mac"])
    b = Replies([_calls(("mac__version", {})), [TextDelta("done"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    assert (await _results(store))[0][:2] == ("mac__version", "ok")
    assert RAN == [{"tool": "version"}]
    got = await loads.select(store, cfg, reg, cid, BACKEND)
    assert got.tools == [], "nothing to carry"
    out = await reg.skills["skills"].load(_ctx(store, cfg, cid), names=["mac__version"])
    assert out.content == ("mac__version takes no parameters: it is in your list in full "
                           "while mac is loaded, from your next step.")


def test_the_name_form_goes_through_every_backend():
    entry = {"type": "function", "function": {
        "name": "mac__run", "description": "Run a command.",
        "parameters": {"type": "object", "properties": {}}}}
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
         "input_schema": {"type": "object", "properties": {}}}]
