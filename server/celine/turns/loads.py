"""What it carries besides the conversation: the notes it always carries, in its system
prompt, and the skills offered here, decided at every turn start by one policy for every
conversation (`var/settings/context.toml`, copied once from `config/context.toml`, holds
the memory's numbers, skills/recall.py, and `result_tokens`, the most one tool result
takes, tokens.result_cap).

Notes, from its notebook: a note whose head has `load: always` and the notes on the user,
`people/{{username}}.md` and `people/{{username}}-*.md`; one over ALWAYS_MAX bytes
carries only its head lines and its `!!` lines (head_of) and names notebook__read for
the rest, its "Love this" samples (conversations/samples.py PATH) aside. Never: a note
whose `load:` line says off or names only other words, the notes the user left out here
(`conversations.notes_off`), and anything outside a sub-agent's folder. Any other note
it reads with notebook__read: the result is the note, and it ages like any other. A
note whose `keys:` line comes up in the talk brings its matching sections, graded,
under `[your memory]` (skills/recall.py).

Skills, only where skills/places.PLACES offers them: every skill with a tool on. The
tools array carries the core skills' tools (skills.CORE) with their parameters and every
other tool by name and one line (ToolSpec.as_name); it changes only with the switches,
never with a load. skills__load answers with the parameters and the skill's part of the
prompt as its result (skills/meta.py), and that result ages like any other.
"""
from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..core.states import LIVE_SQL
from ..conversations.samples import PATH as SAMPLES   # carried whole, whatever its size
from ..skills.notebook import carried_text, inside, load_of, read_note

DEFAULTS = {"memory_floor": 0.5, "memory_sections": 3, "memory_tokens": 1500,
            "result_tokens": 10_000}
WHOLE = ("memory_sections", "memory_tokens", "result_tokens")
KEY_REPLY = 300          # the characters of its last reply a key is looked for in
ALWAYS_MAX = 1536        # bytes of an always-loaded note carried whole; past it its head
OFF_HERE = "left out in this conversation"


def shares(cfg) -> dict:
    """The context numbers: context.toml over DEFAULTS."""
    return {**DEFAULTS, **(getattr(cfg, "context", None) or {})}


def read_shares(path: Path, problems: list) -> dict:
    """context.toml, checked: a bad value is one Problem and keeps its default."""
    from ..config import ConfigError, Problem, shown_path
    try:
        raw = tomllib.loads(path.read_text()) if path.is_file() else {}
    except tomllib.TOMLDecodeError as e:          # celine exits 2 naming the file
        raise ConfigError(f"{shown_path(path)}: {e}") from None
    except OSError as e:
        problems.append(Problem(shown_path(path), f"not read, the defaults count: {e}"))
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    for key, value in raw.items():
        if (why := share_problem(key, value)) is None:
            out[key] = int(value) if key in WHOLE else float(value)
        else:
            problems.append(Problem(shown_path(path), why))
    return out


def share_problem(key: str, value) -> str | None:
    """Why `value` cannot be the number `key`, or None when it can."""
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if key not in DEFAULTS:
        return f"unknown key {key}, ignored."
    if key == "memory_floor" and not (number and 0 <= value <= 1):
        return "memory_floor is a similarity from 0 to 1, such as 0.5."
    if key == "memory_sections" and not (number and value == int(value)
                                         and 0 <= value <= 20):
        return "memory_sections is a whole number from 0 to 20, such as 3."
    if key == "memory_tokens" and not (number and value == int(value)
                                       and 0 <= value <= 20_000):
        return "memory_tokens is a whole number of tokens from 0 to 20000, such as 1500."
    if key == "result_tokens" and not (number and value == int(value)
                                       and 1000 <= value <= 200_000):
        return "result_tokens is a whole number of tokens from 1000 to 200000, such as 10000."
    return None


@dataclass
class Carried:
    """One selection. `notes`: {path, text, tokens, state, why} each, state `included`
    (always carried) or `off` (the user's switch here); `skills`: the skills offered
    here with a tool on, in registry order; `others`: the skills offered here with every
    tool off, with why (`off`, `off here`)."""
    notes: list[dict] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    others: dict[str, str] = field(default_factory=dict)

    @property
    def loaded_notes(self) -> frozenset[str]:
        return frozenset(n["path"] for n in self.notes if n["state"] == "included")


# ---- keys ----------------------------------------------------------------------------

def key_in(text: str, keys: list[str]) -> str | None:
    """The first of `keys` that appears in `text` as a whole word or phrase, any case
    and any spaces between its words ("art" is not in "start")."""
    for k in keys:
        rx = r"(?<!\w)" + r"\s+".join(map(re.escape, k.split())) + r"(?!\w)"
        if k.split() and re.search(rx, text, re.I):
            return k
    return None


# ---- notes ---------------------------------------------------------------------------

_MARKED = re.compile(r"\s*(?:[-*]\s+)?!!")


def head_of(text: str, rel: str) -> str:
    """What it carries of an always-loaded note over ALWAYS_MAX bytes: its head lines
    (up to the first blank line) and the lines it marked with `!!`, or without any
    mark its first lines, at most ALWAYS_MAX bytes either way, then one line naming
    where the rest is."""
    lines = text.splitlines()
    end = next((i for i, line in enumerate(lines) if not line.strip()), len(lines))
    marked = [line for line in lines[end:] if _MARKED.match(line)]
    keep, used = [], 0
    for line in lines[:end] + marked if marked else lines:
        used += len(line.encode()) + 1
        if used > ALWAYS_MAX:
            break
        keep.append(line)
    return "\n".join(keep).rstrip() + f'\n\nrest via notebook__read(path="{rel}")'


async def _notes(store, cfg, conv, counter) -> list[dict]:
    """The always notes of its notebook (Carried.notes), and the ones the user left out
    here, so one can come back on."""
    from ..skills import notebook_index
    root = cfg.her.notebook
    folder, off = conv["notebook_folder"], set(json.loads(conv["notes_off"] or "[]"))
    user = (getattr(cfg, "user", "") or "").lower()
    notes: list[dict] = []
    for r in await notebook_index.list(store, folder):
        rel = r["path"]
        if not inside(rel, folder):
            continue
        # note: every note's head is read at each turn start; a column in the index
        # when notebooks grow into the thousands
        head = read_note(root, rel, head=True)
        if head is None or load_of(rel, head, user) != "always":
            continue
        text = cfg.substitute(carried_text(read_note(root, rel) or ""))   # a seeded note says {{username}}
        if rel != SAMPLES and len(text.encode()) > ALWAYS_MAX:
            text = head_of(text, rel)
        notes.append({"path": rel, "text": text, "tokens": counter.estimate(text),
                      "state": "off" if rel in off else "included",
                      "why": OFF_HERE if rel in off else ""})
    seen = {n["path"] for n in notes}
    for rel in sorted(off - seen):       # the user's switches, so one can come back on
        if inside(rel, folder) and (text := read_note(root, rel)) is not None:
            notes.append({"path": rel, "text": text, "tokens": counter.estimate(text),
                          "state": "off", "why": OFF_HERE})
    return notes


# ---- skills --------------------------------------------------------------------------

def place(c: sqlite3.Connection, cid: str, turn_id: str | None = None) -> str:
    """Where a turn of `cid` runs (skills/places.place_of): turn `turn_id`'s place, else
    the newest live turn's there."""
    from ..skills.places import place_of
    if turn_id is None:
        r = c.execute("SELECT id FROM turns WHERE conversation_id=? AND state IN "
                      f"{LIVE_SQL} ORDER BY rowid DESC LIMIT 1", (cid,)).fetchone()
        turn_id = r[0] if r else None
    return place_of(c, cid, turn_id)


async def offered(registry, store, cid: str, turn_id: str | None = None
                  ) -> dict[str, list]:
    """The tools each skill offers in this conversation: only in the places
    skills/places.PLACES puts it (an MCP connector: chat, mission, background), and only
    what its offered_in allows (no sub_agents at sub_agents.skill.MAX_DEPTH, the intercom's
    start_chat away from the user), and to a sub-agent with read access only its
    read-only tools (skills/gating.read_mode_has), to one started from an agent template
    with `tools` only those skills (sub_agents/templates.py); skills offering none left out. The
    skill index, a load, a guide and the request's tools all read it, so what it is
    offered by name is what it may call."""
    from ..skills.gating import read_mode_has
    from ..skills.places import offered as offered_at
    from ..sub_agents.skill import reads_only
    at = "chat" if store is None else await store.read(lambda c: place(c, cid, turn_id))
    out: dict[str, list] = {}
    for spec in registry.tools.values():
        if offered_at(spec.skill, at):
            out.setdefault(spec.skill, []).append(spec)
    for name, skill in registry.skills.items():
        if name not in out or not hasattr(skill, "offered_in"):
            continue
        o = await skill.offered_in(store, cid)
        if isinstance(o, (set, frozenset)):
            out[name] = [s for s in out[name] if s.name in o]
        elif not o:
            out[name] = []
    if store is not None and await reads_only(store, cid):
        out = {n: [s for s in specs if read_mode_has(s)] for n, specs in out.items()}
    if store is not None and registry.cfg is not None:
        from ..sub_agents import templates
        mine = templates.skills_of(await store.read(
            lambda c: templates.of(c, cid, registry.cfg)))
        if mine is not None:
            out = {n: specs for n, specs in out.items() if n in mine}
    return {n: specs for n, specs in out.items() if specs}


async def _skills(store, cfg, registry, cid: str) -> tuple[list[str], dict[str, str]]:
    """(the skills offered here with a tool on, in registry order; the ones with every
    tool off, with why)."""
    from ..skills.gating import is_enabled, overrides_for, reason
    if not registry:
        return [], {}
    overrides = await overrides_for(store, cid)
    here = await offered(registry, store, cid)
    others: dict[str, str] = {}
    for name, specs in here.items():
        if not any(is_enabled(s, overrides, cfg) for s in specs):
            here_off = any(reason(s, overrides, cfg) == "here" for s in specs)
            others[name] = "off here" if here_off else "off"
    return [n for n in registry.skills if n in here and n not in others], others


# ---- one selection -------------------------------------------------------------------

async def select(store, cfg, registry, cid: str, backend) -> Carried:
    """What it carries now."""
    from . import tokens
    conv = await store.read(lambda c: c.execute(
        "SELECT notebook_folder, notes_off FROM conversations WHERE id=?",
        (cid,)).fetchone())
    if conv is None:
        return Carried()
    skills, others = await _skills(store, cfg, registry, cid)
    return Carried(await _notes(store, cfg, dict(conv), tokens.counter(backend)),
                   skills, others)
