"""Settings, Features: defaults, PUT then GET, a feature off takes its skill and its
archive with it, the config status carries the switches, bad writes are 400."""
from __future__ import annotations

import pytest

from celine import boot
from celine import reload
from celine.settings import features, files
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


@pytest.fixture
async def api(tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    from celine.api import skills as skills_api
    app.include_router(skills_api.router)
    from celine.api import kb as kb_api
    app.include_router(kb_api.router)
    async with client(app) as c:
        c.var, c.cfg, c.services = var, cfg, app.state.services
        yield c


def _skills(c) -> set[str]:
    return set(c.services.registry.skills)


def _fresh(api) -> None:
    """A fresh install: no features.toml yet."""
    (api.var / "settings" / "features.toml").unlink()
    reload.apply(api.services)


async def test_defaults_then_put_then_get(api):
    _fresh(api)
    r = (await api.get("/api/features")).json()
    assert r["saved"] is False and r["done"] is False and "restart" not in r
    assert {f["id"] for f in r["catalog"]} == set(r["features"])
    assert [f["id"] for f in r["catalog"]][:3] == ["shellbox", "shares", "browser"]
    # nothing that needs a service runs before Setup saved it: no connector starts at boot
    off = {i for i, on in r["features"].items() if not on}
    assert off == {"shellbox", "shares", "browser", "images", "music", "web", "discord",
                   "whatsapp", "email", "vault", "guard"}
    assert "shell" not in _skills(api) and "discord" not in _skills(api)
    assert not features.allows_connector(api.cfg, "discord")
    assert api.cfg.settings.features == features.seed()
    put = await api.put("/api/features", json={"features": {"web": False}})
    assert put.status_code == 200 and "applied" in put.json()
    again = (await api.get("/api/features")).json()
    assert again["saved"] is True and again["features"]["web"] is False
    assert again["features"]["email"] is True, "an id left out is on"
    assert again["features"]["shares"] is False, "but shares, on only once saved on"
    assert again["features"]["guard"] is False, "and the guard"
    assert again["version"] == put.json()["version"] != r["version"]
    assert not (api.var / "settings" / "features.toml").read_text().count("email")
    assert "shell" in _skills(api)
    stale = await api.put("/api/features", json={"features": {}},
                          headers={"If-Match": f'"{r["version"]}"'})
    assert stale.status_code == 412


async def test_a_feature_off_takes_its_skill_away_until_it_is_on_again(api):
    assert "web" in _skills(api) and api.cfg.skill_enabled("web")
    await api.put("/api/features", json={"features": {"web": False}})
    assert api.cfg.skill_enabled("web") is False, "whatever skills.toml says"
    assert "web" not in _skills(api)
    names = {s["name"] for s in (await api.get("/api/skills")).json()["skills"]}
    assert "web" not in names and "notebook" in names
    status = (await api.get("/api/status")).json()
    assert status["features"]["web"] is False and status["setup"] is False
    box = next(f for f in status["catalog"] if f["id"] == "shellbox")
    assert box == {"id": "shellbox", "about": features.BY_ID["shellbox"].about,
                   "off_when": features.BY_ID["shellbox"].off_when,
                   "places": ["terminals"], "pages": []}
    await api.put("/api/features", json={"features": {}})
    assert "web" in _skills(api)


async def test_the_catalog_has_every_field_and_the_plain_ones_have_no_probe(api):
    catalog = (await api.get("/api/features")).json()["catalog"]
    keys = {"id", "label", "needs", "about", "off_when", "places", "pages", "setup", "card", "kbs", "archive", "warn",
            "state", "detail"}
    assert all(set(f) == keys for f in catalog)
    by = {f["id"]: f for f in catalog}
    assert by["documents"]["state"] == "none" and by["documents"]["detail"] == ""
    # the onboarding's fold: a card when the service answered, or when it has an address
    # and does not answer; the guard always; email and vault never (the Later card and
    # Settings). On this fake app (no kbs: the config's word) only the archive probe is ready.
    assert {f["id"] for f in catalog if f["card"] == "detect"} == {"messages"}
    assert {f.id for f in features.CATALOG if f.card == "detect"} == \
        {"shellbox", "browser", "images", "music", "web", "messages"}
    assert {f["id"] for f in catalog if f["card"] == "always"} == \
        {"shares", "discord", "whatsapp", "guard", "routines"}
    assert {f["id"] for f in catalog if f["warn"]} == {"shares"}
    assert by["email"]["card"] == "" and by["vault"]["card"] == ""


import httpx as _httpx
_CLIENT = _httpx.AsyncClient      # before any test patches it


def _answering(handler):
    """An httpx.AsyncClient stand-in answered by `handler` (a MockTransport); patched on
    the module under test, so the real class is kept here."""
    import httpx

    def make(**kw):
        return _CLIENT(**{**kw, "transport": httpx.MockTransport(handler)})
    return make


async def test_a_connector_probe_says_one_thing(api, monkeypatch):
    import httpx
    from celine import mcp
    refused = lambda r: httpx.Response(401, json={})  # noqa: E731
    monkeypatch.setattr(mcp.httpx, "AsyncClient", _answering(refused))
    said = await mcp.probe_discord(api.cfg, api.services)
    assert said == {"state": "missing", "detail":
                    "A Discord connector answers at 127.0.0.1:8768 but it is not this app's."}

    def down(r):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(mcp.httpx, "AsyncClient", _answering(down))
    said = await mcp.probe_discord(api.cfg, api.services)
    assert said["state"] == "missing" and said["detail"] == (
        "No Discord connector answers at 127.0.0.1:8768. Add discord to COMPOSE_PROFILES in "
        ".env and run docker compose up -d.")


async def test_the_shellbox_probe_tries_the_default_address_and_hands_it_over(api, monkeypatch):
    import httpx
    from celine import shellbox

    def box(handler):         # the shellbox client, answered by `handler`
        monkeypatch.setattr(shellbox, "client", lambda cfg, read, transport=None: _CLIENT(
            transport=httpx.MockTransport(handler)))
    assert api.cfg.settings.services["shellbox"].get("url", "") == ""
    box(lambda r: httpx.Response(200, json={}))
    found = await features.probe(api.cfg, api.services, "shellbox")
    assert found == {"state": "ready", "detail": "Found at http://127.0.0.1:8770.",
                     "url": "http://127.0.0.1:8770"}
    assert features.card_now(api.cfg, features.BY_ID["shellbox"], found) == "detect"
    box(lambda r: httpx.Response(401 if r.url.path == "/status" else 200, json={}))
    bad = await features.probe(api.cfg, api.services, "shellbox")
    assert bad["state"] == "missing" and bad["url"] == "http://127.0.0.1:8770"
    assert features.card_now(api.cfg, features.BY_ID["shellbox"], bad) == "detect"

    def down(r):
        raise httpx.ConnectError("refused")
    box(down)
    none = await features.probe(api.cfg, api.services, "shellbox")
    assert none["state"] == "unset" and "url" not in none
    assert features.card_now(api.cfg, features.BY_ID["shellbox"], none) == ""


async def test_the_browser_probe_reads_browserd_state(api, monkeypatch):
    import httpx
    from celine.browser import sync
    monkeypatch.setattr(sync.services_mod, "url", lambda cfg, sid: "http://box:8771")
    answers = {"state": "down", "reason": "Brave could not start"}
    monkeypatch.setattr(sync, "_client", lambda cfg, read=0: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=answers))))
    said = await features.probe(api.cfg, api.services, "browser")
    assert said == {"state": "missing", "detail":
                    "The browser box answers at box:8771 but Brave is not running: Brave could not start."}
    answers = {"state": "up", "reason": None}
    assert (await features.probe(api.cfg, api.services, "browser"))["state"] == "ready"


async def test_the_guard_probe_names_its_two_models(api):
    by = {f["id"]: f for f in (await api.get("/api/features")).json()["catalog"]}
    assert by["guard"]["state"] == "ready"
    assert by["guard"]["detail"] == "Decision model: Small. No second look."
    r = await api.put("/api/guard/rules", json={"chain": {"provider": "main", "model": "big"}})
    assert r.status_code == 200 and r.json()["chain"]["model"] == "big"
    assert r.json()["chain"]["seconds"] == 300, "the rest of the table stays"
    assert (await api.get("/api/features/guard/probe")).json()["detail"] == \
        "Decision model: Small. Second look: Big."
    await api.put("/api/guard/rules", json={"chain": {"provider": "gone", "model": "huge"}})
    assert (await api.get("/api/features/guard/probe")).json()["detail"] == \
        "Decision model: Small. Second look: huge is not on a saved provider."
    await api.put("/api/roles/small", json={"chain": []})
    said = (await api.get("/api/features/guard/probe")).json()
    assert said["state"] == "unset" and said["detail"].startswith("Pick a decision model")


async def test_guard_rules_merge_and_refuse_bad_values(api):
    r = (await api.get("/api/guard/rules")).json()
    assert r["saved"] is False and r["chain"]["provider"] == "" and r["patrol"]["on"] is True
    v = r["version"]
    put = await api.put("/api/guard/rules", json={"patrol": {"every_minutes": 5}},
                        headers={"If-Match": f'"{v}"'})
    assert put.status_code == 200 and put.json()["patrol"]["every_minutes"] == 5
    assert put.json()["patrol"]["turns"] == 3 and put.json()["saved"] is True
    again = await api.put("/api/guard/rules", json={"chain": {"provider": "x"}})
    assert again.json()["patrol"]["every_minutes"] == 5, "a PUT merges"
    bad = await api.put("/api/guard/rules", json={"patrol": {"every_minutes": 0}})
    assert bad.status_code == 400 and bad.json()["error"]["detail"]["field"] == "guard.patrol.every_minutes"
    assert (await api.put("/api/guard/rules", json={"laser": {}})).status_code == 400
    stale = await api.put("/api/guard/rules", json={}, headers={"If-Match": f'"{v}"'})
    assert stale.status_code == 412


def _fake(state: str, detail: str):
    async def probe(cfg, services):
        return {"state": state, "detail": detail}
    return probe


async def test_get_carries_the_probes_and_caches_them(api, monkeypatch):
    from celine.skills import shell
    monkeypatch.setattr(shell, "probe", _fake("missing", "The shellbox at box:8770 refused."))
    by = {f["id"]: f for f in (await api.get("/api/features")).json()["catalog"]}
    assert by["shellbox"] == {**by["shellbox"], "state": "missing",
                              "detail": "The shellbox at box:8770 refused."}
    assert by["email"]["state"] == "unset"
    monkeypatch.setattr(shell, "probe", _fake("ready", "The shellbox at box:8770 answers."))
    by = {f["id"]: f for f in (await api.get("/api/features")).json()["catalog"]}
    assert by["shellbox"]["state"] == "missing", "cached for 10 s"
    fresh = (await api.get("/api/features/shellbox/probe")).json()
    assert fresh == {"state": "ready", "detail": "The shellbox at box:8770 answers."}
    api.services.feature_probes = None
    by = {f["id"]: f for f in (await api.get("/api/features")).json()["catalog"]}
    assert by["shellbox"]["state"] == "ready"
    assert (await api.get("/api/features/laser/probe")).status_code == 404


async def test_a_slow_or_broken_probe_is_missing(api, monkeypatch):
    import anyio
    from celine.skills import web

    async def slow(cfg, services):
        await anyio.sleep_forever()

    async def broken(cfg, services):
        raise RuntimeError("boom")
    monkeypatch.setattr(features, "PROBE_SECONDS", 0.05)
    monkeypatch.setattr(web, "probe", slow)
    assert (await api.get("/api/features/web/probe")).json() == {
        "state": "missing", "detail": "Web search did not answer in time."}
    monkeypatch.setattr(web, "probe", broken)
    assert (await api.get("/api/features/web/probe")).json()["state"] == "missing"


async def test_done_is_sticky_and_is_the_config_status_setup(api):
    await api.put("/api/features", json={"features": {"web": False}})
    assert (await api.get("/api/status")).json()["setup"] is False
    r = await api.put("/api/features", json={"features": {}, "done": True})
    assert r.json()["done"] is True and r.json()["features"]["web"] is True
    assert (await api.get("/api/status")).json()["setup"] is True
    await api.put("/api/features", json={"features": {"web": False}})
    again = (await api.get("/api/features")).json()
    assert again["done"] is True and again["features"]["web"] is False
    bad = await api.put("/api/features", json={"features": {}, "done": "yes"})
    assert bad.status_code == 400 and bad.json()["error"]["detail"]["field"] == "done"


async def test_the_messages_probe_says_what_search_can_do(api, monkeypatch):
    from celine.kb import KBs
    from celine.kb.connectors import Connectors
    api.services.kbs = KBs(api.cfg, lambda e: None)
    api.services.connectors = Connectors(api.cfg, api.services.kbs, lambda e: None)
    for sid in ("discord", "whatsapp"):
        api.services.connectors.open(sid)
    said = await features.probe(api.cfg, api.services, "messages")
    assert said == {"state": "unset", "detail": "No source connected yet."}, "no connector answered yet"
    for c in api.services.connectors.all.values():
        c.status = {"connection": {"state": "connected"}}
    said = await features.probe(api.cfg, api.services, "messages")
    assert said["state"] == "ready" and "Word search only" not in said["detail"]
    await api.put("/api/roles/embeddings", json={"chain": []})
    await api.put("/api/roles/reranker", json={"chain": []})
    said = await features.probe(api.cfg, api.services, "messages")
    assert said["detail"].endswith(" Word search only: no topic search model. No reranking model.")
    assert said["detail"].startswith("Sources: ")
    files.write(api.var / "settings", "features", {"discord": False, "whatsapp": False})
    reload.apply(api.services)
    api.services.kbs = KBs(api.cfg, lambda e: None)
    api.services.connectors = Connectors(api.cfg, api.services.kbs, lambda e: None)
    assert (await features.probe(api.cfg, api.services, "messages"))["state"] == "unset"


async def test_put_provider_fills_the_empty_roles_from_caps_and_presets(tmp_path, monkeypatch,
                                                                       store):
    from settings_fixtures import PROVIDERS
    scratch(tmp_path, monkeypatch, providers={}, roles={})
    cfg = boot.load()
    async with client(make_app(cfg, store)) as c:
        assert (await c.get("/api/status")).json()["problems"] == []
        r = await c.put("/api/providers/main", json=PROVIDERS["main"])
        assert r.status_code == 200, r.text
        roles = (await c.get("/api/roles")).json()["roles"]
        assert roles["chat"]["chain"] == ["main/big"] and roles["small"]["chain"] == ["main/big"]
        assert roles["embeddings"]["chain"] == ["main/emb"]
        assert roles["reranker"]["chain"] == ["main/rr"]
        assert roles["vision"]["chain"] == ["main/eyes"]
        assert not roles.get("image_generation", {}).get("chain")
        r = await c.put("/api/providers/comfyui", json={
            "kind": "comfyui", "label": "ComfyUI", "base_url": "http://127.0.0.1:8188"})
        assert r.status_code == 200, r.text
        roles = (await c.get("/api/roles")).json()["roles"]
        from celine.comfyui import presets
        images = [f"comfyui/{p}" for p in presets.of("images")]
        assert roles["image_generation"]["chain"] == images[:6] and len(images) >= 2
        assert roles["music_generation"]["chain"] == [f"comfyui/{p}" for p in presets.of("music")]
        # a second provider with the same caps changes nothing: the roles are set
        r = await c.put("/api/providers/lan", json=PROVIDERS["lan"])
        assert (await c.get("/api/roles")).json()["roles"]["chat"]["chain"] == ["main/big"]


async def test_guard_off_means_bastet_is_off(api):
    from celine.guard import switch
    await api.put("/api/features", json={"features": {"guard": True}})
    await api.put("/api/settings", json={"general": {"guard": "on"}})
    assert switch.mode(api.cfg) == "on"
    await api.put("/api/features", json={"features": {"guard": False}})
    assert switch.mode(api.cfg) == "off" and not switch.enabled(api.cfg)


class FakeRunner:
    def __init__(self):
        self.log: list[str] = []
        self.running = False

    async def start(self):
        if not self.running:
            self.running = True
            self.log.append("start")

    async def stop(self):
        if self.running:
            self.running = False
            self.log.append("stop")


async def test_a_switch_or_an_address_change_starts_and_stops_what_runs(api, monkeypatch):
    from celine.core.runners import Runners

    async def unset(cfg, services, cache=True):      # no probe of the made-up addresses
        return {f.id: {"state": "unset", "detail": ""} for f in features.CATALOG}
    monkeypatch.setattr(features, "probe_all", unset)
    runners = api.services.runners = Runners()
    runners["shellbox"] = box = FakeRunner()
    runners["browser"] = browser = FakeRunner()
    await api.put("/api/features", json={"features": {"shellbox": False}})
    assert box.log == [] and browser.log == ["start"]
    await api.put("/api/features", json={"features": {}})
    assert box.log == ["start"]
    r = await api.put("/api/services", json={"services": {"shellbox": {"url": "http://b:8770"}}})
    assert r.status_code == 200, r.text
    assert box.log == ["start", "stop", "start"] and browser.log == ["start"]
    await api.put("/api/services", json={"services": {"shellbox": {"url": "http://b:8770"},
                                                      "browser": {"url": "http://c:1"}}})
    assert box.log == ["start", "stop", "start"] and browser.log == ["start", "stop", "start"]
    await api.put("/api/features", json={"features": {"shellbox": False}})
    assert box.log == ["start", "stop", "start", "stop"] and not box.running


async def test_a_real_runner_runs_under_its_own_scope(api):
    import anyio
    from celine.core.runners import Runner
    ran: list[str] = []

    async def follow():
        ran.append("in")
        try:
            await anyio.sleep_forever()
        finally:
            ran.append("out")
    async with anyio.create_task_group() as tg:
        r = Runner(tg, follow)
        await r.start()
        await r.start()
        await anyio.sleep(0)
        assert ran == ["in"] and r.running
        await r.stop()
        assert ran == ["in", "out"] and not r.running
        await r.start()
        await anyio.sleep(0)
        assert ran == ["in", "out", "in"]
        tg.cancel_scope.cancel()


async def test_discord_off_closes_its_archive_and_its_server_and_on_opens_them(
        tmp_path, monkeypatch, store):
    import anyio
    from celine.core.runners import Runner, Runners
    from celine.kb import KBs
    from celine.kb.connectors import Connectors
    from celine.mcp import Supervisor
    var = scratch(tmp_path, monkeypatch)
    files.write(var / "settings", "features", {"discord": False})
    cfg = boot.load()
    kbs = KBs(cfg, lambda e: None)
    assert "discord" not in kbs.all and "whatsapp" in kbs.all
    app = make_app(cfg, store)
    from celine.api import kb as kb_api
    app.include_router(kb_api.router)
    services = app.state.services
    services.kbs = kbs
    connectors = services.connectors = Connectors(cfg, kbs, lambda e: None)
    sup = Supervisor(cfg, store, services.registry, kbs=kbs)
    ran: list[str] = []

    async def run_one(sid):
        ran.append(sid)
        await anyio.sleep_forever()
    monkeypatch.setattr(sup, "run_one", run_one)

    async def sources(c):
        return {k["id"] for k in (await c.get("/api/sources")).json()["kbs"]}
    async with anyio.create_task_group() as tg:
        runners = services.runners = Runners()

        async def discord():
            kbs.open("discord")
            connectors.open("discord")
            try:
                await sup.run_one("discord")
            finally:
                connectors.close("discord")
                kbs.close("discord")
        runners["discord"] = Runner(tg, discord)
        async with client(app) as c:
            assert "discord" not in await sources(c)
            await c.put("/api/features", json={"features": {}})
            assert "discord" in await sources(c) and ran == ["discord"]
            assert connectors.get("discord") is not None
            await c.put("/api/features", json={"features": {"discord": False}})
            assert "discord" not in await sources(c) and "whatsapp" in await sources(c)
            assert connectors.get("discord") is None and not runners["discord"].running
        tg.cancel_scope.cancel()
    async with anyio.create_task_group() as tg:      # start() leaves a feature's server alone
        await sup.start(tg)
        assert "discord" not in sup.skills and "whatsapp" not in sup.skills
        tg.cancel_scope.cancel()


async def test_bad_writes_are_400_with_a_field(api):
    _fresh(api)
    for body, field in (({"features": {"laser": True}}, "features.laser"),
                        ({"features": {"web": "off"}}, "features.web"),
                        ({"features": []}, "features")):
        r = await api.put("/api/features", json=body)
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request", body
        assert r.json()["error"]["detail"]["field"] == field
    assert (await api.get("/api/features")).json()["saved"] is False


async def test_services_hold_the_addresses_and_show_the_key_hashes(api, monkeypatch, tmp_path):
    from celine import keys
    from celine.settings import services as services_mod
    (api.cfg.data_dir / "keys" / "shell.key").write_text("s3cret\n")
    keys.ensure(api.cfg.data_dir)
    r = (await api.get("/api/services")).json()
    assert r["saved"] is False and r["services"]["shellbox"] == {"url": ""}
    assert r["services"]["searxng"] == {"url": "http://127.0.0.1:8888"}, "the default"
    assert set(r["keys"]) == {"shellbox", "browser"}
    assert r["keys"]["shellbox"] == {"hash": keys.sha256("s3cret")}, "the hash, never the key"
    assert "s3cret" not in (await api.get("/api/services")).text
    assert [f["key"] for f in r["fields"]["shellbox"]] == ["url"]
    put = await api.put("/api/services", json={"services": {"shellbox": {"url": "http://box:8770/"}}})
    assert put.status_code == 200, put.text
    assert put.json()["services"]["shellbox"] == {"url": "http://box:8770"}
    assert services_mod.url(api.cfg, "shellbox") == "http://box:8770", "every caller reads the save"
    assert services_mod.url(api.cfg, "searxng") == "", "a url left out is empty"
    for body, field in (({"services": {"laser": {}}}, "services.laser"),
                        ({"services": {"browser": {"url": "box"}}}, "services.browser.url")):
        bad = await api.put("/api/services", json=body)
        assert bad.status_code == 400 and bad.json()["error"]["detail"]["field"] == field
    from celine.skills import shell
    monkeypatch.setattr(shell, "probe", _fake("missing", "The shellbox at box:8770 refused."))
    assert (await api.post("/api/services/shellbox/test")).json()["state"] == "missing"
    assert (await api.post("/api/services/laser/test")).status_code == 404


async def test_the_environment_seeds_the_services_once(tmp_path, monkeypatch, store):
    monkeypatch.setenv("CELINE_SHELL_URL", "http://shellbox:8770/")
    monkeypatch.setenv("CELINE_SEARXNG_URL", "http://searxng:8080")
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    assert cfg.settings.services["shellbox"] == {"url": "http://shellbox:8770"}
    assert cfg.settings.services["searxng"] == {"url": "http://searxng:8080"}
    async with client(make_app(cfg, store)) as c:
        await c.put("/api/services", json={"services": {"browser": {"url": "http://b:1"}}})
    cfg = boot.load()
    assert cfg.settings.services.get("shellbox", {}) == {}, "saved: the env is read no more"
    assert cfg.settings.services["browser"] == {"url": "http://b:1"}


async def test_her_talk_keeps_the_placeholders_and_names_become_them(api):
    """The page edits the text with {{username}} and {{name}} in it; a name typed in
    full becomes its placeholder when stored, so a rename carries."""
    await api.put("/api/settings", json={"general": {"user_name": "Joe"}})
    r = (await api.get("/api/agent")).json()
    assert r["talk"] is None and "{{username}}" in r["talk_shipped"]
    put = await api.put("/api/agent", json={"talk": "Talk to Joe like Celine would.\n"})
    assert put.status_code == 200, put.text
    assert put.json()["talk"] == "Talk to {{username}} like {{name}} would.\n"
    assert api.cfg.her.talk == "Talk to {{username}} like {{name}} would.\n"


async def test_put_answers_from_the_probes_the_page_saw(api, monkeypatch):
    """Next in the onboarding saves with a PUT: it answers with the states the GET found
    and runs no probe (a dead name's DNS wait would hold every step)."""
    await api.get("/api/features")
    assert api.services.feature_probes is not None

    async def never(*a, **k):
        raise AssertionError("the PUT probed")
    monkeypatch.setattr(features, "probe_all", never)
    r = await api.put("/api/features", json={"features": {"web": False}})
    assert r.status_code == 200 and {f["id"] for f in r.json()["catalog"]} == set(features.IDS)
    assert api.services.feature_probes is None, "the next GET probes anew"


async def test_shares_start_off_and_their_switch_saves_on(api):
    """Shares are off on a fresh install and for an id left out; while off its three
    share tools are not built, the shell's others are; saved on, they are back. Off
    again, a share that still runs answers 404 (tests/test_shares.py)."""
    from celine import shares
    tools = {"shell__share", "shell__unshare", "shell__list_shares"}
    _fresh(api)
    assert features.on(api.cfg, "shares") is False
    assert set(features.BY_ID["shares"].tools) == tools
    await api.put("/api/features", json={"features": {}})
    assert features.on(api.cfg, "shares") is False, "an id left out stays off"
    reg = api.services.registry
    assert "shell" in reg.skills and "shell__run" in reg.tools
    assert not tools & set(reg.tools)
    shell = next(s for s in (await api.get("/api/skills")).json()["skills"] if s["name"] == "shell")
    assert not tools & {t["wire_name"] for t in shell["tools"]}
    put = await api.put("/api/features", json={"features": {"shares": True}})
    assert put.json()["features"]["shares"] is True
    assert "shares = true" in (api.var / "settings" / "features.toml").read_text()
    assert tools <= set(reg.tools)
    assert (await api.get("/api/features/shares/probe")).json()["state"] == "ready"
    await api.put("/api/features", json={"features": {"shares": True, "shellbox": False}})
    assert (await api.get("/api/features/shares/probe")).json() == \
        {"state": "unset", "detail": "The shellbox is off."}
    assert f"{shares.PRIVATE_HOURS} hours" in features.BY_ID["shares"].warn


async def test_the_guard_starts_off_until_the_user_switches_it_on(api):
    """A fresh install has no Bastet, a decision model picked or not: the Guard feature is
    on only once saved on (Settings, Features), and an id left out stays off."""
    from celine import guard
    _fresh(api)
    assert features.on(api.cfg, "guard") is False and guard.mode(api.cfg) == "off"
    await api.put("/api/features", json={"features": {}})
    assert features.on(api.cfg, "guard") is False, "an id left out stays off"
    put = await api.put("/api/features", json={"features": {"guard": True}})
    assert put.json()["features"]["guard"] is True
    assert guard.mode(api.cfg) == "watch"
