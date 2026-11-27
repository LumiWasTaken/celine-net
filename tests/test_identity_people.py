"""People across sources: accounts marked as the same person, how they relate to you, the
notes and aliases kept with them, over HTTP, in search and on the card. Fixture data only."""
from __future__ import annotations

from types import SimpleNamespace

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
        assert out["members"] == ["discord:person:1", "whatsapp:person:1"]
        r = await c.patch("/api/people/discord:person:1", json={
            "name": "Alexander", "relation": "school friend", "aliases": ["Xander"]})
        assert r.json()["person"]["name"] == "Alexander"
        r = await c.post("/api/people/discord:person:1/notes", json={"text": "plays chess"})
        [note] = r.json()["person"]["notes"]
        assert (note["text"], note["author"]) == ("plays chess", "user")
        r = await c.patch(f"/api/people/discord:person:1/notes/{note['id']}",
                          json={"text": "plays chess on Sundays"})
        [note] = r.json()["person"]["notes"]
        assert note["text"] == "plays chess on Sundays"
        people = (await c.get("/api/people", params={"q": "xander"})).json()["people"]
        assert [(p["name"], p["relation"], [n["text"] for n in p["notes"]],
                 len(p["accounts"])) for p in people] == [
            ("Alexander", "school friend", ["plays chess on Sundays"], 2)]
        assert (await c.post("/api/people", json={"refs": ["whatsapp:person:3"],
                                                  "name": "B"})).status_code == 422
        with pytest.raises(CelineError):
            await c.delete("/api/people/whatsapp:person:3/link")
        r = await c.delete("/api/people/discord:person:1/link")
        assert r.json() == {"person": out["id"], "deleted": False}      # what it says stays
        with pytest.raises(CelineError):                 # an old shape is no person
            await c.patch("/api/people/whatsapp:1", json={"relation": "x"})
    rec = (await ident.snapshot(store)).identity_of("whatsapp:person:1")
    assert rec.relation == "school friend" and rec.members == {"whatsapp:person:1"}
    # the reworded note stands; the first wording stays on record
    rows = await store.read(lambda c: c.execute(
        "SELECT text, replaced_at IS NULL FROM person_notes ORDER BY id").fetchall())
    assert [tuple(r) for r in rows] == [("plays chess", 0), ("plays chess on Sundays", 1)]
    # a single account's record goes when it says nothing any more
    await store.write(lambda c: ident.set_person_sync(c, "whatsapp:person:3", relation="x"))
    assert (await ident.snapshot(store)).identity_of("whatsapp:person:3") is not None
    await store.write(lambda c: ident.set_person_sync(c, "whatsapp:person:3", relation=""))
    assert (await ident.snapshot(store)).identity_of("whatsapp:person:3") is None


async def test_search_follows_a_person_and_its_aliases(tmp_path, store):
    kbs = build(tmp_path)
    await store.write(lambda c: ident.link_sync(c, ["whatsapp:person:1", "discord:person:1"], name="Sam"))
    await store.write(lambda c: ident.set_person_sync(c, "discord:person:1", aliases=["Sammy"]))
    ids = await ident.snapshot(store)
    for q in ("from:Sam", "from:sam"):
        res = kbs.search(q, identities=ids)
        assert {h["kb"] for h in res["hits"]} == {"whatsapp", "discord"} and res["total"] == 5
    for ref in ("whatsapp:person:1", "discord:person:1"):
        assert kbs.search("chess", person=ref, identities=ids)["total"] == 2
    assert kbs.search("from:sam")["total"] == 0                       # no records, no alias


async def test_a_person_card_has_every_name_the_profile_the_history_and_the_notes(
        tmp_path, store, monkeypatch):
    """What look_up returns for a person: each name with what it is, the profile, the
    names they had before with when, how they relate to the user, the notes with who
    wrote them, and the direct chat with them, also when only the user wrote in it."""
    from kb_fixtures import archive_skill
    kbs = build(tmp_path)
    dc = kbs.get("discord")
    s = dc.sink()
    s.person("900", display_name="Luna", username="shiniloli", ts=1_700_000_000_000)
    s.person("900", display_name="Moon", username="shiniloli", bio="sleepy\nalways",
             pronouns="she/her", nick="Moony", nick_scope="Chess server",
             ts=1_750_000_000_000)
    dm = s.chat("dm900", kind="dm", person_key="900")
    s.message(dm, "x1", ts=1_750_000_100_000, text="are you there", from_me=True)
    s.close()
    pid = dc.file.read_sync(lambda c: c.execute(
        "SELECT id FROM people WHERE key = '900'").fetchone()[0])
    ref = f"discord:person:{pid}"
    skill = archive_skill(monkeypatch, kbs)
    ctx = SimpleNamespace(turn_id=None, store=store, max_output_tokens=None, emit=None,
                          conversation_id="c")
    out = await skill.write_person(ctx, person=ref, note="moved to Hamburg",
                                   relation="sister", aliases=["Sis"])
    assert not out.is_error and "relation to sam: sister" in out.content
    await store.write(lambda c: ident.note_sync(c, ref, "birthday on 3 May"))
    for name in ("shiniloli", "Moon", "Sis"):
        card = (await skill.look_up(ctx, name=name)).content
        assert f"person {ref} Moon (username shiniloli)" in card, name
        assert "pronouns: she/her" in card and "bio: sleepy" in card
        assert "earlier: display name Luna until 2025-06-15" in card
        assert "nickname on Chess server: Moony" in card
        assert "relation to sam: sister" in card and "also called Sis" in card
        assert ", you: moved to Hamburg" in card and ", sam: birthday on 3 May" in card
        assert f"the direct chat with them: chat discord:chat:{dm} Moon " \
               f"(dm with {ref})" in card, "also when only the user wrote in it"
    assert (await skill.write_person(ctx, person=f"discord:chat:{dm}", note="x")).is_error
    assert (await skill.write_person(ctx, person="Moon", note="x")).is_error


def test_a_deleted_account_keeps_its_last_names(tmp_path):
    kbs = build(tmp_path)
    dc = kbs.get("discord")
    s = dc.sink()
    s.person("901", display_name="Ghost", username="ghosty")
    s.person("901", deleted=True)
    s.close()
    row = dc.file.read_sync(lambda c: c.execute(
        "SELECT display_name, username, deleted FROM people WHERE key = '901'").fetchone())
    assert tuple(row) == ("Ghost", "ghosty", 1)
