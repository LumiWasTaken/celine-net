"""The REST catch-up into the journal, subscriptions, lists, mentions as names. Fake
Discord only."""
from __future__ import annotations

import pytest

from conftest import ME, T0, msg, sid, tick_dms
from discord_link import policy, records, sync
from discord_link.errors import NO_CTX, Unreachable
from discord_link.jobs import UNREACHABLE_RETRY_MS, due_at


def journal(state, kind=None):
    rows = state.after(0, 100000)
    return [r for r in rows if kind is None or r["kind"] == kind]


def dm(discord, cid="D1", peer="U2", n=0):
    msgs = [msg(sid(T0 + i * 60_000), cid, peer if i % 3 else ME, f"message {i}")
            for i in range(n)]
    discord.add_channel({"id": cid, "type": 1, "recipients": [
        {"id": peer, "username": peer.lower(), "global_name": peer.title()}]}, msgs)


def test_backfill_then_only_new(link, discord, state):
    dm(discord, n=250)
    tick_dms(link.rest, state)
    out = sync.sync(link.rest, NO_CTX)
    assert out["channels"] == 1
    got = journal(state, "message")
    assert len(got) == 250
    first = got[0]
    assert first["chat"] == {"key": "D1", "kind": "dm",
                             "peer": {"key": "U2", "display_name": "U2", "username": "u2"}}
    assert first["message"]["from_me"] is True and "sender" not in first["message"]
    assert got[1]["message"]["sender"]["key"] == "U2"
    # the account is a person record marked as me
    assert any(r["person"].get("is_me") for r in journal(state, "person"))
    before = len(discord.requests)
    discord.messages["D1"].append(msg(sid(T0 + 10 ** 8), "D1", "U2", "new one"))
    sync.sync(link.rest, NO_CTX)
    pages = [p for m, p, q in discord.requests[before:] if p == "/channels/D1/messages"]
    assert len(pages) == 1                         # forwards from the cursor only
    assert [r["message"]["text"] for r in journal(state, "message")][-1] == "new one"
    assert len(journal(state, "message")) == 251
    assert state.get("last_sync") and state.get("sync_unreachable_since") is None


def test_reading_twice_journals_once_and_a_changed_text_is_an_edit(link, discord, state):
    dm(discord, n=3)
    tick_dms(link.rest, state)
    sync.sync(link.rest, NO_CTX)
    head = state.head
    state.x("DELETE FROM cursors")                  # read everything again
    sync.sync(link.rest, NO_CTX)
    assert state.head == head
    discord.messages["D1"][1] = {**discord.messages["D1"][1], "content": "changed",
                                 "edited_timestamp": "2026-09-24T10:00:00+00:00"}
    state.x("DELETE FROM cursors")
    sync.sync(link.rest, NO_CTX)
    edits = journal(state, "edit")
    assert len(edits) == 1 and edits[0]["text"] == "changed"


def test_unreachable_keeps_the_cursor_and_retries_in_five_minutes(link, discord, state):
    dm(discord, n=120)
    sync.sync(link.rest, NO_CTX)
    discord.down = True
    with pytest.raises(Unreachable, match="not reachable"):
        sync.sync(link.rest, NO_CTX)
    since = state.get("sync_unreachable_since")
    assert since
    assert link.connection()["state"] in ("unreachable", "connected")
    nxt = link.next_sync()
    assert nxt == state.get("sync_last_attempt") + UNREACHABLE_RETRY_MS
    assert due_at(60_000, last_ok=0, last_attempt=10, unreachable=True) == 60_010
    discord.down = False
    sync.sync(link.rest, NO_CTX)
    assert state.get("sync_unreachable_since") is None


def test_a_rate_limit_is_waited_out(link, discord, state):
    dm(discord, n=2)
    tick_dms(link.rest, state)
    discord.rate_limit_once.add("/users/@me")
    sync.sync(link.rest, NO_CTX)
    assert len(journal(state, "message")) == 2


def test_targets_come_from_the_state(link, discord, state):
    dm(discord, "D1", "U2", n=2)
    dm(discord, "D2", "U3", n=1)
    discord.guilds = [{"id": "G1", "name": "Chess Club", "icon": None}]
    tick_dms(link.rest, state)
    sync.sync(link.rest, NO_CTX)
    n = len(discord.requests)
    t = sync.targets(state)
    assert len(discord.requests) == n                 # no request
    assert {d["id"]: d["messages"] for d in t["dms"]} == {"D1": 2, "D2": 1}
    assert t["guilds"] == [{"id": "G1", "name": "Chess Club", "icon": None,
                           "channels": None}]


def test_settings_are_validated(state):
    sub, grew = policy.put(state, "G1", settings={"backfill": "none", "channels": [111, "222"]})
    assert grew is None and sub["state"] == "draft"
    sub, newly = policy.start(state, "G1")
    assert newly and sub["state"] == "on" and sub["settings"]["channels"] == ["111", "222"]
    for bad in ({"nope": 1}, {"backfill": "1y"}, {"include_bots": "yes"},
                {"channels": "some"}, {"attachment_max_mb": 0}):
        with pytest.raises(policy.SettingsError):
            policy.put(state, "G1", settings=bad)
    _, grew = policy.put(state, "G1", settings={"channels": ["111", "333"]})
    assert grew == ["111", "222"]
    assert policy.unsubscribe(state, "G1") and policy.active(state) == {}


def _server(discord):
    discord.guilds = [{"id": "G1", "name": "Subscribed", "icon": None},
                      {"id": "G2", "name": "Other", "icon": None}]
    now = T0 + 10 ** 9
    discord.guild_channels["G1"] = [{"id": "C1", "name": "general", "type": 0},
                                    {"id": "C3", "name": "later", "type": 0}]
    discord.guild_channels["G2"] = [{"id": "C2", "name": "general", "type": 0}]
    discord.add_channel({"id": "C1", "type": 0, "guild_id": "G1", "name": "general"},
                        [msg(sid(now - 1000), "C1", "U2", "in c1"),
                         msg(sid(now - 900), "C1", {"id": "BOT", "username": "b",
                                                    "bot": True}, "a bot"),
                         msg(sid(now - 800), "C1", "BAD", "blocked person")])
    discord.add_channel({"id": "C3", "type": 0, "guild_id": "G1", "name": "later"},
                        [msg(sid(now - 700), "C3", "U2", "in c3")])
    discord.add_channel({"id": "C2", "type": 0, "guild_id": "G2", "name": "general"},
                        [msg(sid(now - 600), "C2", "U2", "not subscribed")])


def test_only_subscribed_servers_and_their_settings(link, discord, state):
    _server(discord)
    policy.put(state, "G1", settings={"channels": ["C1"], "backfill": "all"},
               guild_name="Subscribed")
    policy.start(state, "G1")
    sync.backfill_guild(link.rest, "G1", NO_CTX)
    texts = [r["message"]["text"] for r in journal(state, "message")]
    assert texts == ["in c1"]                     # no bot, no blocked user, no C3, no G2
    ch = journal(state, "message")[0]["chat"]
    assert ch == {"key": "C1", "kind": "channel", "name": "#general", "parent": "Subscribed"}
    # a channel ticked later gets its history now
    _, before = policy.put(state, "G1", settings={"channels": ["C1", "C3"]})
    out = sync.backfill_channels(link.rest, "G1", before, NO_CTX)
    assert out["channels"] == 1
    assert "in c3" in [r["message"]["text"] for r in journal(state, "message")]
    chans = sync.guild_channels(link.rest, "G1")
    assert {c["id"]: c["sync"] for c in chans} == {"C1": True, "C3": True}
    stats = policy.list_all(state)[0]["stats"]
    assert stats["channels"] == 2 and stats["messages"] == 2
    # the answers to change and start carry the same counts: the settings page
    # shows them right after saving
    changed, _ = policy.put(state, "G1", settings={"channels": ["C1", "C3"]})
    assert changed["stats"] == stats
    assert policy.start(state, "G1")[0]["stats"] == stats


def test_mentions_become_names(state):
    state.learn_channel("77", kind="channel", type=0, name="general", guild_id="1",
                        guild_name="Chess Club")
    m = {"id": "5", "channel_id": "77", "timestamp": "2026-10-09T10:00:00+00:00",
         "author": {"id": "2", "username": "ben", "global_name": "Ben"},
         "content": "<@1> <@!1> and <@9> see <#77> and <#78>",
         "mentions": [{"id": "1", "username": "sam", "global_name": "Sam"}]}
    out = records.message(state, m, me_id=None)
    assert out["message"]["text"] == "@Sam @Sam and <@9> see #general and <#78>"
