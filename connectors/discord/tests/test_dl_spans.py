"""Smart reads: the id spans of a chat the journal already holds are answered from it,
and only the gaps are asked of Discord. Fake Discord only."""
from __future__ import annotations

import pytest

from conftest import CLIENTS, ME, T0, msg, sid, tick_dms
from discord_link import policy, spans, sync
from discord_link.errors import NO_CTX
from discord_link.gateway import Writer
from discord_link.link import Link, parse_clients
from discord_link.state import State
from discord_link.tools import build

pytestmark = pytest.mark.anyio

IDS = [sid(T0 + i * 60_000) for i in range(200)]


def chat(discord, cid="4001", n=200):
    discord.add_channel({"id": cid, "type": 1, "last_message_id": IDS[n - 1], "recipients": [
        {"id": "U2", "username": "u2", "global_name": "U2"}]},
        [msg(IDS[i], cid, "U2", f"m{i}") for i in range(n)])


def pages(discord, since: int, cid="4001") -> list[dict]:
    """The message pages asked of Discord since request `since`."""
    return [q for _, p, q in discord.requests[since:] if p == f"/channels/{cid}/messages"]


async def call(link, name, **args) -> dict:
    res = await build(link).call_tool(name, args)
    assert not res.is_error, res.content[0].text
    return res.structured_content


def texts(st) -> list[str]:
    return [r["message"]["text"] for r in st["records"]]


def m(a: int, b: int) -> list[str]:
    return [f"m{i}" for i in range(a, b)]


async def test_a_second_read_of_an_archived_span_asks_discord_nothing(link, discord, state):
    chat(discord)
    tick_dms(link.rest, state)
    n = len(discord.requests)
    first = await call(link, "read_messages", chat="4001", before=IDS[150], limit=50)
    assert texts(first) == m(100, 150) and len(pages(discord, n)) == 1
    n = len(discord.requests)
    again = await call(link, "read_messages", chat="4001", before=IDS[150], limit=50)
    assert len(discord.requests) == n                      # not one request
    assert texts(again) == texts(first)
    assert [r["seq"] for r in again["records"]] == [r["seq"] for r in first["records"]]
    assert again["earlier"] == first["earlier"] == {"chat": "4001", "before": IDS[100]}
    assert again["later"] == first["later"] == {"chat": "4001", "after": IDS[149]}
    inside = await call(link, "read_around_message", chat="4001", message=IDS[125], limit=20)
    assert texts(inside) == m(115, 135) and len(discord.requests) == n
    up = await call(link, "read_messages", chat="4001", after=IDS[109], limit=20)
    assert texts(up) == m(110, 130) and len(discord.requests) == n


async def test_a_read_that_overlaps_fetches_only_the_gap(link, discord, state):
    chat(discord)
    tick_dms(link.rest, state)
    await call(link, "read_messages", chat="4001", before=IDS[150], limit=50)
    n = len(discord.requests)
    down = await call(link, "read_messages", chat="4001", before=IDS[150], limit=80)
    assert texts(down) == m(70, 150)
    assert pages(discord, n) == [{"before": IDS[100], "limit": "30"}]
    n = len(discord.requests)
    up = await call(link, "read_messages", chat="4001", after=IDS[119], limit=50)
    assert texts(up) == m(120, 170)
    assert pages(discord, n) == [{"after": str(int(IDS[150]) - 1), "limit": "20"}]
    assert state.spans("4001") == [(int(IDS[70]), int(IDS[169]))]
    # around a message at a span's edge: one request, as before
    n = len(discord.requests)
    edge = await call(link, "read_around_message", chat="4001", message=IDS[172], limit=10)
    assert texts(edge) == m(167, 177) and len(pages(discord, n)) == 1
    assert state.spans("4001") == [(int(IDS[70]), int(IDS[176]))]


def test_spans_merge(state):
    state.cover("C", 10, 20)
    state.cover("C", 30, 40)
    state.cover("C", 50, 60)
    assert state.spans("C") == [(10, 20), (30, 40), (50, 60)]
    state.cover("C", 21, 29)                               # adjacent on both sides
    assert state.spans("C") == [(10, 40), (50, 60)]
    state.cover("C", 5, 12)                                # overlapping
    state.cover("C", 35, 55)                               # bridging
    assert state.spans("C") == [(5, 60)]
    state.cover("C", 7, 9)                                 # inside
    state.cover("C", 62, 70)                               # one id apart: a gap
    assert state.spans("C") == [(5, 60), (62, 70)]
    assert state.spans("other") == []


async def test_reads_merge_into_one_span(link, discord, state):
    chat(discord)
    tick_dms(link.rest, state)
    await call(link, "read_messages", chat="4001", before=IDS[50], limit=20)
    await call(link, "read_messages", chat="4001", before=IDS[100], limit=20)
    assert state.spans("4001") == [(int(IDS[30]), int(IDS[50]) - 1),
                                 (int(IDS[80]), int(IDS[100]) - 1)]
    n = len(discord.requests)
    both = await call(link, "read_messages", chat="4001", before=IDS[100], limit=70)
    assert texts(both) == m(30, 100)
    assert pages(discord, n) == [{"before": IDS[80], "limit": "50"}]
    assert state.spans("4001") == [(int(IDS[30]), int(IDS[100]) - 1)]
    # a page shorter than asked reached the start of the chat
    await call(link, "read_messages", chat="4001", before=IDS[30], limit=50)
    assert state.spans("4001") == [(0, int(IDS[100]) - 1)]
    n = len(discord.requests)
    start = await call(link, "read_messages", chat="4001", before=IDS[20], limit=50)
    assert texts(start) == m(0, 20) and len(discord.requests) == n
    assert "earlier" not in start


async def test_a_restart_keeps_the_spans(link, discord, state, tmp_path, clock):
    chat(discord)
    tick_dms(link.rest, state)
    first = await call(link, "read_messages", chat="4001", before=IDS[150], limit=50)
    link.rest.close()
    state.close()
    again = State(tmp_path / "link")
    lk = Link(again, clients=parse_clients(CLIENTS), interval=0,
              transport=discord.transport(), sleep=lambda s: None, clock=clock)
    try:
        assert again.spans("4001") == [(int(IDS[100]), int(IDS[150]) - 1)]
        n = len(discord.requests)
        st = await call(lk, "read_messages", chat="4001", before=IDS[150], limit=50)
        assert len(discord.requests) == n and texts(st) == texts(first)
    finally:
        lk.rest.close()
        again.close()


async def test_a_chat_left_out_forgets_its_spans(link, discord, state):
    chat(discord)
    tick_dms(link.rest, state)
    await call(link, "read_messages", chat="4001", limit=20)
    assert state.spans("4001")
    sync.set_targets(state, {"4001": False})
    assert state.spans("4001") == []
    state.cover("4001", 1, 2)                 # a page that lands after: nothing is kept
    assert state.spans("4001") == []


async def test_edits_and_deletes_show_in_what_the_journal_answers(link, discord, state):
    chat(discord)
    tick_dms(link.rest, state)
    await call(link, "read_messages", chat="4001", before=IDS[150], limit=50)
    w = writer(link)
    w.apply("MESSAGE_UPDATE", {**msg(IDS[120], "4001", "U2", "m120 fixed"),
                               "edited_timestamp": "2026-09-24T10:00:00+00:00"})
    w.apply("MESSAGE_DELETE", {"id": IDS[121], "channel_id": "4001"})
    w.apply("MESSAGE_DELETE_BULK", {"ids": [IDS[122], IDS[123]], "channel_id": "4001"})
    n = len(discord.requests)
    st = await call(link, "read_messages", chat="4001", before=IDS[125], limit=5)
    assert len(discord.requests) == n
    assert texts(st) == ["m117", "m118", "m119", "m120 fixed", "m124"]
    assert st["records"][3]["message"]["edited"] is True


def writer(link) -> Writer:
    gw = link.gateway
    gw.me_id = ME
    gw.subs = policy.active(link.state)
    gw.writer = Writer(gw)
    return gw.writer


async def test_the_sync_covers_and_live_messages_extend_the_newest_span(link, discord, state):
    chat(discord)
    tick_dms(link.rest, state)
    sync.sync(link.rest, NO_CTX)
    assert state.spans("4001") == [(0, int(IDS[199]))]
    # without the live connection nobody knows what came since: the newest page is asked
    n = len(discord.requests)
    st = await call(link, "read_messages", chat="4001", limit=10)
    assert texts(st) == m(190, 200) and len(pages(discord, n)) == 1
    # connected: the session starts at the DM's last message, so what comes live joins it
    w = writer(link)
    link.gateway.state_name = "connected"
    w.apply("session", {"4001": IDS[199]})
    new = [sid(T0 + 10 ** 8 + i) for i in range(3)]
    for i, mid in enumerate(new):
        discord.messages["4001"].append(msg(mid, "4001", "U2", f"live {i}"))
        w.apply("MESSAGE_CREATE", msg(mid, "4001", "U2", f"live {i}"))
    assert state.spans("4001") == [(0, int(new[-1]))]
    n = len(discord.requests)
    st = await call(link, "read_messages", chat="4001", limit=5)
    assert texts(st) == ["m198", "m199", "live 0", "live 1", "live 2"]
    assert len(discord.requests) == n
    assert (await call(link, "read_messages", chat="4001", after=new[0], limit=5))["records"]
    assert len(discord.requests) == n
    # a new session that did not see what came in between starts a new span
    w.apply("session", {})
    late = sid(T0 + 2 * 10 ** 8)
    discord.messages["4001"].append(msg(late, "4001", "U2", "after a drop"))
    w.apply("MESSAGE_CREATE", msg(late, "4001", "U2", "after a drop"))
    assert state.spans("4001") == [(0, int(new[-1])), (int(late), int(late))]
    n = len(discord.requests)
    st = await call(link, "read_messages", chat="4001", limit=3)
    assert texts(st) == ["live 1", "live 2", "after a drop"]
    assert pages(discord, n) == [{"before": late, "limit": "2"}]
    assert state.spans("4001") == [(0, int(late))]
    # not connected: the newest page is asked again
    link.gateway.state_name = "reconnecting"
    n = len(discord.requests)
    await call(link, "read_messages", chat="4001", limit=3)
    assert len(pages(discord, n)) == 1


def test_a_message_the_sync_leaves_out_splits_the_span(link, discord, state):
    discord.guilds = [{"id": "G1", "name": "Club", "icon": None}]
    discord.guild_channels["G1"] = [{"id": "4002", "name": "general", "type": 0}]
    bot = {"id": "BOT", "username": "b", "bot": True}
    discord.add_channel({"id": "4002", "type": 0, "guild_id": "G1", "name": "general"},
                        [msg(IDS[i], "4002", bot if i == 5 else "U2", f"m{i}")
                         for i in range(10)])
    policy.put(state, "G1", settings={"channels": ["4002"], "backfill": "all"},
               guild_name="Club")
    policy.start(state, "G1")
    sync.backfill_guild(link.rest, "G1", NO_CTX)
    assert state.spans("4002") == [(0, int(IDS[5]) - 1), (int(IDS[5]) + 1, int(IDS[9]))]


async def test_a_tool_fills_the_hole_the_sync_left(link, discord, state):
    test_a_message_the_sync_leaves_out_splits_the_span(link, discord, state)
    n = len(discord.requests)
    st = await call(link, "read_messages", chat="4002", before=IDS[8], limit=6)
    assert texts(st) == m(2, 8)                       # the bot's message too
    assert pages(discord, n, "4002") == [{"before": str(int(IDS[5]) + 1), "limit": "4"}]
    assert state.spans("4002") == [(0, int(IDS[9]))]


def test_a_page_never_covers_ids_that_do_not_exist_yet(state):
    future = str(int(IDS[199]) + (10 ** 6 << 22))
    page = [msg(IDS[i], "4001") for i in range(190, 200)]
    state.learn_channel("4001", kind="dm", last_message_id=IDS[199], enabled=1)
    spans.covered(state, "4001", {"before": future, "limit": 10}, page)
    assert state.spans("4001") == [(int(IDS[190]), int(IDS[199]))]
