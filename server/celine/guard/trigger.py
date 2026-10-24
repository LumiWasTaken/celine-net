"""Layer 1: which calls the judge looks at. Plain code, microseconds.

A call goes to the judge only when all three hold: outside text is in the request,
the call acts, and the call is linked to that text (an argument copies a piece of it,
or the call is in the always-look set). Never looked at: image and music generation
(their prompts are never checked), the vault (it has its own tap), messages to the
owner.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from .bastet import ADDR
from .mark import Span
from .sources import NEVER

ALWAYS = {"notebook__write", "notebook__edit", "notebook__delete", "notebook__move",
          "plan__set", "missions__update", "sub_agents__start",
          "email__delete", "email__move", "user_documents__write",
          "me__edit", "routines__write"}
# read-only, but data can leave in the URL or the query
OUT_IN_URL = {"browser__open": "url", "web__read_page": "url", "web__search": "query"}

# shell commands with egress: data leaves the box, or code comes in to run. Read before
# a call runs; once it ran, the box's own verdict (display "net", facts.sink) counts
EGRESS = re.compile(
    r"(?<![\w.-])(?:curl|wget|nc|ncat|netcat|ssh|scp|sftp|rsync|telnet|ftp)(?![\w.-])"
    r"|\bgit\s+push\b"
    r"|\b(?:pip3?|pipx|uv\s+pip|npm|pnpm|yarn|cargo|gem|go)\s+(?:install|add|get|i)\b"
    r"[^\n;&|]*(?:https?://|git\+)"
    # an interpreter given its code inline or on stdin: python -c, python3 - <<EOF,
    # node -e, perl -e, php -r, ruby -e; a raw socket; a shell running a script file
    r"|(?<![\w.-])python[\d.]*(?:\s+-[a-z]+)*?\s+-c?(?![\w-])"
    r"|(?<![\w.-])(?:node|nodejs)(?:\s+-[\w-]+)*?\s+(?:-e|--eval|-p|--print)(?![\w-])"
    r"|(?<![\w.-])(?:perl|ruby)(?:\s+-\w+)*?\s+-[a-z]*e(?![\w-])"
    r"|(?<![\w.-])php(?:\s+-\w+)*?\s+-r(?![\w-])"
    r"|\bopenssl\s+s_client\b|/dev/(?:tcp|udp)/"
    r"|(?<![\w.-])(?:ba|da|z)?sh(?:\s+-(?![a-z]*c)[a-z]+)*\s+(?![-<>|;&])[^\s;&|<>]+",
    re.I)

URL = re.compile(r"https?://[^\s\"'<>()\[\]]+", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
HANDLE = re.compile(r"(?<![\w/])(?:@|u/|r/)[a-z0-9_][\w.-]+", re.I)
DOMAIN = re.compile(r"(?<![\w@.-])(?:[a-z0-9-]+\.)+([a-z]{2,24})(?![\w-])", re.I)
# a file name is not a domain
FILE_ENDS = {"md", "sh", "py", "txt", "json", "js", "ts", "tsx", "html", "css", "toml",
             "yaml", "yml", "log", "csv", "png", "jpg", "jpeg", "gif", "pdf", "zip", "gz",
             "tar", "rs", "go", "lock", "conf", "cfg", "ini", "env", "sql", "db", "service"}
RUN = 24          # characters: a run this long shared with outside text is a copy
TOKENS = 3        # shell tokens in a row shared with outside text are a copy


@dataclass
class Hit:
    link: str                        # why the call is looked at, in a few words
    piece: str = ""                  # what it shares with the outside text
    spans: list[Span] = field(default_factory=list)   # the outside text it links to


def norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


# what a judge wraps a quote in ('"ssh root@host nvidia-smi"'); never part of the words
QUOTES = "\"'`\u201c\u201d\u2018\u2019"


def bare(quote: str) -> str:
    """A judge's quote without the quote marks and spaces around it."""
    return (quote or "").strip().strip(QUOTES).strip()


def strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in strings(v)]
    return []


def texts(args) -> list[str]:
    """The strings of a call's arguments, the terminal or tab a default fills in left
    out: what the call itself types or sends."""
    return strings({k: v for k, v in (args or {}).items() if k not in ("terminal", "tab")})


# who a message stays in the house with: the owner, and Bastet (intercom/addresses.py)
HOUSE = ("user", "bastet", ADDR)


def to_house(args: dict) -> bool:
    """An intercom__send only to the owner or Bastet."""
    to = args.get("to")
    return isinstance(to, str) and bool(to.strip()) and \
        all(t.strip().casefold() in HOUSE for t in to.split(","))


def shared_run(value: str, text: str) -> str | None:
    """A run of RUN characters of `value` found in `text`. Any such run holds one of
    the half-length windows at a multiple of RUN // 2, so only those are searched."""
    step = RUN // 2
    for k in range(0, len(value) - step + 1, step):
        if value[k:k + step] in text:
            for j in range(max(0, k - step), k + 1):
                w = value[j:j + RUN]
                if len(w) == RUN and w in text:
                    return w
    return None


def _command(value: str, text: str) -> str | None:
    for line in value.splitlines():
        words = line.split()
        for i in range(len(words) - TOKENS + 1):
            w = " ".join(words[i:i + TOKENS]).casefold()
            if w in text:
                return w
    return None


def copied(name: str, values: list[str], text: str) -> tuple[str, str] | None:
    """(what it copies, the piece) when an argument copies a piece of `text` (normed)."""
    for v in values:
        for u in URL.findall(v):
            if norm(u.rstrip(".,;:!?")) in text:
                return "copies a link", u
        for a in EMAIL.findall(v):
            if a.casefold() in text:
                return "copies an address", a
        for h in HANDLE.findall(v):
            if h.casefold() in text:
                return "copies a handle", h
        for m in DOMAIN.finditer(v):
            if m.group(1).casefold() not in FILE_ENDS and m.group(0).casefold() in text:
                return "copies a domain", m.group(0)
        if name.startswith("shell__") and (w := _command(v, text)):
            return "copies a command", w
        if (w := shared_run(norm(v), text)):
            return "copies text", w
    return None


def always(name: str, args: dict) -> str | None:
    """Why `name` is in the always-look set, else None."""
    if name in ALWAYS:
        return "always looked at"
    if name in ("shell__run", "shell__type"):
        typed = " ".join(texts(args))
        return "a command with egress" if EGRESS.search(typed) else None
    if name == "browser__act":
        for s in args.get("steps") or ():
            if not isinstance(s, dict):
                continue
            do, text = s.get("do"), str(s.get("text") or "")
            if do in ("type", "upload") or (do == "press" and "enter" in text.casefold()):
                return f"a page {'upload' if do == 'upload' else 'form'}"
        return None
    if name == "intercom__send":
        return "a message to someone else"
    return None


def _out(name: str, args: dict) -> list[str]:
    """What a read-only call sends out: the query, or the values in the URL's query."""
    v = args.get(OUT_IN_URL[name])
    if not isinstance(v, str):
        return []
    if name == "web__search":
        return [v]
    u = urlsplit(v)
    return [x for _, x in parse_qsl(u.query)] + ([u.fragment] if u.fragment else [])


RAISED = "after words aimed at an AI"


def look(name: str, args: dict, read_only: bool, spans: list[Span],
         raised: bool = False) -> Hit | None:
    """The layer 1 hit for one call, None when the judge does not see it. `raised`: the
    page check found words aimed at an AI in this turn, so every acting call is looked
    at."""
    if not spans or name.startswith(NEVER):
        return None
    if name == "intercom__send" and to_house(args):
        return None
    if read_only:
        if name not in OUT_IN_URL:
            return None
        whole = norm("\n".join(s.text for s in spans))
        if not any(x.strip() and norm(x) not in whole for x in _out(name, args)):
            return None
    values = strings(args)
    hit: Hit | None = None
    for s in spans:
        got = copied(name, values, norm(s.text))
        if got is None:
            continue
        if hit is None:
            hit = Hit(got[0], got[1])
        hit.spans.append(s)
    if hit is not None:
        return hit
    if why := always(name, args):
        return Hit(why, "", list(spans))
    if raised and not read_only:
        return Hit(RAISED, "", list(spans))
    return None
