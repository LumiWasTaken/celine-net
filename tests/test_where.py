"""Where she is (conversations/here.py where), in the system prompt from ids alone: her
kind and address, the place, the device, the model role, who started her, who she
reports to and her depth in the chain of agents."""
from __future__ import annotations

import pytest

from celine.conversations import here, store as conversations

pytestmark = pytest.mark.anyio

DEPTH = " (0 is a conversation of your own, each sub-agent one more than the one that " \
    "started it)."


async def _where(store, cid, role=None):
    return (await store.read(lambda c: here.where(c, cid, role))).split("\n")


async def test_a_chat_a_mission_a_sub_agent_a_sub_sub_agent_and_a_drone(store, monkeypatch):
    chat = await conversations.create(store, "Groceries")
    assert await _where(store, chat, "chat") == [
        f"Where you are: a chat (address {chat}).", "Place: chat.",
        "Device: the house (the app's own server).", "Model role: chat.",
        "Started by: {{username}}.", "You report to: {{username}}.", "Depth: 0" + DEPTH]

    mission = await conversations.create(store, "Prices", kind="mission")
    await store.write(lambda c: c.execute(
        "INSERT INTO missions(conversation_id, name, terminal, status, created_at, "
        "updated_at, started_by) VALUES(?, 'Prices', 't', 'active', "
        "'2026-10-22T00:00:00+00:00', '2026-10-22T00:00:00+00:00', ?)", (mission, chat)))
    assert await _where(store, mission, "tasks") == [
        f"Where you are: a mission (address {mission}).", "Place: mission.",
        "Device: the house (the app's own server).", "Model role: tasks.",
        f"Started by: a chat (address {chat}).", f"You report to: a chat (address {chat}).",
        "Depth: 0" + DEPTH]

    scout = await conversations.create(store, "s", agent_of=mission, agent_name="scout")
    assert await _where(store, scout, "tasks") == [
        f"Where you are: the sub-agent scout (address {scout}).",
        "Place: mission, as a sub-agent.", "Device: the house (the app's own server).",
        "Model role: tasks.", f"Started by: a mission (address {mission}).",
        f"You report to: a mission (address {mission}).", "Depth: 1" + DEPTH]

    pup = await conversations.create(store, "p", agent_of=scout, agent_name="pup")
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET agent_cli='opencode' WHERE id=?", (pup,)))
    assert await _where(store, pup) == [
        f"Where you are: the sub-agent pup (address {pup}).",
        "Place: mission, as a sub-agent.", "Device: your box, through opencode.",
        f"Started by: the sub-agent scout (address {scout}).",
        f"You report to: the sub-agent scout (address {scout}).", "Depth: 2" + DEPTH]

    drone = await conversations.create(store, "d", agent_of=scout, agent_name="arcade",
                                       agent_cli="opencode", agent_device="mac")
    assert (await _where(store, drone))[2] == "Device: mac, through opencode."
    from types import SimpleNamespace
    from celine import plugins
    monkeypatch.setitem(plugins.LOADED, "mac", SimpleNamespace(
        manifest={"label": "Mac plugin", "drones": {"label": "Her Mac"}}))
    assert (await _where(store, drone))[2] == "Device: Her Mac, through opencode."


async def test_titles_never_reach_it(store):
    chat = await conversations.create(store, "Groceries")
    before = await _where(store, chat)
    await conversations.update(store, chat, title="Shopping")
    assert await _where(store, chat) == before
