"""Her agent templates (sub_agents/templates.py): notes in templates/ in the shape of
Claude Code's and opencode's agent files; a sub-agent started from one gets its body as
the start of a lean system prompt and only its skills. Her own system prompt keeps its
stable start when she writes an always note (turns/prompt.py system_blocks)."""
from __future__ import annotations

import pytest

from notes_fixtures import write_notes
from settings_fixtures import make_app

from celine.boot import load
from celine.conversations import store as conversations
from celine.sub_agents import templates
from celine.sub_agents.skill import SubAgents
from celine.turns import loads
from celine.turns.prompt import system_blocks

pytestmark = pytest.mark.anyio

SCOUT = """Last changed: 2026-10-30 09:00
---
name: Web scout
description: Finds facts on the web and cites them.
model: local/small
access: read
tools:
  - web
  - notebook
---
You look things up on the web. Cite every fact with its link."""


def test_a_template_reads_like_an_agent_file():
    t = templates.parse(SCOUT)
    assert (t.name, t.description, t.model, t.access, t.device) == (
        "Web scout", "Finds facts on the web and cites them.", "local/small", "read", None)
    assert t.tools == ["web", "notebook"]
    assert t.body == "You look things up on the web. Cite every fact with its link."
    assert templates.parse("---\nname: x\ntools: web, shell\n---\nbody").tools == \
        ["web", "shell"]
    assert templates.parse("no frontmatter") is None


async def _prompt(cfg, reg, store, cid) -> str:
    carried = await loads.select(store, cfg, reg, cid, None)
    return "\n\n".join(t for _, t in await system_blocks(cfg, reg, store, cid, "", carried))


async def test_a_sub_agent_from_a_template_gets_its_body_and_only_its_skills(store):
    cfg = load()
    reg = make_app(cfg, store).state.services.registry
    await write_notes(store, cfg, {"templates/scout.md": SCOUT})
    chat = await conversations.create(store, "chat")
    sub = await conversations.create(store, agent_of=chat, agent_name="scout one",
                                      access="read", agent_template="Web scout")
    text = await _prompt(cfg, reg, store, sub)
    assert text.startswith("You look things up on the web.")
    assert "You are scout one, a sub-agent" in text
    assert cfg.substitute(cfg.her.persona).strip()[:40] not in text
    assert set(await loads.offered(reg, store, sub)) <= {"web", "notebook", *templates.ALWAYS}
    plain = await conversations.create(store, agent_of=chat, agent_name="plain one")
    assert (await _prompt(cfg, reg, store, plain)).startswith("You are plain one, a sub-agent")
    assert "- Web scout: Finds facts on the web and cites them." in templates.listing(cfg)


async def test_an_unknown_template_is_refused_with_the_ones_she_has(store):
    cfg = load()
    await write_notes(store, cfg, {"templates/scout.md": SCOUT})
    chat = await conversations.create(store, "chat")
    _, refusal, _, _ = await SubAgents(cfg).begin(store, chat, "x", "a name", template="nope")
    assert refusal == "There is no agent template 'nope' in templates/. Yours: Web scout."


async def test_her_prompt_is_stable_and_a_note_write_changes_only_its_end(store):
    cfg = load()
    reg = make_app(cfg, store).state.services.registry
    await write_notes(store, cfg, {"style.md": "load: always\n\nShort sentences."})
    chat = await conversations.create(store, "chat")
    first = await _prompt(cfg, reg, store, chat)
    assert await _prompt(cfg, reg, store, chat) == first, "byte-identical when nothing changed"
    await write_notes(store, cfg, {"style.md": "load: always\n\nLong sentences."})
    second = await _prompt(cfg, reg, store, chat)
    assert second != first
    at = first.index("[your notes]")
    assert second[:at] == first[:at], "everything before the notes stays"
    assert "Long sentences." in second[at:]
