"""The model proxy for opencode and the other programs in its shellbox.

    GET  /llm/v1/models             the models it may use (settings/her_models.py), and
                                    `default`: the first of them its chats run on
    POST /llm/v1/chat/completions   OpenAI's wire format

Each model in /models carries what the app knows of its size, so a coding agent budgets
with the real numbers (shellbox/agents/opencode.py writes them as opencode's
`limit.context` and `limit.output`), and leaves out what it does not know:

    context_length      the tokens one request may hold: the window its own turns
                        budget with (turns/tokens.py known_window: the server's /props
                        or model list, else the context in Settings, Models)
    max_output_tokens   the most one answer may take: Max tokens in Settings, else what
                        the provider's model list states (OpenRouter, Anthropic)

The shellbox never holds a provider key: it has the app's keys/shellbox-llm.key, which this app checks
(`Authorization: Bearer`). These routes skip the login's gate and the
origin check (auth/gate.py, api/origin.py) and answer 401 without the key, 503 while
the key is missing, 403 to a call while it is frozen (guard/freeze.py). The key
opens these routes only: on /api it is no token (the gate's 404, api/settings.py
_person's 403).

Every call takes its slot in the app's model queue (backends/queue.py) in the
`shellbox` lane at background priority, so its turns and the user's go first; it waits
as long as it takes, and leaving the call gives the slot back. The request goes to the
model's provider with the provider's key and the provider's own model name; the answer
(an SSE stream or JSON) comes back byte for byte, its status included.
"""
from __future__ import annotations

import hmac
import json
import logging

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import keys, metrics
from ..backends.queue import Priority, Ticket
from ..settings import her_models
from ..settings.kinds import headers as provider_headers, server_output
from ..settings.registry import backend_config
from ..turns import tokens
from .deps import LLM_PROXY

log = logging.getLogger("celine.llm_proxy")
router = APIRouter(prefix=LLM_PROXY.rstrip("/"))

LANE = "shellbox"


def key(cfg) -> str:
    return keys.read(cfg.data_dir, "shellbox-llm")


def _error(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind, "code": kind}},
                        status_code=status)


# a reverse proxy adds one of these; the box calls the app directly
_PROXIED = ("x-forwarded-for", "forwarded", "x-real-ip")


def _refused(request: Request) -> JSONResponse | None:
    """The key check: None when the caller holds the shellbox-llm key and came straight
    from the box, not through the public origin."""
    if any(n in request.headers for n in _PROXIED):
        return _error(403, "The model proxy is for the shellbox only.", "permission_error")
    want = key(request.app.state.services.cfg)
    if not want:
        return _error(503, "The model proxy is off: the app has no keys/shellbox-llm.key.", "unavailable")
    h = request.headers
    given = h.get("authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), want.encode()):
        return _error(401, "Wrong key: use $OPENAI_API_KEY from your shell.",
                      "authentication_error")
    return None


# ---- the call --------------------------------------------------------------------------

class ProxyCall(Response):
    """The forwarded call as an ASGI response: it waits for its slot, asks the
    provider, and only then sends the provider's status, so an error comes back as the
    provider gave it. A client that leaves ends the wait or the stream at once; the
    slot is freed whatever happens."""

    def __init__(self, services, ref: str, endpoint: str, path: str, body: dict,
                 headers: dict, timeout: float, provider: str):
        super().__init__()
        self.services, self.ref, self.body = services, ref, body
        self.provider = provider
        self.endpoint, self.url = endpoint, f"{endpoint}/{path}"
        self.upstream, self.timeout = headers, timeout

    async def __call__(self, scope, receive, send) -> None:
        async with anyio.create_task_group() as tg:
            async def watch() -> None:
                while (await receive())["type"] != "http.disconnect":
                    pass
                tg.cancel_scope.cancel()
            tg.start_soon(watch)
            try:
                await self._run(send)
            finally:
                tg.cancel_scope.cancel()

    async def _run(self, send) -> None:
        llm = self.services.llm
        ticket = Ticket(endpoint=self.endpoint, model=self.body["model"],
                        priority=Priority.BACKGROUND, lane=LANE, role=LANE,
                        label="Shellbox")
        raw = json.dumps(self.body).encode()
        await llm.scheduler.acquire(ticket)
        metrics.QUEUE_WAIT.labels(LANE, self.provider).observe(ticket.waited_ms / 1000)
        started, status, seen = False, "cancelled", b""
        try:
            async with httpx.AsyncClient(
                    transport=llm.router.transport,
                    timeout=httpx.Timeout(10.0, read=self.timeout)) as client:
                try:
                    async with client.stream("POST", self.url, content=raw,
                                             headers=self.upstream) as up:
                        await send({"type": "http.response.start",
                                    "status": up.status_code,
                                    "headers": [(b"content-type", up.headers.get(
                                        "content-type", "application/json").encode())]})
                        started = True
                        async for chunk in up.aiter_bytes():
                            # the head and the tail hold the token counts
                            seen = seen + chunk if len(seen) < 4096 else \
                                seen[:4096] + (seen[4096:] + chunk)[-8192:]
                            await send({"type": "http.response.body", "body": chunk,
                                        "more_body": True})
                        await send({"type": "http.response.body", "body": b""})
                        status = str(up.status_code)
                except httpx.HTTPError as e:
                    status = "unreachable"
                    if not started:                 # nothing went out yet
                        await _send_error(send, _error(
                            502, f"The model's server did not answer: "
                                 f"{type(e).__name__}.", "upstream_error"))
        finally:
            llm.scheduler.release(ticket)
            metrics.PROXY_REQUESTS.labels(self.ref, status).inc()
            for side, n in metrics.usage_in(seen).items():
                metrics.PROXY_TOKENS.labels(self.ref, side).inc(n)
            log.info("proxy call", extra={"event": "llm_proxy.call", "model": self.ref,
                                          "waited_ms": ticket.waited_ms})


async def _send_error(send, r: JSONResponse) -> None:
    await send({"type": "http.response.start", "status": r.status_code,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": r.body})


def resolve(s, model: str) -> str | None:
    """`default` (the chat model) or a model it may use -> its ref; None when it is
    not one of its own."""
    if model == "default":
        model = next(iter(s.chat_chain(None)), "")
    return model if model in her_models.allowed(s) else None


async def _forward(request: Request, path: str):
    if (refused := _refused(request)) is not None:
        return refused
    services = request.app.state.services
    from ..guard.freeze import frozen
    if await frozen(services.store) is not None:
        return _error(403, services.cfg.substitute(
            "You are frozen. Nothing runs until {{user}} unfreezes you."),
            "permission_error")
    s = getattr(services.cfg, "settings", None)
    if services.llm is None or s is None:
        return _error(503, "The app's models are not running.", "unavailable")
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _error(400, "The body is a JSON object.", "invalid_request_error")
    asked = str(body.get("model") or "default")
    ref = resolve(s, asked)
    if ref is None:
        names = ", ".join(her_models.allowed(s)) or "none"
        return _error(404, f"{asked} is not one of your models. Yours: {names}.",
                      "model_not_found")
    bc = backend_config(s, ref)
    p = s.providers[s.split(ref)[0]]
    body["model"] = bc.model
    headers = {"content-type": "application/json",
               **provider_headers(p["kind"], bc.api_key)}
    return ProxyCall(services, ref, bc.base_url, path, body, headers,
                     float((bc.raw or {}).get("timeout", 600.0)), bc.label)


@router.get("/models")
async def models(request: Request):
    if (refused := _refused(request)) is not None:
        return refused
    s = getattr(request.app.state.services.cfg, "settings", None)
    refs = her_models.allowed(s) if s is not None else []
    data = [{"id": r, "object": "model", "created": 0, "owned_by": "celine",
             "name": her_models.label(s, r), **_sizes(s, r)} for r in refs]
    if refs:
        d = resolve(s, "default")
        data.append({"id": "default", "object": "model", "created": 0, "owned_by": "celine",
                     "name": f"default: {her_models.label(s, d)}", **_sizes(s, d)})
    return {"object": "list", "data": data}


def _sizes(s, ref: str) -> dict:
    """context_length and max_output_tokens of one model, those the app knows."""
    bc = backend_config(s, ref)
    if bc is None:
        return {}
    out = (s.model(ref) or {}).get("max_tokens") or server_output(bc.base_url, bc.model)
    return {k: v for k, v in (("context_length", tokens.config_window(bc)),
                              ("max_output_tokens", out)) if v}


@router.post("/chat/completions")
async def chat_completions(request: Request):
    return await _forward(request, "chat/completions")
