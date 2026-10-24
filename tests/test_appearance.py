"""Settings, Appearance: GET answers every key (saved or default), PUT keeps what it was
not given, puts a null key back to its default, refuses what is not a choice, writes var/settings/appearance.toml and sends
settings.changed."""
from __future__ import annotations

import tomllib

import pytest

from celine import boot
from celine.api import appearance as appearance_api
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio

DEFAULTS = {
    "accent": "rose",
    "grain": "fine",
    "grain_opacity": 4,
    "tint": 100,
    "contrast": 100,
    "text_size": "md",
    "hide_thinking": False,
    "hide_tools": False,
    "collapse_thinking": True,
}


@pytest.fixture
async def api(tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    app = make_app(boot.load(), store)
    app.include_router(appearance_api.router)
    events: list = []
    store.emit_global = events.append
    async with client(app) as c:
        c.var, c.events = var, events
        yield c


async def test_defaults_then_a_save_keeps_the_rest_and_tells_every_page(api):
    assert (await api.get("/api/appearance")).json() == {"appearance": DEFAULTS}
    r = await api.put("/api/appearance", json={"appearance": {"accent": "sage", "hide_thinking": True}})
    assert r.status_code == 200
    assert r.json()["appearance"] == {**DEFAULTS, "accent": "sage", "hide_thinking": True}
    look = {"text_size": "xl", "hide_tools": True, "collapse_thinking": False, "grain": "paper", "grain_opacity": 12, "tint": 250, "contrast": 130}
    r = await api.put("/api/appearance", json={"appearance": look})
    assert r.json()["appearance"] == {**DEFAULTS, "accent": "sage", "hide_thinking": True, **look}
    saved = tomllib.loads((api.var / "settings" / "appearance.toml").read_text())
    assert saved == {"accent": "sage", "hide_thinking": True, **look}
    assert (await api.get("/api/appearance")).json()["appearance"]["text_size"] == "xl"
    assert api.events.count({"type": "settings.changed", "what": ["appearance"]}) == 2


@pytest.mark.parametrize("bad", [{"accent": "red"}, {"hide_tools": "true"}, {"font": "serif"},
                                 {"grain_opacity": True}, {"contrast": 40}])
async def test_what_is_not_a_choice_is_refused_and_nothing_is_written(api, bad):
    r = await api.put("/api/appearance", json={"appearance": bad})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    assert not (api.var / "settings" / "appearance.toml").exists()
    assert api.events == []


async def test_a_stored_key_that_is_no_setting_is_left_out_and_gone_with_the_next_save(api):
    d = api.var / "settings"
    d.mkdir(parents=True, exist_ok=True)
    (d / "appearance.toml").write_text('accent = "sage"\ndialogue = "peach"\n')
    assert (await api.get("/api/appearance")).json() == {"appearance": {**DEFAULTS, "accent": "sage"}}
    await api.put("/api/appearance", json={"appearance": {"text_size": "lg"}})
    assert tomllib.loads((d / "appearance.toml").read_text()) == {"accent": "sage", "text_size": "lg"}


async def test_null_puts_a_key_back_to_its_default_and_every_key_null_resets_all(api):
    await api.put("/api/appearance", json={"appearance": {"accent": "sage", "tint": 200, "hide_tools": True}})
    r = await api.put("/api/appearance", json={"appearance": {"tint": None}})
    assert r.json()["appearance"] == {**DEFAULTS, "accent": "sage", "hide_tools": True}
    f = api.var / "settings" / "appearance.toml"
    assert tomllib.loads(f.read_text()) == {"accent": "sage", "hide_tools": True}
    r = await api.put("/api/appearance", json={"appearance": {k: None for k in DEFAULTS}})
    assert r.json()["appearance"] == DEFAULTS
    assert tomllib.loads(f.read_text()) == {}
    assert (await api.get("/api/appearance")).json() == {"appearance": DEFAULTS}
