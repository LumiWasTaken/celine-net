"""Slash commands: a small registry the composer reads and runs.

A command is typed in the composer ("/compact the trip dates"), run here, and never
sent to the model as text. The registry is the one place a new command is added:
decorate an async handler with `@command(...)` (commands/handlers.py); `GET
/api/slash-commands` lists it and `POST
/api/conversations/{cid}/slash-commands/{name}/run` runs it (api/commands.py).

The set is the user's decision: /compact [focus], /think <level> (also /effort), /model
<name>, /usage. Each command works in its kinds of conversation (`kinds`). Nothing else
unless the user asks for it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..core.states import LIVE_SQL
from ..errors import Busy, CelineError, NotFound

log = logging.getLogger("celine.commands")


@dataclass(frozen=True)
class Arg:
    name: str
    description: str
    required: bool = False
    choices: tuple[str, ...] = ()      # empty: free text (the rest of the line)

    def view(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "required": self.required, "choices": list(self.choices),
                "kind": "choice" if self.choices else "text"}


@dataclass(frozen=True)
class Command:
    name: str
    description: str                 # one line, for the composer's menu
    run: Callable[..., Awaitable[dict]]
    args: tuple[Arg, ...] = ()
    aliases: tuple[str, ...] = ()
    when: str = "any"                # "idle": refused while it is answering
    kinds: tuple[str, ...] = ("chat", "mission", "agent", "background")

    @property
    def usage(self) -> str:
        parts = [f"/{self.name}"]
        for a in self.args:
            v = "|".join(a.choices) if a.choices else a.name
            parts.append(f"<{v}>" if a.required else f"[{v}]")
        return " ".join(parts)

    def view(self) -> dict[str, Any]:
        return {"name": self.name, "aliases": list(self.aliases), "usage": self.usage,
                "description": self.description, "when": self.when,
                "kinds": list(self.kinds), "args": [a.view() for a in self.args]}


COMMANDS: dict[str, Command] = {}
_NAMES: dict[str, str] = {}          # name or alias -> name


def command(name: str, description: str, *, args: tuple[Arg, ...] = (),
            aliases: tuple[str, ...] = (), when: str = "any",
            kinds: tuple[str, ...] | None = None):
    def deco(fn):
        cmd = Command(name, description, fn, args, aliases, when,
                      *([kinds] if kinds else []))
        for n in (name, *aliases):
            if n in _NAMES:
                raise ValueError(f"command /{n} is registered twice")
            _NAMES[n] = name
        COMMANDS[name] = cmd
        return fn
    return deco


def lookup(name: str) -> Command | None:
    n = _NAMES.get(name.lower().lstrip("/"))
    return COMMANDS.get(n) if n else None


def parse(text: str) -> tuple[str, str] | None:
    """("name", "rest of the line") when `text` starts with a registered command,
    else None. "//x" is not a command (the composer's escape for a literal slash)."""
    t = (text or "").lstrip()
    if not t.startswith("/") or t.startswith("//"):
        return None
    head, _, rest = t[1:].partition(" ")
    head = head.strip()
    cmd = lookup(head) if head else None
    return (cmd.name, rest.strip()) if cmd else None


def literal(text: str) -> str:
    """"//compact" is the composer's way to send the text "/compact" to Celine: one
    slash comes off, only in front of a command name."""
    t = text or ""
    stripped = t.lstrip()
    if stripped.startswith("//") and lookup(stripped[2:].split(" ", 1)[0]) is not None:
        i = len(t) - len(stripped)
        return t[:i] + t[i + 1:]
    return t


async def kind_of(store, cid: str) -> str | None:
    row = await store.read(lambda c: c.execute(
        "SELECT kind FROM conversations WHERE id=?", (cid,)).fetchone())
    return row["kind"] if row else None


async def listing(services, conversation: str | None) -> list[dict[str, Any]]:
    """Every command; with a conversation, only those of its kind, and arguments carry
    `suggestions` for it (the thinking words of its current model)."""
    from .handlers import think_levels
    kind = await kind_of(services.store, conversation) if conversation else None
    out = [c.view() for c in COMMANDS.values() if kind is None or kind in c.kinds]
    levels = think_levels(services, conversation)
    sub = services.cfg.substitute
    for c in out:
        c["description"] = sub(c["description"])
        for a in c["args"]:
            a["description"] = sub(a["description"])
            a["suggestions"] = levels if (c["name"], a["name"]) == ("think", "level") \
                else list(a["choices"])
    return out


async def run(services, cid: str, name: str, text: str, wait: bool = False) -> dict:
    """Command `name` with the rest of the line `text`, in conversation `cid` (the
    caller checked that it is writable)."""
    cmd = lookup(name)
    if cmd is None:
        raise CelineError("unknown_command", f"There is no command /{name}.", 404,
                          {"commands": sorted(COMMANDS)})
    store = services.store
    kind = await kind_of(store, cid)
    if kind is None:
        raise NotFound("conversation")
    if kind not in cmd.kinds:
        raise CelineError("bad_request", f"/{cmd.name} works in a chat.", 400)
    if cmd.when == "idle" and await store.read(lambda c: c.execute(
            f"SELECT 1 FROM turns WHERE conversation_id=? AND state IN {LIVE_SQL}",
            (cid,)).fetchone()):
        # the error handler fills in the name (errors.py)
        raise Busy(f"{{{{name}}}} is still answering. Run /{cmd.name} once the answer is "
                   f"done.")
    text = (text or "").strip()
    for a in cmd.args:
        if a.required and not text:
            raise CelineError("bad_args", f"Usage: {cmd.usage}", 400,
                              {"usage": cmd.usage})
    out = await cmd.run(services, cid, text, wait=wait)
    log.info("command", extra={"event": "command.run", "command": cmd.name,
                               "conversation": cid})
    return {"command": cmd.name, "ok": True, **out}


from . import handlers  # noqa: E402,F401  (registers the commands)
