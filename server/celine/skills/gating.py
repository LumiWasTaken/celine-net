"""Whether a tool is on, here, now.

Resolution, strongest first:
1. this conversation, this tool                 (tool_overrides, exact wire name)
2. this conversation, this skill                (tool_overrides, skill name): the skill's
                                                 switch is the bulk verb, setting it clears
                                                 the tool rows under it (set_override)
3. Settings → Tools default for the tool, then for the skill ("on"/"off")
4. on (a skill with `enabled = false` in config/skills.toml is not loaded at all)
"""
from __future__ import annotations

import sqlite3

from ..core.ids import now
from . import ToolSpec
from ..core.event_types import OVERRIDES_CHANGED

# A tool that is off, or of a skill that is down, is not in its list (turns/prompt.py);
# the skill index says so, and a call to it is refused with OFF or down_sentence.
OFF = "{name} is off here."
# A sub-agent with read access (sub_agents/skill.py) has only read-only tools and the
# intercom in its list. A call to another one is refused with this and what it has of
# that skill.
READ_ONLY_REFUSAL = "You have read access; the read-only tools of {skill} are: {reads}."


def down_why(why: str) -> str:
    """A skill's reason for being down as one sentence: one full stop at its end."""
    return " ".join(str(why).split()).rstrip(" .") + "."


def down_sentence(skill: str, why: str) -> str:
    """What it is told about a skill that is down: why, and to go on without it."""
    return f"{skill} is down: {down_why(why)} Work without it."


def read_mode_has(spec: ToolSpec) -> bool:
    """Whether a sub-agent in read mode has this tool."""
    return not spec.mutating or spec.skill == "intercom"


def _defaults(cfg) -> dict[str, str]:
    s = getattr(cfg, "settings", None)
    return getattr(s, "tools", None) or {}


def is_enabled(spec: ToolSpec, overrides: dict[str, bool], cfg) -> bool:
    for target in (spec.wire_name, spec.skill):
        if target in overrides:
            return overrides[target]
    d = _defaults(cfg)
    for target in (spec.wire_name, spec.skill):
        if target in d:
            return d[target] == "on"
    return cfg.skill_enabled(spec.skill)


def reason(spec: ToolSpec, overrides: dict[str, bool], cfg) -> str:
    """Why the tool is on or off, for the UI: "here" (a conversation override),
    "default" (Settings or config)."""
    return "here" if spec.wire_name in overrides or spec.skill in overrides else "default"


def default_enabled(spec: ToolSpec, cfg) -> bool:
    """The Settings default alone: no conversation."""
    return is_enabled(spec, {}, cfg)


async def overrides_for(store, conversation_id: str) -> dict[str, bool]:
    """The overrides a turn uses: the conversation's rows (tool_overrides)."""
    return await store.read(lambda c: {
        r["target"]: bool(r["enabled"]) for r in c.execute(
            "SELECT target, enabled FROM tool_overrides WHERE conversation_id=?",
            (conversation_id,))})


async def set_override(store, conversation_id: str, target: str, enabled: bool,
                       client_seq: int) -> int | None:
    """Ignores a request older than the last applied for this target. A skill's target
    clears the tool rows under it, as in Settings. KeyError: no such conversation."""
    def fn(c: sqlite3.Connection) -> int | None:
        if c.execute("SELECT 1 FROM conversations WHERE id=?",
                     (conversation_id,)).fetchone() is None:
            raise KeyError(conversation_id)
        row = c.execute(
            "SELECT client_seq FROM tool_overrides WHERE conversation_id=? AND "
            "target=?", (conversation_id, target)).fetchone()
        if row is not None and client_seq <= int(row["client_seq"]):
            return None
        c.execute(
            "INSERT INTO tool_overrides(conversation_id,target,enabled,client_seq,"
            "updated_at) VALUES(?,?,?,?,?) ON CONFLICT(conversation_id,target) "
            "DO UPDATE SET enabled=excluded.enabled, client_seq=excluded.client_seq, "
            "updated_at=excluded.updated_at",
            (conversation_id, target, int(enabled), client_seq, now()))
        if "__" not in target:
            c.execute("DELETE FROM tool_overrides WHERE conversation_id=? AND "
                      "substr(target, 1, ?)=?", (conversation_id, len(target) + 2,
                                                  target + "__"))
        return store.event(c, conversation_id, OVERRIDES_CHANGED,
                           {"target": target, "enabled": enabled, "client_seq": client_seq})

    return await store.write(fn)


async def clear_override(store, conversation_id: str, target: str) -> int:
    def fn(c: sqlite3.Connection) -> int:
        c.execute("DELETE FROM tool_overrides WHERE conversation_id=? AND target=?",
                  (conversation_id, target))
        return store.event(c, conversation_id, OVERRIDES_CHANGED,
                           {"target": target, "enabled": None})

    return await store.write(fn)
