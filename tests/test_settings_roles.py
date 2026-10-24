"""Task models at run time: each task follows its list from Settings, a conversation's
own model goes first."""
from __future__ import annotations

import pytest

from celine import boot
from celine.backends.base import Done, TextDelta
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio


class Net:
    """A backend double built from a compiled BackendConfig."""
    made: list = []

    def __init__(self, bc):
        self.cfg, self.name, self.model = bc, bc.id, bc.model
        self.supports_tools = True
        self.supports_images = bool(bc.raw.get("images"))
        self.calls = 0
        Net.made.append(self)

    async def stream(self, messages, tools, body=None):
        self.calls += 1
        yield TextDelta("ok")
        yield Done(reason="stop")


async def test_small_tasks_follow_their_own_list_and_chat_the_conversations_model(
        tmp_path, monkeypatch):
    from celine.backends.llm import LLM
    from celine.backends.queue import Priority
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    llm = LLM(cfg, build=Net)

    async def run(b):
        events = b.stream([{"role": "user", "content": "hi"}], [])
        try:
            return [e async for e in events]
        finally:
            await events.aclose()
    t = llm.bind(role="small", priority=Priority.BACKGROUND, conversation_id="c1")
    await run(t)
    assert Net.made[-1].model == "small" and Net.made[-1].calls == 1
    assert llm.bind(role="chat", priority=Priority.INTERACTIVE, conversation_id="c1").model \
        == "big"
    cfg.settings.conversation_models["c1"] = "lan/small"
    c = llm.bind(role="chat", priority=Priority.INTERACTIVE, conversation_id="c1")
    assert (await run(c))[-1].reason == "stop" and c.answered.model == "small"
    assert c.request_sent["model"] == "small" and \
        c.request_sent["body"]["messages"][-1]["content"] == "hi", "kept as it went out"
    assert llm.bind(role="chat", priority=Priority.AGENT, conversation_id="c1").model == "small"


async def test_a_conversation_runs_under_its_kinds_role(tmp_path, monkeypatch, store):
    """A chat runs on chat, any other kind on tasks; the conversation's pick goes
    first; load_all knows every role before a first turn."""
    from celine.conversations import store as conversations
    from celine.settings import roles
    scratch(tmp_path, monkeypatch, roles={"chat": {"chain": ["main/big"]},
                                          "tasks": {"chain": ["lan/small"]}})
    cfg = boot.load()
    s = cfg.settings
    plain = await conversations.create(store, "plain")
    task = await conversations.create(store, "task", kind="mission")
    for cid, want in [(plain, "chat"), (task, "tasks")]:
        assert await roles.resolve(store, cfg, cid) == want
    assert s.chat_chain(task) == ["lan/small"] and s.chat_chain(plain) == ["main/big"]
    s.conversation_models[task] = "main/big"
    assert s.chat_chain(task) == ["main/big", "lan/small"]
    s.conversation_roles.clear()
    assert await roles.load_all(store, cfg) == 2
    assert s.conversation_roles[task] == "tasks"


def test_a_thinking_word_goes_out_as_a_level_the_model_takes(tmp_path, monkeypatch):
    """main/big takes low, medium and xhigh: a word it lists goes as is, high as the
    strongest at or below it, max as its strongest, a word typed by hand as typed."""
    from celine.backends.llm import LLM
    from celine.settings.thinking import fit
    scratch(tmp_path, monkeypatch)
    llm = LLM(boot.load(), build=Net)
    sent = {w: llm.make("main/big", w).cfg.params["reasoning_effort"]
            for w in ("off", "low", "medium", "high", "max", "xhigh")}
    assert sent == {"off": "none", "low": "low", "medium": "medium", "high": "medium",
                    "max": "xhigh", "xhigh": "xhigh"}
    assert fit("low", ["medium", "high"]) == "medium", "none below: the weakest"
    assert fit("max", []) == "high" and fit("medium", []) == "medium"
