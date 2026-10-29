"""Skill places and the note layer (turns/loads.py, skills/places.py)."""
from __future__ import annotations

import pytest

from notes_fixtures import notes_cfg, select_turn, write_notes

from celine.conversations import store as conversations
from celine.skills import Guide, Registry, ToolResult, tool
from celine.skills.meta import Skills

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


