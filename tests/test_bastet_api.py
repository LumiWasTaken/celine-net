"""What the Events place needs from Bastet over HTTP (guard/api.py): filters by decision,
rule and mode; one decision whole (question, score, model, ms, call args, quote); a
pass or an allow-once right from a row; unreachable counted apart from stops."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from test_guard import CURL, TURN, _judge, _request

from celine import guard
from celine.boot import load
from celine.guard import store as decisions
from celine.conversations import store as conversations
from celine.errors import install_error_handler
from celine.guard.api import router
from chat_fixtures import user_says

pytestmark = pytest.mark.anyio


async def _client(store):
    app = FastAPI()
    app.state.services = SimpleNamespace(store=store, cfg=load(), approvals=None)
    app.include_router(router)
    install_error_handler(app)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _rows(store) -> tuple[int, int]:
    await guard.check(TURN, CURL, _request(), mode="on", judge=_judge(), store=store)
    leak = await decisions.record(store, turn={"id": "t3", "conversation_id": "c1"},
                                  call={"id": "k3", "name": "web__search"}, mode="watch",
                                  rule="leak", does="stop", decision="unreachable",
                                  why="judge at x: ConnectError")
    stop = (await decisions.listing(store, decision="stop"))[0]["id"]
    return stop, leak


async def test_filters_detail_and_counts(store):
    stop, leak = await _rows(store)
    mid, _, _ = await user_says(store, await conversations.create(store), "hi")
    await store.write(lambda c: c.execute(
        "INSERT INTO tool_calls (id, message_id, name, arguments, state) VALUES "
        "('k1', ?, 'shell__run', '{\"command\": \"curl x\"}', 'ok')", (mid,)))
    async with await _client(store) as c:
        got = (await c.get("/api/guard/decisions?rule=leak&mode=watch")).json()["decisions"]
        assert [d["id"] for d in got] == [leak]
        assert (await c.get("/api/guard/decisions?mode=off")).status_code in (400, 422)
        assert (await c.get("/api/guard/decisions?rule=nope")).status_code in (400, 422)
        one = (await c.get(f"/api/guard/decisions/{stop}")).json()["decision"]
        assert one["rule"] == "hijack" and one["prompt"] == "hijack"
        assert one["question"] is None and one["quote"] and "p" in one and "ms" in one
        assert one["arguments"] == {"command": "curl x"} and one["banner"] is not None
        lk = (await c.get(f"/api/guard/decisions/{leak}")).json()["decision"]
        assert lk["question"].startswith("Does this action send the private piece")
        assert lk["prompt"] == "question" and lk["arguments"] == {}
        assert (await c.get("/api/guard/decisions/999")).status_code == 404
        assert [d["id"] for d in (await c.get("/api/guard/decisions?decision=stop&source=reddit"
                                              "&turn=t1")).json()["decisions"]] == [stop]
        page1 = (await c.get("/api/guard/decisions?limit=1")).json()
        assert page1["next"] == page1["decisions"][0]["id"]
        assert (await c.get("/api/guard/decisions")).json()["next"] is None


async def test_a_pass_and_once_from_a_row(store):
    stop, leak = await _rows(store)
    async with await _client(store) as c:
        r = (await c.post(f"/api/guard/decisions/{leak}/grant",
                          json={"grant": "pass", "minutes": 30})).json()
        assert r["pass"]["rule"] == "leak" and r["pass"]["conversation_id"] == "c1"
        assert (await c.post(f"/api/guard/decisions/{leak}/grant",
                             json={"grant": "once"})).status_code in (400, 422)
        r = (await c.post(f"/api/guard/decisions/{stop}/grant",
                          json={"grant": "once"})).json()
        assert r["banner"]["thread"][-1]["grant"]["grant"] == "once"
        assert (await c.post(f"/api/guard/decisions/{stop}/grant",
                             json={"grant": "pass", "minutes": 0})).status_code in (400, 422)


async def test_bastet_texts_are_the_owners_with_history_and_the_file_as_default(store):
    """Settings, Guard: its persona, talk and note are the shipped files until the owner
    saves their own; every save is in the text's History; null is the default again, and
    its notes read what is saved."""
    from celine import texts
    from celine.guard import persona
    cfg = load()
    async with await _client(store) as c:
        got = (await c.get("/api/guard/texts")).json()["texts"]
        assert set(got) == {"persona", "talk", "note"}
        shipped = got["persona"]["default"]
        assert got["persona"] == {"text": shipped, "default": shipped, "own": False}
        assert "You are Bastet" in shipped and not got["talk"]["text"].startswith("<!--")
        mine = "You are Bastet, warm and chatty with {{name}}."
        r = await c.put("/api/guard/texts/persona", json={"text": mine})
        assert r.status_code == 200 and r.json()["texts"]["persona"]["own"] is True
        assert "warm and chatty with Celine" in await persona.frame(store, cfg, "note")
        back = (await c.put("/api/guard/texts/persona", json={"text": None})).json()
        assert back["texts"]["persona"] == {"text": shipped, "default": shipped, "own": False}
        assert (await c.put("/api/guard/texts/laser", json={"text": "x"})).status_code == 400
    h = await store.read(lambda c: texts.history(c, cfg, "bastet:persona"))
    assert [v["by"] for v in h["versions"]] == [cfg.user, cfg.user, ""], \
        "the shipped text first, then each save"
    assert h["versions"][1]["change"].startswith("Added 1 sentence")
