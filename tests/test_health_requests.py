"""Settings, Models shows what the app last learned of each provider: opening the page
asks no server, a new provider is asked once when it is saved, and Check asks
now, strict, and reads its model list once."""
from __future__ import annotations

import pytest

from celine import boot
from celine.backends.router import app_router
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


@pytest.fixture
async def api(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    router = app_router(cfg)
    asked: list[tuple[str, bool]] = []
    lists: list[str] = []
    probe, discover = router.probe, router.discover

    async def counted(pid, **kw):
        asked.append((pid, kw.get("strict", False)))
        return await probe(pid, **kw)

    async def listed(pid):
        lists.append(pid)
        return await discover(pid)
    monkeypatch.setattr(router, "probe", counted)
    monkeypatch.setattr(router, "discover", listed)
    app = make_app(cfg, store)
    from types import SimpleNamespace
    from celine.api import llm as llm_api
    app.include_router(llm_api.router)
    app.state.services.llm = SimpleNamespace(router=router)
    async with client(app) as c:
        c.asked, c.lists = asked, lists
        yield c


async def test_opening_the_providers_asks_no_server(api):
    assert (await api.get("/api/providers")).status_code == 200
    assert (await api.get("/api/providers/lan")).status_code == 200
    assert api.asked == []


async def test_opening_the_model_chooser_asks_no_server(api):
    r = await api.get("/api/loaded-models")
    assert r.status_code == 200 and "models" in r.json()
    assert api.asked == []


async def test_a_new_provider_is_asked_once_when_saved(api):
    body = {"kind": "ollama", "label": "Den", "base_url": "http://192.0.2.4:11434/v1",
            "models": {}}
    assert (await api.put("/api/providers/den", json=body)).status_code == 200
    assert api.asked == [("den", False)]
    assert (await api.put("/api/providers/den", json={**body, "label": "Den 2"})
            ).status_code == 200
    assert api.asked == [("den", False)], "a saved one is not asked again"


async def test_check_provider_asks_strict_and_reads_its_list_once(api):
    assert (await api.post("/api/providers/lan/test")).status_code == 200
    assert api.asked == [("lan", True)] and api.lists == ["lan"]
