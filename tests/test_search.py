"""GET /api/search: the words found in the agent's chats, Missions, the Notebook and the
Intercom, each place with its count and its results' server-built links."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from chat_fixtures import user_says
from celine.conversations import store as conversations
from celine.core.services import Services
from celine.errors import install_error_handler
from celine.intercom import store as rows
from celine.missions import store as missions
from celine import search as search_api
from celine.skills import notebook_index

pytestmark = pytest.mark.anyio


async def test_every_place_finds_its_words_with_a_count_and_a_link(store):
    chat = await conversations.create(store, "Beach day")
    await user_says(store, chat, "when are the tides low")
    mission = await conversations.create(store, "Tide tables", kind="mission")
    await missions.create(store, mission, {"name": "Tide tables", "terminal": "tides",
                                           "brief": "g"})
    await user_says(store, mission, "collect the tides for May")
    await notebook_index.upsert(store, "sea/tides.md", "a note", 20, "spring tides are high")
    agent = await conversations.create(store)
    await store.write(lambda c: rows.insert(c, f"agent:{agent}", "user", "message",
                                            "the tides turned"))
    app = FastAPI()
    app.state.services = Services()
    install_error_handler(app)
    app.include_router(search_api.router)
    app.state.services.store = store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as http:
        found = (await http.get("/api/search", params={"q": "tides"})).json()["places"]
        by = {p["place"]: p for p in found}
        assert [p["place"] for p in found] == ["celine", "missions", "notebook", "intercom"]
        assert by["celine"]["results"][0]["link"] == f"/chat/{chat}"
        assert "<mark>tides</mark>" in by["celine"]["results"][0]["snippet"]
        assert by["missions"]["results"][0] | {"time": None, "snippet": None} == {
            "title": "Tide tables", "detail": None, "snippet": None, "time": None,
            "link": f"/mission/{mission}"}
        assert by["notebook"]["results"][0]["link"] == "/notebook?note=sea/tides.md"
        assert by["intercom"]["count"] == 1
        assert by["intercom"]["results"][0]["link"].startswith("/intercom/")
        only = (await http.get("/api/search", params={"q": "tides", "places": "notebook"})).json()
        assert [p["place"] for p in only["places"]] == ["notebook"]
        # an archive operator narrows the Messages archive alone
        assert (await http.get("/api/search", params={"q": "tides has:photo"})).json() == \
            {"places": []}
