"""The Settings API: whole-object PUTs with versions, plain 400s with a field path,
write-only keys, config/ never written, conversation settings."""
from __future__ import annotations

import tomllib

import pytest

from celine.conversations import store as conversations
from celine import boot
from celine import config as config_mod
from settings_fixtures import KEY, client, make_app, scratch

pytestmark = pytest.mark.anyio


@pytest.fixture
async def api(tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    async with client(make_app(cfg, store)) as c:
        c.var, c.cfg = var, cfg
        yield c


async def test_providers_round_trip_idempotent_versioned_and_config_untouched(api):
    config = {p: p.read_bytes() for p in config_mod.CONFIG_DIR.rglob("*") if p.is_file()}
    lan = (await api.get("/api/providers/lan")).json()["provider"]
    body = {**lan, "label": "The GPU box"}
    r1 = await api.put("/api/providers/lan", json=body)
    assert r1.status_code == 200
    v = r1.json()["version"]
    assert (await api.put("/api/providers/lan", json=body)).json()["version"] == v
    text = (api.var / "settings" / "providers.toml").read_text()
    assert tomllib.loads(text)["providers"]["lan"]["label"] == "The GPU box"
    stale = await api.put("/api/providers/lan", json={**body, "label": "x"},
                          headers={"If-Match": '"0123456789abcdef"'})
    assert stale.status_code == 412 and stale.json()["error"]["code"] == "version_conflict"
    assert api.cfg.settings.providers["lan"]["label"] == "The GPU box", "a stale PUT changes nothing"
    ok = await api.put("/api/providers/lan", json={**body, "label": "y"},
                       headers={"If-Match": f'"{v}"'})
    assert ok.status_code == 200 and api.cfg.settings.providers["lan"]["label"] == "y"
    await api.put("/api/roles/chat", json={"chain": ["main/big"]})
    await api.put("/api/settings", json={"general": {}})
    assert config == {p: p.read_bytes() for p in config_mod.CONFIG_DIR.rglob("*")
                      if p.is_file()}, "config/ is never written"


async def test_bad_writes_are_refused_with_a_field_and_change_nothing(api):
    before = (api.var / "settings" / "providers.toml").read_bytes()
    r = await api.put("/api/providers/new", json={
        "kind": "ollama", "label": "x", "base_url": "not a url at all", "models": {}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    assert r.json()["error"]["detail"]["field"] == "provider.base_url"
    r = await api.put("/api/roles", content=b"{not json")
    assert r.status_code == 400 and r.json()["error"]["detail"]["field"] == "body"
    # removing a model a role uses, or a provider in use, is refused
    lan = api.cfg.settings.providers["lan"]
    r = await api.put("/api/providers/lan", json={**lan, "models": {}})
    assert r.status_code == 400 and "roles." in r.json()["error"]["detail"]["field"]
    r = await api.delete("/api/providers/lan")
    assert r.status_code == 409 and r.json()["error"]["code"] == "in_use"
    assert (api.var / "settings" / "providers.toml").read_bytes() == before
    assert (await api.delete("/api/providers/or")).json()["deleted"] is True
    assert (await api.delete("/api/providers/or")).json()["deleted"] is False


async def test_keys_are_write_only(api):
    r = await api.put("/api/secrets/or", json={"key": KEY})
    assert r.status_code == 200 and r.json()["state"] == "set" and KEY not in r.text
    for path in ("/api/secrets", "/api/providers",
                 "/api/providers/or", "/api/status"):
        assert KEY not in (await api.get(path)).text, path
    from celine.settings.registry import backend_config
    assert backend_config(api.cfg.settings, "or/anthropic/claude-x").api_key == KEY
    bad = await api.put("/api/secrets/or", json={"kye": KEY})
    assert bad.status_code == 400 and KEY not in bad.text, "a wrong body never echoes"
    assert (await api.put("/api/secrets/nobody", json={"key": KEY})).status_code == 404
    assert (await api.delete("/api/secrets/or")).json()["state"] == "not set"
    assert backend_config(api.cfg.settings, "or/anthropic/claude-x").api_key is None


async def test_conversation_settings_create_and_merge_patch(api, store):
    n = len(await conversations.list(store))
    bad = await api.post("/api/conversations", json={"title": "x", "tools": {"nope": False}})
    assert bad.status_code == 400 and bad.json()["error"]["detail"]["field"] == "tools.nope"
    assert len(await conversations.list(store)) == n, "nothing is created on a 400"
    r = await api.post("/api/conversations", json={
        "title": "planning", "model": "or/anthropic/claude-x", "thinking": "low",
        "tools": {"shell": False}})
    cid, s = r.json()["id"], r.json()["settings"]
    assert (s["model"], s["thinking"], s["tools"]) == ("or/anthropic/claude-x", "low",
                                                       {"shell": False})
    r = await api.patch(f"/api/conversations/{cid}/settings", json={"model": "lan/small"})
    assert r.json()["thinking"] == "low" and r.json()["tools"] == {"shell": False}, \
        "absent keys stay"
    assert api.cfg.settings.chat_chain(cid)[0] == "lan/small"
    eff = r.json()["effective"]
    assert eff["model"] == "lan/small" and eff["default"] == {
        "model": "main/big", "label": "Big", "provider_label": "llama-swap"}, \
        "what Default comes to while a model is picked: the chat list's head"
    r = await api.patch(f"/api/conversations/{cid}/settings",
                        json={"model": None, "thinking": None, "tools": {"shell": None}})
    assert (r.json()["model"], r.json()["thinking"], r.json()["tools"]) == (None, None, {})
    r = await api.patch(f"/api/conversations/{cid}/settings", json={"model": "main/emb"})
    assert r.status_code == 400, "a model that cannot chat"
    assert (await api.patch("/api/conversations/nope/settings",
                            json={"thinking": "off"})).status_code == 404


async def test_tabby_discovery():
    import httpx
    from celine.settings import kinds

    def reply(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/model":
            return httpx.Response(200, json={"id": "my-model-27b", "object": "model",
                                             "owned_by": "tabbyAPI"})
        if req.url.path == "/running":
            return httpx.Response(200, json={"running": [
                {"model": "my-model-27b", "state": "ready", "cmd": "tabbyapi -c 131072 --kv-unified"}
            ]})
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{
                "id": "my-model-27b",
                "context_length": 131072,
                "architecture": {"input_modalities": ["text", "image"]},
                "capabilities": {"vision": True, "function_calling": True, "reasoning": True},
                "supported_parameters": ["tools", "reasoning"],
                "reasoning": {"supported_efforts": ["low", "medium", "xhigh"]},
                "status": {"value": "loaded"},
            }]})
        return httpx.Response(404)

    t = httpx.MockTransport(reply)
    kind, answered, url = await kinds.detect("http://192.0.2.10:11434/v1", transport=t)
    assert (kind, answered, url) == ("tabby", True, "http://192.0.2.10:11434/v1"), \
        "tabby wins over the router's /running"

    def swap_only(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404) if req.url.path == "/v1/model" else reply(req)
    kind, answered, url = await kinds.detect("http://192.0.2.10:11434/v1",
                                             transport=httpx.MockTransport(swap_only))
    assert (kind, answered) == ("llama-swap", True)
    found = await kinds.discover("tabby", "http://192.0.2.10:11434/v1",
                                 refresh=True, transport=t)
    assert found == [{
        "id": "my-model-27b",
        "name": "my-model-27b",
        "caps": ["chat", "tools", "vision"],
        "context": 131072,
        "thinking_levels": ["low", "medium", "xhigh"],
        "loaded": True,
    }]


async def test_a_new_address_keeps_the_providers_key_and_models(api):
    await api.put("/api/secrets/lan", json={"key": KEY})
    lan = (await api.get("/api/providers/lan")).json()["provider"]
    assert (await api.put("/api/providers/lan", json={**lan, "base_url": "192.0.2.10:11434"})
            ).status_code == 200
    now = api.cfg.settings.providers["lan"]
    assert now["base_url"].startswith("http://192.0.2.10:11434") and now["models"] == lan["models"]
    assert (await api.get("/api/providers/lan")).json()["key"]["state"] == "set"


async def test_detect_tries_v1_when_the_address_has_no_path():
    """A server that lists its models only under /v1: the address as typed answers as no
    known kind, so detect appends /v1 once and says the address that worked."""
    import httpx
    from celine.settings import kinds

    def only_v1(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "m", "owned_by": "vllm"}]})
        return httpx.Response(404)
    t = httpx.MockTransport(only_v1)
    assert await kinds.detect("http://192.0.2.10:8000", transport=t) == (
        "openai", True, "http://192.0.2.10:8000/v1")
    assert await kinds.detect("http://192.0.2.10:8000/api", transport=t) == (
        None, True, "http://192.0.2.10:8000/api"), "a typed path is not second-guessed"
    assert kinds.address("192.0.2.10:8000/v1/chat/completions/") == "http://192.0.2.10:8000/v1"


async def test_a_provider_view_says_the_context_the_server_runs_with(api):
    """Settings, Models shows the server's number beside the written one (`context`),
    so a stale number in providers.toml is seen, never a silent wrong limit."""
    from celine.turns import tokens
    view = (await api.get("/api/providers/main")).json()
    big = next(m for m in view["models"] if m["name"] == "big")
    assert big["server_context"] is None and "context" not in big
    tokens.reported_windows("http://127.0.0.1:11434/v1", {"big": 262144})
    view = (await api.get("/api/providers/main")).json()
    assert next(m for m in view["models"] if m["name"] == "big")["server_context"] == 262144


async def test_an_advanced_value_is_saved_checked_and_read_at_once(api):
    fields = {f["key"]: f for f in (await api.get("/api/settings")).json()["fields"]}
    assert fields["tool_timeout_seconds"]["advanced"] and not fields.get("user_name", {}).get(
        "advanced")
    assert api.cfg.r("max_concurrent_turns") == fields["max_concurrent_turns"]["default"]
    r = await api.put("/api/settings", json={"general": {"max_concurrent_turns": 2}})
    assert r.status_code == 200 and api.cfg.r("max_concurrent_turns") == 2
    for bad, field in (({"max_concurrent_turns": 0}, "general.max_concurrent_turns"),
                       ({"job_timeout_seconds": 100, "tool_timeout_seconds": 100},
                        "general.job_timeout_seconds")):
        r = await api.put("/api/settings", json={"general": bad})
        assert r.status_code == 400 and r.json()["error"]["detail"]["field"] == field, bad
    assert api.cfg.r("max_concurrent_turns") == 2, "a refused save changes nothing"


async def test_status_links_the_source_of_this_build(api, monkeypatch):
    """The AGPL's offer: GET /api/status `build.source` is the code this build runs, at its
    commit when the commit is known."""
    repo = "https://github.com/LumiWasTaken/celine-net"
    try:
        for commit, source in (("904475d", f"{repo}/tree/904475d"), ("dev", repo)):
            monkeypatch.setenv("CELINE_COMMIT", commit)
            config_mod.build.cache_clear()
            build = (await api.get("/api/status")).json()["build"]
            assert build["commit"] == commit and build["source"] == source
    finally:
        config_mod.build.cache_clear()
