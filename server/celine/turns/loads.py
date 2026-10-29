"""What it carries by need: the notes loaded in full, decided at every turn start by one
policy for every conversation and measured in tokens as a share of the model's window
(`var/settings/context.toml`, copied once from `config/context.toml`), and the skills
offered here.

Notes, from its notebook. Always:
a note whose head has `load: always` and the notes on the user, `people/{{username}}.md`
and `people/{{username}}-*.md`; one over ALWAYS_MAX bytes carries only its head lines and
its `!!` lines (head_of) and names notebook__read for the rest, it "Love this"
samples (conversations/samples.py PATH) aside. For a while: a note it read, wrote or
edited (the notebook tools touch it) or the user loaded by hand. A note whose `keys:`
line comes up in the talk is not loaded here: its matching sections come back graded
under `[your memory]` (skills/recall.py).
Never: a note whose `load:` line says off or names only other words, the notes the user left out
here (`conversations.notes_off`), and anything outside a sub-agent's folder.

Skills, only where skills/places.PLACES offers them: every skill with a tool on. The
tools array carries the core skills' tools (skills.CORE) with their parameters and every
other tool by name and one line (ToolSpec.as_name); it changes only with the switches,
never with a load. skills__load answers with the parameters and the skill's part of the
prompt as its result (skills/meta.py), and that result ages like any other.

A note no turn touched for `idle_turns` rounds is unloaded. The always-loaded notes
always load, even past the notes share (the Context tab shows the block over its
share); the share is for the notes loaded by need, the most recently touched first,
measured as the notes block it reads (headings and lines included). What does not fit
is named.

The state is per conversation: the `context_loaded` rows, the notes it carries, each
with the round that last touched it, and `context_state.turns`, the round of its last
turn start. A round is a turn of the conversation.
"""
from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..core.states import LIVE_SQL
from ..core.ids import now
from ..conversations.samples import PATH as SAMPLES   # carried whole, whatever its size
from ..skills.notebook import carried_text, inside, load_line, load_of, read_note
from .message_context import unsent
from ..core.event_types import CONTEXT_CHANGED

DEFAULTS = {"notes": 0.10, "idle_turns": 6,
            "memory_floor": 0.5, "memory_sections": 3, "memory_tokens": 1500}
WHOLE = ("idle_turns", "memory_sections", "memory_tokens")
KEY_REPLY = 300          # the characters of its last reply a key is looked for in
ALWAYS_MAX = 1536        # bytes of an always-loaded note carried whole; past it its head
OFF_HERE = "left out in this conversation"
OVER = "over the notes share"


def shares(cfg) -> dict:
    """The context shares: context.toml over DEFAULTS."""
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
    """Why `value` cannot be the share `key`, or None when it can."""
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if key not in DEFAULTS:
        return f"unknown key {key}, ignored."
    if key == "idle_turns" and not (number and value == int(value) and 1 <= value <= 100):
        return "idle_turns is a whole number of turns from 1 to 100."
    if key == "memory_floor" and not (number and 0 <= value <= 1):
        return "memory_floor is a similarity from 0 to 1, such as 0.5."
    if key == "memory_sections" and not (number and value == int(value)
                                         and 0 <= value <= 20):
        return "memory_sections is a whole number from 0 to 20, such as 3."
    if key == "memory_tokens" and not (number and value == int(value)
                                       and 0 <= value <= 20_000):
        return "memory_tokens is a whole number of tokens from 0 to 20000, such as 1500."
    if key == "notes" and not (number and 0 <= value <= 0.5):
        return "notes is a share of the window from 0 to 0.5, such as 0.1."
    return None


@dataclass
class Carried:
    """One selection. `notes`: {path, text, tokens, state, why, since} each, state
    `included`, `left_out` (over the share) or `off` (its load line, or the user's
    switch here); `skills`: the skills offered here with a tool on, in registry order;
    `others`: the skills offered here with every tool off, with why (`off`, `off
    here`); `since`: turns since each loaded note was touched, by ("note", path);
    `budgets`: notes as tokens of the window."""
    turn: int = 0
    notes: list[dict] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    others: dict[str, str] = field(default_factory=dict)
    since: dict[tuple[str, str], int] = field(default_factory=dict)
    budgets: dict[str, int] = field(default_factory=dict)

    @property
    def loaded_notes(self) -> frozenset[str]:
        return frozenset(n["path"] for n in self.notes if n["state"] == "included")


# ---- rows ------------------------------------------------------------------------------

def rows(c: sqlite3.Connection, cid: str) -> tuple[int, dict]:
    """(the round of its last turn start, {(kind, name): the round that touched it})."""
    st = c.execute("SELECT turns FROM context_state WHERE conversation_id=?",
                   (cid,)).fetchone()
    got = {(r["kind"], r["name"]): int(r["touched"]) for r in c.execute(
        "SELECT kind, name, touched FROM context_loaded WHERE conversation_id=?", (cid,))}
    return (int(st["turns"]) if st else 0), got


def touch_sync(c: sqlite3.Connection, store, cid: str, kind: str, names) -> None:
    """Inside a write: `names` loaded, or kept loaded, from this round on."""
    names = [n for n in dict.fromkeys(names) if n]
    if not names or c.execute("SELECT 1 FROM conversations WHERE id=?",
                              (cid,)).fetchone() is None:
        return
    mine, before = rows(c, cid)
    turn = max(mine, round_of(c, cid))
    c.executemany(
        "INSERT INTO context_loaded(conversation_id, kind, name, touched) "
        "VALUES(?,?,?,?) ON CONFLICT(conversation_id, kind, name) DO UPDATE SET "
        "touched=excluded.touched", [(cid, kind, n, turn) for n in names])
    _changed(c, store, cid, set(before), set(before) | {(kind, n) for n in names})


async def touch(store, cid: str, kind: str, names) -> None:
    """Notes or skills it used now: loaded, or kept loaded, from this round on."""
    await store.write(lambda c: touch_sync(c, store, cid, kind, names))


async def unload(store, cid: str, kind: str, names) -> list[str] | None:
    """Notes it carries are let go now (the user's Unload): each loads again only by
    need. -> the names that were loaded, None when the conversation is not there."""
    def fn(c: sqlite3.Connection) -> list[str] | None:
        if c.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone() is None:
            return None
        before = set(rows(c, cid)[1])
        gone = {(kind, n) for n in names}
        c.executemany("DELETE FROM context_loaded WHERE conversation_id=? AND kind=? "
                      "AND name=?", [(cid, k, n) for k, n in gone])
        _changed(c, store, cid, before, before - gone)
        return [n for n in names if (kind, n) in before]

    return await store.write(fn)


async def _save(store, cid: str, turn: int, keep: dict[tuple[str, str], int],
                start: bool) -> None:
    """The agent's rows become `keep`, its round `turn`. A turn start says so on the global feed
    even when nothing changed: the Context tab's "turns ago" moved."""
    def fn(c: sqlite3.Connection) -> None:
        before = set(rows(c, cid)[1])
        c.execute("INSERT INTO context_state(conversation_id, turns, updated_at) "
                  "VALUES(?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET "
                  "turns=excluded.turns, updated_at=excluded.updated_at",
                  (cid, turn, now()))
        c.execute("DELETE FROM context_loaded WHERE conversation_id=?", (cid,))
        c.executemany("INSERT INTO context_loaded(conversation_id, kind, name, touched) "
                      "VALUES(?,?,?,?)", [(cid, k, n, t) for (k, n), t in keep.items()])
        _changed(c, store, cid, before, set(keep), always=start)

    await store.write(fn)


def _changed(c: sqlite3.Connection, store, cid: str, before: set, after: set,
             always: bool = False) -> None:
    """`context.changed` on the global feed: {conversation, loaded: {notes},
    unloaded: {notes}}, what came and what went; nothing when nothing did, unless
    `always`."""
    def split(items) -> dict:
        return {"notes": sorted(n for kind, n in items if kind == "note")}
    if after != before or always:
        store.global_event(c, CONTEXT_CHANGED, {
            "conversation": cid, "loaded": split(after - before),
            "unloaded": split(before - after)})


# ---- keys ----------------------------------------------------------------------------

def key_in(text: str, keys: list[str]) -> str | None:
    """The first of `keys` that appears in `text` as a whole word or phrase, any case
    and any spaces between its words ("art" is not in "start")."""
    for k in keys:
        rx = r"(?<!\w)" + r"\s+".join(map(re.escape, k.split())) + r"(?!\w)"
        if k.split() and re.search(rx, text, re.I):
            return k
    return None


def talk(chain: list[dict]) -> str:
    """What keys are looked for in (skills/recall.py): the new user messages and the
    start of its last reply before them."""
    new = unsent(chain)
    reply = next((m["content"] for m in reversed(chain[:len(chain) - len(new)])
                  if m["role"] == "assistant" and (m.get("content") or "").strip()), "")
    return "\n".join([*(m.get("content") or "" for m in new), reply[:KEY_REPLY]])


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


async def _notes(store, cfg, cid: str, conv, rows: dict, turn: int, idle: int,
                 budget: int, counter) -> tuple[list[dict], dict]:
    """-> (the notes, as Carried.notes, the rows to keep), from its notebook."""
    from ..skills import notebook_index
    from ..skills.notebook import notes_block
    root = cfg.her.notebook
    folder, off = conv["notebook_folder"], set(json.loads(conv["notes_off"] or "[]"))
    user = (getattr(cfg, "user", "") or "").lower()
    notes: list[dict] = []
    seen: set[str] = set()
    always: set[str] = set()

    def consider(rel: str, touched: int, how: str) -> bool:
        """Adds the note when it is one; True when it is loaded (not off)."""
        if rel in seen or not inside(rel, folder):
            return False
        # note: every note's head is read at each turn start; a column in the index
        # when notebooks grow into the thousands
        head = read_note(root, rel, head=True)
        if head is None:
            return False
        load = load_of(rel, head, user)
        if how == "always" and load != "always":
            return False
        why = OFF_HERE if rel in off else (
            "load: " + ", ".join(load_line(head) or []) if load == "off" else "")
        seen.add(rel)
        if how == "always" and not why:
            always.add(rel)
        text = cfg.substitute(carried_text(read_note(root, rel) or ""))   # a seeded note says {{username}}
        if how == "always" and rel != SAMPLES and len(text.encode()) > ALWAYS_MAX:
            text = head_of(text, rel)
        notes.append({"path": rel, "text": text, "tokens": counter.estimate(text),
                      "state": "off" if why else "included", "why": why,
                      "since": turn - touched, "always": how == "always" and not why})
        return not why

    indexed = [r["path"] for r in await notebook_index.list(store, folder)]
    for rel in indexed:
        consider(rel, turn, "always")
    for (kind, rel), touched in sorted(rows.items(), key=lambda x: -x[1]):
        if kind == "note" and turn - touched <= idle:
            consider(rel, touched, "row")
    # by need: each in turn (by touch) while the block fits the
    # share; the ones not admitted yet count as named, so the final block is never larger
    need = [n for n in notes if n["state"] == "included" and n["path"] not in always]
    for n in need:
        n["state"], n["why"] = "left_out", OVER
    for n in need:
        n["state"], n["why"] = "included", ""
        if counter.estimate(notes_block(cfg, notes)) > budget:
            n["state"], n["why"] = "left_out", OVER
    keep = {("note", n["path"]): turn - n["since"] for n in notes
            if n["state"] == "included"}
    for rel in sorted(off - seen):       # the user's switches, so one can come back on
        if inside(rel, folder) and (text := read_note(root, rel)) is not None:
            notes.append({"path": rel, "text": text, "tokens": counter.estimate(text),
                          "state": "off", "why": OFF_HERE, "always": False})
    for n in notes:
        if n["state"] != "included":
            n["since"] = None
    return notes, keep


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
    what its offered_in allows (sub_agents not inside a sub-agent, the intercom's
    start_chat away from the user); skills offering none left out."""
    from ..skills.places import offered as offered_at
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

def round_of(c: sqlite3.Connection, cid: str, turn_id: str | None = None) -> int:
    """The round turn `turn_id` is in (None: the round now): the conversation's turns up
    to it."""
    if turn_id is None:
        return c.execute("SELECT count(*) FROM turns WHERE conversation_id=?",
                         (cid,)).fetchone()[0]
    return c.execute(
        "SELECT count(*) FROM turns WHERE conversation_id=? AND rowid <= (SELECT rowid "
        "FROM turns WHERE id=?)", (cid, turn_id)).fetchone()[0]


async def select(store, cfg, registry, cid: str, backend, *, when: str = "dry",
                 turn_id: str | None = None) -> Carried:
    """What it carries. `when`: "start" (turn `turn_id` starts: its round is its round,
    and the result is saved), "dry" (what it carries now; nothing is saved)."""
    from . import tokens
    counter, win = tokens.counter(backend), tokens.window(backend)
    s = shares(cfg)
    idle = int(s["idle_turns"])
    budgets = {"notes": int(win * s["notes"])}
    start = when == "start" and turn_id is not None

    def read(c: sqlite3.Connection):
        conv = c.execute("SELECT kind, notebook_folder, notes_off FROM conversations "
                         "WHERE id=?", (cid,)).fetchone()
        turn, have = rows(c, cid)
        return conv, round_of(c, cid, turn_id) if start and conv else turn, have

    conv, turn, have = await store.read(read)
    if conv is None:
        return Carried(budgets=budgets)
    skills, others = await _skills(store, cfg, registry, cid)
    notes, keep = await _notes(store, cfg, cid, dict(conv), have, turn, idle,
                               budgets["notes"], counter)
    if start:
        await _save(store, cid, turn, keep, True)
    return Carried(turn, notes, skills, others,
                   {k: turn - t for k, t in keep.items()}, budgets)
