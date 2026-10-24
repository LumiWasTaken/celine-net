"""The one switch: the Guard feature (Settings, Features) and the
General setting `guard` (Guard mode). Feature off: Bastet is not here at all (no check,
no label on a result, no page check, no patrol, no answer on the intercom, no line in its
context; its context is exactly as without it); watch: every rule only writes its row;
on. Read at call time, so a change applies at once. Every file outside guard/ asks
`enabled` or `mode` here, through the port (guard/__init__.py).

One chat can turn it off for itself (its settings' `guard`, column
conversations.guard_off): asked with that conversation's id, the switch is off there.
OFF holds those ids in memory (settings/conversations.py fills it at startup and on
every write), so a call never reads the database for it.
"""
from __future__ import annotations

MODES = ("off", "watch", "on")
OFF: set[str] = set()


def mode(cfg, conversation_id: str | None = None) -> str:
    """The switch, as saved now (General settings); off in a chat that turned it off."""
    from ..settings import features, general
    if conversation_id is not None and conversation_id in OFF:
        return "off"
    if not features.on(cfg, "guard"):          # Settings, Features: Bastet is not here
        return "off"
    s = getattr(cfg, "settings", None)
    # watch or on: settings/general.py refuses any other value when the file is read
    return general.value(getattr(s, "general", None) or {}, "guard")


def enabled(cfg, conversation_id: str | None = None) -> bool:
    """Is Bastet here at all (Watch or On), in this conversation when one is named?"""
    return mode(cfg, conversation_id) != "off"
