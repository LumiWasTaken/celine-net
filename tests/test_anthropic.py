"""Anthropic's Messages API (backends/anthropic.py, settings/kinds.py): the request as the
API takes it, the stream as BackendEvents, the errors, discovery and thinking. An httpx
MockTransport answers; nothing touches a network. The streams are the shapes the API
documents (event types, deltas, usage, stop reasons)."""
from __future__ import annotations

import json

import httpx
import pytest

from celine.backends.anthropic import AnthropicBackend, wire_messages
from celine.backends.base import BackendError, Done, TextDelta, ToolCallDelta
from celine.backends.openai import OpenAIBackend, build
from celine.config import BackendConfig
from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.core.bus import EventBus
from celine.settings import kinds, thinking
from celine.settings.files import Invalid
from celine.settings.registry import Settings, backend_config, validate_provider
from celine.skills import Registry, ToolResult, tool
from celine.turns import context, store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from chat_fixtures import user_says

pytestmark = pytest.mark.anyio
KEY = "sk-ant-test-0000"
URL = "https://api.anthropic.com/v1"
REAL = httpx.AsyncClient


def sse(*events: dict) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def start(tokens_in: int = 12, **cache) -> dict:
    return {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
        "content": [], "usage": {"input_tokens": tokens_in, "output_tokens": 1, **cache}}}


def block(index: int, kind: str, **more) -> dict:
    return {"type": "content_block_start", "index": index,
            "content_block": {"type": kind, **more}}


def delta(index: int, kind: str, **more) -> dict:
    return {"type": "content_block_delta", "index": index, "delta": {"type": kind, **more}}


def end(stop: str, tokens_out: int) -> tuple[dict, ...]:
    return ({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
             "usage": {"output_tokens": tokens_out}},
            {"type": "message_stop"})


def backend(monkeypatch, answer, **raw) -> tuple[AnthropicBackend, list]:
    """The backend with every request answered by `answer` (a Response, or a function of
    the request); the requests it sent land in the list. `raw`: model settings."""
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answer(request) if callable(answer) else answer

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: REAL(transport=httpx.MockTransport(handle), **kw))
    b = AnthropicBackend(BackendConfig(
        id="a/claude-x", kind="anthropic", label="Anthropic", base_url=URL, model="claude-x",
        params={"max_tokens": 8192},
        raw={"provider_kind": "anthropic", "tools": True, "images": True, **raw},
        _api_key=KEY))
    return b, sent


def streamed(*events: dict) -> httpx.Response:
    return httpx.Response(200, text=sse(*events),
                          headers={"content-type": "text/event-stream"})


async def run(b, messages=None, tools=None):
    return [ev async for ev in b.stream(messages or [{"role": "user", "content": "hi"}],
                                        tools or [])]


async def test_a_text_reply_with_its_usage(monkeypatch):
    b, sent = backend(monkeypatch, streamed(
        start(12, cache_read_input_tokens=3000, cache_creation_input_tokens=200),
        block(0, "text", text=""), delta(0, "text_delta", text="Hel"),
        {"type": "ping"}, delta(0, "text_delta", text="lo"),
        {"type": "content_block_stop", "index": 0}, *end("end_turn", 5)))
    events = await run(b, [{"role": "system", "content": "be kind"},
                           {"role": "user", "content": "hi"}])
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    done = events[-1]
    assert isinstance(done, Done) and done.reason == "stop"
    u = done.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read, u.cache_write) == (3212, 5, 3000, 200)
    assert done.raw["usage"]["cache_read_input_tokens"] == 3000
    assert done.raw["message"]["content"] == "Hello" and done.raw["id"] == "msg_1"
    req = sent[0]
    assert req.url == f"{URL}/messages"
    assert req.headers["x-api-key"] == KEY and req.headers["anthropic-version"] == "2023-06-01"
    assert req.headers["anthropic-beta"] == "thinking-binding-controls-2026-08-01"
    assert "authorization" not in req.headers
    body = json.loads(req.content)
    assert body["stream"] is True and body["max_tokens"] == 8192
    cached = {"type": "ephemeral"}
    assert body["system"] == [{"type": "text", "text": "be kind", "cache_control": cached}]
    assert body["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "hi", "cache_control": cached}]}]


@pytest.mark.parametrize("stop, reason", [
    ("max_tokens", "length"), ("model_context_window_exceeded", "length"),
    ("pause_turn", "length"), ("refusal", "content_filter"), ("tool_use", "tool_calls"),
    ("stop_sequence", "stop")])
async def test_every_stop_reason_has_its_meaning(monkeypatch, stop, reason):
    b, _ = backend(monkeypatch, streamed(
        start(1), block(0, "text", text=""), delta(0, "text_delta", text="x"),
        {"type": "content_block_stop", "index": 0}, *end(stop, 1)))
    events = await run(b, [{"role": "user", "content": "hi"}])
    assert events[-1].reason == reason


def breakpoints(body: dict) -> list[tuple[str, dict]]:
    """(where, cache_control) of every block that has one."""
    blocks = [("system", b) for b in body.get("system") or []] + [
        (f"{m['role']}:{b['type']}", b) for m in body["messages"] for b in m["content"]]
    return [(where, b["cache_control"]) for where, b in blocks if "cache_control" in b]


def test_two_cache_breakpoints_at_the_end_of_system_and_of_the_history(monkeypatch):
    history = [{"role": "system", "content": "persona"}, {"role": "system", "content": "notes"},
               {"role": "user", "content": "look"},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "t1", "type": "function", "function": {"name": "a", "arguments": ""}}]},
               {"role": "tool", "tool_call_id": "t1", "content": "found"}]
    b, _ = backend(monkeypatch, None)
    body = b.request_body(history, [])
    assert breakpoints(body) == [("system", {"type": "ephemeral"}),
                                 ("user:tool_result", {"type": "ephemeral"})]
    assert "cache_control" not in body["system"][0]
    b, _ = backend(monkeypatch, None, cache_ttl="1h")
    assert [c for _, c in breakpoints(b.request_body(history, []))] == \
        [{"type": "ephemeral", "ttl": "1h"}] * 2


async def test_a_tool_call_with_its_arguments_in_three_deltas(monkeypatch):
    b, sent = backend(monkeypatch, streamed(
        start(), block(0, "text", text=""), delta(0, "text_delta", text="Looking."),
        block(1, "tool_use", id="toolu_1", name="search", input={}),
        delta(1, "input_json_delta", partial_json='{"q"'),
        delta(1, "input_json_delta", partial_json=': "ca'),
        delta(1, "input_json_delta", partial_json='ts"}'), *end("tool_use", 20)))
    tool = {"type": "function", "function": {"name": "search", "description": "Search.",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}
    events = await run(b, tools=[tool])
    calls = [e for e in events if isinstance(e, ToolCallDelta)]
    assert calls[0] == ToolCallDelta(index=0, id="toolu_1", name="search", arguments="")
    assert all(c.index == 0 for c in calls), "tool calls count tool_use blocks only"
    assert json.loads("".join(c.arguments for c in calls)) == {"q": "cats"}
    assert events[-1].reason == "tool_calls"
    assert events[-1].raw["message"]["tool_calls"][0]["function"]["arguments"] == '{"q": "cats"}'
    body = json.loads(sent[0].content)
    assert body["tools"] == [{"name": "search", "description": "Search.",
                              "input_schema": tool["function"]["parameters"]}]
    assert body["tool_choice"] == {"type": "auto"}


def test_two_tool_results_go_back_in_one_user_message_and_roles_alternate():
    system, wire = wire_messages([
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "", "reasoning_content": "hmm", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "a", "arguments": '{"x": 1}'}},
            {"id": "t2", "type": "function", "function": {"name": "b", "arguments": ""}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "A"},
        {"role": "tool", "tool_call_id": "t2", "content": "B"},
        {"role": "user", "content": "and?"},
    ])
    assert system == [{"type": "text", "text": "persona"}]
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert wire[0]["content"] == [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
    assert wire[1]["content"] == [
        {"type": "tool_use", "id": "t1", "name": "a", "input": {"x": 1}},
        {"type": "tool_use", "id": "t2", "name": "b", "input": {}}]
    assert wire[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "A"}]},
        {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "B"}]},
        {"type": "text", "text": "and?"}]


def test_the_apps_call_ids_go_out_as_ids_the_api_takes():
    _, wire = wire_messages([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "01JT:0:1", "type": "function", "function": {"name": "a", "arguments": ""}}]},
        {"role": "tool", "tool_call_id": "01JT:0:1", "content": "done"}])
    assert wire[1]["content"][0]["id"] == wire[2]["content"][0]["tool_use_id"] == "01JT_0_1"


def test_an_image_data_url_is_a_base64_image_block():
    _, wire = wire_messages([{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0K"}}]}])
    assert wire[0]["content"][1] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "iVBORw0K"}}


def test_max_tokens_and_the_cache_time_come_from_the_model_options():
    p = validate_provider("a", {"kind": "anthropic", "base_url": "", "models": {
        "claude-x": {"caps": ["chat"], "max_tokens": 1000, "cache_ttl": "1h"},
        "claude-y": {"caps": ["chat"]}}})
    assert p["base_url"] == URL and p["label"] == "a"
    s = Settings(providers={"a": p})
    body = build(backend_config(s, "a/claude-x")).request_body(
        [{"role": "user", "content": "hi"}], [])
    assert body["max_tokens"] == 1000
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral",
                                                                  "ttl": "1h"}
    assert build(backend_config(s, "a/claude-y")).request_body([], [])["max_tokens"] == 8192
    with pytest.raises(Invalid, match="one of '5m', '1h'"):
        validate_provider("a", {"kind": "anthropic", "models": {"m": {"cache_ttl": "2h"}}})
    with pytest.raises(Invalid, match="Anthropic option"):
        validate_provider("o", {"kind": "openai", "base_url": "http://h:1",
                                "models": {"m": {"cache_ttl": "1h"}}})
    # the most is what the provider states for the model, once it has
    assert validate_provider("a", {"kind": "anthropic", "models": {
        "m": {"max_tokens": 200_000}}})["models"]["m"]["max_tokens"] == 200_000
    from celine.settings import kinds
    kinds.reported_outputs(URL, {"m": 128_000})
    with pytest.raises(Invalid, match="1 to 128,000"):
        validate_provider("a", {"kind": "anthropic", "models": {"m": {"max_tokens": 200_000}}})


LIMIT_PROVIDERS = {
    "p": {"kind": "llama-server", "base_url": "http://p:8080/v1", "models": {
        "big": {"caps": ["chat", "tools"]}, "mid": {"caps": ["chat"]},
        "small": {"caps": ["chat"], "params": {"max_tokens": 300}}}},
    "o": {"kind": "ollama", "base_url": "http://o:11434", "models": {
        "q": {"caps": ["chat"], "params": {"num_predict": 99}}}},
    "a": {"kind": "anthropic", "base_url": "https://api.anthropic.com", "models": {
        "c": {"caps": ["chat"]}}},
}


def test_a_reply_limit_only_where_params_set_one_or_anthropic_needs_it():
    s = Settings(providers=LIMIT_PROVIDERS)
    assert "max_tokens" not in backend_config(s, "p/big").params
    assert backend_config(s, "p/small").params["max_tokens"] == 300
    assert "max_tokens" not in backend_config(s, "o/q").params
    assert backend_config(s, "a/c").params["max_tokens"] == 8192


async def test_the_error_answers(monkeypatch):
    async def error_of(status: int, error: str, message: str, **headers) -> BackendError:
        b, _ = backend(monkeypatch, httpx.Response(status, headers=headers, json={
            "type": "error", "error": {"type": error, "message": message}}))
        with pytest.raises(BackendError) as e:
            await run(b)
        return e.value

    refused = await error_of(401, "authentication_error", "invalid x-api-key")
    assert not refused.retryable and "rejected the API key" in str(refused)
    limited = await error_of(429, "rate_limit_error", "slow down", **{"retry-after": "7"})
    assert limited.retryable and limited.status == 429 and limited.retry_after == 7.0
    busy = await error_of(529, "overloaded_error", "Overloaded")
    assert busy.retryable and busy.status == 529
    broke = await error_of(400, "invalid_request_error", "Your credit balance is too low to "
                           "access the Anthropic API. Please go to Plans & Billing.")
    assert broke.code == "out_of_credits" and not broke.retryable
    assert str(broke) == "Anthropic is out of credits."
    # too long for the model: a context overflow, so the runner compacts and tries again
    for said in ("prompt is too long: 215000 tokens > 200000 maximum",
                 "input length and `max_tokens` exceed context limit: 187254 + 20000 > "
                 "200000, decrease input length or `max_tokens` and try again"):
        full = await error_of(400, "invalid_request_error", said)
        assert full.code == "context_overflow" and not full.retryable


async def test_an_error_event_mid_stream_fails_the_call(monkeypatch):
    b, _ = backend(monkeypatch, streamed(
        start(), block(0, "text", text=""), delta(0, "text_delta", text="par"),
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
    seen = []
    with pytest.raises(BackendError) as e:
        async for ev in b.stream([{"role": "user", "content": "hi"}], []):
            seen.append(ev)
    assert seen == [TextDelta("par")] and e.value.retryable and e.value.status == 529


async def test_discovery_pages_through_the_model_list():
    pages = {None: {"data": [{"type": "model", "id": "claude-b", "display_name": "Claude B",
                              "max_input_tokens": 1_000_000, "max_tokens": 128_000,
                              "capabilities": {"effort": {
                                  "supported": True, "low": {"supported": True},
                                  "medium": {"supported": True}, "high": {"supported": True},
                                  "xhigh": {"supported": False}, "max": {"supported": True}}}}],
                    "has_more": True, "first_id": "claude-b", "last_id": "claude-b"},
             "claude-b": {"data": [{"type": "model", "id": "claude-a",
                                    "display_name": "Claude A"}],
                          "has_more": False, "first_id": "claude-a", "last_id": "claude-a"}}
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        assert request.headers["x-api-key"] == KEY
        return httpx.Response(200, json=pages[request.url.params.get("after_id")])

    found = await kinds.discover("anthropic", URL, KEY, refresh=True,
                                 transport=httpx.MockTransport(answer))
    assert len(asked) == 2 and asked[1].url.params["after_id"] == "claude-b"
    assert found == [
        {"id": "claude-a", "name": "Claude A", "caps": ["chat", "tools", "vision"]},
        {"id": "claude-b", "name": "Claude B", "caps": ["chat", "tools", "vision"],
         "context": 1_000_000, "output": 128_000,
         "thinking_levels": ["low", "medium", "high", "max"]}]

    refused = await kinds.check("anthropic", URL, KEY, key_required=True,
                                transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert refused["fix"] == "replace_key"
    broke = await kinds.check("anthropic", URL, KEY, key_required=True,
                              transport=httpx.MockTransport(lambda r: httpx.Response(400, json={
                                  "type": "error", "error": {"type": "invalid_request_error",
                                  "message": "Your credit balance is too low."}})))
    assert broke["sentence"] == "Anthropic is out of credits."


async def test_thinking_sends_the_effort_and_room_for_it_and_no_temperature(monkeypatch):
    bc = BackendConfig(id="a/claude-x", kind="anthropic", label="Anthropic", base_url=URL,
                       model="claude-x", params={"max_tokens": 8192, "temperature": 0.7},
                       raw={"provider_kind": "anthropic"})
    body = AnthropicBackend(thinking.apply(bc, "high")).request_body([], [])
    assert body["thinking"] == {"type": "adaptive", "display": "summarized",
                                "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert body["output_config"] == {"effort": "high"}
    assert body["max_tokens"] == 16384 + 1024 and "temperature" not in body
    assert thinking.levels({"kind": "anthropic"}, {}) == ["low", "medium", "high", "xhigh"]


def test_off_is_the_lowest_effort_the_model_lists():
    """Newer Claude models think whatever is sent: off asks for the least thinking, so a
    caption at 500 tokens is not cut off, and sends no `thinking`."""
    bc = BackendConfig(id="a/claude-x", kind="anthropic", label="Anthropic", base_url=URL,
                       model="claude-x", params={"max_tokens": 500},
                       raw={"provider_kind": "anthropic"})

    def off(offered):
        return AnthropicBackend(thinking.apply(bc, "off", offered)).request_body([], [])

    body = off(["low", "medium", "high", "xhigh"])
    assert body["output_config"] == {"effort": "low"} and "thinking" not in body
    assert body["max_tokens"] == 500
    assert off(["medium", "minimal", "low"])["output_config"] == {"effort": "minimal"}
    assert "output_config" not in off([])


class Echo:
    name = "t"
    description = "test"

    @tool()
    async def echo(self, ctx, q: str) -> ToolResult:
        """echo"""
        return ToolResult(content=f"echo {q}")


async def thought_and_called(store, monkeypatch):
    """A turn whose first reply thinks (signed, and one redacted block) and calls a
    tool, then answers. -> (cfg, backend, the requests sent, conversation id)."""
    cfg = load()
    cfg.skills = {**cfg.skills, "t": {"enabled": True}}
    replies = [streamed(
        start(), block(0, "thinking", thinking="", signature=""),
        delta(0, "thinking_delta", thinking="look it up"),
        delta(0, "signature_delta", signature="sig-1"),
        block(1, "redacted_thinking", data="opaque"),
        block(2, "tool_use", id="toolu_1", name="t__echo", input={}),
        delta(2, "input_json_delta", partial_json='{"q": "hi"}'), *end("tool_use", 30)),
        streamed(start(), block(0, "text", text=""), delta(0, "text_delta", text="done"),
                 *end("end_turn", 2))]
    b, sent = backend(monkeypatch, lambda request: replies[len(sent) - 1])
    registry = Registry()
    registry.add(Echo())
    cid = await conversations.create(store)
    await user_says(store, cid, "echo hi")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, b, EventBus(), Token(turn["id"]), registry)
    return cfg, b, sent, cid


SIGNED = [{"type": "thinking", "thinking": "look it up", "signature": "sig-1"},
          {"type": "redacted_thinking", "data": "opaque"}]


async def test_signed_thinking_goes_back_before_the_tool_call(store, monkeypatch):
    """A reply that thinks and calls a tool: the next request of the tool loop carries
    the signed thinking block (and the redacted one) first in that reply, and the stored
    reply keeps them in its meta, so a request built again from the branch has them."""
    cfg, b, sent, cid = await thought_and_called(store, monkeypatch)
    follow = json.loads(sent[1].content)["messages"]
    assert follow[1]["role"] == "assistant"
    assert follow[1]["content"][:2] == SIGNED
    assert follow[1]["content"][2]["type"] == "tool_use"
    kept = [{"text": "look it up", "signature": "sig-1"}, {"data": "opaque"}]
    meta = await store.read(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT meta FROM messages WHERE role='assistant' AND meta IS NOT NULL")])
    assert meta == [{"thinking": kept}]
    built = await context.build(store, cfg, b, cid, system="", tools=[])
    call = next(m for m in built.messages if m.get("tool_calls"))
    assert call["thinking"] == kept
    assert b.request_body(built.messages, [])["messages"][1]["content"][:2] == SIGNED
    plain = OpenAIBackend(b.cfg).request_body(built.messages, [])["messages"]
    assert not any("thinking" in m for m in plain), "only Anthropic takes signed blocks"


async def test_an_edited_reply_sends_no_thinking(store, monkeypatch):
    """The signature belongs to the old text: the edited version carries no thinking
    blocks, so the next request is not refused. A continued version (and a tool result
    row) takes the meta the same way."""
    cfg, b, _, cid = await thought_and_called(store, monkeypatch)
    signed_id = await store.read(lambda c: c.execute(
        "SELECT id FROM messages WHERE json_extract(meta, '$.thinking') IS NOT NULL"
    ).fetchone()[0])
    edited, _, _ = await tree.edit(store, cid, signed_id, "echoing it")
    built = await context.build(store, cfg, b, cid, system="", tools=[])
    assert [m["content"] for m in built.messages if m["role"] == "assistant"] == ["echoing it"]
    assert not any("thinking" in m for m in built.messages)
    body = b.request_body(built.messages, [])
    assert not [x for m in body["messages"] for x in m["content"] if "thinking" in x["type"]]
