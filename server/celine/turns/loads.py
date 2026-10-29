"""What it carries by need: the notes loaded in full and the skills whose tools are in
its list, decided at every turn start by one policy for every conversation and measured
in tokens: notes as a share of the model's window, tools as a flat number of tokens
(`var/settings/context.toml`, copied once from `config/context.toml`).

Notes, from its notebook. Always:
a note whose head has `load: always` and the notes on the user, `people/{{username}}.md`
and `people/{{username}}-*.md`; one over ALWAYS_MAX bytes carries only its head lines and
its `!!` lines (head_of) and names notebook__read for the rest, it "Love this"
samples (conversations/samples.py PATH) aside. For a while: a note it read, wrote or
edited (the notebook tools touch it) or the user loaded by hand. While a skill is
loaded: its note on it, `skills/<name>.md`. A note whose `keys:` line comes up in the
talk is not loaded here: its matching sections come back graded under `[your memory]`
(skills/recall.py).
Never: a note whose `load:` line says off or names only other words, the notes the user left out
here (`conversations.notes_off`), and anything outside a sub-agent's folder.

Skills, only where skills/places.PLACES offers them. Always: the core skills
(skills.CORE), every tool with its parameters. For a while: a skill it loaded
(`skills__load`), used (a tool of it ran), the user loaded by hand, or that the new user
message asks for (`asked`): a word or phrase of the `words:` line of the skill's own
guide, as a whole word in any case; an attached image counts as `image`, a link as
`url`, a Documents reference as `document` (never with `conversations.auto_context` off).
A loaded skill puts each of its tools in the list by name and one line
(ToolSpec.as_name). A tool of it carries its parameters (ToolSpec.as_wire) for a while
when it loaded the tool (`skills__load` with `skill__tool`) or called it.
Never: a skill whose tools are all off (Settings, Tools, or this conversation): what may
load at all is still the switches'.

A note or skill no turn touched for `idle_turns` rounds is unloaded. The always-loaded
notes always load, even past the notes share (the Context tab shows the block over its
share); the share is for the notes loaded by need, the most recently touched first,
measured as the notes block it reads (headings and lines included). What does not fit
is named. The tools budget counts parameters only: the core skills' tools and the
loaded tools, the most recently touched first; a tool that does not fit is named (`no
room`). The names of the loaded skills' tools are not counted.

The state is per conversation: the `context_loaded` rows (kinds note, skill and tool),
what it carries, each with the round that last touched it, and `context_state.turns`,
the round of its last turn start. A round is a turn of the conversation.
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
from ..skills import CORE
from ..conversations.samples import PATH as SAMPLES   # carried whole, whatever its size
from ..skills.notebook import carried_text, inside, load_line, load_of, read_note
from .message_context import unsent
from ..core.event_types import CONTEXT_CHANGED

DEFAULTS = {"notes": 0.10, "tools": 10_000, "idle_turns": 6,
            "memory_floor": 0.5, "memory_sections": 3, "memory_tokens": 1500}
WHOLE = ("idle_turns", "tools", "memory_sections", "memory_tokens")
KEY_REPLY = 300          # the characters of its last reply a key is looked for in
ALWAYS_MAX = 1536        # bytes of an always-loaded note carried whole; past it its head
TOOLS_MAX = 200_000      # the most tokens of tool parameters context.toml may give
OFF_HERE = "left out in this conversation"
OVER = "over the notes share"


def shares(cfg) -> dict:
    """The context shares: context.toml over DEFAULTS."""
    return {**DEFAULTS, **(getattr(cfg, "context", None) or {})}


def read_shares(path: Path, problems: list) -> dict:
    """context.toml, checked: a bad value is one Problem and keeps its default."""
    from ..config import ConfigError, Problem, shown_path
    out = dict(DEFAULTS)
    try:
        raw = tomllib.loads(path.read_text()) if path.is_file() else {}
    except tomllib.TOMLDecodeError as e:          # celine exits 2 naming the file
        raise ConfigError(f"{shown_path(path)}: {e}") from None
    except OSError as e:
        problems.append(Problem(shown_path(path), f"not read, the defaults count: {e}"))
        return out
    for key, value in raw.items():
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if key not in DEFAULTS:
            problems.append(Problem(shown_path(path), f"unknown key {key}, ignored."))
        elif key == "idle_turns" and not (number and value == int(value)
                                          and 1 <= value <= 100):
            problems.append(Problem(shown_path(path), "idle_turns is a whole number of turns "
                                                "from 1 to 100."))
        elif key == "tools" and not (number and value == int(value)
                                     and 0 <= value <= TOOLS_MAX):
            problems.append(Problem(shown_path(path), f"tools is a whole number of tokens from "
                                                f"0 to {TOOLS_MAX}, such as 10000."))
        elif key == "memory_floor" and not (number and 0 <= value <= 1):
            problems.append(Problem(shown_path(path), "memory_floor is a similarity from 0 to 1, "
                                                "such as 0.5."))
        elif key == "memory_sections" and not (number and value == int(value)
                                               and 0 <= value <= 20):
            problems.append(Problem(shown_path(path), "memory_sections is a whole number from 0 "
                                                "to 20, such as 3."))
        elif key == "memory_tokens" and not (number and value == int(value)
                                             and 0 <= value <= 20_000):
            problems.append(Problem(shown_path(path), "memory_tokens is a whole number of tokens "
                                                "from 0 to 20000, such as 1500."))
        elif key == "notes" and not (number and 0 <= value <= 0.5):
            problems.append(Problem(shown_path(path), f"{key} is a share of the window from 0 "
                                                f"to 0.5, such as 0.1."))
        else:
            out[key] = int(value) if key in WHOLE else float(value)
    return out


@dataclass
class Carried:
    """One selection. `notes`: {path, text, tokens, state, why, since} each, state
    `included`, `left_out` (over the share) or `off` (its load line, or the user's
    switch here); `skills`: the skills loaded, in registry order; `tools`: the tools of
    the loaded skills that carry their parameters, but the core skills' (they always
    do), in registry order; `others`: the skills offered here and not loaded, with why
    (`off`: every tool is off; empty: not needed now), and the tools left out for room
    (`no room`); `since`: turns since each loaded note, skill or tool was touched, by
    (kind, name); `costs`: in tokens, each loaded skill's entries in the list (a core
    skill's tools with their parameters, another's names) and each tool's parameters,
    carried or left out for room; `budgets`: notes as tokens of the window, tools as
    set."""
    turn: int = 0
    notes: list[dict] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    others: dict[str, str] = field(default_factory=dict)
    since: dict[tuple[str, str], int] = field(default_factory=dict)
    budgets: dict[str, int] = field(default_factory=dict)
    costs: dict[str, int] = field(default_factory=dict)

    @property
    def loaded_notes(self) -> frozenset[str]:
        return frozenset(n["path"] for n in self.notes if n["state"] == "included")

    @property
    def loaded(self) -> frozenset[str]:
        """The skills and tools it carries, as its rows name them."""
        return frozenset(self.skills) | frozenset(self.tools)

    def full(self, name: str) -> bool:
        """Whether tool `name` (skill__tool) is in its list with its parameters."""
        return name.split("__")[0] in CORE or name in self.tools

    @property
    def counted(self) -> int:
        """The tokens of parameters the tools budget counts now."""
        return sum(self.costs.get(n, 0) for n in self.skills if n in CORE) + \
            sum(self.costs[n] for n in self.tools)


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


async def loaded(store, cid: str) -> frozenset[str]:
    """The skills and tools its rows hold now: Carried.loaded right after a saved
    selection, another set once something was loaded or unloaded."""
    def fn(c: sqlite3.Connection) -> frozenset[str]:
        return frozenset(n for k, n in rows(c, cid)[1] if k in ("skill", "tool"))

    return await store.read(fn)


async def unload(store, cid: str, kind: str, names) -> list[str] | None:
    """Notes, skills or tools it carries are let go now (the user's Unload,
    skills__unload): each loads again only by need; a skill takes its tools with it.
    -> the names that were loaded, None when the conversation is not there."""
    def fn(c: sqlite3.Connection) -> list[str] | None:
        if c.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone() is None:
            return None
        before = set(rows(c, cid)[1])
        gone = {(kind, n) for n in names}
        if kind == "skill":
            gone |= {(k, t) for k, t in before if k == "tool" and t.split("__")[0] in names}
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
    """`context.changed` on the global feed: {conversation, loaded: {notes, skills,
    tools}, unloaded: {...}}, what came and what went; nothing when nothing did, unless
    `always`."""
    def split(items) -> dict:
        return {word: sorted(n for kind, n in items if kind == k)
                for k, word in (("note", "notes"), ("skill", "skills"), ("tool", "tools"))}
    if after != before or always:
        store.global_event(c, CONTEXT_CHANGED, {
            "conversation": cid, "loaded": split(after - before),
            "unloaded": split(before - after)})


# ---- what the new message asks for -----------------------------------------------------

_URL = re.compile(r"https?://", re.I)


def asked(registry, msgs: list[dict]) -> set[str]:
    """The skills the new user messages ask for: a word or phrase of the `words:` line of
    a skill's own guide comes up in them (key_in: a whole word, any case). An attached
    image says `image`, a link `url`, a Documents reference `document`."""
    marks = set()
    for m in msgs:
        text = m.get("content") or ""
        if _URL.search(text):
            marks.add("url")
        if "[[documents:" in text:
            marks.add("document")
        if any(a.get("kind") == "image" for a in m.get("attachments") or []):
            marks.add("image")
    said = "\n".join([*(m.get("content") or "" for m in msgs), *sorted(marks)])
    return {name for name, skill in registry.skills.items()
            if key_in(said, list(getattr(getattr(skill, "guide", None), "words", ())))}


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
                 budget: int, counter, skills=()) -> tuple[list[dict], dict]:
    """-> (the notes, as Carried.notes, the rows to keep), from its notebook. A loaded
    skill brings its note on it, `skills/<name>.md`, for as long as the skill is loaded
    (no row of its own)."""
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
        if why and how == "match":
            return False
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
    before = set(seen)
    for rel in sorted({f"skills/{s}.md" for s in skills}):
        consider(rel, turn, "match")
    with_skill = seen - before
    # by need: each in turn (by touch, then the skills' notes) while the block fits the
    # share; the ones not admitted yet count as named, so the final block is never larger
    need = [n for n in notes if n["state"] == "included" and n["path"] not in always]
    for n in need:
        n["state"], n["why"] = "left_out", OVER
    for n in need:
        n["state"], n["why"] = "included", ""
        if counter.estimate(notes_block(cfg, notes)) > budget:
            n["state"], n["why"] = "left_out", OVER
    keep = {("note", n["path"]): turn - n["since"] for n in notes
            if n["state"] == "included" and n["path"] not in with_skill}
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


def schema_cost(counter, specs) -> int:
    """Tools with their parameters in tokens, as the tools budget counts them."""
    return counter.estimate(json.dumps([s.as_wire() for s in specs], ensure_ascii=False),
                            "schema")


def names_cost(counter, specs) -> int:
    """Tools by name and one line in tokens, a tool without parameters in full (the
    tools budget does not count them)."""
    return counter.estimate(json.dumps([s.as_name() for s in specs], ensure_ascii=False),
                            "schema")


async def _skills(store, cfg, registry, cid: str, rows: dict, turn: int, idle: int,
                  budget: int, counter, new: set[str]
                  ) -> tuple[list[str], list[str], dict[str, str], dict, dict[str, int]]:
    from ..skills.gating import is_enabled, overrides_for, reason
    if not registry:
        return [], [], {}, {}, {}
    overrides = await overrides_for(store, cid)
    here = await offered(registry, store, cid)
    wanted: dict[str, int] = {}          # skill -> the turn that touched it
    core: set[str] = set()
    others: dict[str, str] = {}
    for name, specs in here.items():
        if name in CORE:
            core.add(name)
            wanted[name] = turn
        elif not any(is_enabled(s, overrides, cfg) for s in specs):
            here_off = any(reason(s, overrides, cfg) == "here" for s in specs)
            others[name] = "off here" if here_off else "off"
        elif name in new:
            wanted[name] = turn
        elif ("skill", name) in rows and turn - rows[("skill", name)] <= idle:
            wanted[name] = rows[("skill", name)]
        else:
            others[name] = ""
    keep = {("skill", n): t for n, t in wanted.items()}
    costs = {n: (schema_cost if n in core else names_cost)(counter, here[n])
             for n in wanted}
    used = sum(costs[n] for n in core)
    specs = {s.wire_name: s for n in wanted if n not in core for s in here[n]
             if not s.bare and is_enabled(s, overrides, cfg)}
    tools = [(n, t) for (k, n), t in rows.items()
             if k == "tool" and n in specs and turn - t <= idle]
    for name, touched in sorted(tools, key=lambda x: -x[1]):
        costs[name] = cost = schema_cost(counter, [specs[name]])
        if used + cost > budget:
            others[name] = "no room"
            continue
        used += cost
        keep[("tool", name)] = touched
    return ([n for n in registry.skills if ("skill", n) in keep],
            [n for n in registry.tools if ("tool", n) in keep], others, keep, costs)


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
                 turn_id: str | None = None, path: list[dict] | None = None) -> Carried:
    """What it carries. `when`: "start" (turn `turn_id` starts: its round is its round,
    the new message loads its skills, and the result is saved), "step" (inside the turn,
    after a load or an unload: the rows again, saved), "dry" (what it carries now; nothing is
    saved). With the conversation's automatic context off (`conversations.auto_context`
    0) no message loads a skill: it carries the core skills, the always notes and what
    it or the user loaded. `path`: the conversation's active path when the caller read
    it already."""
    from ..conversations import tree
    from . import tokens
    counter, win = tokens.counter(backend), tokens.window(backend)
    s = shares(cfg)
    idle = int(s["idle_turns"])
    budgets = {"notes": int(win * s["notes"]), "tools": int(s["tools"])}

    def read(c: sqlite3.Connection):
        conv = c.execute("SELECT kind, notebook_folder, notes_off, auto_context FROM "
                         "conversations WHERE id=?", (cid,)).fetchone()
        start = conv is not None and when == "start" and turn_id is not None
        chain = (path if path is not None else tree.path(c, cid)) if start else []
        if start and not conv["auto_context"]:
            chain = []                   # no message signals
        turn, have = rows(c, cid)
        return conv, chain, round_of(c, cid, turn_id) if start else turn, have

    conv, chain, turn, have = await store.read(read)
    if conv is None:
        return Carried(budgets=budgets)
    conv = dict(conv)
    start = when == "start"
    skills, tools, others, kept, costs = await _skills(store, cfg, registry, cid, have, turn, idle,
                                                budgets["tools"], counter,
                                                asked(registry, unsent(chain))
                                                if start and registry else set())
    notes, keep = await _notes(store, cfg, cid, conv, have, turn, idle, budgets["notes"],
                               counter, skills)
    keep |= kept
    if when != "dry":
        await _save(store, cid, turn, keep, start)
    return Carried(turn, notes, skills, tools, others,
                   {k: turn - t for k, t in keep.items()}, budgets, costs)
