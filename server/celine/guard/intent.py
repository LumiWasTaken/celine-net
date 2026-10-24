"""What the judges read of it: its intent before the outside text (`build`: the hijack
judge, the questions judge, the patrol, the second look), and a finished turn as the
patrol and its second look see it (`finished`)."""
from __future__ import annotations

import json
import sqlite3

from ..conversations import tree
from . import caps, sources
from .store import args_of

NOTES = ("notebook__", "mind__")     # its notes: never read by a judge after the fact


def agent_name(c, turn: dict, text: str) -> str:
    """`text` with {{agent_name}} filled: the turn's sub-agent by its own name, else the
    agent's."""
    row = c.execute("SELECT agent_name FROM conversations WHERE id=?",
                    (turn["conversation_id"],)).fetchone()
    her = c.execute("SELECT name FROM her WHERE id = 1").fetchone()
    return text.replace("{{agent_name}}", (row and row[0]) or (her and her[0]) or "")


def build(c, turn: dict, call: dict) -> str:
    """The agent's intent (`c`: a database connection): the owner's last message in this turn,
    else the turn's trigger (its outside parts named, not quoted), then its words and
    calls after it up to the first result someone else wrote, known by its label
    (display "outside", guard/sources.py). Never its words after it, never its persona,
    never the step that makes `call`. KeyError: the conversation is gone."""
    root = (c.execute("SELECT root_message_id FROM turns WHERE id=?",
                      (turn["id"],)).fetchone() or [None])[0]
    chain = tree.path(c, turn["conversation_id"])
    at = next((i for i, m in enumerate(chain) if m["id"] == root), None)
    if at is None:
        return "(none)"
    chain = chain[at:]
    start = max((i for i, m in enumerate(chain) if m["role"] == "user" and
                 (m["intercom"] is None or m["intercom"]["from"] == "user")), default=0)
    out = [named(chain[start])]
    for m in chain[start + 1:]:
        if m["id"] == call.get("message_id"):
            break
        if m["role"] == "user" and sources.line_label(m["intercom"]) is not None:
            break                               # another sender's line, a job's outside result
        if m["role"] != "assistant":
            continue
        if (m["content"] or "").strip():
            out.append("{{agent_name}} said: " + m['content'].strip())
        for r in c.execute("SELECT name, arguments, display FROM tool_calls WHERE "
                           "message_id=? ORDER BY rowid", (m["id"],)):
            out.append(f"{{{{agent_name}}}} called {r['name']} {r['arguments'] or ''}".strip())
            if isinstance(json.loads(r["display"] or "{}").get("outside"), dict):
                return agent_name(c, turn, caps.intent(out))
    return agent_name(c, turn, caps.intent(out)) or "(none)"


async def read(store, turn: dict, call: dict) -> str:
    """build() in a read; "(none)" when the conversation is gone."""
    try:
        return await store.read(lambda c: build(c, turn, call))
    except KeyError:
        return "(none)"


def named(m: dict) -> str:
    """A user line with its outside parts as a name only: what they say is not its
    intent."""
    text = (m["content"] or "").strip()
    label = sources.line_label(m["intercom"])
    if label is None:
        return text
    name = f"[text from {label.get('source') or 'outside'}]"
    got = sources.replace_parts(text, label, lambda _: name)
    return name if got is None else got


def finished(c: sqlite3.Connection, turn: dict) -> tuple[dict, str, str, list] | None:
    """(what the judge sees of one finished turn, its last reply's id, the
    conversation's title, its calls {name, args, display}), None when it holds no
    reply of its own."""
    msgs = c.execute("SELECT id, content FROM messages WHERE turn_id=? AND "
                     "role='assistant' ORDER BY rowid", (turn["id"],)).fetchall()
    if not msgs:
        return None
    try:
        her = build(c, turn, {"message_id": msgs[0]["id"]})
    except KeyError:                         # the conversation is gone
        return None
    steps, calls = [], []
    for m in msgs:
        if (m["content"] or "").strip():
            steps.append(agent_name(c, turn, caps.head(
                "{{agent_name}} said: " + ' '.join(m['content'].split()), caps.SAID)))
        for r in c.execute("SELECT name, arguments, state, display FROM tool_calls "
                           "WHERE message_id=? ORDER BY rowid", (m["id"],)):
            if r["name"].startswith(NOTES):
                continue
            d = json.loads(r["display"] or "{}")
            calls.append({"name": r["name"], "args": args_of(r["arguments"]),
                          "display": d if isinstance(d, dict) else {}})
            line = agent_name(c, turn, caps.head(
                f"{{{{agent_name}}}} called {r['name']} {' '.join((r['arguments'] or '').split())}",
                caps.CALLED))
            if d.get("gated"):
                line += "; it did not run"
            elif isinstance(d.get("outside"), dict):
                line += f"; its result was written by others ({d['outside'].get('source')})"
            if isinstance(d.get("injection"), dict):
                line += "; it held words aimed at an AI"
            steps.append(f"{line} ({r['state']})")
    title = (c.execute("SELECT title FROM conversations WHERE id=?",
                       (turn["conversation_id"],)).fetchone() or [None])[0]
    return ({"intent": her, "steps": caps.lines(steps)}, msgs[-1]["id"],
            title or "a conversation", calls)
