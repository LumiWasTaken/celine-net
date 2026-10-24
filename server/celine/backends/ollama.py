"""Ollama's own /api/chat, streaming.

Not its OpenAI-compatible /v1: that endpoint runs a model at the server's default
context (4,096 tokens unless OLLAMA_CONTEXT_LENGTH says otherwise) whatever the request
asks, and cuts the start of a longer prompt without a word, so the persona, the skill
guides and the tool list are gone and the model answers without tools. /api/chat takes
the context as `options.num_ctx` (the model's context size in Settings, Models) and,
with `truncate: false`, answers a prompt that does not fit with an error instead.

The request and the events are the same as for every other backend: messages come in
the OpenAI shape and are converted here, and the stream is turned into BackendEvents.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .base import Done, ReasoningDelta, TextDelta, ToolCallDelta, Usage
from .openai import OpenAIBackend, RawReply, cap, http_error, read_chunk

# request fields that are top-level in /api/chat; every other field is a model option
_TOP = {"keep_alive", "format", "think", "logprobs", "top_logprobs"}
_RENAMED = {"max_tokens": "num_predict"}


class OllamaBackend(OpenAIBackend):
    def url(self) -> str:
        from ..settings.kinds import root_of
        return f"{root_of(self.cfg.base_url)}/api/chat"

    def request_body(self, messages, tools, extra: dict[str, Any] | None = None,
                     model: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model or self.model, "messages": wire_messages(messages),
                                "stream": True, "truncate": False}
        options: dict[str, Any] = {}
        context = self.cfg.raw.get("context")
        if context:
            options["num_ctx"] = int(context)
        for key, value in {**self.cfg.params, **(extra or {})}.items():
            if key == "reasoning_effort":
                body["think"] = False if value == "none" else value
            elif key == "response_format":
                body["format"] = _format(value)
            elif key in _TOP:
                body[key] = value
            else:
                options[_RENAMED.get(key, key)] = value
        if options:
            body["options"] = options
        if tools:
            body["tools"] = tools
        return body

    async def _parse(self, res: httpx.Response):
        """One JSON object per line: message deltas, then a last line with `done`."""
        n_calls = 0
        reply = RawReply()
        async for line in res.aiter_lines():
            if not line.strip():
                continue
            reply.last(line)
            chunk = read_chunk(self.cfg.label, line)
            if chunk.get("error"):         # a failure after the 200, as openai.py's
                err = http_error(self.cfg.label, self.model, 500, line)
                err.raw = cap(chunk)
                raise err
            reply.top(chunk)
            msg = chunk.get("message") or {}
            if think := msg.get("thinking"):
                reply.reasoning.append(think)
                yield ReasoningDelta(think)
            if text := msg.get("content"):
                reply.content.append(text)
                yield TextDelta(text)
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                reply.call(n_calls, tc.get("id"), fn.get("name"), fn.get("arguments"))
                yield ToolCallDelta(index=n_calls, id=tc.get("id"), name=fn.get("name"),
                                    arguments=json.dumps(fn.get("arguments") or {},
                                                         ensure_ascii=False))
                n_calls += 1
            if chunk.get("done"):
                reason = chunk.get("done_reason") or "stop"
                if reason == "stop" and n_calls:
                    reason = "tool_calls"
                yield Done(reason=reason if reason in ("stop", "length", "tool_calls")
                           else "stop",
                           usage=Usage(int(chunk.get("prompt_eval_count") or 0),
                                       int(chunk.get("eval_count") or 0)),
                           raw=reply.message(chunk.get("done_reason") or "stop"))
                return


def _format(response_format: Any) -> Any:
    """OpenAI's response_format as Ollama's `format`: "json", or the JSON schema."""
    if isinstance(response_format, dict):
        schema = (response_format.get("json_schema") or {}).get("schema")
        if schema:
            return schema
    return "json"


def wire_messages(messages: list[dict]) -> list[dict]:
    """OpenAI-shaped messages as /api/chat takes them: text parts joined and images
    as a base64 list, tool call arguments as objects, a tool result named by its
    tool."""
    names: dict[str, str] = {}
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        msg: dict[str, Any] = {"role": role}
        content = m.get("content")
        if isinstance(content, list):
            texts, images = [], []
            for part in content:
                if part.get("type") == "text":
                    texts.append(part.get("text") or "")
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url") or ""
                    images.append(url.partition("base64,")[2])
            msg["content"] = "\n\n".join(t for t in texts if t)
            if images:
                msg["images"] = images
        else:
            msg["content"] = content or ""
        if m.get("reasoning_content"):
            msg["thinking"] = m["reasoning_content"]
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                fn = c.get("function") or {}
                names[c.get("id") or ""] = fn.get("name") or ""
                calls.append({"function": {"name": fn.get("name") or "",
                                           "arguments": _arguments(fn.get("arguments"))}})
            msg["tool_calls"] = calls
        if role == "tool":
            msg["tool_name"] = names.get(m.get("tool_call_id") or "", "")
        out.append(msg)
    return out


def _arguments(raw: Any) -> dict:
    """Ollama takes the arguments as an object. Arguments the model wrote that are not
    a JSON object were already refused as a tool error; they go back as none."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}
