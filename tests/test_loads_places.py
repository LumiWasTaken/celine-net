"""Skill places and the note layer (turns/loads.py, skills/places.py)."""
from __future__ import annotations

import pytest

from notes_fixtures import BACKEND, notes_cfg, select_turn, write_notes

from celine.conversations import store as conversations
from celine.skills import Guide, Registry, ToolResult, tool
from celine.skills.meta import Skills
from celine.turns import loads

pytestmark = pytest.mark.anyio


def _skill(name: str, words: tuple[str, ...] = ()):
    class S:
        description = f"The {name} skill."

        @tool()
        async def look(self, ctx) -> ToolResult:
            """Look."""
            return ToolResult("ok")
    s = S()
    s.name = name
    s.guide = Guide(name, s.description, "", words)
    return s


def _registry(cfg, *names):
    reg = Registry(cfg)
    for n in ("notebook", *names):
        reg.add(_skill(n, ("url", "web") if n == "web" else ()))
    reg.add(Skills(reg, cfg))
    return reg


async def test_only_the_users_own_people_notes_are_always_loaded(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    await write_notes(store, cfg, {"people/joe.md": "Joe.", "people/joe-taste.md": "Tea.",
                                   "people/joes-friend.md": "Her.",
                                   "people/joea.md": "Someone else."})
    cid = await conversations.create(store)
    got = await select_turn(store, cfg, None, cid, 1)
    assert got.loaded_notes == {"people/joe.md", "people/joe-taste.md"}


async def test_auto_context_off_loads_nothing_by_need(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    reg = _registry(cfg, "web", "shell")
    cid = await conversations.create(store)
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET auto_context=0 WHERE id=?", (cid,)))
    await loads.touch(store, cid, "skill", ["shell"])         # loaded by hand
    got = await select_turn(store, cfg, reg, cid, 1, "look at https://example.com")
    assert "web" not in got.skills and "shell" in got.skills


async def test_a_loaded_skill_brings_her_note_on_it(store, tmp_path):
    cfg = notes_cfg(tmp_path)
    reg = _registry(cfg, "web")
    await write_notes(store, cfg, {"skills/web.md": "Search down: use DuckDuckGo HTML.",
                                   "skills/shell.md": "Not loaded."})
    cid = await conversations.create(store)
    got = await select_turn(store, cfg, reg, cid, 1, "hello")
    assert "skills/web.md" not in got.loaded_notes
    got = await select_turn(store, cfg, reg, cid, 2, "look at https://example.com")
    assert got.loaded_notes == {"skills/web.md"}
    assert ("note", "skills/web.md") not in got.since, "no row: it follows the skill"
    await loads.unload(store, cid, "skill", "web")
    got = await loads.select(store, cfg, reg, cid, BACKEND, when="step")
    assert got.loaded_notes == frozenset()
