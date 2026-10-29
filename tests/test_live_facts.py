"""Model facts come live from the servers: the context a model runs with (the running
numbers over the training size), the caps a server states over the saved ones, what is
loaded as its status says; she reads them in models__list, and a sub-agent's report
carries what its work took. Fake servers in the shapes the real ones answer."""
from __future__ import annotations

import types

import httpx
import pytest

from celine import boot
from celine.backends.llm import LLM
from celine.backends.router import Router
from celine.conversations import store as conversations
from celine.settings import her_models, kinds
from celine.settings.files import Invalid
from celine.settings.registry import backend_config, validate_role
from celine.skills import ToolContext
from celine.skills.models import Models
from celine.sub_agents.report import work, work_line
from celine.turns import tokens
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

# llama-server in router mode (a home server): the training size in context_length, the size it
# loaded with in meta.n_ctx and on its command line; a sleeping model is not loaded
LLAMA_SERVER = {"data": [
    {"id": "qwen3.5-9b", "owned_by": "llamacpp",
     "status": {"value": "sleeping", "args": ["llama-server", "--ctx-size", "32768",
                                              "--parallel", "1"]},
     "context_length": 262144, "meta": {"n_ctx": 32768, "n_ctx_train": 262144},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "capabilities": {"vision": False, "function_calling": True, "reasoning": False},
     "supported_parameters": ["tools"]},
    {"id": "two-slots", "owned_by": "llamacpp",
     "status": {"value": "loaded", "args": ["llama-server", "--fit", "on", "-np", "2"]},
     "context_length": 131072, "meta": {"n_ctx": 16384}},
    {"id": "qwen3-embedding", "owned_by": "llamacpp", "status": {"value": "loaded"},
     "context_length": 40960, "meta": {"n_ctx": 8192}},
]}

# tabbyAPI behind a router: the list says max_seq_len while the
# model is unloaded; /v1/model and /props say what the loaded one runs with
TABBY_LIST = {"data": [
    {"id": "Gemma-4-31B", "owned_by": "tabbyAPI", "parameters": None,
     "meta": {"n_ctx": None, "n_ctx_train": 262144}, "context_length": 262144,
     "architecture": {"input_modalities": ["text", "image"]},
     "capabilities": {"vision": True, "function_calling": True},
     "supported_parameters": ["tools"], "status": {"value": "loaded"}},
    {"id": "Other", "owned_by": "tabbyAPI", "context_length": 131072,
     "parameters": {"max_seq_len": 131072, "cache_size": 49152}},
]}
TABBY_MODEL = {"id": "Gemma-4-31B", "parameters": {"max_seq_len": 65536,
                                                   "cache_size": 65536}}
PROPS = {"default_generation_settings": {"n_ctx": 65536}}


def _server(routes: dict):
    def answer(req):
        return httpx.Response(200, json=routes.get(req.url.path, {"detail": "none"}))
    return httpx.MockTransport(answer)


async def test_llama_server_states_the_size_it_runs_with_and_its_caps():
    found = {m["id"]: m for m in await kinds.discover(
        "llama-server", "http://home:8080/v1", transport=_server({"/v1/models": LLAMA_SERVER}))}
    assert found["qwen3.5-9b"]["context"] == 32768, "meta.n_ctx, not the training size"
    assert found["qwen3.5-9b"]["caps"] == ["chat", "tools"] and \
        "guessed" not in found["qwen3.5-9b"], "the server says: no vision"
    assert found["qwen3.5-9b"]["loaded"] is False, "sleeping is not loaded"
    assert found["two-slots"]["context"] == 8192, "two slots split what it loaded with"
    assert found["qwen3-embedding"]["context"] == 8192
    assert found["qwen3-embedding"]["guessed"], "it states no caps: a guess"


async def test_tabby_states_the_size_its_loaded_model_runs_with():
    found = {m["id"]: m for m in await kinds.discover(
        "tabby", "http://gpu:11434/v1", transport=_server({
            "/v1/models": TABBY_LIST, "/v1/model": TABBY_MODEL,
            "/running": {"running": [{"model": "Gemma-4-31B", "state": "ready"}]}}))}
    assert found["Gemma-4-31B"]["context"] == 65536, "/v1/model over the list's max"
    assert found["Gemma-4-31B"]["caps"] == ["chat", "tools", "vision"]
    assert found["Other"]["context"] == 49152, "cache_size over max_seq_len"
    assert kinds.window_of({"id": "x", "context_length": 131072, "meta": {"n_ctx": None}}) \
        == 131072, "no running number: the context the list states"


PROVIDERS = {
    "home": {"kind": "llama-server", "label": "home", "base_url": "http://home:8080/v1",
             "models": {"qwen3.5-9b": {"caps": ["chat", "tools", "vision"]},
                        "two-slots": {"caps": ["chat"]},
                        "qwen3-embedding": {"caps": ["embeddings"]}}},
    "gpu": {"kind": "tabby", "label": "gpu", "base_url": "http://gpu:11434/v1",
            "models": {"Gemma-4-31B": {"caps": ["chat"]}}},
}


@pytest.fixture
def router(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS)
    cfg = boot.load()

    def answer(req):
        host, path = req.url.host, req.url.path
        if host == "home":
            return httpx.Response(200, json=LLAMA_SERVER)
        routes = {"/v1/models": TABBY_LIST, "/v1/model": TABBY_MODEL, "/running": {
            "running": [{"model": "Gemma-4-31B", "state": "ready"}]},
            "/upstream/Gemma-4-31B/props": PROPS}
        return httpx.Response(200, json=routes.get(path, {}))
    t = httpx.MockTransport(answer)
    return Router(cfg, None, transport=t, sync_transport=t)


async def test_the_servers_caps_count_over_the_saved_ones(router):
    s = router.s
    assert backend_config(s, "home/qwen3.5-9b").raw["images"], "nothing stated yet: saved"
    await router.ping_once()
    assert kinds.server_caps("http://home:8080/v1", "qwen3.5-9b") == ["chat", "tools"]
    assert not backend_config(s, "home/qwen3.5-9b").raw["images"], "the server: no vision"
    assert backend_config(s, "gpu/Gemma-4-31B").raw["images"], "the server: vision"
    assert backend_config(s, "gpu/Gemma-4-31B").raw["tools"]
    assert kinds.server_caps("http://home:8080/v1", "qwen3-embedding") is None
    assert kinds.caps_of(s.providers["home"], "qwen3-embedding") == ["embeddings"], \
        "it states nothing: saved"
    assert her_models.allowed(s) == ["home/qwen3.5-9b", "home/two-slots", "gpu/Gemma-4-31B"]
    with pytest.raises(Invalid, match="no 'vision' capability"):
        validate_role("vision", {"chain": ["home/qwen3.5-9b"]}, s.providers)
    validate_role("vision", {"chain": ["gpu/Gemma-4-31B"]}, s.providers)


async def test_a_vision_twin_sees_as_its_server_says(router):
    p = {"base_url": "http://home:8080/v1", "models": {
        "text": {"caps": ["chat", "tools", "vision"], "vision_twin": "text-vision"}}}
    kinds.stated_caps("http://home:8080/v1", {"text": ["chat", "tools"],
                                              "text-vision": ["chat", "vision"]})
    assert kinds.caps_of(p, "text") == ["chat", "tools", "vision"]
    kinds.stated_caps("http://home:8080/v1", {"text": ["chat", "tools"],
                                              "text-vision": ["chat"]})
    assert kinds.caps_of(p, "text") == ["chat", "tools"]
    kinds.stated_caps("http://home:8080/v1", {"text": ["chat", "tools"]})
    assert kinds.caps_of(p, "text") == ["chat", "tools", "vision"], \
        "the twin's server states nothing: the saved vision"


async def test_loaded_is_what_the_status_says_and_windows_are_the_running_ones(router):
    await router.ping_once()
    assert router.provider_state("home").loaded == {"two-slots", "qwen3-embedding"}, \
        "a sleeping model is not loaded"
    assert {m["ref"]: m["loaded"] for m in router.loaded_view()["models"]} == {
        "home/qwen3.5-9b": False, "home/two-slots": True, "home/qwen3-embedding": True,
        "gpu/Gemma-4-31B": True}
    assert tokens.server_window("http://home:8080/v1", "qwen3.5-9b") == 32768
    assert tokens.server_window("http://gpu:11434/v1", "Gemma-4-31B") == 65536


async def test_her_model_list_says_window_caps_host_and_loaded(router, tmp_path):
    await router.ping_once()
    cfg = router.cfg
    llm = LLM(cfg, router=router)
    skill = Models(cfg)
    skill.app = types.SimpleNamespace(llm=llm)
    ctx = ToolContext(conversation_id="c", turn_id="t", tool_call_id="x", store=None,
                      config=None, emit=None)  # type: ignore[arg-type]
    lines = (await skill.list(ctx)).content.splitlines()
    assert lines[0] == ("home/qwen3.5-9b: qwen3.5-9b on home. Its context window is 32,768 "
                        "tokens. It does not see images. It calls tools. Its server "
                        "answers. It is not loaded.")
    assert lines[2] == ("gpu/Gemma-4-31B: Gemma-4-31B on gpu. Its context window is 65,536 "
                        "tokens. It sees images. It calls tools. Its server answers. It is "
                        "loaded.")


async def test_a_report_carries_the_models_tokens_and_seconds(store):
    parent = await conversations.create(store)
    sub = await conversations.create(store, "s", agent_of=parent, agent_name="s")

    def rows(c):
        for tid, start, end in (("t0", "2026-10-29T10:00:00.000+00:00",
                                 "2026-10-29T10:00:30.000+00:00"),
                                ("t1", "2026-10-29T11:00:00.000+00:00",
                                 "2026-10-29T11:00:40.000+00:00"),
                                ("t2", "2026-10-29T11:01:00.000+00:00",
                                 "2026-10-29T11:01:02.500+00:00")):
            c.execute("INSERT INTO turns(id, conversation_id, state, created_at, finished_at) "
                      "VALUES(?,?,'done',?,?)", (tid, sub, start, end))
        for mid, tid, ref, p, o, at in (
                ("m0", "t0", "home/a", 999, 9, "2026-10-29T10:00:10.000+00:00"),
                ("m1", "t1", "gpu/b", 1000, 100, "2026-10-29T11:00:10.000+00:00"),
                ("m2", "t2", "home/a", 2345, 600, "2026-10-29T11:01:01.000+00:00")):
            c.execute("INSERT INTO requests(message_id, step, turn_id, provider, model, "
                      "body, prompt_tokens, completion_tokens, created_at) "
                      "VALUES(?,0,?,?,?,x'',?,?,?)", (mid, tid, ref, ref, p, o, at))
        # its last report came between t0 and t1: t0 is in that one
        c.execute("INSERT INTO intercom(created_at, from_addr, to_addr, kind) "
                  "VALUES('2026-10-29T10:30:00.000+00:00', ?, ?, 'report')",
                  (f"agent:{sub}", f"agent:{parent}"))
    await store.write(rows)
    w = await store.read(lambda c: work(c, sub))
    assert w == {"models": ["gpu/b", "home/a"], "prompt_tokens": 3345,
                 "completion_tokens": 700, "seconds": 42}
    assert work_line(w) == "[On gpu/b, home/a: 3,345 tokens in, 700 out, 42 s]"
    assert work_line({"models": []}) == ""
