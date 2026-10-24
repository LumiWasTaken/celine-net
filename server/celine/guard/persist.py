"""persist: a call that tries again, another way, what a rule stopped in this
conversation (flow.py asks the persist rule about it). Plain code over the stopped row
and the new call: the same private piece to the same kind of place, the same host,
address or path for a call that again sends out, destroys or breaks out, or any of
those in what it asks a sub-agent to do.
"""
from __future__ import annotations

import re

from .facts import DESTROY, HOST, REMOVE, Private, copies, damage, escape, found_text
from .trigger import FILE_ENDS, texts

IP = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
_SPLIT = re.compile(r"[\s'\"`;|&()<>=,]+")


def targets(xs: list[str]) -> set[str]:
    """What the texts `xs` point at: hosts and addresses, and the last part of each path
    (a path a destroying command names, or any with a slash)."""
    out: set[str] = set()
    for s in xs:
        for h in HOST.findall(s):
            if h.rsplit(".", 1)[-1].lower() not in FILE_ENDS:
                out.add(h.lower().removeprefix("www."))
        out.update(IP.findall(s))
        words = [w for w in _SPLIT.split(s) if "/" in w and "://" not in w]
        if m := (DESTROY.search(s) or REMOVE.search(s)):
            words += [w for w in s[m.start():].split("\n")[0].split()[1:]
                      if not w.startswith("-") and ("." in w or "~" in w or "/" in w)]
        for w in words:
            base = w.rstrip("/").rsplit("/", 1)[-1].lower()
            if len(base) >= 3:
                out.add(base)
    return out


def asking(call: dict, sink: str) -> bool:
    """A call that hands work to someone else of its own: a sub-agent's brief, a message to
    a sub-agent or a mission."""
    return call["name"] == "sub_agents__start" or (call["name"] == "intercom__send"
                                                   and sink != "house")


def can_again(call: dict, sink: str) -> bool:
    """A call that could repeat a stop at all: it sends out, destroys, breaks out or
    asks someone else of its own. Any other call never reads the stopped rows."""
    return sink != "house" or asking(call, sink) or bool(damage(call) or escape(call))


def again(prev: dict, call: dict, sink: str, sent: list[str]) -> str | None:
    """What `call` shares with a call a rule stopped (`prev`: the row's rule, sink,
    piece, quote and the stopped call's args), when it tries the same thing another way:
    the same private piece to the same kind of place, the same host, address or path
    for a call that again sends out, destroys or breaks out, or any of those in what it
    asks a sub-agent to do. None: nothing in common."""
    ask = asking(call, sink)
    text = texts(call.get("args"))
    rule = prev["rule"]
    if rule == "leak":
        if not prev.get("piece") or not (ask or sink == prev.get("sink")):
            return None
        got = copies([Private(prev["piece"], "sensitive", "")], text if ask else sent)
        return got[0].text if got else None
    acts = {"damage": lambda: damage(call), "escape": lambda: escape(call),
            "sites": lambda: sink != "house", "hijack": lambda: sink != "house" or
            damage(call) or escape(call)}
    if rule not in acts or not (ask or acts[rule]()):
        return None
    if rule == "escape" and prev.get("piece") and escape(call) == prev["piece"]:
        return found_text(prev["piece"])
    before = targets(texts(prev.get("args")) + [prev.get("piece") or "",
                                                  prev.get("quote") or ""])
    shared = sorted(before & targets(text))
    return shared[0] if shared else None
