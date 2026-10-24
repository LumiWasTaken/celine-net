"""The chat bridge with fakes only: which messages are prompts, the lease, delivery in
parts (every part, no limit on prompts), what stays in the app, its messages to the user,
its text in WhatsApp's syntax, and no prompt lost at start."""
from __future__ import annotations

from chat_fixtures import user_says

from types import SimpleNamespace

import anyio
import pytest

from celine.conversations import store as conversations
from celine.bridge import service as bmod
from celine.intercom import Intercom
from celine.bridge import Bridge
from celine.core.services import Services
from celine.kb.chat import ChatKB
from celine.kb.feed import Consumer

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg():
    from celine.boot import load
    return load()

BRIDGE_CHAT = "4917012345678@s.whatsapp.net"
T0 = 1_758_700_000_000


@pytest.fixture(autouse=True)
def switched_on_at_t0(monkeypatch):
    """The bridge's clock: switching on happens at T0, messages are written after it."""
    monkeypatch.setattr(bmod, "_now", lambda: T0 / 1000)


class FakeConnector:
    def __init__(self, kb):
        self.kb, self.server, self.title, self.on_batch = kb, kb.id, kb.title, None
        self.info = {"write_chat": BRIDGE_CHAT, "marker": "Celine: ", "part_chars": 3000}
        self.lease_ok = True

    async def request(self, method, path, body=None, timeout=15.0):
        if path == "/admin/bridge/lease" and method == "POST" and not self.lease_ok:
            return 409, {"error": "Another instance holds the bridge."}
        return 200, {}


async def _world(store, tmp_path, on=True, kb=None):
    kid = "whatsapp"
    kb = kb or ChatKB(id=kid, title="WhatsApp", platform=kid, path=tmp_path / "w.db")
    conn, sent = FakeConnector(kb), []

    async def call(server, tool, args):
        sent.append((server, tool, args))
        return SimpleNamespace(content=[], structured_content={"sent": [str(len(sent))]})
    b = Bridge(store, SimpleNamespace(all={kid: conn}), call, Intercom(store),
               sleep=lambda s: anyio.sleep(0.001), resolve=lambda refs: {r: None for r in refs})
    w = SimpleNamespace(conn=conn, sent=sent, bridge=b, store=store, side=b.sides[kid])
    if on:
        b.set_on(kid, True)
        await b.lease(w.side)
    return w


@pytest.fixture
async def world(store, tmp_path):
    return await _world(store, tmp_path)


def rec(seq, text, *, chat=BRIDGE_CHAT, from_me=True, from_link=False, key=None, ts=None):
    m = {"key": key or f"m{seq}", "ts": ts or T0 + seq, "from_me": from_me, "type": "text",
         "text": text}
    m.update({"from_link": True} if from_link else {})
    m.update({} if from_me else {"sender": {"key": "U2"}})
    return {"seq": seq, "kind": "message",
            "chat": {"key": chat, "kind": "group", "name": "Bridge"}, "message": m}


async def feed(w, records):
    Consumer(w.side.kb).apply(records)
    await w.bridge.read(w.side)


async def prompts(store):
    return await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT t.id, m.content FROM turns t JOIN messages m ON m.id = t.root_message_id")])


async def test_only_a_new_message_of_yours_in_the_bridged_chat_is_a_prompt(store, tmp_path):
    w = await _world(store, tmp_path, on=False)
    await feed(w, [rec(1, "asked before the bridge was on")])
    assert await prompts(store) == [], "off by default"
    w.bridge.set_on("whatsapp", True)
    await w.bridge.lease(w.side)
    await feed(w, [rec(1, "asked before the bridge was on")])          # a replay
    await feed(w, [rec(2, "asked before the bridge was on", key="m1")])  # journaled again
    await feed(w, [rec(3, "an old note to myself", ts=T0 - 60_000)])    # older than on
    await feed(w, [rec(4, "someone else", from_me=False),
                   rec(5, "her answer, sent by the link", from_me=False, from_link=True),
                   rec(6, "   "), rec(7, "in another chat", chat="D1"),
                   rec(8, "Celine: her answer")])                # the marker: its own
    await feed(w, [rec(9, "hi Celine"), rec(10, "hi Celine", key="m9")])  # twice: one prompt
    assert [t["content"] for t in await prompts(store)] == ["hi Celine"]


async def test_without_the_lease_nothing_happens(store, tmp_path):
    w = await _world(store, tmp_path, on=False)
    w.bridge.set_on("whatsapp", True)
    w.conn.lease_ok = False
    await w.bridge.lease(w.side)
    assert not w.side.held
    await feed(w, [rec(1, "hi")])
    assert await prompts(store) == []
    assert await w.bridge.deliver(w.side, "x") == [] and w.sent == []


async def test_an_answer_goes_back_once_in_parts_and_app_turns_stay_in_the_app(world):
    w = world
    await feed(w, [rec(1, "where did we talk about the celine?")])
    [t] = await prompts(w.store)
    cid = await conversations.for_trigger(w.store, "bridge:whatsapp", "x")
    answer = "\n\n".join(f"part {i}" + " word" * 500 for i in range(9))
    await w.store.write(lambda c: c.execute(
        "INSERT INTO messages (id, conversation_id, parent_id, role, content, ordinal, "
        "author, created_at, turn_id) VALUES ('a1', ?, NULL, 'assistant', ?, 0, 'model', 1, ?)",
        (cid, answer, t["id"])))
    assert await w.bridge.turn_ended(t["id"], "done", None)
    parts = [a["text"] for _, tool, a in w.sent if tool == "send"]
    # every part, in order, none cut: the whole answer reaches the chat
    assert len(parts) == 9 and all(len(p) <= 3000 for p in parts)
    assert "\n\n".join(parts) == answer
    assert not await w.bridge.turn_ended(t["id"], "done", None), "delivered once"
    _, app_turn, _ = await user_says(w.store, cid, "typed in the app")
    assert not await w.bridge.turn_ended(app_turn, "done", None)


def _her(w, cfg):
    from celine.intercom.skill import IntercomSkill
    skill = IntercomSkill(cfg)
    skill.app = Services(store=w.store, bridge=w.bridge, intercom=Intercom(w.store))
    return skill


async def _send(skill, store, cid, **kw):
    from celine.skills import ToolContext

    async def emit(_):
        return None
    return await skill.send(ToolContext(cid, "t", "c", store, None, emit), **kw)


async def test_her_message_to_the_user_goes_to_the_chat_and_a_quote_reply_joins_it(world, cfg):
    w = world
    cid = await conversations.create(w.store, "typed in the app")
    r = await _send(_her(w, cfg), w.store, cid, to="user", text="the captcha", urgent=True)
    assert not r.is_error and "Also sent to Bridge (WhatsApp)." in r.content
    assert w.sent == [("whatsapp", "send", {"text": "the captcha"})]
    await feed(w, [{**rec(1, "done, solved it"), "message": {
        **rec(1, "")["message"], "text": "done, solved it", "reply_key": "1"}}])
    rows = await w.store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT id, to_addr, reply_to, thread FROM intercom ORDER BY id")])
    assert rows[1]["to_addr"] == f"agent:{cid}" and rows[1]["reply_to"] == rows[0]["id"]


async def test_with_the_bridge_off_urgent_lands_in_the_intercom_only(store, tmp_path, cfg):
    from celine.intercom import store as rows
    w = await _world(store, tmp_path, on=False)
    cid = await conversations.create(store, "x")
    r = await _send(_her(w, cfg), store, cid, to="user", text="help", urgent=True)
    assert not r.is_error and "it went to no chat" in r.content and w.sent == []
    [t] = await store.read(lambda c: rows.threads(c, "user"))
    t = await store.read(lambda c: rows.thread_view(c, t))
    assert t["unread"] == 1 and t["last"]["text"] == "help"


async def test_a_prompt_written_while_the_bridge_starts_becomes_one_message(store, tmp_path):
    w = await _world(store, tmp_path)
    await w.bridge.lease(w.side)
    # a restart: a new bridge, and the feed applies a batch before it runs or leases
    w2 = await _world(store, tmp_path, on=False, kb=w.side.kb)
    w2.bridge.attach()
    Consumer(w2.side.kb, on_batch=w2.side.connector.on_batch).apply([rec(1, "are you there?")])
    assert await prompts(store) == []
    await w2.bridge.lease(w2.side)
    await w2.bridge.lease(w2.side)
    assert [t["content"] for t in await prompts(store)] == ["are you there?"]
    n = await store.read(lambda c: c.execute("SELECT count(*) FROM intercom").fetchone()[0])
    assert n == 1


async def test_images_go_only_to_a_bridged_conversation(world):
    w, png = world, b"\x89PNG\r\n\x1a\nxx"
    manual = await conversations.create(w.store, "typed in the app")
    bridged = await conversations.for_trigger(w.store, "bridge:whatsapp", "Bridge")
    assert await w.bridge.send_image(manual, png, "a.png", "image/png") is False
    assert w.sent == []
    assert await w.bridge.send_image(bridged, png, "a.png", "image/png") is True


async def test_a_command_is_answered_by_the_app_and_never_reaches_her(world, store):
    """`!approve` (celine/approvals.py) goes to the bridge's command, its answer
    back into the chat; a text the command does not know is a prompt as before."""
    seen = []

    async def command(text, by):
        from celine.guard.owner import Owner
        assert isinstance(by, Owner) and by.via == "bridge"
        seen.append(text)
        return "Approved: x." if text.startswith("!approve") else None
    world.bridge.command = command
    await feed(world, [rec(1, "!approve"), rec(2, "!what is this")])
    assert seen == ["!approve", "!what is this"]
    # one prompt: the unknown command reaches it, the approve never does
    assert [t["content"] for t in await prompts(store)] == ["!what is this"]
    assert [a["text"] for _, tool, a in world.sent if tool == "send"] == ["Approved: x."]


async def test_a_continue_sends_only_the_part_it_added(world):
    """Continue on a stopped turn (the same turn) and Continue on its reply (a new turn,
    `again` the reply) send what they added; a Regenerate stays in the app."""
    from celine.conversations import tree
    w = world
    await feed(w, [rec(1, "tell me about the celine")])
    [t] = await prompts(w.store)
    cid = await conversations.for_trigger(w.store, "bridge:whatsapp", "x")

    async def reply(mid, text, tid):
        await w.store.write(lambda c: c.execute(
            "INSERT OR REPLACE INTO messages (id, conversation_id, parent_id, role, content, "
            "ordinal, author, created_at, turn_id) SELECT ?, ?, root_message_id, "
            "'assistant', ?, 0, 'model', 1, ? FROM turns WHERE id = ?",
            (mid, cid, text, tid, tid)))

    def texts():
        out = [a["text"] for _, tool, a in w.sent if tool == "send"]
        w.sent.clear()
        return out

    assert await w.bridge.turn_ended(t["id"], "cancelled", None) and texts() == []
    await reply("a1", "it is a sloop.", t["id"])
    assert await w.bridge.turn_ended(t["id"], "done", None)
    assert texts() == ["it is a sloop."]
    await reply("a1", "it is a sloop. from 1970.", t["id"])
    assert await w.bridge.turn_ended(t["id"], "done", None)
    assert texts() == ["from 1970."]

    async def again(text, mid):
        tid, _ = await w.store.write(lambda c: tree.queue_turn(
            c, w.store, cid, None, again="a1"))
        await reply(mid, text, tid)
        return tid
    tid = await again("it is a sloop. from 1970. a wooden one.", "a2")
    assert await w.bridge.turn_ended(tid, "done", None)
    assert texts() == ["a wooden one."]
    tid = await again("a different answer.", "a3")
    assert not await w.bridge.turn_ended(tid, "done", None) and texts() == []


async def test_every_prompt_is_hers_however_many(world, store):
    """No limit on how many messages make prompts: forty in a row are forty."""
    await feed(world, [rec(i, f"message {i}") for i in range(1, 41)])
    n = await store.read(lambda c: c.execute(
        "SELECT count(*) FROM intercom WHERE from_addr = 'user'").fetchone()[0])
    assert n == 40
    assert world.sent == []


async def test_only_her_final_answer_goes_out(world):
    """What it said between tool calls is no answer; a step-limit stop with no answer
    says so."""
    w = world
    await feed(w, [rec(1, "look it up")])
    [t] = await prompts(w.store)
    cid = await conversations.for_trigger(w.store, "bridge:whatsapp", "x")

    async def reply(mid, text, call=False):
        def run(c):
            c.execute("INSERT INTO messages (id, conversation_id, parent_id, role, content, "
                      "ordinal, author, created_at, turn_id) VALUES (?, ?, NULL, 'assistant', "
                      "?, 0, 'model', ?, ?)", (mid, cid, text, len(mid), t["id"]))
            if call:
                c.execute("INSERT INTO tool_calls (id, message_id, name, arguments, state) "
                          "VALUES (?, ?, 'web__search', '{}', 'ok')", (f"c-{mid}", mid))
        await w.store.write(run)
    await reply("a1", "let me check.", call=True)
    assert await w.bridge.turn_ended(t["id"], "capped", None)
    assert [a["text"] for _, _, a in w.sent] == ["Stopped at the step limit."]
    w.sent.clear()
    await reply("a22", "it opens at nine.")
    assert await w.bridge.turn_ended(t["id"], "done", None)
    assert [a["text"] for _, _, a in w.sent] == ["it opens at nine."]


def test_her_text_in_whatsapps_syntax_with_every_word_kept():
    from celine.bridge.render import render
    text = ("# The **Celine**\nIt is a **sloop**, *fast*, ~~slow~~ and __wide__; "
            "snake_case and 2*3*4 stay.\n* one\n- two\n> a quote\n"
            "[the page](https://a.b/c), [https://x.y](https://x.y), `a*b*c`\n"
            "| boat | year |\n|---|---|\n| celine | 1970 |\n"
            "```python\nx = **1**\n```\n[[Notes/Cars|my cars]] [[whatsapp:12]]")
    assert render(text, lambda refs: {r: None for r in refs}) == (
        "*The Celine*\nIt is a *sloop*, _fast_, ~slow~ and *wide*; "
        "snake_case and 2*3*4 stay.\n* one\n- two\n> a quote\n"
        "the page (https://a.b/c), https://x.y, `a*b*c`\n"
        "boat, year\nceline, 1970\n"
        "```\nx = **1**\n```\nmy cars [1]\n\n[1] whatsapp:12")
