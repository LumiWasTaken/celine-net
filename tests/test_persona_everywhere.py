"""All of it in every kind of turn: its persona, the legend, its talk (and the voice cue
it names) in the system prompt of a chat, a bridged chat, a mission, a sub-agent, a
schedule's run and a trigger's task (prompt.system_blocks, the one assembly), and its
Banned words on every call of its turns (settings/voice.of)."""
from __future__ import annotations

import pytest

from celine.boot import load
from celine.conversations import store as conversations
from celine.settings import voice
from celine.turns import loads
from celine.turns.prompt import system_blocks

pytestmark = pytest.mark.anyio


async def _kinds(store) -> dict[str, str]:
    chat = await conversations.create(store, "chat")
    return {
        "chat": chat,
        "bridge": await conversations.create(store, trigger_key="bridge:whatsapp"),
        "mission": await conversations.create(store, kind="mission"),
        "agent": await conversations.create(store, agent_of=chat, agent_name="scout"),
        "run": await conversations.create(store, trigger_key="run:" + "a" * 32 + ":1",
                                          kind="background"),
        "trigger": await conversations.create(store, trigger_key="trigger:mail",
                                              kind="background"),
    }


async def test_every_kind_gets_her_persona_talk_and_legend(store):
    cfg = load()
    cfg.her.talk = "how I talk.\n\n## how I sound\nloud, and emojis when it fits."
    for kind, cid in (await _kinds(store)).items():
        carried = await loads.select(store, cfg, None, cid, None)
        blocks = await system_blocks(cfg, None, store, cid, "", carried)
        text = "\n\n".join(t for _, t in blocks)
        assert text.startswith(f"You are {cfg.name}."), kind
        assert "You have your own taste." in text, kind
        assert "How the app marks what it adds" in text, kind
        assert "[how you sound]" in text, kind
        assert ("talk", "how I talk.\n\n## how I sound\nloud, and emojis when it fits.") \
            in blocks, kind


async def test_her_banned_words_go_with_every_turn_and_the_writer(store):
    cfg = load()
    assert voice.of(cfg, "chat") is cfg.her.voice      # its turns, any kind
    assert voice.of(cfg, "writer") is cfg.her.voice
    for role in ("tasks", "small", "vision"):
        assert voice.of(cfg, role) is None, role
