"""Where text comes from: the one list of outside sources, and the label a result gets
when someone else wrote it (layer 0 of the guard).

The approvals read OUTSIDE too (celine/approvals.py): there is no second list.

A label is {"source": "reddit.com/r/x", "by": "u/name" or None}, kept in the result's
display under "outside". It is written when the result is received (turns/tools.py
_received, a job's too) while the guard is on (Watch or On), an error's like any other
(its body can carry a page's text); with the guard off no result is labelled.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import patterns

# never looked at: image and music generation (their prompts are never checked, image
# moderation is left to the ComfyUI server) and the vault (it has its own tap)
NEVER = ("image_generator__", "music_generator__", "vault__")
# skills whose results hold text someone else wrote. Shell output counts only when
# plain code finds a reason (network, a file from outside, text aimed at an AI);
# every other MCP server counts too (its display names the server), but one whose
# mcp.toml block says `outside = "shell"` (her device's shell and files) counts as shell
OUTSIDE = {"browser", "web", "email", "discord", "whatsapp", "chat_archive",
           "user_documents", "shell"}

# a command that reaches the network, or a file it got from there: curl, wget, git
# clone/pull/fetch, installs, ssh, scp, nc, http clients; anything piped from them
# matches too, since the word is in the line
NETWORK = re.compile(
    r"(?<![\w.-])(?:curl|wget|ssh|scp|sftp|rsync|nc|ncat|netcat|telnet|ftp|http|https|"
    r"xh|aria2c|lynx|w3m|yt-dlp)(?![\w.-])"
    r"|\bgit\s+(?:clone|pull|fetch)\b"
    r"|\b(?:pip3?|pipx|uv\s+pip|npm|pnpm|yarn|cargo|gem|go|brew)\s+"
    r"(?:install|add|get|i|upgrade|reinstall)\b"
    r"|\bpython3?\s+-m\s+(?:pip|urllib|requests|httpx)\b",
    re.I)


def network(command: str) -> bool:
    return bool(NETWORK.search(command or ""))


def is_outside(name: str) -> bool:
    """The skill-level list: a tool of a skill whose results others wrote."""
    return name.split("__")[0] in OUTSIDE


def label(name: str, args: dict, result, mcp=None) -> dict | None:
    """The outside label of `name`'s result, None when the text is its own. `mcp`: its
    server's mcp.toml block (facts.mcp_of); one with `outside = "shell"` (her own
    device's shell and files) is read as the box shell is."""
    d = result.display or {}
    if d.get("gated"):
        return None                          # not run: nobody's text
    given = d.get("outside")
    if isinstance(given, dict):
        return given                         # labelled already (a job, an old call read again)
    if name.startswith(NEVER):
        return None
    skill = name.split("__")[0]
    if skill == "shell" or getattr(mcp, "outside", None) == "shell":
        # the command line: shell__run's, a Desktop Commander start_process's `command`,
        # a file server's path (a link reads as one)
        typed = " ".join(str(v) for v in args.values() if isinstance(v, str))
        if not (given or network(typed) or patterns.aimed(result.content or "")):
            return None
        return {"source": skill, "by": None}
    if skill in OUTSIDE or given or (d.get("server") and d.get("server") == skill):
        return {"source": _source(skill, args, d), "by": _by(d)}
    return None


def line_label(iv: dict | None) -> dict | None:
    """The outside label of an intercom line (its intercom view), None when it is the
    owner's or its own: the label it carries (a mail trigger's sender and subject, a job's
    outside result, the report of a sub-agent that read outside), else the whole line
    when a host script or another app sender wrote it (only the owner's
    messages are trusted). A trigger's own words are the app's."""
    if not iv:
        return None
    given = (iv.get("data") or {}).get("outside")
    if isinstance(given, dict):
        return given
    if iv.get("kind") != "trigger" and (iv.get("source") or {}).get("kind") in (
            "token", "connector"):
        return {"source": iv.get("from_name") or iv.get("from") or "outside", "by": None}
    return None


def shell_source(origin: str | None) -> str:
    """The source of a shell run shellboxd flagged: where its text came from (a link, a
    tagged file's link), else the shell."""
    if origin and "//" in origin:
        return _source("shell", {"url": origin}, {})
    if origin and origin.startswith("upload:"):
        return "uploaded " + origin.removeprefix("upload:")
    return "shell"


def _source(skill: str, args: dict, d: dict) -> str:
    url = d.get("url") or args.get("url")
    if isinstance(url, str) and url:
        u = urlsplit(url if "//" in url else "//" + url)
        host = (u.hostname or "").removeprefix("www.")
        path = "/".join(p for p in u.path.split("/") if p)[:60]
        return f"{host}/{path}" if path else host or skill
    if skill == "web":
        return "web search"
    if skill == "sub_agents":
        return f"sub-agent {d.get('name') or ''}".strip()
    if skill == "user_documents" and isinstance(args.get("path"), str):
        return f"document {args['path']}"
    return skill.replace("_", " ")


def _by(d: dict) -> str | None:
    by = d.get("from") or d.get("author")
    return str(by) if by else None


def replace_parts(text: str, label: dict, fn) -> str | None:
    """`text` with each outside part `label` names (a mail's sender and subject) put
    through `fn` where it stands, the longest first; None when it names none."""
    parts = sorted({str(p) for p in label.get("parts") or () if p}, key=len, reverse=True)
    if not parts:
        return None
    return re.sub("|".join(map(re.escape, parts)), lambda m: fn(m.group(0)), text)


def clean(value: str) -> str:
    """A label value inside the wrapper's quotes: one line, no quote mark."""
    return " ".join(str(value).replace('"', "'").split())[:120]


def agent_read_outside(c, cid: str) -> bool:
    """Did sub-agent `cid` read outside text? An agent CLI always counts: its tools ran
    in the box unchecked."""
    r = c.execute("SELECT agent_cli FROM conversations WHERE id=?", (cid,)).fetchone()
    if r is None:
        return False
    if r[0]:
        return True
    return c.execute(
        "SELECT 1 FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
        "WHERE m.conversation_id=? AND json_extract(tc.display, '$.outside') IS NOT NULL "
        "LIMIT 1", (cid,)).fetchone() is not None
