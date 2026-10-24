"""The agent's hand on the Routines: routines__list, routines__read, routines__write and
routines__run. A write is schedules/edit.py's, the same checks as the Routines page,
recorded as its own (its texts' History by it); a run it starts is `started_by` it
(runs.started_by), so both show as its own in Events and in the routine's history."""
from __future__ import annotations

import json
from typing import Annotated

from ..errors import CelineError
from ..skills import ToolContext, ToolResult, guide, tool, described
from . import WALL_MAX, shown
from .runs import view as run_view

RUNS_SHOWN = 5


def _when(s) -> str:
    return f"every day at {s.time}" if s.time else f"cron {s.cron}"


class Routines:
    name = "routines"
    guide = guide("routines")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg

    app = None      # the running app (core/services.py), from skills.build

    def _every(self) -> dict:
        return getattr(self.app.cfg if self.app else self.cfg, "schedules", None) or {}

    @tool()
    async def list(self, ctx: ToolContext) -> ToolResult:
        """List the Routines: name, label, when, on or off, and the newest run."""
        every = self._every()
        if not every:
            return ToolResult(content="No routines.", display={"routines": 0})
        # SQLite: the bare columns come from the row of max(slot)
        last = await ctx.store.read(lambda c: {r["schedule"]: dict(r) for r in c.execute(
            "SELECT schedule, state, max(slot) AS slot FROM runs GROUP BY schedule")})
        lines = []
        for s in sorted(every.values(), key=lambda s: (s.time or "99", s.name)):
            r = last.get(s.name)
            lines.append(f"- {s.name}: {shown(s)}, {_when(s)}, {'on' if s.on else 'off'}" +
                         (f", last run {r['slot'][:16].replace('T', ' ')} UTC {r['state']}"
                          if r else ", no runs yet"))
        return ToolResult(content="\n".join(lines), display={"routines": len(every)})

    @tool()
    async def read(
        self,
        ctx: ToolContext,
        name: Annotated[str, "The routine's name, as routines__list gives it."],
    ) -> ToolResult:
        """Read one Routine whole (every setting, its prompt and its reviewer prompt)
        and its newest runs with their address on the intercom."""
        s = self._every().get(name.strip())
        if s is None:
            return ToolResult(content=f"There is no routine {name!r}: routines__list.",
                              is_error=True)
        settings = {k: v for k, v in s.raw().items() if k != "prompt"}
        runs = await ctx.store.read(lambda c: [run_view(r, self.app.cfg) for r in c.execute(
            "SELECT * FROM runs WHERE schedule = ? ORDER BY slot DESC LIMIT ?",
            (s.name, RUNS_SHOWN))])
        lines = [f"{shown(s)} ({s.name}), {_when(s)}.",
                 "Settings: " + json.dumps(settings, ensure_ascii=False),
                 f"Prompt:\n{s.prompt}"]
        if s.partner_prompt:
            lines.append(f"Reviewer prompt ({s.partner}):\n{s.partner_prompt}")
        lines.append("Newest runs:" if runs else "No runs yet.")
        lines += [f"- run:{r['id']} {r['slot'][:16].replace('T', ' ')} UTC {r['state']}"
                  + (f", started by {r['started_by']}" if r["started_by"] != "clock" else "")
                  + (f": {r['error'] or r['reason']}" if r["error"] or r["reason"] else "")
                  for r in runs]
        return ToolResult(content="\n".join(lines), display={"routine": s.name})

    @tool(read_only=False, destructive=False)
    async def write(
        self,
        ctx: ToolContext,
        name: Annotated[str, "The routine's name: lowercase letters, digits, - or _. A "
                             "new name makes a new routine."],
        label: Annotated[str | None, "What it is called on the Routines page."] = None,
        prompt: Annotated[str | None, "The whole prompt the run starts with."] = None,
        time: Annotated[str | None, "HH:MM: every day at that time."] = None,
        cron: Annotated[str | None, "A cron line (minute hour day month weekday) when it "
                                    "is not daily."] = None,
        role: Annotated[str | None, "The model role its turns run on, such as chat."] = None,
        on: Annotated[bool | None, "true: it runs at its time; false: it does not."] = None,
        note: Annotated[str | None, "A notebook path its answer is written to, like "
                                    "radar/{{date}}.md. Empty: none."] = None,
        after: Annotated[list[str] | None, "Names of the routines of the same night it "
                                           "runs after."] = None,
        wall_minutes: Annotated[int | None, f"The minutes it may run, at most {WALL_MAX}."] = None,
    ) -> ToolResult:
        """Make or change a Routine with the same checks as the Routines page. A change
        changes only what you give; everything else stays as it is. A new routine needs
        label, prompt and time or cron."""
        from ..settings.files import Invalid
        from .edit import save
        name = name.strip()
        old = self._every().get(name)
        time, cron = (time or "").strip() or None, (cron or "").strip() or None
        if old is None:
            missing = [k for k, v in (("label", label), ("prompt", prompt)) if v is None]
            missing += [] if time or cron else ["time or cron"]
            if missing:
                return ToolResult(content=f"Not saved: there is no routine {name!r} yet "
                                          f"(routines__list), and a new one needs "
                                          f"{', '.join(missing)}.", is_error=True)
        body = old.raw() if old else {}
        if old and old.partner_prompt:
            body["partner_prompt"] = old.partner_prompt
        body.update({k: v for k, v in (("label", label), ("prompt", prompt), ("role", role),
                                       ("on", on), ("note", note), ("after", after),
                                       ("wall_minutes", wall_minutes)) if v is not None})
        if time or cron:                     # the new when replaces the old one
            body.pop("cron", None)
            body.update({k: v for k, v in (("time", time), ("cron", cron)) if v})
        try:
            s, _ = await save(self.app, name, body, "her")
        except Invalid as e:
            return ToolResult(content=f"Not saved ({e.field}): {e.message}", is_error=True)
        except CelineError as e:
            return ToolResult(content=f"Not saved: {e.message}", is_error=True)
        return ToolResult(content=f"Saved {shown(s)} ({s.name}), {_when(s)}, "
                                  f"{'on' if s.on else 'off'}.",
                          display={"routine": s.name, "new": old is None})

    @tool(read_only=False, destructive=False)
    async def run(
        self,
        ctx: ToolContext,
        name: Annotated[str, "The routine's name."],
    ) -> ToolResult:
        """Start a run of a Routine now. It waits like any run and starts when the one
        before it is done. Its address on the intercom is run:<id>."""
        name = name.strip()
        if name not in self._every():
            return ToolResult(content=f"There is no routine {name!r}: routines__list.",
                              is_error=True)
        run = await self.app.schedules.run_now(name, by="her")
        return ToolResult(content=f"Run run:{run['id']} of {run['label']} waits to start.",
                          display={"run": run["id"]})
