"""A model server that refuses, rejects the key, lacks the model, fails, is slow or cuts
the stream: one sentence each, naming the provider and its host, never a traceback or
raw JSON. The openai backend's wrapper, which anthropic and ollama inherit. Ollama's
overflow carries the server's numbers, never cut short (its body as Ollama 0.32.6
answered)."""
from __future__ import annotations

import json

import httpx
import pytest

from celine.backends.base import BackendError, Done, TextDelta, ToolCallDelta
from celine.backends.ollama import OllamaBackend
from celine.backends.openai import OpenAIBackend
from celine.config import BackendConfig

pytestmark = pytest.mark.anyio
REAL = httpx.AsyncClient


def _backend(monkeypatch, answer):
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: REAL(transport=httpx.MockTransport(answer), **kw))
    return OpenAIBackend(BackendConfig(
        id="gpu/qwen", kind="llama-swap", label="llama-swap", base_url="http://gpu:8080/v1",
        model="qwen3-8b", params={}, raw={}), timeout=30.0)


async def _fails(b) -> BackendError:
    with pytest.raises(BackendError) as e:
        [ev async for ev in b.stream([{"role": "user", "content": "hi"}], [])]
    assert "Traceback" not in str(e.value) and "{" not in str(e.value)
    assert "\n" not in str(e.value)
    return e.value


async def test_a_refused_or_slow_server_names_the_host(monkeypatch):
    def refused(request):
        raise httpx.ConnectError("refused", request=request)
    e = await _fails(_backend(monkeypatch, refused))
    assert str(e) == ("llama-swap at gpu:8080 refused the connection. Check its address in "
                      "Settings, Models, and that the server runs.")
    assert e.unreachable and e.retryable, "the router notes the host down and moves on"

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    e = await _fails(_backend(monkeypatch, slow))
    assert str(e) == "llama-swap at gpu:8080 did not answer within 30 s."


async def test_key_model_and_server_errors_are_plain(monkeypatch):
    seen = {}

    def answer(request):
        return httpx.Response(seen["status"], text=seen["body"])
    b = _backend(monkeypatch, answer)
    for status in (401, 403):
        seen.update(status=status, body=json.dumps({"error": {"message": "Unauthorized"}}))
        e = await _fails(b)
        assert str(e) == "llama-swap rejected the API key. Check the key in Settings, Models."
        assert not e.retryable
    seen.update(status=404, body=json.dumps({"error": {"message": "model not found"}}))
    assert str(await _fails(b)) == ("llama-swap has no model qwen3-8b. Check its name in "
                                    "Settings, Models.")
    seen.update(status=500, body=json.dumps({"error": {"message": "failed to load model"}}))
    e = await _fails(b)
    assert str(e) == "llama-swap returned 500. failed to load model" and e.retryable
    seen.update(status=502, body="<html><body>Bad Gateway</body></html>")
    assert str(await _fails(b)) == "llama-swap returned 502.", "no HTML in its transcript"
    seen.update(status=503, body='{"detail": [{"loc": ["x"]}]}')
    assert str(await _fails(b)) == "llama-swap returned 503.", "no raw JSON either"


async def test_a_stream_cut_mid_way_is_one_sentence(monkeypatch):
    class Cut(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices": [{"delta": {"content": "hel"}}]}\n\n'
            raise httpx.RemoteProtocolError("peer closed connection")

    def answer(request):
        return httpx.Response(200, stream=Cut(), headers={"content-type": "text/event-stream"})
    b = _backend(monkeypatch, answer)
    got = []
    with pytest.raises(BackendError) as e:
        async for ev in b.stream([{"role": "user", "content": "hi"}], []):
            got.append(ev)
    assert got == [TextDelta("hel")]
    assert str(e.value) == ("llama-swap at gpu:8080 closed the connection before the reply "
                            "ended (RemoteProtocolError).")
    assert not getattr(e.value, "unreachable", False), "the host is up: no state flip"


async def test_an_ollama_error_line_says_what_the_server_said(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text='{"error": "model requires more system memory"}\n')),
        **kw))
    b = OllamaBackend(BackendConfig(id="far/m", kind="ollama", label="Ollama",
                                    base_url="http://far:11434/v1", model="m", params={},
                                    raw={}), timeout=30.0)
    assert str(await _fails(b)) == "Ollama returned 500. model requires more system memory"


async def test_a_broken_chunk_fails_the_call_instead_of_going_missing(monkeypatch):
    b = _backend(monkeypatch, lambda r: httpx.Response(
        200, text='data: {"choices": [{"delta": {"content": "a"}}]}\n\ndata: {cut\n\n'))
    assert str(await _fails(b)) == "llama-swap sent a reply chunk that is not JSON."


def _chunks(*chunks) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


async def test_null_fields_in_a_chunk_read_as_absent(monkeypatch):
    """An OpenAI-compatible server that sends null for usage counts, choices or a tool
    call's index: the reply is read, nothing fails on the null."""
    b = _backend(monkeypatch, lambda r: httpx.Response(200, text=_chunks(
        {"choices": None},
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": None, "id": "a", "function": {
            "name": "f", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": None, "completion_tokens": 3}})))
    evs = [ev async for ev in b.stream([{"role": "user", "content": "hi"}], [])]
    assert [e.text for e in evs if isinstance(e, TextDelta)] == ["hi"]
    calls = [e for e in evs if isinstance(e, ToolCallDelta)]
    assert [(c.index, c.name) for c in calls] == [(0, "f")]
    done = evs[-1]
    assert isinstance(done, Done) and done.reason == "tool_calls"
    assert (done.usage.input_tokens, done.usage.output_tokens) == (0, 3)


async def test_parallel_calls_without_an_index_stay_apart(monkeypatch):
    """A server that leaves out the index: each call that starts (its id, its name) gets
    the next one; a delta without either continues the call before it."""
    def call(**tc):
        return {"choices": [{"delta": {"tool_calls": [tc]}}]}
    b = _backend(monkeypatch, lambda r: httpx.Response(200, text=_chunks(
        call(id="a", function={"name": "f", "arguments": '{"x":'}),
        call(function={"arguments": " 1}"}),
        call(id="b", function={"name": "g", "arguments": "{}"}),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})))
    evs = [ev async for ev in b.stream([{"role": "user", "content": "hi"}], [])]
    calls = [(e.index, e.name, e.arguments) for e in evs if isinstance(e, ToolCallDelta)]
    assert calls == [(0, "f", '{"x":'), (0, None, " 1}"), (1, "g", "{}")]


def test_arguments_that_are_not_an_object_fail_the_ollama_request_with_the_reason():
    """Ollama takes a call's arguments as an object: what she wrote is never sent as
    none instead."""
    ollama = OllamaBackend(BackendConfig(id="o", kind="ollama", label="o",
                                         base_url="http://o", model="m", raw={}))
    call = {"role": "assistant", "content": "", "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "web__search", "arguments": '["tea"]'}}]}
    with pytest.raises(BackendError, match=r"web__search .* not a JSON object.*\[\"tea\"\]"):
        ollama.request_body([{"role": "user", "content": "hi"}, call], [])
    call["tool_calls"][0]["function"]["arguments"] = '{"query": "tea"}'
    sent = ollama.request_body([{"role": "user", "content": "hi"}, call], [])
    assert sent["messages"][-1]["tool_calls"][0]["function"]["arguments"] == {"query": "tea"}


NATIVE_OVERFLOW = json.dumps({"error": json.dumps({"error": {
    "code": 400, "message": "request (13301 tokens) exceeds the available context size "
    "(4096 tokens), try increasing it", "type": "exceed_context_size_error",
    "n_prompt_tokens": 13301, "n_ctx": 4096}})})


async def test_an_overflow_from_ollama_is_an_error_with_the_servers_numbers(monkeypatch):
    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert body["truncate"] is False and body["options"]["num_ctx"] == 262_144
        return httpx.Response(400, text=NATIVE_OVERFLOW)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: REAL(transport=httpx.MockTransport(answer), **kw))
    b = OllamaBackend(BackendConfig(
        id="lan/small-model", kind="ollama", label="gpu-box", base_url="http://192.0.2.10:11434/v1",
        model="small-model", params={"reasoning_effort": "none"},
        raw={"provider_kind": "ollama", "tools": True, "context": 262_144}))
    with pytest.raises(Exception) as e:
        [ev async for ev in b.stream([{"role": "user", "content": "hi"}], [])]
    assert e.value.code == "context_overflow"
    assert (e.value.n_prompt_tokens, e.value.n_ctx) == (13301, 4096)
