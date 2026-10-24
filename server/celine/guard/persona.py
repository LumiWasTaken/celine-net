"""Bastet's voice, the owner's to edit on Settings, Guard: who she is (`persona`, shipped as
config/guard/bastet.md), how it replies to Celine on the Intercom (`talk`, talk.md) and
how it writes its notes to the owner (`note`, note.md). The judges' prompts stay files:
they are calibrated questions, not a voice.

The shipped file is the default. An edited text is kv `bastet:<field>`, kept as written
({{name}}, {{user}} not filled in), and every save goes into its History (texts.py, the
same name); Use the default deletes the kv key, so the shipped file reads again. Read at
every use: an edit applies at its next note or reply.
"""
from __future__ import annotations

import sqlite3

from .. import texts
from ..settings.files import Invalid
from . import prompts, store as decisions

FILES = {"persona": "bastet", "talk": "talk", "note": "note"}
MAX = 200_000                 # bytes of one text, as its persona


def key(field: str) -> str:
    """The kv key and the History name of one text."""
    return f"bastet:{field}"


def shipped(field: str) -> str:
    return prompts.text(FILES[field])


def text(c: sqlite3.Connection, field: str) -> str:
    """The text in use: the owner's, else the shipped one."""
    own = decisions.kv(c, key(field))
    return shipped(field) if own is None else own


def view(c: sqlite3.Connection) -> dict:
    """{field: {text, default (the shipped text), own (the owner's text is in use)}}."""
    return {f: {"text": text(c, f), "default": shipped(f),
                "own": decisions.kv(c, key(f)) is not None} for f in FILES}


def write(c: sqlite3.Connection, field: str, value: str | None, by: str = "user") -> None:
    """Inside a write: `value` is the text now, None (or the shipped text itself) is the
    default again. Into its History as written by `by`. Raises Invalid."""
    if field not in FILES:
        raise Invalid("field", "This must be one of " + ", ".join(FILES) + ".")
    if value is not None and not isinstance(value, str):
        raise Invalid("text", "This must be text or null.")
    if value is not None and len(value.encode()) > MAX:
        raise Invalid("text", f"This is at most {MAX:,} bytes.")
    was = text(c, field)
    if value is None or value.strip() == shipped(field).strip():
        decisions.kv_del(c, key(field))
        value = shipped(field)
    else:
        decisions.kv_set(c, key(field), value)
    texts.keep(c, key(field), was, value, by)


async def frame(store, cfg, field: str) -> str:
    """The system text of its replies (`talk`) or its notes (`note`): that frame with
    its persona in its {persona} slot, names filled."""
    def read(c):
        return text(c, field), text(c, "persona")
    body, who = await store.read(read)
    return cfg.substitute(body).replace("{persona}", cfg.substitute(who).strip())
