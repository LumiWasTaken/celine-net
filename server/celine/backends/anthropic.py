"""Anthropic's Messages API (/v1/messages), streaming.

Not OpenAI-compatible, so the messages and the events are converted here; the request,
its errors and the SSE reading are openai.py's (OpenAIBackend.stream, http_error,
_sse_data). Messages come in the OpenAI shape the turn builds:
- system messages become `system` text blocks;
- an assistant reply's signed thinking blocks (`thinking`: {text, signature}, or {data}
  when redacted; messages.meta) go first in it: a tool loop with thinking on needs them;
- an assistant reply's tool calls become `tool_use` blocks after its text;
- tool results become `tool_result` blocks in a user message;
- image data URLs become base64 image blocks;
- consecutive messages of one role are merged, since roles must alternate.
The reply's signed blocks come back on Done.thinking, for the runner to keep.
`temperature` is never sent: current Claude models refuse it, and the app's own
(captions, screen reading) are hints for other servers.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .base import TAIL, Done, ReasoningDelta, TextDelta, ToolCallDelta, Usage
from .ollama import _arguments
from .openai import OpenAIBackend, RawReply, _sse_data, http_error, read_chunk

JSON_ONLY = "Answer with one JSON object and nothing else."
# a reply cut off before its end (the token cap, the context window, a paused turn) is
# "length", as max_tokens is
_STOP = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
         "model_context_window_exceeded": "length", "pause_turn": "length",
         "tool_use": "tool_calls", "refusal": "content_filter"}
# an `error` event in the stream, as the status the same error has as an answer
_ERROR_STATUS = {"overloaded_error": 529, "rate_limit_error": 429, "api_error": 500}


class AnthropicBackend(OpenAIBackend):
    def url(self) -> str:
        return f"{self.cfg.base_url}/messages"

    def headers(self) -> dict[str, str]:
        from ..settings.kinds import ANTHROPIC_BETAS, headers
        h = {"Content-Type": "application/json",
             **headers("anthropic", self.cfg.api_key), **self.cfg.headers}
        if "anthropic-beta" in self.cfg.headers:
            betas = {b.strip() for b in self.cfg.headers["anthropic-beta"].split(",") if b.strip()}
            betas.add(ANTHROPIC_BETAS)
            h["anthropic-beta"] = ",".join(sorted(betas))
        return h

    def request_body(self, messages, tools, extra: dict[str, Any] | None = None,
                     model: str | None = None) -> dict[str, Any]:
        """`max_tokens` comes with the model's params (settings/registry.py)."""
        system, wire = wire_messages(messages)
        fields = {**self.cfg.params, **(extra or {})}
        fields.pop("temperature", None)
        if fields.pop("response_format", None):
            system.append({"type": "text", "text": JSON_ONLY})
        body: dict[str, Any] = {"model": model or self.model, "messages": wire,
                                "stream": True, **fields}
        if "thinking" in body and isinstance(body["thinking"], dict):
            body["thinking"].setdefault("block_binding", {})["prefix_mismatch_behavior"] = "drop_block"
        elif any(b.get("type") in ("thinking", "redacted_thinking")
                 for m in wire for b in m.get("content", [])):
            body["thinking"] = {"type": "adaptive",
                                "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        # prompt cache breakpoints: the end of the system blocks (tools and system), and
        # the end of the conversation so far: the block before the tail (what holds now,
        # rebuilt every turn: turns/context.py) and the last block when it is a tool
        # result; without a tail its last block. The next request reads them back
        cache = {"type": "ephemeral",
                 **({"ttl": "1h"} if self.cfg.raw.get("cache_ttl") == "1h" else {})}
        if system:
            system[-1]["cache_control"] = cache
            body["system"] = system
        for at in breakpoints(wire):
            at["cache_control"] = dict(cache)
        if tools:
            body["tools"] = [_tool(t) for t in tools]
            body["tool_choice"] = {"type": "auto"}
        return body

    async def _parse(self, res: httpx.Response):
        reply = RawReply()
        calls: dict[int, int] = {}          # content block index -> tool call index
        thinking: dict[int, dict] = {}      # content block index -> its block, as kept
        tokens_in = tokens_out = cache_read = cache_write = 0
        stop: str | None = None
        async for payload in _sse_data(res):
            reply.last(payload)
            ev = read_chunk(self.cfg.label, payload)
            kind = ev.get("type")
            if kind == "error":
                status = _ERROR_STATUS.get(str((ev.get("error") or {}).get("type")), 500)
                err = http_error(self.cfg.label, self.model, status, payload)
                err.raw = reply.message("error")
                raise err
            if kind == "message_start":
                msg = ev.get("message") or {}
                reply.top({k: v for k, v in msg.items() if k != "content"})
                u = msg.get("usage") or {}
                cache_read = int(u.get("cache_read_input_tokens") or 0)
                cache_write = int(u.get("cache_creation_input_tokens") or 0)
                tokens_in = int(u.get("input_tokens") or 0) + cache_read + cache_write
            elif kind == "content_block_start":
                block = ev.get("content_block") or {}
                if block.get("type") == "tool_use":
                    n = calls[ev.get("index")] = len(calls)
                    reply.call(n, block.get("id"), block.get("name"), "")
                    yield ToolCallDelta(index=n, id=block.get("id"), name=block.get("name"))
                elif block.get("type") == "thinking":
                    thinking[ev.get("index")] = {"text": block.get("thinking") or "",
                                                 "signature": block.get("signature") or ""}
                elif block.get("type") == "redacted_thinking":
                    thinking[ev.get("index")] = {"data": block.get("data") or ""}
            elif kind == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    reply.content.append(d["text"])
                    yield TextDelta(d["text"])
                elif d.get("type") == "thinking_delta" and d.get("thinking"):
                    if ev.get("index") in thinking:
                        thinking[ev["index"]]["text"] += d["thinking"]
                    reply.reasoning.append(d["thinking"])
                    yield ReasoningDelta(d["thinking"])
                elif d.get("type") == "signature_delta" and ev.get("index") in thinking:
                    thinking[ev["index"]]["signature"] += d.get("signature") or ""
                elif d.get("type") == "input_json_delta" and d.get("partial_json") and \
                        ev.get("index") in calls:
                    n = calls[ev["index"]]
                    reply.call(n, None, None, d["partial_json"])
                    yield ToolCallDelta(index=n, arguments=d["partial_json"])
            elif kind == "message_delta":
                d = ev.get("delta") or {}
                stop = d.get("stop_reason") or stop
                u = ev.get("usage") or {}
                tokens_out = int(u.get("output_tokens") or tokens_out)
                reply.top({**d, "usage": {**(reply.fields.get("usage") or {}), **u}})
            elif kind == "message_stop":
                break
        reply.thinking = [b for b in thinking.values() if b.get("signature") or b.get("data")]
        yield Done(reason=_STOP.get(stop or "", "stop"),  # type: ignore[arg-type]
                   usage=Usage(tokens_in, tokens_out, cache_read, cache_write),
                   raw=reply.message(stop or "stop"), thinking=reply.thinking or None)


def wire_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """(the system blocks, the messages) as /v1/messages takes them."""
    system: list[dict] = []
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system += _blocks(m.get("content"))
            continue
        if role == "tool":
            result: dict[str, Any] = {"type": "tool_result",
                                      "tool_use_id": _id(m.get("tool_call_id"))}
            if content := _blocks(m.get("content")):
                result["content"] = content
            role, blocks = "user", [result]
        else:
            blocks = (_signed(m.get("thinking")) if role == "assistant" else []) + \
                _blocks(m.get("content"))
            for c in (m.get("tool_calls") or []) if role == "assistant" else []:
                fn = c.get("function") or {}
                blocks.append({"type": "tool_use", "id": _id(c.get("id")),
                               "name": fn.get("name") or "",
                               "input": _arguments(fn)})
        if not blocks:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += blocks
        else:
            out.append({"role": role, "content": blocks})
    return system, out


def breakpoints(wire: list[dict]) -> list[dict]:
    """The blocks that get a cache breakpoint (see request_body). The tail's mark is
    taken off its block here."""
    flat = [b for m in wire for b in m["content"]]
    tail = next((i for i, b in enumerate(flat) if b.pop(TAIL, None)), None)
    if not flat:
        return []
    if tail is None:
        return [flat[-1]]
    out = [flat[tail - 1]] if tail > 0 else []
    if flat[-1].get("type") == "tool_result" and flat[-1] is not flat[tail]:
        out.append(flat[-1])
    return out


def _blocks(content: Any) -> list[dict]:
    """Text and image parts as content blocks. Blank text is left out: the API refuses
    an empty text block."""
    parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
    out: list[dict] = []
    for p in parts:
        if p.get("type") == "text" and str(p.get("text") or "").strip():
            out.append({"type": "text", "text": p["text"], **({TAIL: True}
                                                             if p.get(TAIL) else {})})
        elif p.get("type") == "image_url":
            url = str((p.get("image_url") or {}).get("url") or "")
            media, _, data = url.removeprefix("data:").partition(";base64,")
            out.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                    "data": data}})
    return out


def _signed(thinking: list[dict] | None) -> list[dict]:
    """A reply's kept thinking as the blocks it came in. Unsigned thinking cannot go
    back and is left out."""
    out: list[dict] = []
    for t in thinking or []:
        if t.get("data"):
            out.append({"type": "redacted_thinking", "data": t["data"]})
        elif t.get("signature"):
            out.append({"type": "thinking", "thinking": t.get("text") or "",
                        "signature": t["signature"]})
    return out


def _id(call_id: str | None) -> str:
    """A call id as the API takes it (letters, digits, `_`, `-`): the app's own ids are
    `turn:step:index`. The call and its result map alike, so they still match."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", call_id or "")


def _tool(t: dict) -> dict:
    """An OpenAI function tool as Anthropic's tool."""
    fn = t.get("function") or {}
    return {"name": fn.get("name") or "", "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}}
