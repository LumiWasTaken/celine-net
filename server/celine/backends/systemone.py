"""A decision model's /v1/systemone (the System One API that llama.cpp and llama-swap
serve): a state and named questions in, a probability per question out, never text.

It sits behind the same queue as every model (llm.bind): the call's `body` carries
{"systemone": {"state": str, "questions": {id: {type, instructions, criteria}}}}, and the
one event it yields is Done with the server's answer as `raw` ({answers: {id: {noul: p}
| {choice, probabilities}}, usage}). guard/small_role.decide reads it.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import BackendError, Done, Usage
from .openai import OpenAIBackend, _json_or_text, http_error


class SystemOneBackend(OpenAIBackend):
    def __init__(self, cfg, timeout: float = 600.0):
        super().__init__(cfg, timeout)
        self.supports_images = False
        self.supports_tools = False

    def url(self) -> str:
        return f"{self.cfg.base_url}/systemone"

    def request_body(self, messages, tools, extra: dict[str, Any] | None = None,
                     model: str | None = None) -> dict[str, Any]:
        asked = (extra or {}).get("systemone") or {}
        return {"model": model or self.model, "state": asked.get("state", ""),
                "questions": asked.get("questions") or {}}

    async def stream(self, messages, tools, body: dict[str, Any] | None = None,
                     model: str | None = None):
        if not (body or {}).get("systemone"):
            raise BackendError(f"{self.model} is a decision model: it answers questions, "
                               f"not chat.", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.post(self.url(), headers=self.headers(),
                                        json=self.request_body(messages, tools, body, model))
        except (httpx.ConnectError, httpx.ConnectTimeout):
            err = BackendError(f"{self._where()} refused the connection. Check its address "
                               f"in Settings, Models, and that the server runs.")
            err.unreachable = True
            raise err from None
        except httpx.TimeoutException:
            raise BackendError(
                f"{self._where()} did not answer within {self.timeout.read:.0f} s.") from None
        except httpx.RequestError as e:
            raise BackendError(f"{self._where()} closed the connection before the reply "
                               f"ended ({type(e).__name__}).") from None
        if res.status_code >= 400:
            err = http_error(self.cfg.label, self.model, res.status_code, res.text,
                             res.headers.get("retry-after"))
            err.raw = {"status": res.status_code, "body": _json_or_text(res.text)}
            raise err
        try:
            raw = res.json()
        except ValueError:
            raw = None
        if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict):
            raise BackendError(f"{self.model} answered without decisions: "
                               f"{res.text[:200]}", retryable=False)
        u = raw.get("usage") or {}
        yield Done("stop", Usage(int(u.get("prompt_tokens") or 0),
                                 int(u.get("completion_tokens") or 0), 0), raw=raw)
