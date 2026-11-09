"""The agent's own model access (skills/models.py): only its models, its messages with a system
prompt and temperature go out as they are, through the queue at its turn's priority,
and the call fails when that model fails, with no other model tried."""
from __future__ import annotations

import types

import pytest

from celine import boot
from celine.backends.base import BackendError, Done, TextDelta
from celine.backends.llm import LLM
from celine.backends.openai import LlamaServerBackend
from celine.backends.queue import Priority
from celine.skills import ToolContext
from celine.skills.models import Models
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

PROVIDERS = {
    "main": {"kind": "llama-swap", "base_url": "http://main:11434/v1",
             "models": {"big": {"caps": ["chat", "tools"]}, "small": {"caps": ["chat"]},
                        "other": {"caps": ["embeddings"]}}},
}
ROLES = {"chat": {"chain": ["main/big"]}, "small": {"chain": ["main/small"]}}


class Rec(LlamaServerBackend):
    """Keeps each call's body and model, answers one line; `fail` fails it."""
    calls: list[tuple[str, dict]] = []
    fail = False

    async def stream(self, messages, tools, body=None, model=None):
        Rec.calls.append((self.model, self.request_body(messages, tools, body, model=model)))
        if Rec.fail:
            raise BackendError("out of memory")
        yield TextDelta(text=" a grey cat called Ash ")
        yield Done(reason="stop")


@pytest.fixture
def skill(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=ROLES)
    cfg = boot.load()
    cfg.llm = {"queue": {"wait_seconds": 5}}
    llm = LLM(cfg, build=lambda bc: Rec(bc, 5))
    llm.turn_priority["t1"] = Priority.INTERACTIVE
    Rec.calls, Rec.fail = [], False
    s = Models(cfg)
    s.app = types.SimpleNamespace(llm=llm)
    return s


CTX = ToolContext(conversation_id="c1", turn_id="t1", tool_call_id="x", store=None,
                  config=None, emit=None)  # type: ignore[arg-type]


async def test_ask_sends_its_messages_to_exactly_that_model(skill):
    sched, seen = skill.app.llm.scheduler, []
    acquire = sched.acquire

    async def recorded(t, **kw):
        seen.append(t.priority)
        return await acquire(t, **kw)
    sched.acquire = recorded
    r = await skill.ask(CTX, model="main/small", system="be brief", temperature=0.2,
                        messages=[{"role": "user", "content": "a cat name?"}])
    assert not r.is_error and r.content == " a grey cat called Ash ", "as the model said it"
    assert seen == [Priority.INTERACTIVE], "in the queue, at its turn's priority"
    [(model, body)] = Rec.calls
    assert model == "small" and body["temperature"] == 0.2
    assert body["messages"] == [{"role": "system", "content": "be brief"},
                                {"role": "user", "content": "a cat name?"}]
    r = await skill.ask(CTX, model="main/big", messages="just a line")
    assert Rec.calls[-1][1]["messages"] == [{"role": "user", "content": "just a line"}]


async def test_only_chat_models_and_a_failure_is_the_calls(skill):
    r = await skill.ask(CTX, model="main/other", messages=[{"role": "user", "content": "x"}])
    assert r.is_error and "main/big, main/small" in r.content and not Rec.calls
    bad = await skill.ask(CTX, model="main/big", messages=[{"role": "robot", "content": "x"}])
    assert bad.is_error and not Rec.calls
    Rec.fail = True
    r = await skill.ask(CTX, model="main/big", messages=[{"role": "user", "content": "x"}])
    assert r.is_error and "out of memory" in r.content
    assert [m for m, _ in Rec.calls] == ["big"], "no other model is tried"


async def test_a_cloud_model_the_agent_picked_asks_first_and_a_denial_sends_nothing(
        tmp_path, monkeypatch):
    providers = {**PROVIDERS, "or": {"kind": "openai", "label": "Far",
                                     "base_url": "https://api.example.com/v1",
                                     "models": {"glm": {"caps": ["chat"]},
                                                "kimi": {"caps": ["chat"]}}}}
    scratch(tmp_path, monkeypatch, providers=providers,
            roles={**ROLES, "writer": {"chain": ["or/kimi"]}})
    cfg = boot.load()
    cfg.llm = {"queue": {"wait_seconds": 5}}
    llm = LLM(cfg, build=lambda bc: Rec(bc, 5))
    asked, answer = [], ["Lumi denied it; nothing was done."]

    class Approvals:
        async def ask(self, kind, conversation, action, *, facts=(), detail=None, **kw):
            asked.append((kind, action, list(facts), detail))
            return answer[0]
    Rec.calls, Rec.fail = [], False
    s = Models(cfg)
    s.app = types.SimpleNamespace(llm=llm, approvals=Approvals())
    listed = (await s.list(CTX)).content.splitlines()
    marks = [line.split(":")[0] for line in listed if "asks" in line]
    assert marks == ["or/glm"], "only the cloud model nobody listed asks first"
    assert listed[-2].endswith(f"Using it asks {cfg.user} first.")

    hi = [{"role": "user", "content": "hi"}]
    r = await s.ask(CTX, model="or/glm", messages=hi)
    assert r.is_error and r.content == answer[0] and Rec.calls == [], "nothing went out"
    assert asked == [("model", "send messages to a cloud model",
                      [("Model", "glm"), ("Provider", "Far")], ("Messages", "user: hi"))]
    answer[0] = None
    assert not (await s.ask(CTX, model="or/glm", messages=hi)).is_error
    assert len(asked) == 2 and len(Rec.calls) == 1, "every call asks"
    assert not (await s.ask(CTX, model="or/kimi", messages=hi)).is_error
    assert not (await s.ask(CTX, model="main/big", messages=hi)).is_error
    assert len(asked) == 2, "a model in a role's list and a local one never ask"
