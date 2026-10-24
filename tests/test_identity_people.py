"""People across sources: accounts marked as the same person, with the about text and
aliases you write, over HTTP and in search. Fixture data only."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from celine.api import identity as api
from celine.core.services import Services
from celine.errors import CelineError
from celine.kb import identity as ident

from kb_fixtures import build

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _everyone_listed(monkeypatch):
    """The fixture people write fewer messages than the people lists want."""
    from celine.kb import chat
    monkeypatch.setattr(chat, "LISTED_MIN_MESSAGES", 0)


async def test_mark_write_about_and_separate_over_http(tmp_path, store):
    app = FastAPI()
    app.state.services = Services()
    app.include_router(api.router)
    app.state.services.kbs = build(tmp_path)
    app.state.services.store = store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        people = (await c.get("/api/people", params={"q": "alex"})).json()["people"]
        refs = [a["ref"] for p in people for a in p["accounts"]
                if a["handle"] in ("+49 170 0000001", "@alex")]
        out = (await c.post("/api/people",
                            json={"refs": refs, "name": "Alex"})).json()["person"]
        assert out["members"] == ["discord:1", "whatsapp:1"]
        r = await c.patch("/api/people/discord:1", json={"name": "Alexander",
                                                   "about": "school friend",
                                                   "aliases": ["Xander"]})
        assert r.json()["person"]["name"] == "Alexander"
        people = (await c.get("/api/people", params={"q": "xander"})).json()["people"]
        assert [(p["name"], p["about"], len(p["accounts"])) for p in people] == [
            ("Alexander", "school friend", 2)]
        assert (await c.post("/api/people",
                             json={"refs": ["whatsapp:3"], "name": "B"})).status_code == 422
        with pytest.raises(CelineError):
            await c.delete("/api/people/whatsapp:3/link")
        r = await c.delete("/api/people/discord:1/link")
        assert r.json() == {"person": out["id"], "deleted": False}      # the about stays
    rec = (await ident.snapshot(store)).identity_of("whatsapp:1")
    assert rec.about == "school friend" and rec.members == {"whatsapp:1"}


async def test_search_follows_a_person_and_its_aliases(tmp_path, store):
    kbs = build(tmp_path)
    await store.write(lambda c: ident.link_sync(c, ["whatsapp:1", "discord:1"], name="Sam"))
    await store.write(lambda c: ident.set_person_sync(c, "discord:1", aliases=["Sammy"]))
    ids = await ident.snapshot(store)
    for q in ("from:Sam", "from:sam"):
        res = kbs.search(q, identities=ids)
        assert {h["kb"] for h in res["hits"]} == {"whatsapp", "discord"} and res["total"] == 5
    for ref in ("whatsapp:1", "discord:1"):
        assert kbs.search("chess", person=ref, identities=ids)["total"] == 2
    assert kbs.search("from:sam")["total"] == 0                       # no records, no alias
