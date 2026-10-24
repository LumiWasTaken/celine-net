"""Scheduled messages over HTTP: the page sets one as the user, lists them with their
state and cancels one."""
from __future__ import annotations

import anyio
import httpx
import pytest
from fastapi import FastAPI

from celine.conversations import store as conversations
from celine.core.services import Services
from celine.errors import install_error_handler
from celine.boot import load
from celine.core.bus import EventBus
from celine.core.store import Store
from celine.intercom import Intercom
from celine.intercom import api as intercom_api

pytestmark = pytest.mark.anyio


@pytest.fixture
async def c(db_path):
    async with anyio.create_task_group() as tg:
        store = Store(db_path, bus=EventBus())
        await store.start(tg)
        app = FastAPI()
        app.state.services = Services()
        install_error_handler(app)
        app.include_router(intercom_api.router)
        app.state.services.store, app.state.services.cfg = store, load()
        app.state.services.intercom = Intercom(store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            client.store = store
            yield client
        await store.aclose()


async def test_set_list_cancel_and_refuse(c):
    cid = await conversations.create(c.store, "Shell work")
    to = f"agent:{cid}"
    r = await c.post("/api/intercom", json={"to": to, "in": "30 min",
                                            "text": "  Try the shellbox again  "})
    assert r.status_code == 200, r.text
    iid = r.json()["intercom_id"]
    assert r.json()["message_id"] is None, "it lands later"
    await c.post("/api/intercom", json={"to": to, "at": "08:00", "text": "x"})
    assert (await c.post(f"/api/intercom/{iid}/stop")).status_code == 200
    assert (await c.post(f"/api/intercom/{iid}/stop")).status_code == 404
    left = (await c.get("/api/intercom", params={"to": to, "scheduled": "true"})).json()
    assert {(x["text"], x["from"], x["state"]) for x in left["messages"]} == {
        ("x", "user", "pending"), ("Try the shellbox again", "user", "cancelled")}
    for body in ({"in": "soon", "text": "x"}, {"in": "5 min", "text": " "}):
        r = await c.post("/api/intercom", json={"to": to, **body})
        assert r.status_code == 400 and r.json()["error"]["code"]
    r = await c.post("/api/intercom", json={"to": "agent:nope", "in": "5 min", "text": "x"})
    assert r.status_code == 404
