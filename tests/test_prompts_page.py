"""Settings, Prompts: every text a model reads, listed; an edit is read before the shipped
file, kept in its History, and Use the default goes back. The Context tab's rows open
their text."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from celine import boot
from celine import prompts as package_texts
from celine.settings import prompts
from celine.conversations import store as conversations
from celine.guard import prompts as guard_prompts
from celine.turns import title
from compaction_fixtures import Model
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


def test_the_catalog_covers_every_text_file_of_the_package_and_every_judge():
    files = {package_texts.id_of(p) for p in package_texts.PACKAGE.rglob("*.md")}
    assert files == set(package_texts.APP) | {k for k in prompts.editable() if k.startswith("skills.guides.")}
    judges = {package_texts.id_of(p) for p in package_texts.GUARD.glob("*.md")} - {
        "guard.bastet", "guard.talk", "guard.note"}
    assert judges == set(prompts.JUDGES)
    assert all(prompts.path_of(pid).is_file() for pid in prompts.editable())


@pytest.fixture
async def api(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    from celine.api import prompts as prompts_api, texts as texts_api
    app.include_router(prompts_api.router)
    app.include_router(texts_api.router)
    app.state.services.dispatcher = SimpleNamespace(
        notify=lambda: None, backend_factory=lambda: Model(window=65_536),
        bound=lambda cid, **_: Model(window=65_536), llm=None, _tg=None)
    async with client(app) as c:
        c.cfg, c.store = cfg, store
        yield c


async def test_the_list_names_its_groups_and_fills_the_name(api):
    got = (await api.get("/api/prompts")).json()
    assert [g["label"] for g in got["groups"]] == [api.cfg.name, "App", "Skill guides", "Guard"]
    persona = got["groups"][0]["items"][0]
    assert persona["href"] == "/settings/agent/edit/persona" and not persona["edited"]
    writer = next(i for i in got["groups"][1]["items"] if i["id"] == "skills.writer")
    assert api.cfg.name in writer["about"] and "{{" not in writer["about"]
    assert got["help"] == prompts.HELP
    assert not any(i["edited"] for g in got["groups"] for i in g["items"]), "a fresh install"
    bastet = [i["name"] for i in got["groups"][3]["items"][:3]]
    assert bastet == ["Persona", f"Replies to {api.cfg.name}", "Messages to you"], "its editors' titles"


async def test_a_guide_says_what_its_frontmatter_says(api):
    from celine.skills import Guide
    got = (await api.get("/api/prompts/skills.guides.web")).json()
    assert got["about"] == api.cfg.substitute(Guide("web").description) and "{{" not in got["about"]


async def test_an_edit_is_read_first_kept_in_history_and_the_default_comes_back(api):
    shipped = prompts.shipped("turns.title")
    r = await api.put("/api/prompts/turns.title", json={"text": "Name it in one word."})
    assert r.status_code == 200 and r.json()["edited"] and r.json()["text"] == "Name it in one word."
    assert package_texts.text(title.__file__, "title.md") == "Name it in one word."
    history = (await api.get("/api/text-versions", params={"of": "prompt:turns.title"})).json()
    assert history["total"] == 2, "the shipped text is the first version"
    back = (await api.put("/api/prompts/turns.title", json={"text": None})).json()
    assert back["text"] == shipped and not back["edited"]
    assert package_texts.text(title.__file__, "title.md") == shipped
    assert not package_texts.edited_path("turns.title").exists()


async def test_a_judge_edit_reaches_the_guard(api):
    mine = prompts.shipped("guard.hijack") + "\nCheck {{name}} twice."
    assert (await api.put("/api/prompts/guard.hijack", json={"text": mine})).status_code == 200
    assert guard_prompts.text("hijack") == mine
    await api.put("/api/prompts/guard.hijack", json={"text": None})
    assert not guard_prompts.text("hijack").startswith("<!--")


async def test_no_path_reaches_outside_the_catalog(api):
    for pid in ("..", "..%2Fsettings%2Fproviders", "turns.nope", "agent.persona"):
        assert (await api.get(f"/api/prompts/{pid}")).status_code == 404
    assert (await api.put("/api/prompts/..", json={"text": "x"})).status_code == 404


async def test_an_empty_text_is_refused(api):
    r = await api.put("/api/prompts/turns.title", json={"text": "  "})
    assert r.status_code == 400 and r.json()["error"]["detail"]["field"] == "text"


async def test_a_judge_keeps_the_slots_its_shipped_text_has(api):
    shipped = prompts.shipped("guard.reader")
    assert prompts.slots("guard.reader") == ["line", "rule", "text"]
    about = (await api.get("/api/prompts/guard.reader")).json()["about"]
    assert about.endswith("Variables: {line}, {rule} and {text}.")
    for bad, says in ((shipped + " {", "A single { or }"),
                      (shipped.replace("{line}", "{lines}"), "{lines} is not a variable"),
                      (shipped.replace("{line}", ""), "must keep the variable {line}")):
        r = await api.put("/api/prompts/guard.reader", json={"text": bad})
        assert r.status_code == 400 and says in r.json()["error"]["message"], says
    assert not package_texts.edited_path("guard.reader").exists()
    ok = shipped + "\nA brace is written {{."
    assert (await api.put("/api/prompts/guard.reader", json={"text": ok})).status_code == 200


async def test_a_guide_keeps_its_name(api):
    from celine.skills import frontmatter
    shipped = prompts.shipped("skills.guides.web")
    r = await api.put("/api/prompts/skills.guides.web",
                      json={"text": shipped.replace("name: web", "name: search", 1)})
    assert r.status_code == 400 and "name: web" in r.json()["error"]["message"]
    assert not package_texts.edited_path("skills.guides.web").exists()
    assert all(frontmatter(prompts.shipped(k))[0]["name"] == k.removeprefix(prompts.GUIDE)
               for k in prompts.editable() if k.startswith(prompts.GUIDE))


async def test_the_shipped_text_saved_again_is_the_default(api):
    shipped = prompts.shipped("turns.title")
    await api.put("/api/prompts/turns.title", json={"text": "Name it in one word."})
    back = (await api.put("/api/prompts/turns.title", json={"text": shipped + "\n"})).json()
    assert not back["edited"] and not package_texts.edited_path("turns.title").exists()


async def test_the_agents_and_bastets_rows_say_edited_from_their_own_stores(api):
    async def flags():
        groups = (await api.get("/api/prompts")).json()["groups"]
        return {i["id"] for g in (groups[0], groups[3]) for i in g["items"] if i["edited"]}
    assert await flags() == set()
    r = await api.put("/api/agent", json={"persona": "Someone else.", "talk": "hi.",
                                          "voice": {"banned": [" delve"]}})
    assert r.status_code == 200
    r = await api.put("/api/guard/texts/note", json={"text": "Short notes."})
    assert r.status_code == 200
    assert await flags() == {"agent.persona", "agent.talk", "agent.banned", "bastet.note"}


async def test_a_save_drops_the_texts_problem_and_names_its_entry(api):
    from celine.turns import compact
    await api.put("/api/prompts/turns.compact", json={"text": "Sum up in {{wordz}} words."})
    package_texts.fill(api.cfg, compact.__file__, "compact.md", words=100)
    assert [str(p) for p in api.cfg.problems if "nothing fills" in p.message] == \
        ["Prompts, Chat summary: nothing fills {{wordz}}"]
    await api.put("/api/prompts/turns.compact", json={"text": "Sum up in {{words}} words."})
    assert [p for p in api.cfg.problems if "nothing fills" in p.message] == []


async def test_a_context_row_opens_its_text_as_it_goes_out(api):
    cid = await conversations.create(api.store)
    persona = (await api.get(f"/api/conversations/{cid}/context/persona")).json()
    assert persona["name"] == "persona" and persona["text"].strip()
    tools = (await api.get(f"/api/conversations/{cid}/context/tools")).json()
    assert tools["text"].strip()
    assert (await api.get(f"/api/conversations/{cid}/context/nope")).status_code == 404
    assert (await api.get("/api/conversations/nope/context/persona")).status_code == 404
