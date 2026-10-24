"""Hints for a page it wrote: plain regexes over the text, no model. A hit can be
harmless, so a hint never refuses anything; it names the first line it matched."""
from __future__ import annotations

import re

from .page import LOADS_FROM

# where a page loads something from: src=, a <link> href, url(), @import, an import
_LOAD = re.compile(r"""\bsrc\s*=\s*["']?([^"'\s>]+)|<link\b[^>]*?\bhref\s*=\s*["']?([^"'\s>]+)"""
                   r"""|url\(\s*["']?([^"')\s]+)|@import\s+["']([^"']+)"""
                   r"""|\bfrom\s+["']([^"']+)["']|\bimport\s*\(\s*["']([^"']+)""", re.I)
_INLINE = ("data:", "blob:", "#", "about:", "javascript:")

RULES = [
    (re.compile(r"\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\s*\(|\bEventSource\s*\(|"
                r"\bsendBeacon\s*\("),
     "{what} reaches no server: a page has no network. Put the data into the page."),
    (re.compile(r"\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b|\bdocument\.cookie\b"),
     "{what} throws here: a page has no browser storage, so its state resets on reload."),
    (re.compile(r"\balert\s*\(|\bconfirm\s*\(|\bprompt\s*\(|\bwindow\.open\s*\(|"
                r"target\s*=\s*[\"']?_blank"),
     "{what} does nothing here: no dialogs, popups or new tabs. Build them in the page."),
    (re.compile(r"\.download\s*=|<a\b[^>]*\bdownload\b"),
     "{what} does nothing here: a page cannot start a download."),
]


def hints(content: str) -> list[str]:
    """One sentence per kind of problem, with the line of its first hit."""
    out = []
    for m in _LOAD.finditer(content):
        url = next(g for g in m.groups() if g is not None)
        if url.startswith(_INLINE) or url.startswith(LOADS_FROM):
            continue
        if "://" in url or url.startswith("//"):
            out.append(f"line {_line(content, m)}: {url[:80]} will not load. A page loads "
                       f"scripts, styles, fonts and images only from "
                       f"{', '.join(LOADS_FROM)}; anything else goes inline or as a "
                       f"data: URI.")
        else:
            out.append(f"line {_line(content, m)}: {url[:80]} is not there: a page is one "
                       f"file. Put it inline or use a data: URI.")
        break
    for rx, sentence in RULES:
        if m := rx.search(content):
            what = m.group(0).strip()
            out.append(f"line {_line(content, m)}: " + sentence.format(what=what))
    return out


def _line(content: str, m: re.Match) -> int:
    return content.count("\n", 0, m.start()) + 1
