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


# ---- what a server said ------------------------------------------------------------
# One reader for every error answer the app gets from a server it calls (model
# providers, ComfyUI, SearXNG, the boxes, MCP servers, speech): the server's own words,
# whatever shape they come in, never dropped and never reworded. The caller's sentence
# wraps them once ("5090 returned 503: The model server is paused. Retry later.").

SAID_CAP = 400                  # characters of the server's words a sentence carries

# the keys an error's words sit under, in the order they win
_WORDS = ("message", "detail", "error_description", "msg", "reason", "description",
          "title", "error", "errors", "status_message", "statusText")
_KIND = ("type", "code", "status", "error_type")
_LEAF = re.compile(r"error|message|detail|reason|description|msg", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DROP = re.compile(r"<(script|style|head)[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Said:
    """What a server said in an error answer. `text`: its words on one line, masked,
    at most SAID_CAP characters (its start and its end when longer); "" when it said
    nothing. `kind`: the type or code it named, if any. `obj`: the error object itself
    when the body was JSON, for the numbers a caller reads (n_ctx, n_prompt_tokens)."""
    text: str
    kind: str = ""
    obj: dict | None = None


def said(body: str | bytes | None, status: int | None = None) -> Said:
    """The words of an error answer's body: a JSON error in any of the usual shapes
    (OpenAI and every server that copies it, Anthropic, Ollama, FastAPI's detail,
    RFC 9457 problem details, GraphQL's errors, OAuth's error_description, a JSON
    string inside a message), else the first string under a key that names an error,
    else the JSON itself; an HTML page's title, else its visible text; plain text as
    it is. An empty body gives the status's reason phrase (503: "Service Unavailable")."""
    if isinstance(body, bytes):
        body = body.decode(errors="replace")
    raw = (body or "").strip()
    if not raw:
        return Said(_phrase(status))
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if isinstance(value, (dict, list)) and value:
        text, kind, obj = _from_json(value)
        return Said(_fit(text) or _phrase(status), kind, obj)
    if isinstance(value, str):
        return Said(_fit(value) or _phrase(status))
    if isinstance(value, (dict, list)):                    # {} or []: it said nothing
        return Said(_phrase(status))
    if raw.startswith("<") or "<html" in raw[:500].lower():
        return Said(_fit(_html(raw)) or _phrase(status))
    return Said(_fit(raw))


def answered(who: str, r) -> str:
    """The one sentence for an error answer `r` (anything with status_code and content):
    "ComfyUI at host returned 503: Service Unavailable"."""
    words = said(r.content, r.status_code).text
    return f"{who} returned {r.status_code}: {words}" if words else \
        f"{who} returned {r.status_code}."


def _phrase(status: int | None) -> str:
    from http import HTTPStatus
    try:
        return HTTPStatus(int(status)).phrase if status else ""
    except ValueError:
        return ""


def _fit(text: str) -> str:
    """One line, masked, at most SAID_CAP characters: the start and the end of a longer
    text (a server often names the cause last, after quoting what it was given)."""
    text = redact(" ".join(str(text or "").split()))
    if len(text) <= SAID_CAP:
        return text
    half = (SAID_CAP - 3) // 2
    return f"{text[:half].rstrip()} … {text[-half:].lstrip()}"


def _html(raw: str) -> str:
    import html
    if m := _TITLE.search(raw):
        if title := " ".join(html.unescape(_TAG.sub(" ", m[1])).split()):
            return title
    return html.unescape(_TAG.sub(" ", _DROP.sub(" ", raw)))


def _from_json(value, depth: int = 0) -> tuple[str, str, dict | None]:
    """(words, kind, the error object) of a JSON error body."""
    if depth > 6:
        return "", "", None
    if isinstance(value, list):
        parts = [p for p in (_from_json(v, depth + 1) for v in value[:3]) if p[0]]
        return ("; ".join(p[0] for p in parts), parts[0][1], parts[0][2]) if parts \
            else ("", "", None)
    if isinstance(value, str):
        inner = _json_in(value)
        return _from_json(inner, depth + 1) if inner is not None else (value, "", None)
    if not isinstance(value, dict):
        return "", "", None
    kind = next((str(value[k]) for k in _KIND
                 if isinstance(value.get(k), (str, int)) and value[k] not in ("", "error")), "")
    title, detail = value.get("title"), value.get("detail")
    if isinstance(title, str) and title and isinstance(detail, str) and detail:
        return f"{title}: {detail}", kind, value           # RFC 9457 problem details
    loc, msg = value.get("loc"), value.get("msg")
    if isinstance(loc, list) and isinstance(msg, str):     # one pydantic detail item
        return f"{'.'.join(map(str, loc))}: {msg}", kind, value
    for key in _WORDS:
        v = value.get(key)
        if (isinstance(v, str) and v.strip()) or (isinstance(v, (dict, list)) and v):
            words, k, obj = _from_json(v, depth + 1)
            if words:
                return words, k or kind, value if obj is None else obj
    words = _leaf(value, depth) or json.dumps(value, ensure_ascii=False,
                                              separators=(", ", ": "))
    return words, kind, value


def _leaf(value, depth: int) -> str:
    """The first non-empty string under a key that names an error, anywhere in `value`."""
    if depth > 6 or not isinstance(value, dict):
        return ""
    for k, v in value.items():
        if isinstance(v, str) and v.strip() and _LEAF.search(str(k)):
            return v
    for v in value.values():
        if isinstance(v, dict) and (found := _leaf(v, depth + 1)):
            return found
    return ""


def _json_in(text: str):
    """The JSON object or list a string holds (Ollama wraps llama.cpp's error object in
    one), else None."""
    t = text.strip()
    if not t.startswith(("{", "[")):
        return None
    try:
        inner = json.loads(t)
    except ValueError:
        return None
    return inner if isinstance(inner, (dict, list)) else None


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


async def _filled(request: Request, text: str) -> str:
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
            err["message"] = await _filled(request, err["message"])
        for k, v in (err.get("detail") or {}).items():
            if "{{" in v:
                err["detail"][k] = await _filled(request, v)
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
# the agent reads (a tool result, the tail, me__whoami) is the agent_ sentence: the fact alone.

def _name(what: str) -> str:
    return f"{what[0].upper()}{what[1:]}"


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).netloc or url


def not_set_up(what: str) -> str:
    """The box has no address. Shown in Setup beside its address field and elsewhere beside
    Open Setup, so it names the fact alone."""
    return f"{_name(what)} has no address yet."


# a compose service's host name -> the profile in .env that starts it (docker-compose.yml)
PROFILES = {"shellbox": "shellbox", "browser": "browser", "searxng": "web"}


def unreachable_words(what: str, url: str, e: Exception) -> str:
    """No answer from the box at `url`: refused, too slow or cut off. At a compose service's
    own address, the way to start that container (settings.services.unreachable first names
    a compose variable that points elsewhere)."""
    if not url:
        return not_set_up(what)
    from urllib.parse import urlsplit
    profile = PROFILES.get(urlsplit(url).hostname or "")
    if profile:
        return (f"{_name(what)} is not running. In the Celine folder, put {profile} in "
                f"COMPOSE_PROFILES in .env and run docker compose up -d.")
    return (f"{_name(what)} did not answer at {_host(url)}. Make sure it is running and the "
            f"address is right, then press Check.")


def agent_not_set_up(what: str) -> str:
    return f"{_name(what)} is not set up."


def agent_unreachable(what: str, url: str) -> str:
    if not url:
        return agent_not_set_up(what)
    return f"{_name(what)} at {_host(url)} does not answer."


def agent_bad_key(what: str, url: str) -> str:
    return f"{_name(what)} at {_host(url)} does not take the app's key."


def bad_key(what: str, url: str) -> str:
    """The box answered 401 or 403: its key.hash is not the hash of the app's key."""
    return (f"{_name(what)} at {_host(url)} does not accept this app's key. Copy the key "
            f"hash below into its key.hash file, then press Check.")
