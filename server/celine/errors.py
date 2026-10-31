"""Error taxonomy and the one redaction set: the keys of the config and Settings and the
vault's passwords. redact() masks logs and error text with it, and the shapes of common
tokens besides; mask() masks a tool call's arguments and result (turns/) and its text
when it is stored (conversations/tree.py) with the set only, so a token the user hands
it still works; blank() does it to the bytes of its stored terminal logs
(terminals/sync.py)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

log = logging.getLogger("celine.errors")

_SECRETS: list[str] = []
_BYTES: list[bytes] = []
_BY_SOURCE: dict[str, list[str]] = {}

_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
]


def set_secrets(values: list[str], source: str = "config") -> None:
    """Replace one source's part of the known-secret set: the config's keys (reload),
    or the vault's passwords (vault/). Longest first, so a secret that contains another
    is masked whole."""
    global _SECRETS, _BYTES
    _BY_SOURCE[source] = list(values)
    # each as written, and as a JSON string holds it (a tool call's raw arguments)
    _SECRETS = sorted({w for vs in _BY_SOURCE.values() for v in vs if v and len(v) >= 8
                       for w in (v, json.dumps(v, ensure_ascii=False)[1:-1])},
                      key=len, reverse=True)
    _BYTES = sorted({s.encode() for s in _SECRETS}, key=len, reverse=True)


def mask(text: str) -> str:
    """The known secrets out of a text."""
    if not text:
        return text
    for s in _SECRETS:
        if s in text:
            text = text.replace(s, "****")
    return text


def blank(data: bytes) -> bytes:
    """The known secrets out of raw bytes, each byte of one as a *: the length stays, so
    an offset into the bytes still holds."""
    for s in _BYTES:
        if s in data:
            data = data.replace(s, b"*" * len(s))
    return data


def longest() -> int:
    """The bytes of the longest known secret: how far one written in two pieces reaches
    back."""
    return len(_BYTES[0]) if _BYTES else 0


def mask_all(value):
    """mask() on every string in a value (a tool's display, a dict, a list)."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {k: mask_all(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_all(v) for v in value]
    return value


def redact(text: str) -> str:
    """The known secrets and anything shaped like a token out of a log line or an
    error text."""
    if not text:
        return text
    text = mask(text)
    for pat in _PATTERNS:
        text = pat.sub("****", text)
    return text


@dataclass
class CelineError(Exception):
    """An error with a code the UI knows how to place. `message` is shown to the user
    as it is."""

    code: str
    message: str
    status: int = 400
    detail: dict | None = None

    def __str__(self) -> str:
        return redact(self.message)

    def as_dict(self) -> dict:
        out: dict = {"code": self.code, "message": redact(self.message)}
        if self.detail:
            out["detail"] = {k: redact(str(v)) for k, v in self.detail.items()}
        return {"error": out}


class NotFound(CelineError):
    def __init__(self, what: str):
        super().__init__("not_found", f"The {what} does not exist.", 404)


class Busy(CelineError):
    def __init__(self, message: str):
        super().__init__("busy", message, 409)


class UnresolvedToolCalls(CelineError):
    def __init__(self, names: list[str]):
        joined = ", ".join(names)
        super().__init__(
            "unresolved_tool_calls",
            f"{joined} is still running. Stop it first, then edit or regenerate.",
            409,
            {"tools": joined},
        )


class TooLarge(CelineError):
    def __init__(self, what: str, size: int, limit: int):
        super().__init__(
            "too_large",
            f"{what} is {size / 1e6:.1f} MB. The limit is {limit / 1e6:.0f} MB.",
            413,
        )


class ForbiddenOrigin(CelineError):
    def __init__(self, origin: str):
        super().__init__("forbidden_origin", f"This page's address ({origin}) cannot use the app. "
                                             "Open the app at its own address.", 403)


async def _as_hers(request: Request, text: str) -> str:
    """A sentence still holding {{name}} and the like, filled in."""
    services = getattr(request.app.state, "services", None)
    cfg = getattr(services, "cfg", None)
    return text if cfg is None else cfg.substitute(text)


class ToolRefused(Exception):
    """A tool call that is not made, or that its box said no to: the sentence is the
    tool's whole result, as an error. Here, not in skills/, because the box clients
    (shellbox.py) raise it for the app's pages too."""


_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
               405: "method_not_allowed", 413: "too_large"}


def install_error_handler(app) -> None:
    # a sentence built outside a turn (conversations/tree.py) reaches the user filled in,
    # whichever route let it through
    @app.exception_handler(CelineError)
    async def _celine_error(request: Request, exc: CelineError):
        body = exc.as_dict()
        err = body["error"]
        if "{{" in err["message"]:
            err["message"] = await _as_hers(request, err["message"])
        for k, v in (err.get("detail") or {}).items():
            if "{{" in v:
                err["detail"][k] = await _as_hers(request, v)
        return JSONResponse(body, status_code=exc.status)

    # A bad request body gets one short sentence: the first field and what is wrong with
    # it, never the body itself (FastAPI's default 422 echoes every input it got).
    @app.exception_handler(RequestValidationError)
    async def _bad_body(request: Request, exc: RequestValidationError):
        errs = exc.errors()
        first = errs[0] if errs else {}
        where = ".".join(str(x) for x in first.get("loc", ()) if x != "body") or "body"
        what = first.get("msg") or "not valid"
        return JSONResponse({"error": {"code": "bad_request",
                                       "message": f"{where}: {what}.",
                                       "detail": {"field": where}}},
                            status_code=400)

    # what the framework answers itself (a wrong method, a body it cannot read), in the
    # same shape
    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        code = _HTTP_CODES.get(exc.status_code, "error")
        return JSONResponse({"error": {"code": code, "message": str(exc.detail)}},
                            status_code=exc.status_code, headers=exc.headers)

    # a bug: logged with its trace, and the page gets the shape it reads, never plain text
    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        log.error("unhandled error", exc_info=exc,
                  extra={"event": "http.unhandled", "path": request.url.path})
        return JSONResponse({"error": {"code": "internal", "message": redact(
            f"The server failed: {type(exc).__name__}: {exc}")}}, status_code=500)

    # JSON nested deeper than Python's recursion limit
    @app.exception_handler(RecursionError)
    async def _too_deep(request: Request, exc: RecursionError):
        return JSONResponse({"error": {"code": "bad_request",
                                       "message": "The request is nested too deeply."}},
                            status_code=400)


# ---- a box that is not there ----------------------------------------------------------------
# The shellbox, the browser, SearXNG and ComfyUI are services the app runs without. When one
# has no address, does not answer or does not take the app's key, a page or probe says so in
# the same plain sentence: the service, its address and what the user does about it. What
# she reads (a tool result, the tail, me__whoami) is the her_ sentence: the fact alone.

def _name(what: str) -> str:
    return f"{what[0].upper()}{what[1:]}"


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).netloc or url


def not_set_up(what: str) -> str:
    """The box has no address: type one in Setup, or leave the feature off."""
    return f"{_name(what)} has no address yet. Type one below, or leave it off."


# a compose service's host name -> the profile in .env that starts it (docker-compose.yml)
PROFILES = {"shellbox": "shellbox", "browser": "browser", "searxng": "web"}


def unreachable_words(what: str, url: str, e: Exception) -> str:
    """No answer from the box at `url`: refused, too slow or cut off. At a compose service's
    own address, the way to start that container."""
    if not url:
        return not_set_up(what)
    from urllib.parse import urlsplit
    profile = PROFILES.get(urlsplit(url).hostname or "")
    if profile:
        return (f"{_name(what)} is not running. In the Celine folder, put {profile} in "
                f"COMPOSE_PROFILES in .env and run docker compose up -d.")
    return (f"{_name(what)} did not answer at {_host(url)}. Make sure it is running and the "
            f"address is right, then press Check.")


def her_not_set_up(what: str) -> str:
    return f"{_name(what)} is not set up."


def her_unreachable(what: str, url: str) -> str:
    if not url:
        return her_not_set_up(what)
    return f"{_name(what)} at {_host(url)} does not answer."


def her_bad_key(what: str, url: str) -> str:
    return f"{_name(what)} at {_host(url)} does not take the app's key."


def bad_key(what: str, url: str) -> str:
    """The box answered 401 or 403: its key.hash is not the hash of the app's key."""
    return (f"{_name(what)} at {_host(url)} does not accept this app's key. Copy the key "
            f"hash below into its key.hash file, then press Check.")
