"""The router picks a model that can answer: models left out with a reason, a cloud
model as an entry like any other, a failure only while it is current. Fake servers."""
from __future__ import annotations

import httpx
import pytest

from celine.backends import router as router_mod
from celine.backends.router import Router
from celine.boot import load
from celine.settings.secrets import set_key
from settings_fixtures import KEY, scratch

pytestmark = pytest.mark.anyio

PROVIDERS = {
    "swap": {"kind": "llama-swap", "label": "llama-swap", "base_url": "http://swap:1/v1",
             "models": {"big": {"caps": ["chat"]}, "emb": {"caps": ["embeddings"]}}},
    "oll": {"kind": "ollama", "label": "gpu-box", "base_url": "http://oll:2/v1",
            "models": {"small": {"caps": ["chat", "vision"]}}},
    "srv": {"kind": "llama-server", "label": "server", "base_url": "http://srv:3/v1",
            "models": {"one": {"caps": ["chat"]}}},
    "or": {"kind": "openrouter", "label": "OpenRouter", "base_url": "",
           "models": {"a/b": {"caps": ["chat"]}}},
}


def _router(tmp_path, monkeypatch, roles=None):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=roles or {})
    cfg = load()
    down: set[str] = set()

    def reply(request):
        if request.url.host in down:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"running": [], "models": [], "data": []})
    t = httpx.MockTransport(reply)
    return Router(cfg, None, transport=t, sync_transport=t), down, cfg


async def test_order_leaves_out_what_cannot_answer_and_a_down_server(tmp_path, monkeypatch):
    r, down, cfg = _router(tmp_path, monkeypatch, roles={
        "chat": {"chain": ["or/a/b", "swap/big", "oll/small"]}})
    down.add("swap")
    o = await r.order("chat")
    assert o.refs == ["oll/small"], "a server that is down is left out"
    assert dict(o.skipped)["swap/big"] == "llama-swap does not answer."
    assert set(dict(o.skipped)) == {"or/a/b", "swap/big"}, "each one left out says why"
    set_key(tmp_path / "var" / "secrets", "or", KEY)
    down.discard("swap")
    r._providers["swap"].checked -= router_mod.PING_EVERY   # no ping: asked again when old
    assert (await r.order("chat")).refs == ["or/a/b", "swap/big", "oll/small"]
    # a conversation's own model goes first, and is passed over when it cannot answer
    cfg.settings.conversation_models["c"] = "oll/small"
    assert (await r.order("chat", conversation_id="c")).refs[0] == "oll/small"
    down.add("oll")
    await r.probe("oll", max_age=0)
    assert (await r.order("chat", conversation_id="c")).refs[0] == "or/a/b"
    assert (await r.order("vision")).refs == []


async def test_can_answer_asks_a_down_server_again_and_says_why_not(tmp_path, monkeypatch):
    """A scheduled run waits on this: Nix off is a sentence, Nix back is None, without
    anything else probing the server in between."""
    r, down, _ = _router(tmp_path, monkeypatch, roles={"chat": {"chain": ["swap/big"]}})
    down.add("swap")
    why = await r.can_answer("chat")
    assert why.endswith(" does not answer.") and ":" not in why, "a label, no address"
    down.discard("swap")
    r._providers["swap"].checked = 0.0          # the cached probe has run out
    assert await r.can_answer("chat") is None
    assert "No model is set" in await r.can_answer("vision")


async def test_a_failure_shows_only_while_it_is_current(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS,
            roles={"embeddings": {"chain": ["swap/emb"]}})
    running = {"list": [{"model": "big", "state": "ready"}]}
    t = httpx.MockTransport(lambda req: httpx.Response(200, json={"running": running["list"]}))
    cfg = load()
    r = Router(cfg, None, transport=t, sync_transport=t)

    async def probe():
        r._providers["swap"].checked = 0.0
        await r.probe("swap")

    await r.probe("swap", max_age=0)
    r.note_failed("swap/emb", "could_not_load", "out of memory", frozenset({"big"}))
    r._providers["swap"].checked = 0.0
    assert (await r.order("embeddings")).refs == [], "left out while nothing was freed"
    running["list"] = []                       # the server freed what was loaded then
    await probe()
    assert r.model_state("swap/emb") is None
    # nothing of ours was loaded and it still failed: tried again after a while
    r.note_failed("swap/emb", "could_not_load", "out of memory", frozenset())
    await probe()
    assert r.model_state("swap/emb") is not None
    r._models["swap/emb"].at -= router_mod.RETRY_OUTSIDE_MS
    r._providers["swap"].checked = 0.0
    assert (await r.order("embeddings")).refs == ["swap/emb"]
    # a later answer or a changed setting clears a failure
    r.note_failed("swap/big", "error", "500")
    r.note_ok("swap/big")
    assert r.model_state("swap/big") is None
    r.note_failed("swap/emb", "could_not_load", "out of memory", frozenset({"big"}))
    cfg.settings.providers["swap"]["models"]["emb"]["context"] = 4096
    assert r.model_state("swap/emb") is None


CMD = ("start-llama-server --fit on --fit-ctx 32768 -fa on --parallel 2 --kv-unified --jinja\n"
       " -m /models/big.gguf\n")


def test_cmd_window_reads_the_command_line():
    from celine.settings.kinds import cmd_window
    assert cmd_window(CMD) is None, "--fit-ctx is a floor, the loaded server says"
    assert cmd_window("llama-server -c 65536 --parallel 2") == 32768, "slots split it"
    assert cmd_window("llama-server -c 65536 --parallel 2 --kv-unified") == 65536, \
        "kv-unified: one request may use all of it"
    assert cmd_window("llama-server -m x.gguf") is None
    assert cmd_window(None) is None


async def test_a_running_model_teaches_its_real_window(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from celine.turns import tokens
    scratch(tmp_path, monkeypatch, providers=PROVIDERS)
    running = [{"model": "big", "state": "ready", "cmd": CMD}]
    asked: list[str] = []

    def answer(req):
        asked.append(req.url.path)
        if req.url.path == "/running":
            return httpx.Response(200, json={"running": running})
        if req.url.path == "/upstream/big/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 40960}})
        return httpx.Response(404)
    t = httpx.MockTransport(answer)
    r = Router(load(), None, transport=t, sync_transport=t)
    backend = SimpleNamespace(model="big", cfg=SimpleNamespace(
        base_url="http://swap:1/v1", raw={"context": 116992}))
    assert tokens.window(backend) == 116992
    await r.probe("swap", max_age=0)
    assert tokens.window(backend) == 40960, "the server runs with less than Settings says"
    assert asked == ["/running", "/upstream/big/props"], "props only of a loaded model"
    running.clear()
    await r.probe("swap", max_age=0)
    assert tokens.window(backend) == 116992, "unloaded: the next load may differ"


async def test_the_context_the_server_reports_wins_over_settings(tmp_path, monkeypatch):
    """The server's context is larger than Settings says: the model list the ping reads (the modality router's /v1/models) wins for
    a model that is not loaded yet, so its first turn budgets with the new number.
    Ollama's list counts for its caps only: Settings is its num_ctx, the list says the
    model's most."""
    from types import SimpleNamespace
    from celine.turns import tokens
    providers = {**PROVIDERS, "swap": {**PROVIDERS["swap"], "models": {
        "big": {"caps": ["chat"], "context": 131072}}},
        "oll": {**PROVIDERS["oll"], "models": {"small": {"caps": ["chat"], "context": 8192}}}}
    scratch(tmp_path, monkeypatch, providers=providers)
    ctx = {"big": 262144}
    asked: list[tuple[str, str]] = []

    def answer(req):
        asked.append((req.url.host, req.url.path))
        if req.url.path == "/running":
            return httpx.Response(200, json={"running": []})
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [
                {"id": "big", "context_length": ctx["big"]},
                {"id": "not-added", "context_length": 4096}]})
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {"x.context_length": 131072}})
        return httpx.Response(200, json={"models": [{"name": "small"}], "data": []})
    t = httpx.MockTransport(answer)
    r = Router(load(), None, transport=t, sync_transport=t)
    big = SimpleNamespace(model="big", cfg=SimpleNamespace(
        base_url="http://swap:1/v1", raw={"context": 131072}))
    small = SimpleNamespace(model="small", cfg=SimpleNamespace(
        base_url="http://oll:2/v1", raw={"context": 8192}))
    assert tokens.window(big) == 131072, "no server said anything yet"
    await r.ping_once()
    assert tokens.window(big) == 262144
    assert tokens.server_window("http://swap:1/v1", "not-added") is None, "only added models"
    assert tokens.window(small) == 8192 and ("oll", "/api/show") in asked
    ctx["big"] = 196608
    await r.ping_once()
    assert tokens.window(big) == 262144, "the list is read once a minute"
    await r.discover("swap", force=True)                # "Check provider"
    assert tokens.window(big) == 196608
    ctx["big"] = None
    await r.discover("swap", force=True)
    assert tokens.window(big) == 131072, "the server says nothing now: Settings counts"


async def test_the_loaded_view_says_what_is_loaded_and_what_a_swap_took(tmp_path, monkeypatch):
    """GET /api/loaded-models: llama-swap's models, the loaded one, the measured load
    times; `model.loaded` goes out when that changes. It never names a model for a new
    chat to start on: that stays the chat role's chain."""
    import asyncio
    providers = {**PROVIDERS, "swap": {**PROVIDERS["swap"], "models": {
        "big": {"caps": ["chat"]}, "fast": {"caps": ["chat"]}, "emb": {"caps": ["embeddings"]}}}}
    scratch(tmp_path, monkeypatch, providers=providers,
            roles={"chat": {"chain": ["swap/big", "oll/small"]}})
    running = [{"model": "fast", "state": "ready"}]

    def answer(req):
        return httpx.Response(200, json={"running": running} if req.url.path == "/running" else {})
    t = httpx.MockTransport(answer)
    sent: list[dict] = []
    r = Router(load(), sent.append, transport=t, sync_transport=t)
    r.bind(asyncio.get_running_loop())
    await r.probe("swap", max_age=0)
    view = r.loaded_view()
    assert {m["ref"]: m["loaded"] for m in view["models"]} == {
        "swap/big": False, "swap/fast": True, "swap/emb": False}
    assert set(view) == {"models"}, "no pin to the loaded model"
    assert any(e["type"] == "model.loaded" for e in sent)
    r.note_load("swap/big", 21.04)
    assert next(m for m in r.loaded_view()["models"]
                if m["ref"] == "swap/big")["load_seconds"] == 21.0


def test_the_window_and_counter_follow_the_model_that_answered():
    from types import SimpleNamespace
    from celine.turns import tokens
    head = SimpleNamespace(model="big", cfg=SimpleNamespace(base_url="http://swap:1/v1",
                                                            raw={"context": 100000}))
    other = SimpleNamespace(model="small", cfg=SimpleNamespace(base_url="http://b:1/v1",
                                                               raw={"context": 8192}))
    queued = SimpleNamespace(model="big", cfg=head.cfg, answered=None)
    assert tokens.window(queued) == 100000
    queued.answered = other
    assert tokens.window(queued) == 8192
    tokens.learn_window(queued, 4096)
    assert tokens.window(other) == 4096 and tokens.window(head) == 100000
    assert tokens.counter(queued) is tokens.counter(other)
    assert tokens.counter(queued) is not tokens.counter(head)


async def test_a_busy_host_is_down_only_after_slow_pings_timeouts_in_a_row(tmp_path, monkeypatch):
    """A server with one stream on its only slot answers its list late: one late ping
    must not fail its turn. A refused connection is down at once."""
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles={"chat": {"chain": ["swap/big"]}})
    how = {"swap": "ok"}

    def reply(request):
        if how.get(request.url.host) == "slow":
            raise httpx.ReadTimeout("late", request=request)
        if how.get(request.url.host) == "off":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"running": [], "models": [], "data": []})
    t = httpx.MockTransport(reply)
    r = Router(load(), None, transport=t, sync_transport=t)
    assert (await r.probe("swap", max_age=0)).reachable
    how["swap"] = "slow"
    for _ in range(router_mod.SLOW_PINGS - 1):
        assert (await r.probe("swap", max_age=0)).reachable, "late is not down yet"
    assert (await r.probe("swap", max_age=0)).reachable is False
    how["swap"] = "ok"
    assert (await r.probe("swap", max_age=0)).reachable
    how["swap"] = "slow"
    assert (await r.probe("swap", max_age=0)).reachable, "an answer starts the count again"
    how["swap"] = "off"
    assert (await r.probe("swap", max_age=0)).reachable is False, "refused is down at once"
