"""A skill's knobs (`KNOBS` beside its code, skills.knob): Settings, Advanced lists them
under the skill's label, var/settings/general.toml keeps them in a table per skill, and
`cfg.r("<skill>.<knob>")` reads them at use. A skill whose module is gone takes its knobs,
and any value saved for them, with it."""
from __future__ import annotations

import tomllib

import pytest

from celine import boot, skills
from celine.settings import general
from celine.settings.files import Invalid
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


@pytest.fixture
async def api(tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    async with client(make_app(cfg, store)) as c:
        c.var, c.cfg = var, cfg
        yield c


@pytest.fixture
def fresh_fields():
    general.schema.cache_clear()
    yield
    general.schema.cache_clear()


async def test_every_skills_knobs_are_advanced_fields_under_its_label(api):
    fields = {f["key"]: f for f in (await api.get("/api/settings")).json()["fields"]}
    web = fields["web.results"]
    assert web["advanced"] and web["group"] == "Web" and web["default"] == 8
    assert fields["chat_archive.rerank_floor"]["group"] == "Messages"
    assert fields["web.page_max_mb"]["unit"] == fields["web.pdf_max_mb"]["unit"] == "MB"
    assert {k.partition(".")[0] for k in fields if "." in k} == {
        "notebook", "past_conversations", "sub_agents", "code", "chat_archive", "web"}
    assert "group" not in fields["max_concurrent_turns"], "the app's own: no skill"


async def test_a_knob_is_saved_in_its_skills_table_checked_and_read_at_once(api):
    assert api.cfg.r("web.results") == 8
    r = await api.put("/api/settings", json={"general": {
        "web.results": 3, "chat_archive.rerank_floor": -2.5}})
    assert r.status_code == 200, r.text
    assert api.cfg.r("web.results") == 3 and api.cfg.r("chat_archive.rerank_floor") == -2.5
    saved = tomllib.loads((api.var / "settings" / "general.toml").read_text())
    assert saved["web"] == {"results": 3} and saved["chat_archive"] == {"rerank_floor": -2.5}
    for bad in ({"web.results": 99}, {"web.resutls": 1}, {"web.results": 2.5}):
        r = await api.put("/api/settings", json={"general": bad})
        assert r.status_code == 400, bad
    assert boot.load().settings.value("web.results") == 3, "read back from its table"


def test_a_skill_whose_module_is_gone_takes_its_knobs_and_values_with_it(monkeypatch,
                                                                       fresh_fields):
    assert "web.results" in {f["key"] for f in general.fields()}
    gone = tuple((n, "web_was_here" if n == "web" else m, c) for n, m, c in skills.BUILTIN)
    monkeypatch.setattr(skills, "BUILTIN", gone)
    general.schema.cache_clear()
    assert skills.module("web_was_here") is None
    assert not any(f["key"].startswith("web.") for f in general.fields())
    assert general.validate({"web": {"results": 3}, "code.poll_seconds": 5}) == \
        {"code.poll_seconds": 5}, "its saved values go with it"
    with pytest.raises(Invalid):
        general.validate({"code.nonsense": 5})
