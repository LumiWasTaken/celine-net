"""What is included: a DM once the user ticks it, a server by its channel include list, and a
chat the user left out (unticked or deleted from its chat header) is never read
again by the sync, the gateway or a tool, and the feed skips it. Included again, it is
read from the start. Fake Discord only."""
from __future__ import annotations

import asyncio
import json

import pytest

from conftest import ME, OWNER, READER, T0, msg, sid, tick_dms
from discord_link import policy, sync
from discord_link.app import feed_lines
from discord_link.errors import NO_CTX
from discord_link.gateway import Gateway, Writer
from test_dl_doors import Served

NOW = T0 + 10 ** 9


def texts(state, chat=None):
    return [r["message"]["text"] for r in state.after(0, 100_000)
            if r["kind"] == "message" and (chat is None or r["chat"]["key"] == chat)]


async def fed(link, after=0) -> list[dict]:
    """What the feed hands an app instance from `after` up to the journal's head."""
    out: list[dict] = []
    head = link.state.head
    if head <= after:
        return out
    async for line in feed_lines(link, after, heartbeat=0.05, poll=0.01):
        if not line.strip():
            break
        r = json.loads(line)
        out.append(r)
        if r["seq"] >= head:
            break
    return out


def writer(state) -> Writer:
    gw = Gateway(state)
    gw.me_id = ME
    gw.subs = policy.active(state)
    return Writer(gw)


def dm(discord, cid, peer, n):
    discord.add_channel({"id": cid, "type": 1, "recipients": [
        {"id": peer, "username": peer.lower(), "global_name": peer.title()}]},
        [msg(sid(T0 + i * 60_000), cid, peer, f"{cid} {i}") for i in range(n)])


def server(discord):
    discord.guilds = [{"id": "G1", "name": "Chess Club", "icon": None}]
    discord.guild_channels["G1"] = [
        {"id": "CAT", "name": "Talk", "type": 4},
        {"id": "C1", "name": "general", "type": 0, "parent_id": "CAT"},
        {"id": "C3", "name": "memes", "type": 0, "parent_id": "CAT"}]
    for cid in ("C1", "C3"):
        discord.add_channel({"id": cid, "type": 0, "guild_id": "G1", "name": cid.lower()},
                            [msg(sid(NOW - 1000 + i), cid, "U2", f"{cid} {i}")
                             for i in range(3)])


# ---- DMs: only the ones the user ticks --------------------------------------------------------

def test_a_new_dm_waits_for_a_tick(link, discord, state):
    dm(discord, "D1", "U2", 2)
    out = sync.sync(link.rest, NO_CTX)
    assert out["new_dms"] == 1 and state.channel("D1")["enabled"] == 0
    assert texts(state, "D1") == []
    # a DM that starts while connected: learned, not read
    writer(state).apply("MESSAGE_CREATE", msg(sid(NOW), "D7", "U7", "hello"))
    assert state.channel("D7")["enabled"] == 0 and texts(state, "D7") == []
    # ticked: the next sync reads it whole
    assert sync.set_targets(state, {"D1": True}) == (1, ["D1"])
    sync.sync(link.rest, NO_CTX)
    assert len(texts(state, "D1")) == 2
    assert {d["id"]: d["sync"] for d in sync.targets(state)["dms"]} == {
        "D1": True, "D7": False}


# ---- leaving a DM out, and including it again -------------------------------------------------

@pytest.mark.anyio
async def test_a_dm_left_out_is_never_read_and_comes_back_whole(link, discord, state):
    dm(discord, "D1", "U2", 3)
    dm(discord, "2002", "U3", 2)
    tick_dms(link.rest, state)
    sync.sync(link.rest, NO_CTX)
    assert len(await fed(link)) and texts(state, "2002") == ["2002 0", "2002 1"]

    assert sync.set_targets(state, {"2002": False}) == (1, [])
    # the feed no longer hands out what the journal has of it
    assert not [r for r in await fed(link) if r.get("chat", {}).get("key") == "2002"
                or r.get("chat_key") == "2002"]
    # the sync skips it, the gateway skips it, a tool's search result is not kept
    head = state.head
    discord.messages["2002"].append(msg(sid(NOW), "2002", "U3", "later"))
    sync.sync(link.rest, NO_CTX)
    w = writer(state)
    w.apply("MESSAGE_CREATE", msg(sid(NOW + 1), "2002", "U3", "live"))
    w.apply("MESSAGE_DELETE", {"id": sid(NOW + 1), "channel_id": "2002"})
    assert state.journal([{"kind": "message", "chat": {"key": "2002", "kind": "dm"},
                           "message": {"key": "x", "ts": NOW, "text": "tool"}}]) == []
    assert state.head == head
    # a tool does not read it either, and asks Discord nothing
    n = len(discord.requests)
    async with Served(link) as srv:
        async with srv.mcp(OWNER) as c:
            got = await c.call_tool("read_messages", {"chat": "2002"})
    assert got.is_error and "does not include this chat" in got.content[0].text
    assert len(discord.requests) == n
    # its cursor is gone: including it again reads it from the start
    assert state.one("SELECT 1 FROM cursors WHERE chat_key = '2002'") is None
    changed, back = sync.set_targets(state, {"2002": True})
    assert (changed, back) == (1, ["2002"])
    sync.sync(link.rest, NO_CTX, keys=set(back))
    again = [r for r in await fed(link, head) if r["kind"] == "message"]
    assert [r["message"]["text"] for r in again] == ["2002 0", "2002 1", "later"]
    assert all(r["chat"]["key"] == "2002" for r in again)


# ---- server channels: the include list ------------------------------------------------------

def test_only_ticked_channels_are_synced_and_new_ones_wait(link, discord, state):
    server(discord)
    assert policy.DEFAULTS["channels"] == []
    first = sync.guild_channels(link.rest, "G1")
    assert [(c["id"], c["category"], c["sync"], c["new"]) for c in first] == [
        ("C1", "Talk", False, False), ("C3", "Talk", False, False)]
    policy.put(state, "G1", settings={"channels": ["C1"], "backfill": "all"})
    policy.start(state, "G1")
    link.reload_subs()
    sync.backfill_guild(link.rest, "G1", NO_CTX)
    assert texts(state) == ["C1 0", "C1 1", "C1 2"]
    # a channel that appears later is not synced, and the pop-up shows it as new
    discord.guild_channels["G1"].append({"id": "C9", "name": "new-one", "type": 0,
                                         "parent_id": "CAT"})
    discord.add_channel({"id": "C9", "type": 0, "guild_id": "G1", "name": "new-one"},
                        [msg(sid(NOW), "C9", "U2", "C9 0")])
    chans = {c["id"]: c for c in sync.guild_channels(link.rest, "G1")}
    assert chans["C9"]["new"] and not chans["C9"]["sync"] and not chans["C3"]["new"]
    sync.sync(link.rest, NO_CTX)
    w = writer(state)
    w.apply("MESSAGE_CREATE", {**msg(sid(NOW + 5), "C9", "U2", "live in c9"), "guild_id": "G1"})
    assert "C9 0" not in texts(state) and "live in c9" not in texts(state)
    # saving the list from the pop-up: nothing is new any more
    policy.put(state, "G1", settings={"channels": ["C1"]})
    assert not any(c["new"] for c in sync.guild_channels(link.rest, "G1"))


@pytest.mark.anyio
async def test_a_channel_left_out_from_its_chat_and_included_again(link, discord, state):
    server(discord)
    sync.guild_channels(link.rest, "G1")
    policy.put(state, "G1", settings={"channels": ["C1", "C3"], "backfill": "all"})
    policy.start(state, "G1")
    link.reload_subs()
    sync.backfill_guild(link.rest, "G1", NO_CTX)
    assert len(texts(state, "C3")) == 3

    assert sync.set_targets(state, {"C3": False}) == (1, [])
    link.reload_subs()
    assert policy.get(state, "G1")["settings"]["channels"] == ["C1"]
    assert state.is_ignored("C3")
    head = state.head
    w = writer(state)
    w.apply("MESSAGE_CREATE", {**msg(sid(NOW + 9), "C3", "U2", "live"), "guild_id": "G1"})
    discord.messages["C3"].append(msg(sid(NOW + 10), "C3", "U2", "later"))
    sync.sync(link.rest, NO_CTX)
    assert state.head == head
    assert not [r for r in await fed(link) if r.get("chat", {}).get("key") == "C3"]
    # ticked again (the chat header's reverse, or the pop-up): back on the list, read whole
    changed, back = sync.set_targets(state, {"C3": True})
    assert (changed, back) == (1, ["C3"])
    assert policy.get(state, "G1")["settings"]["channels"] == ["C1", "C3"]
    link.reload_subs()
    sync.sync(link.rest, NO_CTX, keys=set(back))
    assert [r["message"]["text"] for r in await fed(link, head) if r["kind"] == "message"] \
        == ["C3 0", "C3 1", "C3 2", "later"]
    # ticking it in the pop-up takes it off the ignore list too
    sync.set_targets(state, {"C3": False})
    policy.put(state, "G1", settings={"channels": ["C1", "C3"]})
    assert not state.is_ignored("C3")


# ---- the door ------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_targets_door_leaves_out_and_includes_again(link, discord, state):
    dm(discord, "2002", "U3", 2)
    tick_dms(link.rest, state)
    sync.sync(link.rest, NO_CTX)
    async with Served(link) as srv:
        async with srv.http(READER) as h:
            assert (await h.put("/admin/targets", json={"set": {"2002": False}})).status_code == 403
        async with srv.http(OWNER) as h:
            r = await h.put("/admin/targets", json={"set": {"2002": False}})
            assert r.json() == {"changed": 1, "job": None}
            r = await h.put("/admin/targets", json={"set": {"2002": "no"}})
            assert r.status_code == 400
            r = (await h.put("/admin/targets", json={"set": {"2002": True}})).json()
            assert r["changed"] == 1 and r["job"]
            for _ in range(200):
                j = (await h.get(f"/admin/jobs/{r['job']}")).json()
                if j["state"] != "running":
                    break
                await asyncio.sleep(0.01)
            assert j["state"] == "done"
    assert texts(state, "2002").count("2002 0") == 2       # journaled again, a new seq
