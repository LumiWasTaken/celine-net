"""What it carries (turns/loads.py): the always notes, the ones left out here, the
skills offered here, and the context numbers."""
from __future__ import annotations


import pytest

from notes_fixtures import notes_cfg, select_turn, write_notes

from celine.conversations import store as conversations
from celine.skills import Registry, ToolContext, ToolResult, guide, tool
from celine.skills.meta import Skills
from celine.skills.notebook import notes_block
from celine.turns import loads

pytestmark = pytest.mark.anyio


async def test_always_notes_and_the_ones_left_out_here(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    await write_notes(store, cfg, {
        "people/joe.md": "Joe likes the sea.",
        "style.md": "load: always\n\nShort sentences.",
        "lore/keeper.md": "The lighthouse keeper rows out at night.",
        "opinions/lighthouse.md": "load: off\n\nThe lighthouse is ugly.",
    })
    cid = await conversations.create(store)
    got = await select_turn(store, cfg, None, cid, 1, "Tell me about the lighthouse keeper")
    assert {n["path"]: n["state"] for n in got.notes} == {
        "people/joe.md": "included", "style.md": "included"}, "nothing by need"
    block = notes_block(cfg, got.notes)
    assert block.startswith("[your notes] you always carry: your notes about Joe")
    assert "### style.md\nShort sentences." in block, "no load line"
    await conversations.update(store, cid, notes_off=["people/joe.md"])
    got = await select_turn(store, cfg, None, cid, 2)
    assert {n["path"]: (n["state"], n["why"]) for n in got.notes}["people/joe.md"] == \
        ("off", loads.OFF_HERE)
    assert "Left out by Joe in this conversation: people/joe.md." in \
        notes_block(cfg, got.notes).split("\n\n")


class Web:
    name = "web"
    description = "The public web."
    guide = guide("web")              # its words: a link, "web", ...

    @tool()
    async def search(self, ctx, query: str) -> ToolResult:
        """Search the web."""
        return ToolResult("hits")


class Shell:
    name = "shell"
    description = "Her terminals."

    @tool(read_only=False)
    async def run(self, ctx, command: str) -> ToolResult:
        """Run a command."""
        return ToolResult("ran")


class Notebook:
    name = "notebook"
    description = "Her notes."

    @tool()
    async def read(self, ctx, path: str) -> ToolResult:
        """Read a note."""
        return ToolResult("note")


def _registry(cfg):
    reg = Registry(cfg)
    for s in (Notebook(), Web(), Shell()):
        reg.add(s)
    reg.add(Skills(reg, cfg))
    return reg


async def test_every_offered_skill_is_in_the_index_and_off_ones_say_so(store, tmp_path):
    cfg = notes_cfg(tmp_path, tools={"shell": "off"})
    reg = _registry(cfg)
    cid = await conversations.create(store)
    got = await select_turn(store, cfg, reg, cid, 1, "hello")
    assert got.skills == ["notebook", "web", "skills"] and got.others == {"shell": "off"}
    index = reg.index(cfg, got)
    assert "- web: The public web." in index
    assert "- shell: Her terminals." in index, "what is off is said in the tail"

    ctx = ToolContext(conversation_id=cid, turn_id="t2", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    meta = reg.skills["skills"]
    out = await meta.load(ctx, names=["shell"])
    assert out.is_error and out.content.startswith("shell is off in Settings, Tools")
    cfg.settings.tools = {}
    from celine.skills.gating import clear_override, set_override
    await set_override(store, cid, "shell", False, 1)
    out = await meta.load(ctx, names=["shell"])
    assert out.is_error and out.content.startswith("shell is off in this conversation")
    await clear_override(store, cid, "shell")
    out = await meta.load(ctx, names="shell")          # one string is a name too
    assert not out.is_error and out.content.startswith("# shell\n\n")


def test_the_shares_read_and_an_unknown_key_is_a_problem(tmp_path):
    from celine.settings import seed
    root = tmp_path / "settings"
    root.mkdir()
    seed.context_path(root).write_text("memory_sections = 4\n")
    problems: list = []
    s = loads.read_shares(seed.context_path(root), problems)
    assert s["memory_sections"] == 4 and not problems
    seed.context_path(root).write_text("notes = 0.1\n")
    loads.read_shares(seed.context_path(root), problems)
    assert problems, "no notes share"


def test_the_migration_drops_the_tools_budget_from_context_toml(tmp_path):
    import importlib.util
    from celine.db.migrate import MIGRATIONS_DIR
    spec = importlib.util.spec_from_file_location(
        "m12", MIGRATIONS_DIR / "0012_loads_append.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    path = tmp_path / "context.toml"
    path.write_text("# shares\nnotes = 0.10\ntools = 10000  # gone\nmemory_floor = 0.5\n")
    m.drop_keys(path, ("tools",))
    assert path.read_text() == "# shares\nnotes = 0.10\nmemory_floor = 0.5\n"


