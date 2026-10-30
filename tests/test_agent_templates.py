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
    with pytest.raises(ValueError, match="does not start with a --- frontmatter"):
        templates.parse("no frontmatter")


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


def test_the_frontmatter_is_yaml():
    t = templates.parse("---\nname: x\ndescription: |\n  one\n  --- two\ntools: [web, shell]\n"
                        "---\nbody")
    assert (t.description, t.tools, t.body) == ("one\n--- two", ["web", "shell"], "body")
    with pytest.raises(ValueError, match="no closing ---"):
        templates.parse("---\nname: x\n")
    with pytest.raises(ValueError, match="not YAML on line 2"):
        templates.parse("---\nname: x\ndescription: a: b\n---\nbody")
    with pytest.raises(ValueError, match="no name"):
        templates.parse("---\ndescription: d\n---\nbody")


async def test_files_that_are_no_template_and_shared_names_are_said(store):
    cfg = load()
    await write_notes(store, cfg, {"templates/scout.md": SCOUT,
                                   "templates/scout2.md": SCOUT,
                                   "templates/bad.md": "---\nname: x\n",
                                   "templates/ok.md": "---\nname: Ok one\n---\nbody"})
    text = templates.listing(cfg)
    assert "- Ok one" in text
    assert "templates/bad.md is no template: its frontmatter has no closing --- line." in text
    assert ("templates/scout.md and templates/scout2.md share the name 'Web scout', so it "
            "does not start.") in text
    chat = await conversations.create(store, "chat")
    _, refusal, _, _ = await SubAgents(cfg).begin(store, chat, "x", "a name",
                                                  template="Web scout")
    assert refusal.splitlines()[0] == ("There is no agent template 'Web scout' in "
                                       "templates/. Yours: Ok one.")
    assert "templates/bad.md is no template" in refusal


async def test_a_note_without_frontmatter_is_named_and_a_path_finds_its_template(store):
    cfg = load()
    await write_notes(store, cfg, {"templates/digger.md": "# digger\n\nDig.",
                                   "templates/scout.md": SCOUT})
    chat = await conversations.create(store, "chat")
    _, refusal, _, _ = await SubAgents(cfg).begin(store, chat, "x", "a name",
                                                  template="digger")
    assert ("templates/digger.md is no template: it does not start with a --- "
            "frontmatter.") in refusal.splitlines()
    got = templates.all_templates(cfg)
    assert (templates.read_note(cfg.her.notebook, "templates/scout.md") or "").startswith(
        "Last changed:"), "the stamp above the frontmatter is read past"
    for given in ("templates/scout.md", "templates/scout", "scout.md", "web SCOUT"):
        assert templates.pick(got, given).name == "Web scout", given


async def test_tools_that_are_no_skills_are_refused(store):
    cfg = load()
    await write_notes(store, cfg, {"templates/t.md": "---\nname: Typo one\n"
                                                     "tools: web, notbook\n---\nbody"})
    chat = await conversations.create(store, "chat")
    _, refusal, _, _ = await SubAgents(cfg).begin(store, chat, "x", "a name",
                                                  template="Typo one")
    assert refusal.startswith("Typo one's tools name notbook, which is no skill. Skills: ")


async def test_a_resume_keeps_its_model_when_the_template_changed(store):
    from celine.core.bus import EventBus
    from celine.core.services import Services
    from celine.intercom import Intercom
    from celine.skills import Registry
    from celine.sub_agents import report
    from celine.turns.dispatcher import Dispatcher
    cfg = load()
    await write_notes(store, cfg, {"templates/t.md": "---\nname: Plain one\n---\nbody"})
    chat = await conversations.create(store, "chat")
    d = Dispatcher(store, cfg, EventBus(), lambda: None, Registry())
    skill = SubAgents(cfg)
    skill.app = Services(cfg=cfg, store=store, dispatcher=d,
                         intercom=Intercom(store, d.notify))
    first, refusal, _, _ = await skill.begin(store, chat, "x", "the helper",
                                             template="Plain one")
    assert refusal is None
    await store.write(lambda c: c.execute("UPDATE turns SET state='done'"))
    await report.on_end(skill.app, first)
    await write_notes(store, cfg, {"templates/t.md": "---\nname: Plain one\n"
                                                     "model: gone/model\n---\nbody"})
    again, refusal, resumed, _ = await skill.begin(store, chat, "y", "the helper",
                                                   template="Plain one")
    assert (again, refusal, resumed) == (first, None, True)
