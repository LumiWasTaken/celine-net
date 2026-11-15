"""The First look: a System One decision model (backends/systemone.py) answers Bastet's
questions first; only what it flags reaches the Decision model, whose yes needs its
quote (guard/questions.FirstLook, guard/judge.ask). On waits at most flow.ON_LIMIT."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from celine.backends import systemone
from celine.backends.base import BackendError, Done
from celine.guard import flow, questions
from celine.guard.small_role import Unanswerable, Unavailable
from celine.settings import kinds

pytestmark = pytest.mark.anyio

D3 = {"id": "d3-mini", "object": "model",
      "architecture": {"input_modalities": ["text"], "output_modalities": ["decisions"]},
      "capabilities": {"vision": False, "function_calling": False},
      "supported_parameters": []}


def test_a_model_list_entry_with_decision_output_is_a_decision_model_only():
    assert kinds.reported_caps(D3) == ["decisions"]
    chat = {**D3, "architecture": {"input_modalities": ["text"],
                                   "output_modalities": ["text"]},
            "supported_parameters": ["tools"]}
    assert kinds.reported_caps(chat) == ["chat", "tools"]


def _backend(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(systemone.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    cfg = SimpleNamespace(id="cube", model="d3-mini", label="cube", kind="llama-server",
                          base_url="http://gpu.example/v1", api_key=None, headers={},
                          params={}, raw={"decisions": True})
    return systemone.SystemOneBackend(cfg)


async def test_a_decision_model_gets_the_state_and_questions_and_yields_its_answers(
        monkeypatch):
    seen = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"answers": {"leak": {"type": "noul", "noul": 0.8}},
                                         "usage": {"prompt_tokens": 40}})
    b = _backend(monkeypatch, handle)
    qs = {"leak": {"type": "noul", "instructions": "Does it leak?"}}
    out = [e async for e in b.stream([], [], {"systemone": {"state": "s", "questions": qs}})]
    assert seen == {"url": "http://gpu.example/v1/systemone",
                    "body": {"model": "d3-mini", "state": "s", "questions": qs}}
    assert isinstance(out[0], Done) and out[0].raw["answers"]["leak"]["noul"] == 0.8


async def test_a_decision_model_asked_to_chat_or_answering_without_decisions_fails(
        monkeypatch):
    b = _backend(monkeypatch, lambda req: httpx.Response(200, json={"choices": []}))
    with pytest.raises(BackendError, match="decision model"):
        [e async for e in b.stream([{"role": "user", "content": "hi"}], [])]
    with pytest.raises(BackendError, match="without decisions"):
        [e async for e in b.stream([], [], {"systemone": {"state": "s", "questions": {}}})]


STATE = {"intent": "Owner: search", "call": {"tool": "web__search", "what": "Search.",
                                             "arguments": {"query": "12 Example Road"}},
         "sink": "public", "facts": ["private piece: address"]}


async def test_only_what_the_first_look_flags_reaches_the_decision_model(monkeypatch):
    asked: dict = {}

    async def decide(llm, state, qs, **_):
        asked["first"] = sorted(qs)
        return {"leak": {"noul": 0.9}, "escape": {"noul": 0.01}}, "d3-mini"

    class Second:
        def __init__(self, *a, **k):
            pass

        async def __call__(self, state, qs):
            asked["second"] = sorted(qs)
            return questions.Answers({"leak": 1.0}, "qwen", 5, {"leak": "12 Example Road"},
                                     {"leak": "It sends the address."})
    monkeypatch.setattr(questions, "decide", decide)
    monkeypatch.setattr(questions, "SmallRole", Second)
    cfg = SimpleNamespace(substitute=lambda t, **_: t)
    got = await questions.FirstLook(None, cfg, {"id": "t", "conversation_id": "c"})(
        STATE, {"leak": "Leak?", "escape": "Escape?"})
    assert asked == {"first": ["escape", "leak"], "second": ["leak"]}
    assert got.yes("leak") and not got.yes("escape")
    assert got.p == {"leak": 1.0, "escape": 0.01} and got.model == "d3-mini, qwen"


async def test_a_first_look_with_nothing_flagged_asks_no_decision_model(monkeypatch):
    async def decide(llm, state, qs, **_):
        return {n: {"noul": 0.05} for n in qs}, "d3-mini"

    class Second:
        def __init__(self, *a, **k):
            raise AssertionError("the Decision model is not asked")
    monkeypatch.setattr(questions, "decide", decide)
    monkeypatch.setattr(questions, "SmallRole", Second)
    cfg = SimpleNamespace(substitute=lambda t, **_: t)
    got = await questions.FirstLook(None, cfg, {"id": "t", "conversation_id": "c"})(
        STATE, {"leak": "Leak?"})
    assert not got.yes("leak") and got.p == {"leak": 0.05}


async def test_on_stops_waiting_for_a_judge_after_its_limit(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(flow.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(flow, "RETRY", 0)

    async def away():
        clock["t"] += 100.0
        raise Unavailable("the model server is loading another model.")
    with pytest.raises(Unanswerable, match="No answer in 300 s. the model server is loading"):
        await flow._until(away, "on", "web__search")
    assert clock["t"] == 300.0
