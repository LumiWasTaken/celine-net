"""The app with its big GPU server gone: a server seen down is left out of every chain at once, background work answers on the next model
of its task, the user's own turn fails with the reason, and waiting work asks it again
once RETRY_DOWN has passed. Nothing polls it in between. Fake servers only."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from celine.backends.base import BackendError, Done, TextDelta
from celine.backends.llm import LLM
from celine.backends.queue import Priority
from celine.backends import router as router_mod
from celine.backends.router import NoModel, Router
from celine.boot import load
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

PROVIDERS = {
    "gpu": {"kind": "llama-swap", "label": "llama-swap", "base_url": "http://gpu:11434/v1",
            "models": {"big": {"label": "Big", "caps": ["chat", "tools"]}}},
    "lan": {"kind": "ollama", "label": "gpu-box", "base_url": "http://lan:11434/v1",
            "models": {"small": {"label": "Small", "caps": ["chat", "tools"]}}},
}


class Model:
    """A chat model behind a base_url; the one on `gpu` is gone while `gone` says so."""

    def __init__(self, ref: str, url: str, gone: dict):
        self.name, self.model, self.calls = ref, ref.split("/")[1], 0
        self.cfg = SimpleNamespace(id=ref, base_url=url, raw={}, label=ref)
        self.supports_tools, self.supports_images = True, False
        self.gone = gone

    async def stream(self, messages, tools, body=None):
        self.calls += 1
        if "//gpu:" in self.cfg.base_url and self.gone["gpu"]:
            err = BackendError("Couldn't reach llama-swap: ConnectTimeout.")
            err.unreachable = True
            raise err
        yield TextDelta(f"from {self.model}")
        yield Done(reason="stop")


def _llm(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS,
            roles={"chat": {"chain": ["gpu/big", "lan/small"]}})
    cfg = load()
    cfg.llm = {"queue": {"wait_seconds": 5, "background_wait_seconds": 5}}
    gone = {"gpu": True, "asks": 0}

    async def net(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gpu":
            gone["asks"] += 1
            if gone["gpu"]:                 # the machine is off: no answer at all
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.Response(200, json={"running": []})
        return httpx.Response(200, json={"models": [], "data": []})
    models = {f"{pid}/{name}": Model(f"{pid}/{name}", p["base_url"], gone)
              for pid, p in PROVIDERS.items() for name in p["models"]}
    llm = LLM(cfg, build=lambda bc: models[bc.id],
              router=Router(cfg, None, transport=httpx.MockTransport(net)))
    return llm, models, gone


async def _chat(llm, cid):
    b = llm.bind(role="chat", priority=Priority.AGENT, conversation_id=cid)
    out = [ev async for ev in b.stream([{"role": "user", "content": "hi"}], [])]
    assert isinstance(out[-1], Done)
    return out[0].text


async def test_a_server_seen_down_is_left_out_at_once_and_asked_again_after_retry_down(
        tmp_path, monkeypatch):
    llm, models, gone = _llm(tmp_path, monkeypatch)
    await llm.router.check_all()
    assert llm.router.hosts()["gpu"] == "down"
    asked = gone["asks"]
    for i in range(3):
        assert await _chat(llm, f"c{i}") == "from small"
    assert models["gpu/big"].calls == 0 and gone["asks"] == asked, \
        "no call waits for the missing server"
    gone["gpu"] = False
    llm.router._providers["gpu"].checked -= router_mod.RETRY_DOWN
    assert await _chat(llm, "c9") == "from big", "the next call asks it again"
    assert llm.router.hosts()["gpu"] == "up"


async def test_the_users_turn_fails_with_the_reason_and_tries_no_other(tmp_path, monkeypatch):
    llm, models, _ = _llm(tmp_path, monkeypatch)
    b = llm.bind(role="chat", priority=Priority.INTERACTIVE, conversation_id="c",
                 fallback=False)
    with pytest.raises(NoModel, match="llama-swap at gpu:11434 did not answer"):
        [ev async for ev in b.stream([{"role": "user", "content": "hi"}], [])]
    assert models["gpu/big"].calls == 0 and models["lan/small"].calls == 0


async def test_the_users_turn_asks_a_host_seen_down_once_more_before_it_fails(
        tmp_path, monkeypatch):
    llm, models, gone = _llm(tmp_path, monkeypatch)
    await llm.router.check_all()
    assert llm.router.hosts()["gpu"] == "down"
    gone["gpu"] = False                     # back, and RETRY_DOWN has not passed
    b = llm.bind(role="chat", priority=Priority.INTERACTIVE, conversation_id="c",
                 fallback=False)
    out = [ev async for ev in b.stream([{"role": "user", "content": "hi"}], [])]
    assert out[0].text == "from big" and models["gpu/big"].calls == 1
