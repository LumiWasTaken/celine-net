"""api/attachments.py, api/skills.py and conversations/api.py over HTTP: uploaded files
are served so nothing can run on the app's origin, bad requests get their status, the
conversation list loses nothing."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from celine import attachments
from celine.core.services import Services
from celine.api import attachments as attachments_api, skills as skills_api
from celine.errors import install_error_handler
from celine.conversations import api as conversations_api, store as conversations, tree
from celine.boot import load

pytestmark = pytest.mark.anyio


@pytest.fixture
async def c(store):
    app = FastAPI()
    app.state.services = Services()
    install_error_handler(app)
    app.include_router(attachments_api.router)
    app.include_router(skills_api.router)
    app.include_router(conversations_api.router)
    app.state.services.store, app.state.services.cfg = store, load()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        client.cfg = app.state.services.cfg
        yield client


async def test_a_file_comes_back_under_its_own_name_and_type(c, store):
    async def stored(data: bytes, media_type: str, name: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        p = attachments.blob_path(c.cfg, sha)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return (await attachments.add(store, sha, len(data), media_type, name, "binary"))["id"]

    # a page or an svg is never shown on this origin: a sandboxed download
    for aid in (await stored(b"<script>alert(1)</script>", "text/html", "page.html"),
                await stored(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml",
                             "pic.svg")):
        r = await c.get(f"/api/attachments/{aid}/file")
        assert r.headers["content-disposition"].startswith("attachment;")
        assert "sandbox" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
    # the name it gave it, and its type, not the blob's hash
    aid = await stored(b"PK\x03\x04zip", "application/zip", "silly.zip")
    r = await c.get(f"/api/attachments/{aid}/file")
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == 'attachment; filename="silly.zip"'
    aid = await stored(b"%PDF-1.4", "application/pdf", "fünny time.pdf")
    r = await c.get(f"/api/attachments/{aid}/file")
    assert r.headers["content-disposition"] == \
        "attachment; filename*=utf-8''f%C3%BCnny%20time.pdf"
    # an image shows inline, and downloads under its name
    aid = await stored(b"\x89PNG\r\n\x1a\nfake", "image/png", "cat.png")
    r = await c.get(f"/api/attachments/{aid}/file")
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"] == 'inline; filename="cat.png"'
    assert "sandbox" in r.headers["content-security-policy"]
    r = await c.get(f"/api/attachments/{aid}/file?download=1")
    assert r.headers["content-disposition"] == 'attachment; filename="cat.png"'
    assert (await c.get("/api/attachments/nope/file")).status_code == 404


async def test_bad_targets_and_unknown_conversations_are_refused(c, store):
    cid = await conversations.create(store)
    r = await c.put(f"/api/conversations/{cid}/tool-settings/chat_archive__search",
                    json={"enabled": False, "client_seq": 1})
    assert r.json()["applied"] is True
    assert (await c.put(f"/api/conversations/{cid}/tool-settings/a b",
                        json={"enabled": False})).status_code == 400
    assert (await c.put("/api/conversations/nope/tool-settings/archive",
                        json={"enabled": False})).status_code == 404
    assert (await c.patch(f"/api/conversations/{cid}", json={})).json() == {"ok": True}
    assert (await c.patch("/api/conversations/nope", json={})).status_code == 404
    assert (await c.get("/api/conversations", params={"cursor": "nonsense"})).status_code == 400


async def test_the_conversation_list_pages_through_everything(c, store):
    ids = [await conversations.create(store, f"c{i}") for i in range(7)]
    await conversations.update(store, ids[3], pinned=True)
    # several with the same updated_at: the id keeps the order stable across pages
    await store.write(lambda db: db.execute(
        "UPDATE conversations SET updated_at = '2026-01-01T00:00:00' WHERE id IN (?, ?, ?)",
        ids[:3]))
    whole = (await c.get("/api/conversations")).json()["conversations"]
    paged, cursor = [], None
    while True:
        body = (await c.get("/api/conversations",
                            params={"limit": 2, **({"cursor": cursor} if cursor else {})})).json()
        paged += body["conversations"]
        if (cursor := body["next"]) is None:
            break
    assert [r["id"] for r in paged] == [r["id"] for r in whole] and len(whole) == 7
    assert paged[0]["id"] == ids[3], "pinned first"


async def test_archive_hides_from_the_list_keeps_everything_and_a_message_brings_it_back(
        c, store):
    from chat_fixtures import user_says
    from celine.core.bus import EventBus
    from celine.turns import store as turns
    store.bus = EventBus()
    _, feed = store.bus.subscribe("global")
    cid = await conversations.create(store, "trip")
    sub = await conversations.create(store, "scout", agent_of=cid, agent_name="scout")
    other = await conversations.create(store, "other")
    _, tid, _ = await user_says(store, cid, "plan the trip to Lisbon")
    assert (await c.post(f"/api/conversations/{cid}/archive")).status_code == 409
    await turns.transition(store, tid, cid, "done")

    r = await c.post(f"/api/conversations/{sub}/archive")      # a sub-agent: its family
    assert r.status_code == 200 and r.json()["archived_at"]
    listed = (await c.get("/api/conversations")).json()["conversations"]
    assert [x["id"] for x in listed] == [other] and listed[0]["archived_at"] is None
    archived = (await c.get("/api/conversations?archived=true")).json()["conversations"]
    assert [x["id"] for x in archived] == [cid] and archived[0]["archived_at"]
    assert (await c.get(f"/api/conversations/{cid}")).json()["conversation"]["archived_at"]
    assert [h["conversation_id"] for h in await conversations.search(store, "Lisbon")] \
        == [cid], "its search still finds it"
    assert (await c.post("/api/conversations/nope/archive")).status_code == 404

    await user_says(store, sub, "and the hotels?")              # a message brings it back
    assert [x["id"] for x in (await c.get("/api/conversations")).json()[
        "conversations"]][0] == cid
    assert (await c.get(f"/api/conversations/{sub}")).json()["conversation"][
        "archived_at"] is None
    seen = []
    while True:
        try:
            seen.append(feed.receive_nowait())
        except Exception:
            break
    said = [(e["type"], e["id"]) for e in seen if e["type"].startswith("conversation.")
            and "archived" in e["type"]]
    assert said == [("conversation.archived", cid), ("conversation.unarchived", cid)]



async def test_a_replys_raw_response_is_kept_and_served(c, store):
    from chat_fixtures import user_says
    from fake_backend import FakeBackend
    from celine.backends.openai import OpenAIBackend
    from celine.core.bus import EventBus
    from celine.turns import store as turns
    from celine.turns.cancel import Token
    from celine.turns.runner import run_turn
    sse = ('data: {"id":"g1","model":"m","provider":"P","choices":[{"delta":'
           '{"reasoning":"hmm"}}]}\n\n'
           'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
           '"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\ndata: [DONE]\n\n')
    events = [e async for e in OpenAIBackend._parse(SimpleNamespace(cfg=SimpleNamespace(label="gpu"), model="m"),
                                                 httpx.Response(200, text=sse))]
    cid = await conversations.create(store)
    await user_says(store, cid, "hi")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, c.cfg, FakeBackend(events), EventBus(), Token(turn["id"]))
    snap = (await c.get(f"/api/conversations/{cid}")).json()
    mid = [m for m in snap["messages"] if m["role"] == "assistant"][-1]["id"]
    raw = (await c.get(f"/api/conversations/{cid}/messages/{mid}/raw")).json()["raw"]
    assert raw["provider"] == "P" and raw["usage"]["completion_tokens"] == 1
    assert raw["message"]["content"] == "" and raw["message"]["reasoning"] == "hmm"
    assert raw["finish_reason"] == "stop" and raw["last_chunks"][-1] == "[DONE]"
    yours = [m for m in snap["messages"] if m["role"] == "user"][-1]["id"]
    assert (await c.get(f"/api/conversations/{cid}/messages/{yours}/raw")).json() == {
        "raw": None}, "nothing kept: no 404"
    r = await c.get(f"/api/conversations/{cid}/messages/nope/raw")
    assert r.status_code == 404


async def test_every_request_a_reply_sent_is_kept_and_served(c, store):
    """turns/store.record_usage keeps the request with the numbers; the route gives
    the steps back, bodies as they went out."""
    from chat_fixtures import user_says
    from celine.turns import store as turns
    cid = await conversations.create(store)
    await user_says(store, cid, "hi")
    turn = await turns.claim(store, "w")
    mid = await tree.start_assistant_message(store, cid, turn["root_message_id"], turn["id"])
    body = {"model": "m", "messages": [{"role": "system", "content": "be nice"},
                                       {"role": "user", "content": "hi"}],
            "tools": [], "logit_bias": [["\u2014", -100.0]]}
    for step in (0, 1):
        await turns.record_usage(store, cid, turn["id"], {
            "message_id": mid, "iteration": step, "estimate": 40, "prompt_tokens": 42,
            "completion_tokens": 7, "window": 65536, "finish_reason": "stop"},
            request={"provider": "swap", "model": "m", "body": body})
    got = (await c.get(f"/api/conversations/{cid}/messages/{mid}/requests")).json()["requests"]
    assert [r["step"] for r in got] == [0, 1]
    assert got[0]["body"] == body and got[0]["model"] == "m" and got[0]["provider"] == "swap"
    assert got[1]["prompt_tokens"] == 42 and got[1]["window"] == 65536
    assert (await c.get(f"/api/conversations/{cid}/messages/nope/requests")).json() == \
            {"requests": [], "guard": []}


async def test_a_conversation_is_a_chat_of_a_kind_fixed_at_creation(c, store):
    from celine.conversations import store as conversations
    cid = (await c.post("/api/conversations", json={"title": "one"})).json()["id"]
    plain = (await c.post("/api/conversations")).json()
    snap = (await c.get(f"/api/conversations/{cid}")).json()["conversation"]
    assert snap["kind"] == "chat"
    r = await c.patch(f"/api/conversations/{cid}", json={"kind": "mission"})
    assert r.status_code == 400, "the kind is fixed at creation"
    # a background task is not in the chat list
    await conversations.for_trigger(store, "schedule:morning", "Morning")
    rows = (await c.get("/api/conversations")).json()["conversations"]
    assert {r["id"]: r["kind"] for r in rows} == {cid: "chat", plain["id"]: "chat"}


async def test_her_last_line_impersonate_and_a_refused_continue(c, store):
    """The list's rows carry the newest message's first line; Impersonate writes the
    user's line without sending it; Continue on a message that is not its own says why."""
    from types import SimpleNamespace
    from chat_fixtures import user_says
    from celine.backends.base import Done, TextDelta
    from fake_backend import FakeBackend

    app = c._transport.app
    sent = []

    class Writer(FakeBackend):
        async def stream(self, messages, tools, body=None):
            sent.append(messages)
            yield TextDelta(f"{c.cfg.user}: and then we went home")
            yield Done(reason="stop")

    app.state.services.dispatcher = SimpleNamespace(backend_factory=Writer, notify=lambda: None)
    cid = await conversations.create(store)
    rows = (await c.get("/api/conversations")).json()["conversations"]
    assert rows[0]["last_line"] == ""
    umid, tid, _ = await user_says(store, cid, "\n  first line here\nsecond " + "x" * 200)
    rows = (await c.get("/api/conversations")).json()["conversations"]
    assert rows[0]["last_line"] == "first line here"
    await store.write(lambda db: db.execute("UPDATE turns SET state='done'"))
    hers = await store.write(lambda db: tree.append(db, store, cid, umid, "assistant",
                                                    "y" * 300, "model"))
    rows = (await c.get("/api/conversations")).json()["conversations"]
    assert rows[0]["last_line"] == "y" * 120

    r = await c.post(f"/api/conversations/{cid}/impersonate")
    assert r.json() == {"text": "and then we went home"}, "the echoed label goes"
    system, user = sent[0]
    assert system["content"].startswith(f"Write {c.cfg.user}'s next message")
    assert "first line here" in user["content"], "the talk: the user's lines and hers"
    assert user["content"].endswith(f"{c.cfg.name}: {'y' * 300}")
    # what the user typed is where the message starts
    r = await c.post(f"/api/conversations/{cid}/impersonate", json={"start": "*waves*"})
    assert r.json() == {"text": "*waves* and then we went home"}
    assert sent[1][0]["content"].endswith("*waves*"), "quoted when not prefilled"

    class Prefill(Writer):
        supports_prefill = True

        async def stream(self, messages, tools, body=None):
            sent.append(messages)
            yield TextDelta(" and pulls")
            yield Done(reason="stop")

    app.state.services.dispatcher = SimpleNamespace(backend_factory=Prefill, notify=lambda: None)
    r = await c.post(f"/api/conversations/{cid}/impersonate", json={"start": "I stay"})
    assert r.json() == {"text": "I stay and pulls"}
    assert sent[2][-1] == {"role": "assistant", "content": "I stay"}, "prefilled"
    assert (await c.post("/api/conversations/nope/impersonate")).status_code == 404
    shown = await store.read(lambda db: db.execute(
        "SELECT count(*) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0])
    assert shown == 2, "nothing is sent"

    r = await c.post(f"/api/conversations/{cid}/messages/{umid}/continue")
    assert r.status_code == 200
    assert r.json() == {"refused": "Only Celine's replies can be continued."}
    r = await c.post(f"/api/conversations/{cid}/messages/{hers['id']}/continue")
    assert r.json()["turn_id"] and r.json()["message_id"] != hers["id"]
    assert (await c.post(f"/api/conversations/{cid}/messages/nope/continue")).status_code == 404

    r = await c.post(f"/api/conversations/{cid}/messages/{hers['id']}/versions",
                     json={"text": "edited"})
    assert r.status_code == 409, "a turn is queued"
    feed = await store.read(lambda db: [(r["type"], json.loads(r["data"])) for r in db.execute(
        "SELECT type, data FROM global_events ORDER BY id")])
    assert next(d for t, d in feed if t == "conversation.created")["last_line"] == ""
    assert any(t == "version.changed" and d["conversation"] == cid for t, d in feed), \
        "a row's last line can change without a turn: the pages hear it"
