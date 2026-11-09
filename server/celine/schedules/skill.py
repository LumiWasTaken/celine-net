"""The agent's hand on the Routines: routines__list (the routines and the triggers, or the
runs of a night or the newest runs), routines__read, routines__write and routines__run. A write is schedules/edit.py's, the same checks as the Routines page,
recorded as its own (its texts' History by it); a run it starts is `started_by` it
(runs.started_by), so both show as its own in Events and in the routine's history."""
from __future__ import annotations

import datetime as dt
import json
from typing import Annotated

from ..clock import zone
from ..core.ids import when
from ..errors import CelineError
from ..skills import ToolContext, ToolResult, guide, tool, described
from ..turns.compact import one_line
from . import shown
from .runs import ROW, view as run_view

RUNS_SHOWN = 5
RUNS_MAX = 50       # runs one routines__list(runs=) gives at most
RESULT_LINE = 160   # characters of a run's result's last line


def _when(s) -> str:
    return f"every day at {s.time}" if s.time else f"cron {s.cron}"


def _run_line(r: dict, tz, label: bool = False) -> str:
    """One run (runs.view) as a line: run:<id> (an intercom address that
    past_conversations__read takes too), its routine's label with `label`, its slot, its
    state, who started it, its error or reason, and its result's last line."""
    last = next((x for x in reversed((r["result"] or "").splitlines()) if x.strip()), "")
    return (f"- run:{r['id']}" + (f" {r['label']}" if label else "") +
            f" {when(r['slot'], tz)} {r['state']}" +
            (f", started by {r['started_by']}" if r["started_by"] != "clock" else "") +
            (f": {r['error'] or r['reason']}" if r["error"] or r["reason"] else "") +
            (f"\n  result: {one_line(last, RESULT_LINE)}" if last else ""))


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
    async def list(
        self,
        ctx: ToolContext,
        night: Annotated[str | None, "A night, such as 2026-11-08 (from 12:00 the day "
                                     "before to 12:00 that day): its runs, every "
                                     "routine's."] = None,
        runs: Annotated[int, f"The newest runs of every routine, this many, at most "
                             f"{RUNS_MAX}."] = 0,
    ) -> ToolResult:
        """List the Routines (name, label, when, on or off, the newest run) and the
        triggers (name, label, on or off, when it last fired, its background task's
        conversation id). With night or runs, runs instead: each run:<id> with its
        routine, time, state, reason and its result's last line."""
        tz = zone(ctx.config)
        if night or runs:
            return await self._runs(ctx, night, runs, tz)
        every = self._every()
        # SQLite: the bare columns come from the row of max(slot)
        last = await ctx.store.read(lambda c: {r["schedule"]: dict(r) for r in c.execute(
            "SELECT schedule, state, max(slot) AS slot FROM runs GROUP BY schedule")})
        lines = []
        for s in sorted(every.values(), key=lambda s: (s.time or "99", s.name)):
            r = last.get(s.name)
            lines.append(f"- {s.name}: {shown(s)}, {_when(s)}, {'on' if s.on else 'off'}" +
                         (f", last run {when(r['slot'], tz)} {r['state']}"
                          if r else ", no runs yet"))
        lines = lines or ["No routines."]
        if fires := await self._triggers(ctx, tz):
            lines += ["Triggers:", *fires]
        return ToolResult(content="\n".join(lines), display={"routines": len(every)})

    async def _runs(self, ctx: ToolContext, night: str | None, n: int, tz) -> ToolResult:
        """routines__list's runs: those of `night`, else the newest `n`."""
        if night:
            try:
                night = dt.date.fromisoformat(night.strip()).isoformat()
            except ValueError:
                return ToolResult(content=f"{night!r} is not a night. Use 2026-11-08.",
                                  is_error=True)
            sql, args = f"{ROW} WHERE r.night = ? ORDER BY r.slot", (night,)
        else:
            sql, args = f"{ROW} ORDER BY r.slot DESC LIMIT ?", (min(max(n, 1), RUNS_MAX),)
        rows = await ctx.store.read(lambda c: [run_view(r, self.app.cfg)
                                               for r in c.execute(sql, args)])
        if not rows:
            return ToolResult(content=f"No runs the night of {night}." if night
                              else "No runs yet.", display={"runs": 0})
        return ToolResult(content="\n".join(_run_line(r, tz, label=True) for r in rows),
                          display={"runs": len(rows)})

    async def _triggers(self, ctx: ToolContext, tz) -> list[str]:
        """The triggers the loaded skills declare, one line each."""
        from ..triggers import declared, fired, state
        found = declared(getattr(self.app, "registry", None)).values()
        if not found:
            return []
        names = [t.name for _, t in found]

        def fn(c):
            return {n: (fired(c, n), c.execute(
                "SELECT id FROM conversations WHERE trigger_key = ?",
                (f"trigger:{n}",)).fetchone()) for n in names}
        seen = await ctx.store.read(fn)
        cfg = self.app.cfg
        out = []
        for _, t in found:
            at, conv = seen[t.name]
            out.append(f"- {t.name}: {cfg.substitute(t.label)}, "
                       f"{'on' if state(cfg, t)['on'] else 'off'}, " +
                       (f"last fired {when(at, tz)}" if at else "never fired") +
                       (f", conversation {conv[0][:8]}" if conv else ""))
        return out

    @tool()
    async def read(
        self,
        ctx: ToolContext,
        name: Annotated[str, "The routine's name, as routines__list gives it."],
    ) -> ToolResult:
        """Read one Routine whole (every setting, its prompt and its reviewer prompt)
        and its newest runs: each run:<id> (its address on the intercom, and what
        past_conversations__read takes) with its time, state, reason and its result's
        last line."""
        s = self._every().get(name.strip())
        if s is None:
            return ToolResult(content=f"There is no routine {name!r}: routines__list.",
                              is_error=True)
        settings = {k: v for k, v in s.raw().items() if k != "prompt"}
        runs = await ctx.store.read(lambda c: [run_view(r, self.app.cfg) for r in c.execute(
            f"{ROW} WHERE r.schedule = ? ORDER BY r.slot DESC LIMIT ?",
            (s.name, RUNS_SHOWN))])
        lines = [f"{shown(s)} ({s.name}), {_when(s)}.",
                 "Settings: " + json.dumps(settings, ensure_ascii=False),
                 f"Prompt:\n{s.prompt}"]
        if s.reviewer_prompt:
            lines.append(f"Reviewer prompt ({s.reviewer}):\n{s.reviewer_prompt}")
        lines.append("Newest runs:" if runs else "No runs yet.")
        lines += [_run_line(r, zone(ctx.config)) for r in runs]
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
        wall_minutes: Annotated[int | None, "The minutes it may run; the Routine time limit in Settings caps it."] = None,
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
        if old and old.reviewer_prompt:
            body["reviewer_prompt"] = old.reviewer_prompt
        body.update({k: v for k, v in (("label", label), ("prompt", prompt), ("role", role),
                                       ("on", on), ("note", note), ("after", after),
                                       ("wall_minutes", wall_minutes)) if v is not None})
        if time or cron:                     # the new when replaces the old one
            body.pop("cron", None)
            body.update({k: v for k, v in (("time", time), ("cron", cron)) if v})
        try:
            s, _ = await save(self.app, name, body, "agent")
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
        if self.app.schedules is None:
            return ToolResult(content="Routines are off.",
                              is_error=True)
        run = await self.app.schedules.run_now(name, by="agent")
        return ToolResult(content=f"Run run:{run['id']} of {run['label']} waits to start.",
                          display={"run": run["id"]})
