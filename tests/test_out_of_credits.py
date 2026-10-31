"""OpenRouter out of credits: the turn fails with the error, as any other failure, and
nothing waits for the provider. Fakes only: the OpenRouter model answers 402 while
`broke`."""
from __future__ import annotations

from chat_fixtures import until, user_says

import copy
from types import SimpleNamespace

import anyio
import httpx
import pytest

from celine.conversations import store as conversations, view
from celine.backends.base import Done, TextDelta
from celine.backends.llm import LLM
from celine.backends.openai import http_error
from celine.backends.router import Router
from celine.boot import load
from celine.core.bus import EventBus
from celine.settings.secrets import set_key
from celine.turns.dispatcher import Dispatcher
from settings_fixtures import KEY, scratch

pytestmark = pytest.mark.anyio

PROVIDERS = {
    "or": {"kind": "openrouter", "label": "OpenRouter",
           "base_url": "https://openrouter.ai/api/v1", "models": {"x/y": {"caps": ["chat"]}}},
    "local": {"kind": "llama-server", "label": "gpu-box", "base_url": "http://local:8080/v1",
              "models": {"small": {"caps": ["chat"]}}},
}
ROLES = {"chat": {"chain": ["or/x/y", "local/small"]}}


class Net:
    supports_tools, supports_images = True, False

    def __init__(self, ref: str, url: str, model: str):
        self.name, self.model, self.broke = ref, model, False
        self.cfg = SimpleNamespace(id=ref, base_url=url, raw={}, label=ref)
        self.asked: list[str] = []      # the last user message of each request, no time

    async def stream(self, messages, tools, body=None):
        from celine import prompts
        from celine.turns import title
        if messages[0]["content"] != prompts.text(title.__file__, "title.md"):   # a title asks the chat model too
            last = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            text = last.rsplit("\n\nToday is ", 1)[0]     # before the last tail
            self.asked.append(text.split("\n", 1)[-1])
        if self.broke:
            raise http_error("OpenRouter", self.model, 402,
                             '{"error": {"code": 402, "message": "Insufficient credits."}}')
        yield TextDelta("ok")
        yield Done(reason="stop")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=ROLES)
    set_key(tmp_path / "var" / "secrets", "or", KEY)
    c = copy.copy(load())
    c.settings.general.update({"max_concurrent_turns": 1})
    c.llm = {"queue": {"wait_seconds": 5, "background_wait_seconds": 5}}
    return c


def app(cfg, store):
    """Fresh nets and a dispatcher on a fresh LLM, as after a start."""
    nets = {f"{pid}/{name}": Net(f"{pid}/{name}", p["base_url"], name)
            for pid, p in PROVIDERS.items() for name in p["models"]}
    router = Router(cfg, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"data": []})))
    bus = EventBus()
    return Dispatcher(store, cfg, bus, None, None,
                      llm=LLM(cfg, build=lambda bc: nets[bc.id], router=router)), bus, nets


async def turns_are(store, *want: tuple[str, str, str | None]) -> None:
    """Waits until each (conversation, state, reason code or None) holds."""
    async def ok(cid, st, code):
        t = (await view.snapshot(store, cid))["turn"]
        return t is not None and t["state"] == st and (code is None or t["reason_code"] == code)

    async def all_ok():
        return all([await ok(*w) for w in want])
    await until(all_ok, 5)


async def test_out_of_credits_fails_the_turn_like_any_other_error(store, cfg):
    d, _, nets = app(cfg, store)
    opr, local = nets["or/x/y"], nets["local/small"]
    opr.broke = True
    c1 = await conversations.create(store)
    async with anyio.create_task_group() as tg:
        await d.start(tg)
        await user_says(store, c1, "first")
        d.notify()
        await turns_are(store, (c1, "failed", "out_of_credits"))
        errors = [e["data"] for e in (await view.resume(store, c1, 0))["events"]
                  if e["type"] == "error"]
        assert errors[-1]["code"] == "out_of_credits" and \
            "OpenRouter is out of credits." in errors[-1]["message"]
        assert opr.asked == ["first"] and local.asked == []
        # nothing is held: the next message asks OpenRouter again
        opr.broke = False
        await user_says(store, c1, "second")
        d.notify()
        await turns_are(store, (c1, "done", None))
        assert len(opr.asked) == 2 and opr.asked[1].endswith("second")
        await d.stop()
        tg.cancel_scope.cancel()
