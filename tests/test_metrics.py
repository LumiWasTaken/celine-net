"""The numbers for Prometheus (server/celine/metrics.py): served on their own port only,
refused to anything a proxy forwarded, and counted where the app already passes."""
from __future__ import annotations

import asyncio
import socket

import anyio
import pytest
from prometheus_client import REGISTRY

from chat_fixtures import Scripted, until, user_says
from celine import metrics
from celine.backends.queue import Priority
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.skills import Registry, ToolResult, tool
from celine.turns.dispatcher import Dispatcher
from test_llm_proxy import ask, proxy  # noqa: F401 - the fixture
from test_llm_queue import collect, routed

pytestmark = pytest.mark.anyio


def value(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def calls_of(tool: str) -> float:
    return sum(s.value for m in REGISTRY.collect() if m.name == "celine_tool_calls"
               for s in m.samples
               if s.name.endswith("_total") and s.labels.get("tool") == tool)


def test_metrics_answer_only_requests_that_no_proxy_forwarded():
    ok = metrics.answer(b"GET /metrics HTTP/1.1\r\nHost: celine:8772\r\n\r\n")
    assert ok.startswith(b"HTTP/1.1 200") and b"celine_turns_total" in ok
    for header in (b"X-Forwarded-For: 192.0.2.7", b"Forwarded: for=1.2.3.4",
                   b"x-real-ip: 1.2.3.4"):
        refused = metrics.answer(b"GET /metrics HTTP/1.1\r\n" + header + b"\r\n\r\n")
        assert refused.startswith(b"HTTP/1.1 403"), header
    assert metrics.answer(b"GET / HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 404")
    assert metrics.answer(b"POST /metrics HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 404")


def test_the_apps_own_port_has_no_metrics():
    from celine.__main__ import build_app
    app = build_app(load(), False)
    assert not [r for r in app.routes if getattr(r, "path", "") == "/metrics"]


async def test_the_listener_serves_its_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    task = asyncio.create_task(metrics.serve(port))
    seen: list[bytes] = []

    async def scraped() -> bool:
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            return False                # not listening yet
        w.write(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n")
        seen.append(await r.read())
        w.close()
        return True
    try:
        await until(scraped, 5)
    finally:
        task.cancel()
    assert seen[0].startswith(b"HTTP/1.1 200") and b"celine_model_requests_total" in seen[0]


def test_usage_is_read_from_both_wire_formats():
    openai = (b'data: {"choices":[],"usage":{"prompt_tokens":30,"completion_tokens":12,'
              b'"prompt_tokens_details":{"cached_tokens":4}}}\n\n')
    assert metrics.usage_in(openai) == {"in": 30, "out": 12}
    anthropic = (b'event: message_start\ndata: {"message":{"usage":{"input_tokens":25,'
                 b'"output_tokens":1}}}\n\n...event: message_delta\ndata: {"usage":'
                 b'{"output_tokens":15}}\n\n')
    assert metrics.usage_in(anthropic) == {"in": 25, "out": 15}
    assert metrics.usage_in(b"not json") == {"in": 0, "out": 0}


def test_a_guard_decision_counts_by_mode_verdict_and_short_source():
    before = value("celine_guard_decisions_total", mode="watch", verdict="would_stop",
                   source="reddit.com")
    judged = value("celine_guard_judge_seconds_count")
    metrics.record_guard("would_stop", "reddit.com/r/x", 1200, mode="watch", waited_ms=300)
    metrics.record_guard("allow", "browser__open", 0, mode="watch")
    assert value("celine_guard_decisions_total", mode="watch", verdict="would_stop",
                 source="reddit.com") == before + 1
    assert value("celine_guard_decisions_total", mode="watch", verdict="allow",
                 source="browser") >= 1
    assert value("celine_guard_judge_seconds_count") == judged + 1, "no judge, no time"


async def test_a_model_call_counts_its_outcome_time_and_wait(tmp_path, monkeypatch):
    llm, nets = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}})
    labels = dict(role="chat", provider="main/big", model="big")
    ok = value("celine_model_requests_total", outcome="ok", **labels)
    first = value("celine_model_first_token_seconds_count", **labels)
    waits = value("celine_model_queue_wait_seconds_count", role="chat", provider="main/big")
    await collect(llm.bind(role="chat", priority=Priority.INTERACTIVE, conversation_id="c"))
    assert value("celine_model_requests_total", outcome="ok", **labels) == ok + 1
    assert value("celine_model_first_token_seconds_count", **labels) == first + 1
    assert value("celine_model_queue_wait_seconds_count", role="chat",
                 provider="main/big") == waits + 1


async def test_a_proxy_call_counts_its_status_and_tokens(proxy):  # noqa: F811
    labels = dict(model="main/big")
    calls = value("celine_llm_proxy_requests_total", status="200", **labels)
    tokens = value("celine_llm_proxy_tokens_total", direction="out", **labels)
    assert (await ask(proxy)).status_code == 200
    assert value("celine_llm_proxy_requests_total", status="200", **labels) == calls + 1
    assert value("celine_llm_proxy_tokens_total", direction="out", **labels) == tokens + 12


class Ping:
    name = "ping"
    description = "ping"

    @tool()
    async def pong(self, ctx) -> ToolResult:
        """pong"""
        return ToolResult(content="pong")


async def test_a_turn_counts_its_origin_outcome_and_tool_calls(store):
    reg = Registry()
    reg.add(Ping())
    done = value("celine_turns_total", origin="owner", outcome="done", reason="")
    started = value("celine_turns_started_total", origin="owner")
    pongs = calls_of("ping__pong")
    made_up = value("celine_tool_calls_total", tool="unknown", state="error")
    cid = await conversations.create(store)
    await user_says(store, cid, "ping twice")
    async with anyio.create_task_group() as tg:
        cfg = load()
        cfg.skills = {**cfg.skills, "ping": {"enabled": True}}
        d = Dispatcher(store, cfg, EventBus(),
                       lambda: Scripted(["ping__pong", "nope__made_up"]), reg)
        await d.start(tg)
        d.notify()
        await until(lambda: value("celine_turns_total", origin="owner",
                                  outcome="done", reason="") == done + 1, 10)
        await d.stop()
        tg.cancel_scope.cancel()
    assert value("celine_turns_started_total", origin="owner") == started + 1
    assert calls_of("ping__pong") == pongs + 1, "by its state, whatever it was"
    assert value("celine_tool_calls_total", tool="unknown", state="error") == made_up + 1


async def test_a_model_call_counts_why_it_failed(tmp_path, monkeypatch):
    """A busy answer counts as busy, a load failure as could_not_load, a server out of
    credits as its code, a refused connection as unreachable."""
    from celine.backends.base import BackendError
    from celine.backends.openai import http_error
    llm, nets = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}})
    labels = dict(role="chat", provider="main/big", model="big")
    cases = [(http_error("Main", "big", 503, ""), "busy"),
             (http_error("Main", "big", 500, "failed to start the model"), "could_not_load"),
             (http_error("Main", "big", 402, "Insufficient credits"), "out_of_credits")]
    refused = BackendError("Main refused the connection.")
    refused.unreachable = True
    cases.append((refused, "unreachable"))
    for error, outcome in cases:
        before = value("celine_model_requests_total", outcome=outcome, **labels)
        nets["main/big"].fail = error
        with pytest.raises(BackendError):
            await collect(llm.bind(role="chat", priority=Priority.INTERACTIVE,
                                   conversation_id="c", fallback=False))
        assert value("celine_model_requests_total", outcome=outcome, **labels) == \
            before + 1, outcome
        llm.router.note_ok("main/big")


async def test_a_call_that_waited_out_its_slot_counts_as_queue_timeout(tmp_path, monkeypatch):
    from celine.backends.queue import QueueTimeout, Ticket
    llm, nets = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}})
    held = await llm.scheduler.acquire(Ticket(endpoint="http://main:11434/v1", model="big",
                                              priority=Priority.INTERACTIVE, lane="x"),
                                       timeout=1)
    labels = dict(role="chat", provider="main/big", model="big")
    before = value("celine_model_requests_total", outcome="queue_timeout", **labels)
    with pytest.raises(QueueTimeout):
        await collect(llm.bind(role="chat", priority=Priority.BACKGROUND, wait=0.01))
    llm.scheduler.release(held)
    assert value("celine_model_requests_total", outcome="queue_timeout", **labels) == before + 1


async def test_a_host_counts_down_only_when_it_stops_answering(tmp_path, monkeypatch):
    llm, _ = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}})
    before = value("celine_host_down_total", provider="main")
    llm.router.note_unreachable("main", "Main refused the connection.")
    assert value("celine_host_down_total", provider="main") == before, "a first look"
    llm.router.note_ok("main/big")
    llm.router.note_unreachable("main", "Main refused the connection.")
    llm.router.note_unreachable("main", "Main refused the connection.")
    assert value("celine_host_down_total", provider="main") == before + 1
    assert value("celine_host_up", provider="main") == 0


async def test_a_turn_counts_its_reason_tokens_and_context(store):
    cid = await conversations.create(store)
    _, tid, _ = await user_says(store, cid, "hi")

    def fn(c):
        c.execute("UPDATE turns SET state='failed', reason_code='busy' WHERE id=?", (tid,))
        for step, prompt in ((0, 1200), (1, 1500)):
            c.execute("INSERT INTO requests(message_id, step, turn_id, provider, model, body, "
                      "prompt_tokens, completion_tokens, created_at) "
                      "VALUES('m', ?, ?, 'p', 'm', x'', ?, 5, 'x')", (step, tid, prompt))
    await store.write(fn)
    failed = value("celine_turns_total", origin="owner", outcome="failed", reason="busy")
    tin = value("celine_turn_tokens_total", origin="owner", direction="in")
    tout = value("celine_turn_tokens_total", origin="owner", direction="out")
    seen = value("celine_turn_context_tokens_count", origin="owner")
    await Dispatcher(store, load(), EventBus())._count(tid, "owner", 1.0)
    assert value("celine_turns_total", origin="owner", outcome="failed",
                 reason="busy") == failed + 1
    assert value("celine_turn_tokens_total", origin="owner", direction="in") == tin + 2700
    assert value("celine_turn_tokens_total", origin="owner", direction="out") == tout + 10
    assert value("celine_turn_context_tokens_count", origin="owner") == seen + 1
