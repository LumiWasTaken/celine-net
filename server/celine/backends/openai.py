"""OpenAI-compatible /v1/chat/completions, streaming, and the error answers every HTTP
backend shares (backends/ollama.py and backends/anthropic.py build on this module)."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import BackendConfig
from .base import (
    ORIGIN, TAIL, BackendError, Done, ReasoningDelta, TextDelta, ToolCallDelta, Usage,
)


class OpenAIBackend:
    def __init__(self, cfg: BackendConfig, timeout: float = 600.0):
        self.cfg = cfg
        # the model's `vision` and `tools` capabilities in Settings (registry.backend_config);
        # a model without tools gets no tool list
        self.supports_images = bool(cfg.raw.get("images"))
        self.supports_tools = bool(cfg.raw.get("tools"))
        self.name = cfg.id
        self.model = cfg.model
        # read timeout is the long one: llama-swap holds the request while a cold model loads
        self.timeout = httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0)

    def request_body(self, messages, tools, extra: dict[str, Any] | None = None,
                     model: str | None = None) -> dict[str, Any]:
        """`model`: the name the request goes out under when it is not the model's own
        (the vision twin, which the queued backend decides)."""
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": wire_messages(messages),
            "stream": True,
            **self.cfg.params,
            **(extra or {}),
        }
        if tools:
            body["tools"] = tools
        # usage in the final chunk: the token counter calibrates on it
        body.setdefault("stream_options", {"include_usage": True})
        return body

    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self.cfg.headers}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    def url(self) -> str:
        return f"{self.cfg.base_url}/chat/completions"

    async def stream(self, messages, tools, body: dict[str, Any] | None = None,
                     model: str | None = None):
        """`body`: extra request fields for this call (a caption's response_format)."""
        try:
            body = self.request_body(messages, tools, body, model)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", self.url(), json=body,
                                         headers=self.headers()) as res:
                    if res.status_code >= 400:
                        raw = (await res.aread()).decode(errors="replace")
                        err = http_error(self.cfg.label, self.model, res.status_code,
                                         raw, res.headers.get("retry-after"))
                        err.raw = {"status": res.status_code, "body": _json_or_text(raw)}
                        raise err
                    async for event in self._parse(res):
                        yield event
        except (httpx.ConnectError, httpx.ConnectTimeout):
            err = BackendError(f"{self._where()} refused the connection. Check its address "
                               f"in Settings, Models, and that the server runs.")
            err.unreachable = True
            raise err from None
        except httpx.TimeoutException:
            raise BackendError(
                f"{self._where()} did not answer within {self.timeout.read:.0f} s.") \
                from None
        except httpx.RequestError as e:
            raise BackendError(f"{self._where()} closed the connection before the reply "
                               f"ended ({type(e).__name__}).") from None

    def _where(self) -> str:
        """The provider's label and host, for a sentence about not reaching it."""
        host = urlsplit(str(self.cfg.base_url)).netloc
        return f"{self.cfg.label} at {host}" if host else self.cfg.label

    async def _parse(self, res: httpx.Response):
        usage: Usage | None = None
        finish = "stop"
        reply = RawReply()
        async for payload in _sse_data(res):
            reply.last(payload)
            if payload == "[DONE]":
                break
            chunk = read_chunk(self.cfg.label, payload)
            reply.top(chunk)
            if isinstance(chunk, dict) and chunk.get("error"):
                # llama-server reports a failure that began after the 200 as an error
                # chunk (a context overflow mid-generation): the turn failed
                err = http_error(self.cfg.label, self.model, 500, payload)
                err.raw = reply.message("error")
                raise err
            if u := chunk.get("usage"):
                usage = Usage(int(u.get("prompt_tokens", 0)),
                              int(u.get("completion_tokens", 0)))
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                # llama.cpp --reasoning-format deepseek uses reasoning_content, OpenRouter uses reasoning
                if think := (delta.get("reasoning_content") or delta.get("reasoning")):
                    reply.reasoning.append(think)
                    yield ReasoningDelta(think)
                if text := delta.get("content"):
                    reply.content.append(text)
                    yield TextDelta(text)
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    index = int(tc.get("index", 0))
                    reply.call(index, tc.get("id"), fn.get("name"), fn.get("arguments"))
                    yield ToolCallDelta(
                        index=index,
                        id=tc.get("id"),
                        name=fn.get("name"),
                        arguments=fn.get("arguments") or "",
                    )
                # llama-server streams the logprobs a request asked for per chunk
                # (guard/judge.p_letter reads them from Done.raw)
                if lp := (choice.get("logprobs") or {}).get("content"):
                    reply.logprobs.extend(lp)
                if fr := choice.get("finish_reason"):
                    finish = fr
        yield Done(reason=finish, usage=usage,  # type: ignore[arg-type]
                   raw=reply.message(finish))


RAW_CAP = 256 * 1024


def plain_parts(parts: list[dict]):
    """A message's content parts as an OpenAI-shaped server takes them: text only is one
    string (the parts a blank line apart: a local chat template reads plain text), else
    the parts without the app's own marks (the tail's, turns/context.py)."""
    if all(p.get("type") == "text" for p in parts):
        return "\n\n".join(p.get("text") or "" for p in parts if p.get("text"))
    return [{k: v for k, v in p.items() if k != TAIL} for p in parts]


def wire_messages(messages: list[dict]) -> list[dict]:
    """Messages as an OpenAI-shaped server takes them: without the signed thinking
    blocks (Anthropic's, backends/anthropic.py; the thinking goes as reasoning_content)
    and the app's origin, content parts plain (plain_parts), and a tool result with its
    tool's `name` next to its tool_call_id (a local chat template, qwen's among them,
    names a result by it)."""
    names: dict[str, str] = {}
    out: list[dict] = []
    for m in messages:
        msg = {k: v for k, v in m.items() if k not in ("thinking", ORIGIN)}
        if isinstance(m.get("content"), list):
            msg["content"] = plain_parts(m["content"])
        for c in m.get("tool_calls") or []:
            names[c.get("id") or ""] = (c.get("function") or {}).get("name") or ""
        if m.get("role") == "tool" and (name := names.get(m.get("tool_call_id") or "")):
            msg["name"] = name
        out.append(msg)
    return out


class RawReply:
    """A streamed reply put back together as the server reported it: the top-level
    fields of its chunks (id, model, provider, usage, timings, ...), the message the
    deltas add up to, and the raw text of the last two chunks."""

    def __init__(self):
        self.fields: dict = {}
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, dict] = {}
        self.tail: list[str] = []
        self.thinking: list[dict] = []         # signed thinking blocks (Anthropic)
        self.logprobs: list[dict] = []         # per token, when the request asked

    def last(self, payload: str) -> None:
        self.tail = (self.tail + [payload])[-2:]

    def top(self, chunk: dict) -> None:
        self.fields.update({k: v for k, v in chunk.items()
                            if k not in ("choices", "message")})

    def call(self, index: int, id, name, arguments) -> None:
        slot = self.calls.setdefault(index, {"id": None, "type": "function",
                                             "function": {"name": None, "arguments": ""}})
        if id:
            slot["id"] = id
        if name:
            slot["function"]["name"] = name
        if isinstance(arguments, str):
            slot["function"]["arguments"] += arguments
        elif arguments is not None:
            slot["function"]["arguments"] = arguments

    def message(self, finish: str) -> dict:
        return cap({**self.fields,
                    "message": {"content": "".join(self.content),
                                "reasoning": "".join(self.reasoning) or None,
                                "tool_calls": [self.calls[i] for i in sorted(self.calls)],
                                **({"thinking": self.thinking} if self.thinking else {})},
                    "finish_reason": finish,
                    **({"logprobs": self.logprobs} if self.logprobs else {}),
                    "last_chunks": self.tail})


def cap(raw: dict) -> dict:
    """At most RAW_CAP of JSON: a longer one is kept as its cut text, with a note."""
    text = json.dumps(raw, ensure_ascii=False)
    if len(text) <= RAW_CAP:
        return raw
    return {"note": f"cut to {RAW_CAP // 1024} KB of {len(text) // 1024} KB",
            "text": text[:RAW_CAP]}


def read_chunk(label: str, text: str) -> dict:
    """One stream chunk's JSON object; a chunk that is not one fails the call."""
    try:
        chunk = json.loads(text)
    except json.JSONDecodeError:
        chunk = None
    if not isinstance(chunk, dict):
        raise BackendError(f"{label} sent a reply chunk that is not JSON.")
    return chunk


def _json_or_text(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw[:RAW_CAP]


async def _sse_data(res: httpx.Response):
    """One string per SSE event: its data lines joined with newlines. Other
    fields (event, id, retry, comments) are dropped."""
    data: list[str] = []
    async for line in res.aiter_lines():
        line = line.rstrip("\r\n")
        if line == "":
            if data:
                yield "\n".join(data)
                data = []
            continue
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


def _error(raw: str) -> dict:
    """The error object of an error body. llama.cpp's own error arrives wrapped in a
    string by Ollama (`{"error": "{\"error\": {...}}"}` from /api/chat, the same
    string as `message` from its /v1): the inner object wins, it has the numbers."""
    try:
        err = json.loads(raw).get("error") or {}
    except (json.JSONDecodeError, AttributeError):
        return {}
    text = err.get("message") if isinstance(err, dict) else err
    if isinstance(text, str) and text.lstrip().startswith("{"):
        try:
            inner = json.loads(text).get("error")
        except (json.JSONDecodeError, AttributeError):
            inner = None
        if isinstance(inner, dict):
            return inner
    if isinstance(err, str):
        return {"message": err}
    return err if isinstance(err, dict) else {}


def _is_context_overflow(raw: str) -> bool:
    err = _error(raw)
    marks = (str(err.get("type", "")), str(err.get("code", "")), str(err.get("message", "")))
    text = " ".join(marks).lower()
    # Anthropic: "prompt is too long: 215000 tokens > 200000 maximum", and "input length
    # and `max_tokens` exceed context limit: ..."
    return ("exceed_context_size" in text or "context_length_exceeded" in text
            or "maximum context length" in text
            or "context size has been exceeded" in text
            or "prompt is too long" in text or "exceed context limit" in text)


# what servers say when a request has an image and the model takes text only:
# OpenRouter (404 "No endpoints found that support image input") and llama.cpp
# without an mmproj ("image input is not supported")
_NO_IMAGES = ("support image input", "image input is not supported")


def _is_unsupported_image(raw: str) -> bool:
    msg = str(_error(raw).get("message", "")).lower()
    return any(w in msg for w in _NO_IMAGES)


def _overflow_numbers(raw: str) -> tuple[int | None, int | None]:
    """llama.cpp puts n_prompt_tokens and n_ctx in the error body."""
    err = _error(raw)

    def num(k):
        v = err.get(k)
        return int(v) if isinstance(v, int) and v > 0 else None
    return num("n_prompt_tokens"), num("n_ctx")


def no_credits(status: int, raw: str) -> bool:
    """A 402, or a body that says the credits ran out (OpenRouter "Insufficient credits",
    Anthropic's 400 "Your credit balance is too low")."""
    text = raw.lower()
    return status == 402 or "insufficient credits" in text or \
        "credit balance is too low" in text


def http_error(label: str, model: str, status: int, raw: str,
               retry_after: str | None = None) -> BackendError:
    """The BackendError for an error answer: a context overflow (code
    `context_overflow`, with the server's numbers when it gives them), an image the
    model cannot take (`unsupported_input`), no credits left (`out_of_credits`,
    `no_credits`), or the status and the body.
    `retry_after` is the Retry-After header; its seconds are kept on the error
    (backends/llm.py waits that long on a 429)."""
    if _is_context_overflow(raw):
        n_prompt, n_ctx = _overflow_numbers(raw)
        err = BackendError(
            f"This turn needs more context than {model} has ({n_prompt:,} of {n_ctx:,} "
            f"tokens)." if n_prompt and n_ctx
            else f"This conversation is longer than {label} can take.", retryable=False)
        err.code = "context_overflow"
        err.n_ctx = n_ctx
        err.n_prompt_tokens = n_prompt
        return err
    if _is_unsupported_image(raw):
        err = BackendError(
            f"{model} does not accept images, but Settings marks it as a vision model. "
            f"Remove its vision capability in Settings, Models.", retryable=False)
        err.code = "unsupported_input"
        return err
    if no_credits(status, raw):
        err = BackendError(f"{label} is out of credits.", retryable=False)
        err.code = "out_of_credits"
        return err
    err = BackendError(_http_message(label, model, status, raw),
                       retryable=status in (408, 429, 500, 502, 503, 529))
    err.status = status
    err.retry_after = (float(retry_after) if retry_after and retry_after.isdigit()
                       else None)
    return err


def _http_message(label: str, model: str, status: int, raw: str) -> str:
    if status in (401, 403):
        return f"{label} rejected the API key. Check the key in Settings, Models."
    if status == 429:
        return f"{label} is rate-limiting."
    if status == 404:
        return f"{label} has no model {model}. Check its name in Settings, Models."
    return f"{label} returned {status}. {_said(raw)[:300]}".strip()


def _said(raw: str) -> str:
    """What an error body says: an OpenAI-shaped body's message, else the body when it
    is plain text (never raw JSON or HTML). Only the message's last line: a llama.cpp
    chat template error quotes the template first and ends with the template's own
    sentence."""
    msg = str(_error(raw).get("message") or "")
    if not msg:
        text = " ".join(raw.split())
        return "" if text[:1] in ("{", "[", "<") else text
    lines = [ln.strip() for ln in msg.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def has_images(messages) -> bool:
    return any(isinstance(m.get("content"), list) and any(
        isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages)


class OpenRouterBackend(OpenAIBackend):
    def request_body(self, messages, tools, extra=None, model=None):
        body = super().request_body(messages, tools, extra, model)
        routing = self.cfg.raw.get("routing", {})
        if models := routing.get("models"):
            body["models"] = models
        if tier := routing.get("service_tier"):
            body["service_tier"] = tier
        provider = {k: v for k, v in (self.cfg.raw.get("provider") or {}).items()
                    if v not in ([], "", None)}
        if provider:
            body["provider"] = provider
        return body


class LlamaServerBackend(OpenAIBackend):
    """llama-server (alone or behind llama-swap) and tabbyAPI: the `llama` block of the
    model's settings goes into the request as it is, `grammar.file` read from config/. A
    chat call also carries its voice (settings/voice.py): on llama-server its Banned
    words at its Bias as `logit_bias` (`raw["voice"]`), on tabbyAPI its Banned words and
    phrases as `banned_strings` (`raw["banned_strings"]`, the engine backtracks)."""
    def request_body(self, messages, tools, extra=None, model=None):
        body = super().request_body(messages, tools, model=model)
        llama = dict(self.cfg.raw.get("llama") or {})
        grammar = llama.pop("grammar", None) or {}
        body.update(llama)
        if bias := self.cfg.raw.get("voice"):
            body["logit_bias"] = bias
        if banned := self.cfg.raw.get("banned_strings"):
            body["banned_strings"] = banned
        if text := self._grammar(grammar):
            body["grammar"] = text
        body.update(extra or {})
        return body

    def _grammar(self, grammar: dict) -> str | None:
        file = str(grammar.get("file") or "").strip()
        if not file:
            return None
        from ..config import CONFIG_DIR
        path = (CONFIG_DIR / file).expanduser()
        try:
            return path.read_text()
        except OSError as e:
            raise BackendError(f"The grammar file {file} of {self.model} cannot be read "
                               f"({type(e).__name__}).", retryable=False) from None


def build(cfg: BackendConfig, timeout: float = 600.0):
    from .anthropic import AnthropicBackend    # these two build on this module
    from .ollama import OllamaBackend
    kinds = {"openai": OpenAIBackend, "openrouter": OpenRouterBackend,
             "llama-server": LlamaServerBackend, "ollama": OllamaBackend,
             "anthropic": AnthropicBackend}
    try:
        return kinds[cfg.kind](cfg, timeout)
    except KeyError:
        raise BackendError(f"unknown backend kind {cfg.kind!r}", retryable=False) from None
