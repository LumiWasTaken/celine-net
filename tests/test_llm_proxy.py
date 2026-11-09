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


def ask(c, model="main/big", key=KEY, headers=None, **extra):
    return c.post("/llm/v1/chat/completions",
                  headers={"Authorization": f"Bearer {key}", **(headers or {})},
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


async def test_a_call_through_a_reverse_proxy_is_refused(proxy):
    """The box reaches the app directly; a forwarded header means the call came in
    through the public origin, where the box's key must not open anything."""
    for name in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
        r = await ask(proxy, headers={name: "203.0.113.9"})
        assert r.status_code == 403, name
    r = await proxy.get("/llm/v1/models", headers={"Authorization": f"Bearer {KEY}",
                                                   "X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403
    assert len(proxy.up.seen) == 0
    assert (await ask(proxy)).status_code == 200, "straight from the box it still works"


def test_the_stack_keys_are_secrets(stack_key, tmp_path):
    from celine import keys
    from celine.errors import mask, set_secrets
    stack_key("shellbox-llm", KEY)
    set_secrets(keys.values(tmp_path / "var"), "keys")
    try:
        assert mask(f"OPENAI_API_KEY={KEY}") == "OPENAI_API_KEY=****"
        assert len(keys.values(tmp_path / "var")) == len(keys.NAMES)
    finally:
        set_secrets([], "keys")


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
    from celine.settings import kinds
    from celine.turns import tokens
    s = proxy.cfg.settings
    s.providers["main"]["models"]["small"]["context"] = 65536
    tokens.running_windows("http://main:8080/v1", {"big": 262144})
    kinds.reported_outputs("http://main:8080/v1", {"big": 32768})
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


async def test_a_device_gets_its_local_models_and_the_cloud_one_a_drone_there_was_approved_for(
        proxy, store, monkeypatch):
    from celine import devices
    from celine.conversations import store as conversations
    from celine.devices import Device
    from celine.settings import conversations as conv
    monkeypatch.setattr(devices, "devices", lambda cfg: {"mac": Device(
        "mac", "Her Mac", "http://mac:4096", "pw", "dev-key", "", "", None)})
    monkeypatch.setattr(proxy.cfg.settings, "key_missing", lambda pid: False)

    async def ids(key):
        r = await proxy.get("/llm/v1/models", headers={"Authorization": f"Bearer {key}"})
        return [m["id"] for m in r.json()["data"]]
    assert await ids(KEY) == ["main/big", "main/small", "default"], \
        "nothing approved: the box gets no cloud model either"
    assert await ids("dev-key") == ["main/big", "main/small", "default"]
    proxy.cfg.user = "Joe"
    refused = await ask(proxy, model="or/x/glm", key="dev-key")
    assert refused.status_code == 403 and refused.json()["error"]["message"] == (
        "or/x/glm is a cloud model that needs Joe's approval: sub_agents__start with "
        "device mac on that model asks for it.")
    parent = await conversations.create(store)
    elsewhere = await conversations.create(store, title="pc", agent_of=parent,
                                           agent_name="pc", access="work",
                                           agent_cli="opencode", agent_device="pc")
    await conv.apply(store, elsewhere, {"model": "or/x/glm"})
    assert await ids("dev-key") == ["main/big", "main/small", "default"], \
        "approved for a drone on another device"
    sub = await conversations.create(store, title="arcade", agent_of=parent,
                                     agent_name="arcade", access="work",
                                     agent_cli="opencode", agent_device="mac")
    await conv.apply(store, sub, {"model": "or/x/glm"})
    # no turn of it is live: opencode's late title call after its turn still gets through
    assert await ids("dev-key") == ["main/big", "main/small", "or/x/glm", "default"]
    assert (await ask(proxy, model="or/x/glm", key="dev-key")).status_code == 200
    # long done: the approval served that drone, not the device for good
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET created_at='2020-01-01T00:00:00.000+00:00' WHERE id=?",
        (sub,)))
    assert await ids("dev-key") == ["main/big", "main/small", "default"]


async def test_the_box_gets_a_cloud_model_only_for_approved_work(proxy, store, monkeypatch,
                                                                clock, db_path):
    """A cloud model reaches the box when the user listed it in a role, while a CLI
    sub-agent approved on it works, or once code__start was approved on it (a grant
    each call keeps alive, kept across a restart); any other is refused with the
    sentence that names what asks the user."""
    from celine.conversations import store as conversations
    from celine.core.store import Store
    from celine.settings import conversations as conv, agent_models
    s = proxy.cfg.settings
    proxy.cfg.user = "Joe"
    monkeypatch.setattr(s, "key_missing", lambda pid: False)
    monkeypatch.setattr(agent_models, "_now", lambda: clock.at)

    async def ids():
        r = await proxy.get("/llm/v1/models", headers={"Authorization": f"Bearer {KEY}"})
        return [m["id"] for m in r.json()["data"]]
    refused = await ask(proxy, model="or/x/glm")
    assert refused.status_code == 403 and refused.json()["error"]["message"] == (
        "or/x/glm is a cloud model that needs Joe's approval: code__start, or "
        "sub_agents__start with cli, on that model asks for it.")
    assert len(proxy.up.seen) == 0

    await store.write(lambda c: agent_models.grant_sync(c, "or/x/glm"))  # code__start's
    assert "or/x/glm" in await ids()
    clock.advance(agent_models.BOX_GRANT - 1)
    assert (await ask(proxy, model="or/x/glm")).status_code == 200
    clock.advance(agent_models.BOX_GRANT - 1)
    assert "or/x/glm" in await ids(), "the call kept the approval alive"
    async with anyio.create_task_group() as tg:    # the app restarts on the same file
        again = Store(db_path)
        await again.start(tg)
        proxy.services.store = again
        assert "or/x/glm" in await ids(), "the restart kept the approval"
        await again.aclose()
    proxy.services.store = store
    clock.advance(agent_models.BOX_GRANT)
    assert "or/x/glm" not in await ids(), "an idle grant runs out"

    parent = await conversations.create(store)
    sub = await conversations.create(store, title="scout", agent_of=parent,
                                     agent_name="scout", access="work",
                                     agent_cli="opencode")
    await conv.apply(store, sub, {"model": "or/x/glm"})
    assert "or/x/glm" in await ids(), "a CLI sub-agent approved on it, just started"
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET created_at='2020-01-01T00:00:00.000+00:00' WHERE id=?",
        (sub,)))
    assert "or/x/glm" not in await ids()

    monkeypatch.setattr(s, "roles", {**s.roles, "writer": {"chain": ["or/x/glm"]}})
    assert "or/x/glm" in await ids(), "the user listed it in a role"


async def test_a_provider_over_its_requests_per_hour_answers_429(proxy, monkeypatch):
    s = proxy.cfg.settings
    main = {**s.providers["main"], "options": {"max_per_hour": 1}}
    monkeypatch.setattr(s, "providers", {**s.providers, "main": main})
    proxy.llm.router.now = lambda: 50.0
    assert (await ask(proxy)).status_code == 200
    over = await ask(proxy)
    assert over.status_code == 429 and over.headers["retry-after"] == "3600"
    assert over.json()["error"]["message"] == "main reached its 1 request this hour."
    assert len(proxy.up.seen) == 1, "the provider was asked once"
