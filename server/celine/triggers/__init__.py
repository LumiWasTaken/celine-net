"""Triggers: background tasks that start on an event, without anyone typing.

Skills declare them, in code: `triggers = (Watch(...), Event(...))` on the skill class,
each with `label` (its name on the Routines page) and `about` (one line on what it does).
The user's state is `var/settings/triggers.toml` (settings section `triggers`): per
trigger `on`. Every trigger is off until the user turns it on (GET /api/triggers, PUT
/api/triggers/{name}). Jobs at a time of day are schedules, which are data
(celine/schedules), not triggers.

A fire is a background task: a `trigger` intercom message from `system:<skill>` to the
trigger's own conversation (kind `background`, made on its first fire, never in the chat
list, its fires never in the user's intercom list): it starts a turn there, or reaches
the one still running at its next step. The text is the trigger's prompt and RULE:
nothing that needs the user ends the turn with one line for its log; something
actionable becomes a chat for the user (`intercom__start_chat`) with a message on the
intercom that waits for them. GET /api/background-conversations lists the background tasks and the
runs.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import anyio

from ..conversations import store as conversations

log = logging.getLogger("celine.triggers")

RULE = ("This is a background task: {{username}} is not here and reads nothing of it. If "
        "nothing in it needs {{username}}, end your turn with one short line for your log. "
        "If something needs {{username}} to act or to know now, start a chat for them with "
        "intercom__start_chat: a title and what they read first, short and complete.")


@dataclass(frozen=True)
class Watch:
    """Fires once per batch of changes in a folder; {path} and {event} in the prompt are
    the changed paths and what happened (added, modified, deleted)."""
    name: str
    path: str
    events: tuple[str, ...]
    prompt: str
    debounce: float = 5.0
    label: str = field(kw_only=True)
    about: str = field(kw_only=True)


@dataclass(frozen=True)
class Event:
    """Fires once per event of a source: `source` names a coroutine method of the skill,
    `source(store) -> [dict]`, asked every `every` seconds; {key} in the prompt is that
    key of the event. `outside`: the keys whose values someone else wrote (a mail's
    sender and subject), wrapped for it while the guard is on (guard/mark.py)."""
    name: str
    source: str
    prompt: str
    every: float = 300.0
    label: str = field(kw_only=True)
    about: str = field(kw_only=True)
    outside: tuple[str, ...] = field(default=(), kw_only=True)


def declared(registry) -> dict[str, tuple[object, Watch | Event]]:
    """Every trigger the loaded skills declare, by name: (the skill, the trigger)."""
    out: dict[str, tuple[object, Watch | Event]] = {}
    for skill in (registry.skills.values() if registry is not None else ()):
        for t in getattr(skill, "triggers", ()):
            out[t.name] = (skill, t)
    return out


def state(cfg, t) -> dict:
    """The user's state of trigger `t`: {on}."""
    saved = (getattr(getattr(cfg, "settings", None), "triggers", None) or {}).get(t.name, {})
    return {"on": bool(saved.get("on", False))}


class _Fill(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Triggers:
    """The running triggers. `apply()` (at start, after every settings save and config
    reload) stops them all and starts the ones that are on."""

    def __init__(self, app):
        self.app = app
        self._tg = None
        self._scope: anyio.CancelScope | None = None
        self._polled: dict[str, float] = {}      # an event source's last ask, monotonic

    async def run(self, tg) -> None:
        self._tg = tg
        self.apply()

    def apply(self) -> None:
        if self._scope is not None:
            self._scope.cancel()
        if self._tg is None:
            return
        self._scope = anyio.CancelScope()
        self._tg.start_soon(self._all, self._scope)

    async def _all(self, scope: anyio.CancelScope) -> None:
        cfg = self.app.cfg
        with scope:
            async with anyio.create_task_group() as tg:
                for name, (skill, t) in declared(self.app.registry).items():
                    if not state(cfg, t)["on"]:
                        continue
                    if isinstance(t, Watch):
                        tg.start_soon(self._watch, skill, t)
                    elif isinstance(t, Event):
                        tg.start_soon(self._event, skill, t)

    async def fire(self, skill_name: str, name: str, prompt: str,
                   outside: dict | None = None) -> str:
        """The fire's message to the trigger's background conversation. -> its id.
        `outside`: the guard's label of the parts others wrote (guard/sources.py)."""
        cfg = self.app.cfg
        cid = await conversations.for_trigger(self.app.store, f"trigger:{name}", name)
        await self.app.intercom.send(f"system:{skill_name}", f"agent:{cid}",
                                     cfg.substitute(f"{prompt}\n\n{RULE}"), kind="trigger",
                                     data={"trigger": name,
                                           **({"outside": outside} if outside else {})})
        log.info("trigger fired", extra={"event": "trigger.fired", "trigger": name})
        return cid

    async def _fire(self, skill_name: str, name: str, prompt: str,
                    outside: dict | None = None) -> None:
        """`fire` from a trigger's loop: a failure is logged, never raised up into the
        lifespan task group (that takes the app down)."""
        try:
            await self.fire(skill_name, name, prompt, outside)
        except Exception:
            log.exception("trigger fire failed", extra={"event": "trigger.fire_failed",
                                                        "trigger": name})

    async def _watch(self, skill, t: Watch) -> None:
        import watchfiles
        kinds = {watchfiles.Change.added: "added", watchfiles.Change.modified: "modified",
                 watchfiles.Change.deleted: "deleted"}
        path = Path(os.path.expandvars(os.path.expanduser(t.path)))
        if not path.exists():
            log.warning("watch path missing", extra={"event": "trigger.no_path",
                                                     "trigger": t.name})
            return
        async for changes in watchfiles.awatch(path, debounce=int(t.debounce * 1000),
                                                 rust_timeout=500):
            hits = sorted((p, kinds[c]) for c, p in changes if kinds[c] in t.events)
            if hits:
                await self._fire(skill.name, t.name, t.prompt.format_map(_Fill(
                    path="\n".join(p for p, _ in hits),
                    event=", ".join(sorted({e for _, e in hits})))))

    async def _event(self, skill, t: Event) -> None:
        while True:
            # a restart of the triggers (a settings save) does not ask again sooner
            wait = self._polled.get(t.name, 0.0) + t.every - time.monotonic()
            await anyio.sleep(max(0.0, wait))
            self._polled[t.name] = time.monotonic()
            try:
                events = await getattr(skill, t.source)(self.app.store)
            except Exception as e:                      # noqa: BLE001 - asked again later
                log.warning("trigger source failed", extra={
                    "event": "trigger.source_failed", "trigger": t.name,
                    "error": f"{type(e).__name__}: {e}"})
                continue
            for ev in events:
                parts = [str(ev[k]) for k in t.outside if ev.get(k)]
                await self._fire(skill.name, t.name, t.prompt.format_map(_Fill(ev)), {
                    "source": skill.name, "by": str(ev.get("from") or "") or None,
                    "parts": parts} if parts else None)
