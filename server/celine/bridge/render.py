"""The agent's messages for WhatsApp: its text as it wrote it, only the markdown WhatsApp would
show as raw symbols written in WhatsApp's own syntax, citations as numbered quotes, and
a long answer in parts."""
from __future__ import annotations

import re
import time
from typing import Callable


# a quote [[kb:id]], or a tool name it wrote as one, marked [[tool__name|invalid]]
_CITE = re.compile(r"\[\[(?:([a-z0-9][a-z0-9_-]*:\d+)|[a-z0-9_]+__[a-z0-9_]+(?=\|invalid))"
                   r"(\|invalid)?\]\]")
_NOTE = re.compile(r"\[\[([^\[\]|]+?)(?:\|([^\[\]]+))?\]\]")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEAD = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.M)
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_ITAL = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<!\s)\*(?![\w*])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_INLINE_CODE = re.compile(r"(`[^`\n]+`)")
_FENCE_INFO = re.compile(r"^[\w+#.-]+(?=\n)")
B = "\x00"            # WhatsApp's bold `*`, held apart while `*italic*` becomes `_italic_`


def _day(ms: int | None) -> str:
    return time.strftime("%-d %b %Y", time.localtime(ms / 1000)) if ms else "?"


def _link(m: re.Match) -> str:
    words, url = m.group(1).strip(), m.group(2)
    return url if words in (url, url.split("://", 1)[-1]) else f"{words} ({url})"


def _marks(t: str) -> str:
    """Markdown emphasis in WhatsApp's syntax, outside inline code: **bold** and
    __bold__ as *bold*, *italic* as _italic_, ~~struck~~ as ~struck~, a heading as a
    bold line."""
    t = _BOLD.sub(lambda m: B + m.group(2) + B, t)
    t = _ITAL.sub(lambda m: "_" + m.group(1) + "_", t)
    t = _STRIKE.sub(lambda m: "~" + m.group(1) + "~", t)
    t = _HEAD.sub(lambda m: B + m.group(1).replace(B, "") + B, t)
    return t.replace(B, "*")


def render(text: str, resolve: Callable[[list[str]], dict[str, dict | None]] | None = None,
           title: Callable[[str], str] = str.title) -> str:
    """The agent's text for WhatsApp, every word of it kept:
    - `[[kb:id]]` becomes `[n]`, with one footer line per quote:
      `[1] WhatsApp, Ana, 12 Mar 2024: "<first 100 characters>"`; an `|invalid` one `[?]`;
    - `[[Note]]` links become the note's name; `[words](url)` becomes `words (url)`;
    - emphasis and headings in WhatsApp's syntax (_marks); table rows become
      `cell, cell` and their rule line goes; a code fence loses its language name.
    Lists, quotes, inline code and code fences are WhatsApp's own and stay."""
    text = text or ""
    fences = text.split("```")
    refs: list[str] = []
    order: dict[str, int] = {}
    invalid = False

    def cite(m: re.Match) -> str:
        nonlocal invalid
        if m.group(2):
            invalid = True
            return "[?]"
        ref = m.group(1)
        if ref not in order:
            order[ref] = len(order) + 1
            refs.append(ref)
        return f"[{order[ref]}]"

    for i in range(len(fences)):
        if i % 2:                                   # inside a code fence
            if i < len(fences) - 1:
                fences[i] = _FENCE_INFO.sub("", fences[i])
            continue
        t = fences[i]
        t = _CITE.sub(cite, t)
        t = _NOTE.sub(lambda m: m.group(2) or m.group(1).rsplit("/", 1)[-1], t)
        t = _LINK.sub(_link, t)
        t = "".join(p if j % 2 else _marks(p) for j, p in enumerate(_INLINE_CODE.split(t)))
        lines = []
        for line in t.split("\n"):
            s = line.strip()
            if _TABLE_RULE.match(line) and "-" in line and "|" in line:
                continue
            if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
                line = ", ".join(c.strip() for c in s.strip("|").split("|"))
            lines.append(line)
        fences[i] = "\n".join(lines)
    out = "```".join(fences).strip()
    foot = []
    if refs:
        found = resolve(refs) if resolve else {}
        for ref in refs:
            m = found.get(ref) or {}
            if not m:
                foot.append(f"[{order[ref]}] {ref}")
                continue
            kb = ref.split(":", 1)[0]
            who = "me" if m.get("from_me") else (m.get("sender") or {}).get("name") or "someone"
            quote = " ".join((m.get("text") or "").split())
            quote = quote[:100] + ("..." if len(quote) > 100 else "")
            foot.append(f"[{order[ref]}] {title(kb)}, {who}, {_day(m.get('ts'))}: \"{quote}\"")
    if invalid:
        foot.append("[?] a quote that is not in its source")
    if foot:
        out += "\n\n" + "\n".join(foot)
    return out


def split(text: str, size: int) -> list[str]:
    """Every part of at most `size` characters, in order, cut at paragraph breaks, then
    lines, then spaces."""
    parts: list[str] = []
    rest = text.strip()
    while rest:
        if len(rest) <= size:
            parts.append(rest)
            break
        cut = max(rest.rfind("\n\n", 0, size), rest.rfind("\n", 0, size))
        if cut < size // 3:
            cut = rest.rfind(" ", 0, size)
        if cut < size // 3:
            cut = size
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return parts
