"""What code finds before Bastet asks anything: plain code, microseconds, so the judge is asked only where a rule has a reason.

- classes: who may see a result. The agent's notes say it in their head (`class:`), the
  sources by kind: Messages archive, documents, mail, their chats sensitive; web and browser
  public; an MCP server its `class` in mcp.toml (default sensitive).
- sinks: where a call sends text. public: a web search's query, the address of a page
  it opens, what it types or uploads into a page, a shell command that reaches the
  net, an MCP tool that writes with `sends = "public"` (the default); people: a message
  to anyone but the owner, an MCP tool with `sends = "people"`; everything else stays in
  the house, never looked at by the leak rule (only a closed house gate looks).
- the private corpus: the notes it carries this turn and the classed results in the
  request (the owner's own messages are not in it); a piece of it copied into a sink
  call is what the leak rule asks about.
- escape, damage, sites: the command, tool or site that gives the rule a reason to ask.
persist's matcher (the same thing again, another way) is persist.py.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, unquote_plus, urlsplit

from .trigger import EGRESS, EMAIL, HANDLE, URL, norm, shared_run, strings, texts, to_house

CLASSES = ("public", "informal", "nsfw", "unclassed", "sensitive")   # loosest first
SENSITIVE = {"chat_archive", "user_documents", "email", "discord", "whatsapp"}
PUBLIC = {"web", "browser"}
SENDS = ("public", "people", "house")


def mcp_of(registry, name: str):
    """The mcp.toml block of the server `name` belongs to, None for an app skill."""
    skill = (getattr(registry, "skills", None) or {}).get(name.split("__")[0])
    cfg = getattr(skill, "cfg", None)
    return cfg if hasattr(cfg, "sends") else None


def result_class(name: str, display: dict, registry) -> str | None:
    """The class of a result (display "class"), None for one with no class."""
    if display.get("class"):
        return display["class"]
    if (m := mcp_of(registry, name)) is not None:
        return m.cls
    skill = name.split("__")[0]
    return "sensitive" if skill in SENSITIVE else "public" if skill in PUBLIC else None


def strictest(classes) -> str | None:
    found = [c for c in classes if c in CLASSES]
    return max(found, key=CLASSES.index) if found else None


# ---- sinks -----------------------------------------------------------------------------

def _address(url) -> list[str]:
    """What leaves in an address besides the site: its path, query and fragment."""
    if not isinstance(url, str) or not url:
        return []
    u = urlsplit(url if "//" in url else "//" + url)
    # a query's "+" is a space ("Lindenstra%C3%9Fe+14" is a street address)
    rest = unquote(u.path) + ("?" + unquote_plus(u.query) if u.query else "") + \
        ("#" + unquote(u.fragment) if u.fragment else "")
    return [rest] if rest.strip("/?#") else []


def typed(steps) -> list[str]:
    """What a browser__act types or uploads into a page."""
    return [str(s.get("text") or "") for s in steps or () if isinstance(s, dict)
            and s.get("do") in ("type", "upload") and s.get("text")]


def sink(call: dict) -> tuple[str, list[str]]:
    """(where the call sends, the text it sends). A house call's text is its arguments:
    only a closed house gate looks at it (guard.closed), the leak rule never does."""
    name, args = call["name"], call.get("args") or {}
    if name == "web__search":
        return "public", [str(args.get("query") or "")]
    if name in ("web__read_page", "browser__open"):
        return "public", _address(args.get("url"))
    if name == "browser__act" and (t := typed(args.get("steps"))):
        return "public", t
    if name in ("shell__run", "shell__type"):
        t = " ".join(texts(args))
        return ("public" if EGRESS.search(t) else "house"), [t]
    if name == "intercom__send" and not to_house(args):
        return "people", strings(args)
    if call.get("sends") in ("public", "people") and not call.get("read_only"):
        return call["sends"], strings(args)
    return "house", strings(args)


# ---- the private corpus and what a call copies of it ------------------------------------

@dataclass(frozen=True)
class Private:
    text: str
    cls: str
    where: str           # a note's path, or the tool whose result it is


@dataclass(frozen=True)
class Piece:
    text: str
    kind: str            # address, e-mail, phone, IBAN, link, handle, text
    cls: str
    where: str


async def private(store, turn: dict, request: list[dict]) -> list[Private]:
    """The notes it carries this turn, each with its class, and every classed result in
    the request; public text is left out."""
    from ..skills.notebook import class_line
    out = [Private(n["text"], class_line(n["text"]), n["path"])
           for n in turn.get("notes") or ()]
    tools = {m.get("tool_call_id"): m for m in request or ()
             if m.get("role") == "tool" and m.get("tool_call_id")}
    if tools:
        marks = ",".join("?" * len(tools))
        rows = await store.read(lambda c: c.execute(
            f"SELECT id, name, display FROM tool_calls WHERE id IN ({marks})",
            list(tools)).fetchall())
        for r in rows:
            d = json.loads(r["display"] or "{}") or {}
            content = tools[r["id"]].get("content")
            if isinstance(content, str) and d.get("class"):
                out.append(Private(content, d["class"], d.get("path") or r["name"]))
    return [p for p in out if p.cls != "public" and p.text.strip()]


async def outside(store, request: list[dict]) -> str:
    """Every outside result in the request (web pages, search, mail), as one text: what
    it read there is public, whatever note it is in too."""
    tools = {m.get("tool_call_id"): m.get("content") for m in request or ()
             if m.get("role") == "tool" and isinstance(m.get("content"), str)}
    if not tools:
        return ""
    marks = ",".join("?" * len(tools))
    rows = await store.read(lambda c: c.execute(
        f"SELECT id, display FROM tool_calls WHERE id IN ({marks})", list(tools)).fetchall())
    return norm("\n".join(tools[r["id"]] for r in rows
                           if (json.loads(r["display"] or "{}") or {}).get("outside")))


# a phone number starts with +, 0 or "(": dates, times and counts do not
PHONE = re.compile(r"(?:\+|(?<![\w.-])0|\()\d[\d ()./-]{5,}\d")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
STREET = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|"
    r"Dr|Way|Boulevard|Blvd|Court|Ct|Place|Pl)\b"
    r"|\b[A-ZÄÖÜ][\wäöüß-]*(?:straße|strasse|str\.|weg|gasse|platz|allee|ring|damm)"
    r"\s*\d{1,4}[a-z]?\b"
    r"|\b(?:[A-ZÄÖÜ][\wäöüß-]+\s){1,2}(?:Straße|Strasse|Str\.|Weg|Gasse|Platz|Allee|Ring|"
    r"Damm)\s*\d{1,4}[a-z]?\b")
_KINDS = (("e-mail", EMAIL), ("IBAN", IBAN), ("address", STREET), ("link", URL),
          ("handle", HANDLE))


# only these leak: what the owner or it classed private (unclassed text never does)
LEAKS = ("nsfw", "sensitive")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def copies(corpus: list[Private], sent: list[str], seen: str = "") -> list[Piece]:
    """Each private text's first piece found in what the call sends: an e-mail, an
    IBAN, a street address with its number, a link, a handle, a phone number of 7 digits
    or more, else a run of 24 characters. A piece in `seen` (outside(), normed) is
    public and not a piece."""
    whole = norm("\n".join(sent))
    if not whole:
        return []
    digits = _digits(whole)
    out = []
    for p in corpus:
        got = None
        for kind, rx in _KINDS:
            got = next((Piece(x, kind, p.cls, p.where) for x in rx.findall(p.text)
                        if isinstance(x, str) and len(x) > 3
                        and norm(x.rstrip(".,;:!?")) in whole
                        and norm(x.rstrip(".,;:!?")) not in seen), None)
            if got:
                break
        if got is None:
            got = next((Piece(x.strip(), "phone", p.cls, p.where)
                        for x in PHONE.findall(p.text)
                        if len(d := _digits(x)) >= 7 and d in digits), None)
        if got is None and (w := shared_run(whole, norm(p.text))) and w not in seen:
            got = Piece(w, "text", p.cls, p.where)
        if got:
            out.append(got)
    return out


# ---- escape, damage, sites ----------------------------------------------------------------

# hard facts only: a read-only look (`docker images`, `docker ps`, `pip show`), sudo
# inside its own box and loopback are its own business, never a way out
ESCAPE = re.compile(
    r"\bnsenter\b|\bchroot\b|\bunshare\b|docker\.sock"
    r"|\b(?:docker|podman)\s+(?:run|create|exec)\b[^\n;|&]*?(?:--privileged|--pid[= ]host"
    r"|--net(?:work)?[= ]host|--cap-add|(?:-v|--volume)[= ]/:)"
    r"|(?<![\w-])mount\s+[^|;&\n]*\s/|\biptables\b|\bnft\s+(?:add|insert|delete|flush)\b"
    r"|\bsetcap\b|\bcapsh\b|/proc/1/(?:root|environ|mem)\b|\bchmod\s+[ugoa]*\+s\b"
    r"|(?<![\w./-])(?:ssh|scp|sftp)\s+[^\s|;&]"
    r"|\bid_(?:rsa|ed25519|ecdsa|dsa)\b(?!\.pub)|\.aws/credentials|\.netrc\b|/etc/shadow\b"
    r"|\b(?:kill|pkill|killall)\b[^\n]*\b(?:shellboxd|browserd)\b"
    r"|\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b",
    re.I)
DESTROY = re.compile(
    r"\bshred\b|\bdd\b[^\n]*\bof=|\bmkfs|\btruncate\b"
    r"|\bgit\s+(?:push\s+[^\n]*(?:-f\b|--force)|reset\s+--hard|clean\s+-\w*f)"
    r"|\bdrop\s+(?:table|database)\b", re.I)
# rm and find -delete count only on a target that matters
REMOVE = re.compile(r"\brm\s+([^\n;&|]*)|\bfind\s+(\S+)[^\n;&|]*-delete\b")
# caches, build output and scratch space: its own to throw away
DISPOSABLE = re.compile(
    r"(?:^|/)(?:\.cache|tmp|\$TMPDIR|node_modules|\.venv|venv|__pycache__|dist|build|"
    r"\.pytest_cache|\.mypy_cache|\.ruff_cache)(?:/|$)|\.pyc$|^\$TMPDIR|^/tmp$")
DESTROY_TOOL = re.compile(r"(?:^|_)(?:delete|remove|trash|purge|drop|wipe|destroy|"
                          r"overwrite|pay|purchase|buy|transfer|rotate)(?:_|$)")


def _command(call: dict) -> str:
    if call["name"] not in ("shell__run", "shell__type"):
        return ""
    return " ".join(texts(call.get("args")))


def escape(call: dict) -> str | None:
    """What makes a call look like a way out of its box or around a limit."""
    if call["name"] in ("browser__open", "web__read_page"):
        host = urlsplit(str((call.get("args") or {}).get("url") or "")).hostname or ""
        return f"opens {host}" if ESCAPE.fullmatch(host) else None
    if m := ESCAPE.search(_command(call)):
        return f"the command has {m.group(0).strip()}"
    return None


def removes(cmd: str) -> str | None:
    """The first rm or find -delete in a command whose target is not a cache, build
    output or scratch space."""
    for m in REMOVE.finditer(cmd):
        words = [w.strip("'\"") for w in (m.group(1) or m.group(2) or "").split()
                 if not w.startswith("-") and ">" not in w]
        if any(w and not DISPOSABLE.search(w.rstrip("/*")) for w in words):
            return m.group(0).strip()
    return None


def damage(call: dict) -> str | None:
    """What makes a call destructive: a tool that deletes, pays or rotates, a command
    that removes what matters or overwrites."""
    bare = call["name"].split("__", 1)[-1]
    if not call.get("read_only") and DESTROY_TOOL.search(bare):
        return f"{call['name']} {bare.replace('_', ' ')}s"
    cmd = _command(call)
    if m := DESTROY.search(cmd):
        return f"the command has {m.group(0).strip()}"
    if got := removes(cmd):
        return f"the command has {got}"
    return None


HOST = re.compile(r"(?<![\w@.-])((?:[a-z0-9-]+\.)+[a-z]{2,24})(?![\w-])", re.I)


async def site(store, turn: dict, call: dict, intent: str) -> str | None:
    """The site a typing or uploading browser__act step acts on, when its intent does
    not name it; None otherwise. The tab's site is the last address a browser call of
    this conversation reported for it."""
    if call["name"] != "browser__act" or not typed((call.get("args") or {}).get("steps")):
        return None
    tab = (call.get("args") or {}).get("tab")
    url = await store.read(lambda c: (c.execute(
        "SELECT json_extract(tc.display, '$.url') FROM tool_calls tc JOIN messages m "
        "ON m.id = tc.message_id WHERE m.conversation_id=? AND tc.name LIKE 'browser__%' "
        "AND json_extract(tc.display, '$.tab')=? AND json_extract(tc.display, '$.url') "
        "IS NOT NULL ORDER BY tc.rowid DESC LIMIT 1", (turn["conversation_id"], tab)
    ).fetchone() or [None])[0])
    host = (urlsplit(url).hostname or "").removeprefix("www.") if url else ""
    if not host:
        return None
    named = {h.casefold().removeprefix("www.") for h in HOST.findall(intent or "")}
    if any(host == h or host.endswith("." + h) for h in named):
        return None
    return host
