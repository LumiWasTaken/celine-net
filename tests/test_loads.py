"""What it carries by need (turns/loads.py): the notes and skills of a conversation,
selected in a fixed window, kept while touched, unloaded after six idle turns, never
over their share; the notes on the user always there."""
from __future__ import annotations


import pytest

from notes_fixtures import BACKEND, WINDOW, notes_cfg, select_turn, write_notes

from celine.conversations import store as conversations
from celine.skills import Registry, ToolContext, ToolResult, guide, tool
from celine.skills.meta import Skills
from celine.skills.notebook import notes_block
from celine.turns import loads

pytestmark = pytest.mark.anyio


async def test_notes_load_by_need_within_their_share(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    await write_notes(store, cfg, {
        "people/joe.md": "Joe likes the sea.",
        "style.md": "load: always\n\nShort sentences.",
        "lore/keeper.md": "The lighthouse keeper rows out at night.",
        "lore/big.md": "The lighthouse in winter. " + "x" * 6000,
        "opinions/lighthouse.md": "load: off\n\nThe lighthouse is ugly.",
        "lore/other.md": "Nothing about it.",
    })
    cid = await conversations.create(store)
    await loads.touch(store, cid, "note", ["lore/keeper.md", "lore/big.md"])
    got = await select_turn(store, cfg, None, cid, 1, "Tell me about the lighthouse keeper")
    states = {n["path"]: n["state"] for n in got.notes}
    assert states == {"people/joe.md": "included", "style.md": "included",
                      "lore/keeper.md": "included", "lore/big.md": "left_out"}
    block = notes_block(cfg, got.notes)
    assert got.budgets["notes"] == WINDOW // 10
    assert _cost(block) <= got.budgets["notes"], "the block as it reads it fits"
    assert block.startswith("[your notes] loaded now: your notes about Joe always")
    assert block.split("\n\n")[1].startswith("### lore/keeper.md\n")
    assert block.endswith("Not loaded, no room for them now: lore/big.md.")
    assert got.since[("note", "lore/keeper.md")] == 1

    # nothing saved by a dry look, and the notes on the user are there in every turn
    assert (await loads.select(store, cfg, None, cid, BACKEND)).turn == 1
    await conversations.update(store, cid, notes_off=["people/joe.md"])
    got = await select_turn(store, cfg, None, cid, 2)
    assert {n["path"]: (n["state"], n["why"]) for n in got.notes}["people/joe.md"] == \
        ("off", loads.OFF_HERE)
    assert block_has(notes_block(cfg, got.notes), "Left out by Joe in this "
                                                  "conversation: people/joe.md.")


def block_has(block: str, line: str) -> bool:
    return line in block.split("\n\n")


def _cost(text: str) -> int:
    from celine.turns import tokens
    return tokens.counter(BACKEND).estimate(text)


async def test_always_notes_load_past_the_share_and_by_need_ones_fit_what_is_left(
        store, tmp_path):
    """The share is for the notes loaded by need: an always note is never left out, even
    when it alone is over the share."""
    cfg = notes_cfg(tmp_path)
    await write_notes(store, cfg, {
        "samples.md": "load: always\n\n" + "a sample. " * 200,
        "lore/keeper.md": "The lighthouse keeper rows out at night.",
    })
    cid = await conversations.create(store)
    await loads.touch(store, cid, "note", ["lore/keeper.md"])
    got = await select_turn(store, cfg, None, cid, 1, "Tell me about the lighthouse keeper")
    assert {n["path"]: n["state"] for n in got.notes} == {
        "samples.md": "included", "lore/keeper.md": "included"}
    cfg.context = {**cfg.context, "notes": 0.05}      # 500 tokens: the samples alone are over
    got = await select_turn(store, cfg, None, cid, 2, "Tell me about the lighthouse keeper")
    assert {n["path"]: n["state"] for n in got.notes} == {
        "samples.md": "included", "lore/keeper.md": "left_out"}
    assert _cost(notes_block(cfg, got.notes)) > got.budgets["notes"]


async def test_a_loaded_note_unloads_after_six_untouched_turns(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    await write_notes(store, cfg, {"lore/keeper.md": "The lighthouse keeper rows out."})
    cid = await conversations.create(store)
    await loads.touch(store, cid, "note", ["lore/keeper.md"])
    got = await select_turn(store, cfg, None, cid, 1, "the lighthouse keeper")
    assert got.loaded_notes == {"lore/keeper.md"}
    for n in range(2, 7):                       # six turns that do not touch it
        got = await select_turn(store, cfg, None, cid, n)
        assert got.loaded_notes == {"lore/keeper.md"}, n
    assert got.since[("note", "lore/keeper.md")] == 6
    got = await select_turn(store, cfg, None, cid, 7)
    assert got.loaded_notes == frozenset()
    # a read loads it again and keeps it from that turn on
    await loads.touch(store, cid, "note", ["lore/keeper.md"])
    got = await select_turn(store, cfg, None, cid, 8)
    assert got.loaded_notes == {"lore/keeper.md"} and \
        got.since[("note", "lore/keeper.md")] == 1
    # a resumed turn is not counted twice
    t9 = await store.read(lambda c: c.execute(
        "SELECT id FROM turns ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    again = await loads.select(store, cfg, None, cid, BACKEND, when="start", turn_id=t9)
    assert again.turn == 8


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


async def test_skills_load_by_need_and_unload_when_idle(store, tmp_path):
    cfg = notes_cfg(tmp_path, tools={"shell": "off"})
    reg = _registry(cfg)
    cid = await conversations.create(store)
    got = await select_turn(store, cfg, reg, cid, 1, "hello")
    assert got.skills == ["notebook", "skills"]
    assert got.others == {"web": "", "shell": "off"}
    index = reg.index(cfg, got)
    assert "Loaded: notebook." in index and "- web: The public web." in index
    assert "- shell: Her terminals. (off in Settings, Tools: it cannot load)" in index

    got = await select_turn(store, cfg, reg, cid, 2, "look at https://example.com")
    assert got.skills == ["notebook", "web", "skills"]

    ctx = ToolContext(conversation_id=cid, turn_id="t2", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    meta = reg.skills["skills"]
    out = await meta.load(ctx, name="shell")
    assert out.is_error and out.content.startswith("shell is off in Settings, Tools")
    cfg.settings.tools = {}
    from celine.skills.gating import clear_override, set_override
    await set_override(store, cid, "shell", False, 1)
    out = await meta.load(ctx, name="shell")
    assert out.is_error and out.content.startswith("shell is off in this conversation")
    off = await loads.select(store, cfg, reg, cid, BACKEND, when="step")
    assert "(off in this conversation: it cannot load)" in reg.index(cfg, off)
    await clear_override(store, cid, "shell")
    out = await meta.load(ctx, name="shell")
    assert out.content == "Loaded shell. Its tools are in your list from your next step."
    got = await loads.select(store, cfg, reg, cid, BACKEND, when="step")
    assert got.skills == ["notebook", "web", "shell", "skills"] and got.turn == 2

    # what does not fit the tools budget is not loaded, and the index says so
    cfg.context["tools"] = 0
    got = await select_turn(store, cfg, reg, cid, 3, "the web, please")
    assert got.skills == ["notebook", "skills"] and got.others["web"] == "no room"
    assert "- web: The public web. (no room for its tools now)" in reg.index(cfg, got)


async def test_the_feed_says_what_came_and_what_went(store, tmp_path):
    import json
    cfg = notes_cfg(tmp_path)
    await write_notes(store, cfg, {"lore/keeper.md": "The lighthouse keeper rows out."})
    reg = _registry(cfg)
    cid = await conversations.create(store)

    async def feed():
        rows = await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
            "SELECT data FROM global_events WHERE type='context.changed' ORDER BY id")])
        await store.write(lambda c: c.execute(
            "DELETE FROM global_events WHERE type='context.changed'"))
        return rows
    await loads.touch(store, cid, "note", ["lore/keeper.md"])
    await feed()
    await select_turn(store, cfg, reg, cid, 1, "the lighthouse keeper")
    assert await feed() == [{"conversation": cid, "loaded": {
        "notes": [], "skills": ["notebook", "skills"]},
        "unloaded": {"notes": [], "skills": []}}]
    await loads.touch(store, cid, "skill", ["web"])
    await loads.touch(store, cid, "skill", ["web"])          # nothing new: no event
    assert [e["loaded"]["skills"] for e in await feed()] == [["web"]]
    for n in range(2, 9):
        await select_turn(store, cfg, reg, cid, n)
    got = await feed()
    assert len(got) == 7, "every turn start says so"
    empty = {"notes": [], "skills": []}
    assert [e["unloaded"] for e in got if e["unloaded"] != empty] == [
        {**empty, "notes": ["lore/keeper.md"]}, {**empty, "skills": ["web"]}]



def test_tools_is_a_flat_budget_and_a_share_is_a_problem(tmp_path):
    from celine.settings import seed
    root = tmp_path / "settings"
    root.mkdir()
    seed.context_path(root).write_text("notes = 0.10\ntools = 10000\n")
    problems: list = []
    s = loads.read_shares(seed.context_path(root), problems)
    assert s["tools"] == 10_000 and s["notes"] == 0.10 and not problems
    seed.context_path(root).write_text("tools = 0.5\n")
    loads.read_shares(seed.context_path(root), problems)
    assert problems, "a share is no longer a tools budget"


def _big():
    async def go(self, ctx, where: str) -> ToolResult:
        return ToolResult("went")
    go.__doc__ = "Go somewhere. " + "far away " * 8000

    class Big:
        name = "big"
        description = "A skill with a large schema."
    Big.go = tool()(go)
    return Big()


async def test_a_skill_over_the_tools_budget_is_not_loaded_and_its_calls_say_why(
        store, tmp_path):
    from celine.turns import tokens
    from celine.turns import tools as turn_tools
    cfg = notes_cfg(tmp_path)
    reg = Registry(cfg)
    for s in (Notebook(), Web(), _big()):
        reg.add(s)
    reg.add(Skills(reg, cfg))
    cid = await conversations.create(store)
    got = await select_turn(store, cfg, reg, cid, 1, "hello")
    budget = got.budgets["tools"]
    ctx = ToolContext(conversation_id=cid, turn_id="t1", tool_call_id="x", store=store,
                      config=cfg, emit=None)
    meta = reg.skills["skills"]
    out = await meta.load(ctx, name="big")
    cost = loads.schema_cost(tokens.counter(BACKEND), reg.by_skill()["big"])
    assert cost > budget
    have = {n: got.costs[n] for n in got.skills}
    assert out.is_error and out.content == (
        f"big is not loaded: its tools take {cost} tokens, the tools budget is {budget}, "
        f"and the skills loaded now take {sum(have.values())}: "
        + ", ".join(f"{n} ({t})" for n, t in have.items()) + ".")
    assert ("skill", "big") not in (await store.read(lambda c: loads.rows(c, cid)))[1]
    assert (await meta.load(ctx, name="web")).content.startswith("Loaded web.")

    # a skill touched another way (a tool loads it) that does not fit: its call says why
    await loads.touch(store, cid, "skill", ["big"])
    carried = await loads.select(store, cfg, reg, cid, BACKEND, when="step")
    assert carried.others["big"] == "no room"
    turn = {"offered": frozenset(), "loaded": frozenset(carried.skills), "carried": carried}
    need = sum(carried.costs[n] for n in carried.skills) + carried.costs["big"]
    _, res, _ = turn_tools._not_offered(turn, "big__go", reg.tools["big__go"], reg, {}, cfg)
    assert res.content == (f"big is not carried: over the tools budget ({need} of {budget} "
                           f"tokens).")
