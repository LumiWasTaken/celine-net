"""The model proxy for its shellbox (api/llm_proxy.py): its own key, only its models,
the shellbox lane at background priority, and the provider's answer byte for byte
(SSE and JSON, errors included)."""
from __future__ import annotations

import json

import anyio
import httpx
import pytest
from chat_fixtures import until

from celine import boot
from celine.backends.llm import LLM
from celine.backends.queue import Priority
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio

KEY = "box-key-0123456789abcdef"
PROVIDERS = {
    "main": {"kind": "llama-swap", "base_url": "http://main:8080/v1",
             "models": {"big": {"caps": ["chat", "tools"]}, "small": {"caps": ["chat"]},
                        "emb": {"caps": ["embeddings"]}}},
    "or": {"kind": "openrouter", "base_url": "https://openrouter.ai/api/v1",
           "models": {"x/glm": {"caps": ["chat"]}}},
}
ROLES = {"chat": {"chain": ["main/big"]}, "small": {"chain": ["main/small"]},
         "embeddings": {"chain": ["main/emb"]}}
SSE = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
       b'data: {"choices":[],"usage":{"prompt_tokens":30,"completion_tokens":12}}\n\n'
       b'data: [DONE]\n\n')


class Upstream:
    """The provider: records each request, answers SSE (or `reply`), and holds each
    answer until `go` is set when `hold` is on."""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.hold = False
        self.go = anyio.Event()
        self.reply: httpx.Response | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.hold:
            await self.go.wait()
        return self.reply or httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=SSE)


@pytest.fixture
async def proxy(tmp_path, monkeypatch, store, stack_key):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=ROLES)
    stack_key("shellbox-llm", KEY)
    cfg = boot.load()
    up = Upstream()
    app = make_app(cfg, store)
    from celine.api import llm_proxy
    app.include_router(llm_proxy.router)
    llm = app.state.services.llm = LLM(cfg)
    llm.router.transport = httpx.MockTransport(up)
    async with client(app) as c:
        c.up, c.llm, c.cfg, c.services = up, llm, cfg, app.state.services
        yield c


def ask(c, model="main/big", key=KEY, **extra):
    return c.post("/llm/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                  json={"model": model, "stream": True,
                        "messages": [{"role": "user", "content": "hello"}], **extra})


async def test_only_the_box_key_gets_in(proxy, tmp_path):
    assert (await ask(proxy, key="nope")).status_code == 401
    r = await proxy.get("/llm/v1/models")
    assert r.status_code == 401 and "OPENAI_API_KEY" in r.json()["error"]["message"]
    r = await proxy.post("/llm/v1/messages", headers={"Authorization": f"Bearer {KEY}"},
                         json={"model": "main/big", "max_tokens": 5, "messages": []})
    assert r.status_code == 404, "OpenAI's format only"
    (tmp_path / "var" / "keys" / "shellbox-llm.key").unlink()
    r = await ask(proxy)
    assert r.status_code == 503 and "shellbox-llm.key" in r.json()["error"]["message"]
    assert len(proxy.up.seen) == 0, "nothing refused reached the provider"


async def test_models_are_every_chat_model_with_a_key(proxy):
    r = await proxy.get("/llm/v1/models", headers={"Authorization": f"Bearer {KEY}"})
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["main/big", "main/small", "default"], \
        "the chat models whose provider has its key: OpenRouter has none here"
    refused = await ask(proxy, model="or/x/glm")
    assert refused.status_code == 404 and "main/big, main/small" in refused.text
    assert (await ask(proxy, model="main/emb")).status_code == 404, "no chat"
    ok = await ask(proxy, model="default")
    assert ok.status_code == 200
    sent = json.loads(proxy.up.seen[-1].content)
    assert sent["model"] == "big", "the provider's own name, default resolved"
    assert str(proxy.up.seen[-1].url) == "http://main:8080/v1/chat/completions"


async def test_models_carry_the_sizes_the_app_knows(proxy):
    """context_length is the window its turns budget with (a loaded model's /props over
    the context in Settings); max_output_tokens what the provider's list states; a size
    nobody knows is left out."""
    from celine.turns import tokens
    s = proxy.cfg.settings
    s.providers["main"]["models"]["small"]["context"] = 65536
    tokens.running_windows("http://main:8080/v1", {"big": 262144})
    tokens.reported_outputs("http://main:8080/v1", {"big": 32768})
    r = await proxy.get("/llm/v1/models", headers={"Authorization": f"Bearer {KEY}"})
    sizes = {m["id"]: {k: m[k] for k in ("context_length", "max_output_tokens") if k in m}
             for m in r.json()["data"]}
    assert sizes == {"main/big": {"context_length": 262144, "max_output_tokens": 32768},
                     "main/small": {"context_length": 65536},
                     "default": {"context_length": 262144, "max_output_tokens": 32768}}
    tokens.running_windows("http://main:8080/v1", {})
    s.providers["main"]["models"]["small"].pop("context")
    r = await proxy.get("/llm/v1/models", headers={"Authorization": f"Bearer {KEY}"})
    assert "context_length" not in r.json()["data"][1], "unknown: left out, never guessed"


async def test_the_answer_streams_back_as_the_provider_sent_it(proxy):
    r = await ask(proxy)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.content == SSE
    sent = json.loads(proxy.up.seen[0].content)
    assert "stream_options" not in sent, "the body goes on as the CLI sent it"
    assert "authorization" not in proxy.up.seen[0].headers, "llama-swap has no key here"
    proxy.up.reply = httpx.Response(400, json={"error": {"message": "context too long"}})
    bad = await ask(proxy)
    assert bad.status_code == 400 and bad.json()["error"]["message"] == "context too long"


async def test_calls_take_the_shellbox_lane_with_no_lock_of_their_own(proxy):
    proxy.up.hold = True
    sched = proxy.llm.scheduler
    done: list[int] = []

    async def one():
        done.append((await ask(proxy)).status_code)

    async with anyio.create_task_group() as tg:
        tg.start_soon(one)
        tg.start_soon(one)
        await until(lambda: len(proxy.up.seen) >= 2 or sched.waiting, 5)
        t = sched.running[0]
        assert (t.lane, t.priority, t.label) == ("shellbox", Priority.BACKGROUND, "Shellbox")
        assert t.endpoint == "http://main:8080/v1" and t.model == "big"
        proxy.up.go.set()
    assert done == [200, 200] and len(proxy.up.seen) == 2 and not sched.running
