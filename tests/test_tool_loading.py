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
from celine.skills import Guide, Registry, ToolContext, ToolResult, tool
from celine.conversations import view
from celine.skills.meta import Skills, had
from celine.turns import compactions, loads, prompt, tools

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
    assert rows[0][:2] == ("mac__tool02", "error")
    assert rows[0][2] == ("invalid arguments: missing command; cmd is not a parameter, did "
                          "you mean command?\nmac__tool02 takes command (a string, required), "
                          "cwd (a string), timeout_ms (a whole number), shell (a string)."), \
        "the name it meant and its parameters in one line, not its description"
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


async def test_a_load_counts_everything_it_holds_against_one_result(store, tmp_path):
    from celine.turns import tokens
    cfg = notes_cfg(tmp_path)
    reg = _registry(cfg)
    cid = await conversations.create(store)
    meta = reg.skills["skills"]
    one = tokens.estimate((await meta.load(_ctx(store, cfg, cid), names="mac__tool01"))
                          .content, "tool")
    room = one * 2 + 200          # two tools, and a line for each name after them
    out = await meta.load(_ctx(store, cfg, cid, room=room),
                          names=["mac__tool01", "mac__tool02", "mac__tool03", "mac"])
    assert out.display == {"skills": ["mac"], "tools": ["mac__tool01", "mac__tool02"]}
    assert "\n\nmac__tool03 did not fit in this result (about " in out.content
    assert f"left of {room:,}): load it on its own.\n\n# mac\n\n" in out.content
    assert out.content.endswith("more than one result holds "
                                f"({room:,}): load the tools you need by name, such as "
                                "mac__tool00."), "its part of the prompt, then by name"
    assert tokens.estimate(out.content, "tool") <= room
    assert "file" not in out.content


def _guided(cfg) -> Registry:
    reg = _registry(cfg)
    reg.skills["mac"].guide = Guide("mac", description="Her Mac.",
                                    body="Wake the Mac before you type.")
    return reg


def _told(request: list[dict]) -> list[str]:
    return [m["content"] for m in request if m.get("role") == "tool"]


async def _had(store, cid) -> set[str]:
    return await store.read(lambda c: had(c, cid))


BROUGHT = ("26.7\n\n[loaded with this call]\n# mac\n\nWake the Mac before you type.\n\n"
           "The Mac is in the living room.\n\nThe parameters of its tools take about ")


async def test_the_first_call_by_name_brings_the_skill_once(store):
    cfg = load()
    reg = _guided(cfg)
    cid = await conversations.create(store)
    b = Replies([_calls(("mac__version", {}), ("mac__version", {})),
                 _calls(("mac__version", {})),
                 [TextDelta("done"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    first, together, later = _told(b.calls[2])
    assert first.startswith(BROUGHT)
    assert together == "26.7", "two calls of one batch: one load"
    assert later == "26.7", "once per skill in a conversation"
    assert [r[2] for r in await _results(store)] == ["26.7"] * 3, \
        "the stored result is the tool's; the load is the call's display"
    systems = {json.dumps(c[0]) for c in b.calls}
    assert len(systems) == 1, "nothing goes into the system prompt"
    assert await _had(store, cid) == {"mac"}

    done = await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT data FROM events WHERE conversation_id=? AND type='tool.done' "
        "ORDER BY seq", (cid,))])
    assert done and all("guide" not in (d["display"] or {}) for d in done), \
        "the load stays off the client's event"
    snap = await view.snapshot(store, cid)
    assert not any("guide" in (c["display"] or {})
                   for calls in snap["tool_calls"].values() for c in calls)

    call = await store.read(lambda c: dict(c.execute(
        "SELECT id, name, message_id FROM tool_calls ORDER BY rowid").fetchone()))
    state, again, _ = await tools._replayed(store, {"id": "t"}, call)
    assert tools.marked(again, False).startswith(BROUGHT), "a replay keeps the load"

    sub = await conversations.create(store, "scout", agent_of=cid, agent_name="scout")
    assert await _had(store, sub) == set(), "a sub-agent's conversation has its own"


async def test_a_guide_aged_or_summarised_away_comes_again(store):
    cfg = load()
    reg = _guided(cfg)
    cid = await conversations.create(store)
    b = Replies([_calls(("mac__version", {})), [TextDelta("done"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    called = await store.read(lambda c: c.execute(
        "SELECT message_id FROM tool_calls ORDER BY rowid").fetchone()[0])
    assert await _had(store, cid) == {"mac"}
    await compactions.set_aged_through(store, cid, called, {})
    assert await _had(store, cid) == set(), "an aged result goes as a stub"

    b = Replies([_calls(("mac__version", {})), [TextDelta("done"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    assert _told(b.calls[1])[-1].startswith(BROUGHT), "it comes again"
    assert await _had(store, cid) == {"mac"}
    last = await store.read(lambda c: c.execute(
        "SELECT active_leaf_id FROM conversations WHERE id=?", (cid,)).fetchone()[0])
    await compactions.save(store, cid, {"through_message_id": last, "trigger": "manual",
                                        "covers_messages": 6, "text": "summary"})
    assert await _had(store, cid) == set(), "a summary covers it"


async def test_the_load_fits_in_the_room_the_result_left(store):
    from celine.turns import tokens
    cfg = load()
    reg = _guided(cfg)
    cid = await conversations.create(store)
    cap = 1000
    turn = {"id": "t", "conversation_id": cid, "max_output_tokens": cap, "guides": {}}
    call = {"id": "x", "name": "mac__version"}
    big = "y " * 3000
    while tokens.estimate(big, "tool") > cap - 20:
        big = big[:-200]
    out = await tools._with_guide(ToolResult(big), turn, call, store, cfg, reg, big)
    assert out.display is None and "mac" not in turn["guides"]["had"], \
        "no room left: it comes with a later call"
    out = await tools._with_guide(ToolResult("26.7"), turn, call, store, cfg, reg, "26.7")
    told = tools.marked(out, False)
    assert told.startswith(BROUGHT) and tokens.estimate(told, "tool") <= cap


async def test_a_loaded_or_read_skill_brings_nothing_with_its_call(store):
    cfg = load()
    reg = _guided(cfg)
    cid = await conversations.create(store)
    b = Replies([_calls(("skills__load", {"names": ["mac"]})),
                 _calls(("mac__version", {})),
                 [TextDelta("done"), Done(reason="stop")]])
    await _run(store, cfg, reg, cid, b)
    loaded, called = _told(b.calls[2])
    assert loaded.startswith("# mac\n\nWake the Mac before you type.\n\n"), \
        "a load brings the guide"
    assert called == "26.7"

    meta = reg.skills["skills"]
    other = await conversations.create(store)
    assert meta.comes_with_calls("mac")
    reg.skills["mac"].guide = None
    assert not meta.comes_with_calls("mac"), "a skill without a guide brings nothing"
    reg.skills["mac"].guide = Guide("mac", description="Her Mac.", body="Wake it.")
    reg.skills["notebook"].guide = Guide("notebook", description="Her notes.",
                                         body="Keep it tidy.")
    assert not meta.comes_with_calls("notebook"), \
        "a core skill's part is in the system prompt: its calls bring nothing"
    loaded = (await meta.load(_ctx(store, cfg, other), names=["notebook"])).content
    assert loaded.startswith("# notebook\n\nKeep it tidy."), "a load still gives its guide"
    b = Replies([_calls(("skills__read_guide", {"name": "mac"})),
                 _calls(("mac__version", {})),
                 [TextDelta("done"), Done(reason="stop")]])
    await _run(store, cfg, reg, other, b)
    assert _told(b.calls[2])[1] == "26.7", "a guide it read counts"


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


def test_a_plain_value_in_a_string_is_read_as_its_type_and_a_wrong_name_gets_a_hint():
    from celine.turns.tools import _lenient, _meant
    schema = {"properties": {"unread": {"type": "boolean"}, "limit": {"type": "integer"},
                             "every": {"type": "number"}, "name": {"type": "string"}}}
    assert _lenient({"unread": "True", "limit": "10", "every": "1.5", "name": "true"}, schema) \
        == {"unread": True, "limit": 10, "every": 1.5, "name": "true"}, "a string stays one"
    assert _lenient({"unread": "yes", "limit": "ten"}, schema) == {"unread": "yes",
                                                                    "limit": "ten"}
    assert _meant("cmd", ["command", "terminal"], ["command", "terminal", "cwd"]) == "command"
    assert _meant("termnal", [], ["command", "terminal"]) == "terminal"
    assert _meant("zzz", [], ["command"]) is None
