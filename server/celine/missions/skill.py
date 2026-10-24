"""A mission: a brief from the user that it works on in a conversation of its own
(missions/store.py holds the rows, missions/api.py starts, edits and stops one). It
plans first, executes with sub-agents, reviews the result against the brief, delivers
an artifact and reports on the intercom. The brief sits in its instructions there; its
plan is the conversation's (plans/: plan__set, plan__step, a step's model);
missions__update records where it stands and what it delivered. missions__list is
offered everywhere, the rest only in the mission's own conversation."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from .. import prompts
from ..core.ids import clock, iso, parse_when
from . import store as missions
from ..plans.flow import chat_models
from ..skills import ToolContext, ToolResult, guide, tool, described
from ..clock import zone

STATUS = {"Active": "active", "Needs you": "needs_you", "Done": "done"}
WORDS = {"active": "active", "needs_you": "needs you", "done": "done",
         "stopped": "stopped by {{username}}"}
ARTIFACT = re.compile(r"(artifact|documents):\S.*")
# how a mission goes: missions/steps.md


async def record(app, store, cid: str, word: str, note: str | None) -> dict | None:
    """The one way a mission's status changes (its missions__update, the watch, the
    user's Stop; a user's reply resumes a stopped one inside its send, with nothing to
    follow): the row and the feed (missions.set_status_sync); done or stopped cancels what
    it scheduled; any status but active is reported in the conversation it reports to.
    -> the row, None when there is no such mission."""
    row = await store.write(lambda c: missions.set_status_sync(c, store, cid, word, note))
    if row is None or app is None:
        return row
    if word in ("done", "stopped"):
        await app.intercom.cancel_scheduled(cid)
        await missions.update(store, cid, {"wake_id": None})
    if word != "active" and row["report_to"]:
        # a report the user reads in that conversation; it starts no turn there
        from ..intercom.addresses import Refused
        said = "stopped" if word == "stopped" and note else WORDS[word]   # not by them
        try:
            await app.intercom.send(
                f"agent:{cid}", f"agent:{row['report_to']}", app.cfg.substitute(
                    f"Mission {row['name']} {said}" + (f": {note}" if note else ".")),
                kind="report", wake=False, data={"mission": cid})
        except Refused:
            pass                       # that chat is locked: it takes no report
    return await missions.get(store, cid)


async def stop(app, cid: str, note: str | None) -> None:
    """The mission stops (the user's Stop, note None, or its ceiling, the note why): its
    status, its turns and sub-agents aborted, its terminals and tabs closed."""
    await record(app, app.store, cid, "stopped", note)
    await app.dispatcher.abort_conversation(cid)
    from .. import leftovers             # no turn ran: nothing else closes its terminals
    await leftovers.on_end(app, cid)


async def report_title(store, row: dict) -> str | None:
    """The title of the conversation a mission reports in, None when it reports here."""
    if not row.get("report_to"):
        return None
    hit = await store.read(lambda c: c.execute(
        "SELECT title FROM conversations WHERE id=?", (row["report_to"],)).fetchone())
    return str(hit["title"]) if hit else None


def brief_lines(row: dict, title: str | None, tz, only: set[str] | None = None,
                now_: datetime | None = None) -> list[str]:
    """The brief, where it reports and the time limit, {{username}} unsubstituted.
    Every one with a value, or with `only` exactly those columns, an empty one as
    "none"."""
    now_ = now_ or datetime.now(timezone.utc)
    out = []
    if only is None or "brief" in only:
        out.append(f"Brief:\n{row['brief'].strip()}")
    if only is None or "report_to" in only:
        if row.get("report_to") or only is not None:
            out.append(f"Report in: {title or 'here'}.")
    if only is None or "until" in only:
        if row.get("until"):
            out.append(f"Time limit: {clock(datetime.fromisoformat(row['until']), tz, now_)}.")
        elif only is not None:
            out.append("Time limit: none.")
    return out


def head(row: dict) -> str:
    return f"Mission {row['name']}, from {{{{username}}}}."


def model_line(models: list[tuple[str, str]]) -> list[str]:
    """The models a step of its plan may name."""
    return ["Models a step can run on: " +
            ", ".join(f"{label} ({ref})" for ref, label in models) + "."] if models else []


class Missions:
    name = "missions"
    guide = guide("missions")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg
        self.tz = zone(cfg)

    app = None      # the running app (core/services.py), from skills.build

    async def offered_in(self, store, cid: str) -> bool | set[str]:
        """Everything in the mission's own conversation; elsewhere only list (a sub-agent
        working in a mission is not the mission itself)."""
        return True if store is not None and await missions.get(store, cid) is not None \
            else {"list"}

    async def prompt_fragment(self, ctx: ToolContext) -> str | None:
        """The brief and the models a step can run on, only in the mission's own
        conversation (its plan is in every turn's tail, plans/flow.block)."""
        if ctx.store is None:
            return None
        row = await missions.get(ctx.store, ctx.conversation_id)
        if row is None:
            return None
        title = await report_title(ctx.store, row)
        lines = [head(row), *brief_lines(row, title, self.tz),
                 f"Status: {WORDS[row['status']]}."]
        if not row["steps"]:
            lines.append("No plan yet.")
        lines += model_line(chat_models(getattr(self.cfg, "settings", None)))
        if row["artifact"]:
            lines.append(f"Delivered: {row['artifact']}.")
        lines.append(prompts.text(__file__, "steps.md"))
        return self.cfg.substitute("\n".join(lines))

    @tool()
    async def list(
        self,
        ctx: ToolContext,
        status: Annotated[Literal["all", "active", "needs_you", "done", "stopped"],
                          "Only the missions in this status."] = "all",
    ) -> ToolResult:
        """List the missions, newest first: name, status, the step of the plan it is on,
        and its conversation id (an intercom address)."""
        rows = await missions.list(ctx.store, None if status == "all" else status)
        if not rows:
            return ToolResult(content="No missions." if status == "all"
                              else f"No missions {WORDS[status]}.", display={"missions": 0})
        lines = []
        for r in rows[:50]:
            n = r["step"]
            where = f", step {n} of {len(r['steps'])}: {r['steps'][n - 1]['title']}" \
                if n else (f", {len(r['steps'])} steps" if r["steps"] else "")
            lines.append(f"- {r['name']} ({r['conversation_id']}): {WORDS[r['status']]}"
                         f"{where}" + (f". {r['note']}" if r.get("note") else ""))
        return ToolResult(content=self.cfg.substitute("\n".join(lines)),
                          display={"missions": len(rows)})

    @tool(read_only=False, destructive=False)
    async def update(
        self,
        ctx: ToolContext,
        status: Literal["Active", "Needs you", "Done"],
        note: Annotated[str, "One line on where you are."] = "",
        artifact: Annotated[str | None, "With Done: what you delivered, "
                                        "artifact:<id> for a page or documents:<path> "
                                        "for a file in Documents."] = None,
    ) -> ToolResult:
        """Record where the mission stands: status, a short note, and with Done the
        artifact you delivered. Your steps are plan__step's."""
        row = await missions.get(ctx.store, ctx.conversation_id)
        if row is None:
            return ToolResult(content="This conversation is not a mission.", is_error=True)
        word = STATUS.get(status)
        if word is None:
            return ToolResult(content="status is Active, Needs you or Done.", is_error=True)
        artifact = (artifact or "").strip() or None
        if artifact and not ARTIFACT.fullmatch(artifact):
            return ToolResult(content="artifact is artifact:<id> or documents:<path>.",
                              is_error=True)
        note = " ".join(note.split())
        open_ = [str(n) for n, s in enumerate(row["steps"], 1) if s["state"] != "done"]
        if word == "done" and open_ and not note:
            return ToolResult(content=f"Step {', '.join(open_)} of your plan is still open. "
                                      "Do it, or close with a note on why it stays open.",
                              is_error=True)
        if artifact:
            await missions.update(ctx.store, ctx.conversation_id, {"artifact": artifact})
        await record(self.app, ctx.store, ctx.conversation_id, word, note)
        return ToolResult(content=f"Recorded: {status.lower()}.",
                          display={"status": word, "note": note,
                                   "artifact": artifact})

    @tool(read_only=False, destructive=False)
    async def wait(
        self,
        ctx: ToolContext,
        why: Annotated[str, "What you wait for and what you do then. You read it when "
                            "you wake."],
        in_: Annotated[str | None, 'Wake in, like "20 min" or "1 h 30 min".'] = None,
        at: Annotated[str | None, 'Wake at a time, like "06:00". Tomorrow once it has '
                                  'passed.'] = None,
    ) -> ToolResult:
        """Pause the mission until a time: your turn ends here, and your next one starts
        then. A sub-agent's report, a job's end or a message wakes you earlier. A new
        wait replaces the one you set before."""
        row = await missions.get(ctx.store, ctx.conversation_id)
        if row is None:
            return ToolResult(content="This conversation is not a mission.", is_error=True)
        why = " ".join(why.split())
        if not why:
            return ToolResult(content="why says what you wait for.", is_error=True)
        now_ = datetime.now(timezone.utc)
        due = parse_when(in_, at, self.tz, now_)
        if due is None:
            return ToolResult(content='in is like "20 min" or "1 h 30 min", or give '
                                      'at="06:00".', is_error=True)
        cid = ctx.conversation_id
        for iid in await ctx.store.read(lambda c: [r[0] for r in c.execute(
                "SELECT id FROM intercom WHERE to_addr=? AND cancelled_at IS NULL AND "
                "due_at IS NOT NULL AND json_extract(data, '$.wait')",
                (f"agent:{cid}",))]):
            await self.app.intercom.cancel(iid)        # one that landed stays as it is
        await self.app.intercom.send(f"agent:{cid}", f"agent:{cid}", why, due_at=iso(due),
                                     data={"mission": cid, "wait": True})
        said = f"You wake {clock(due, self.tz, now_)}."
        if row["wake_id"] and (limit := await ctx.store.read(lambda c: c.execute(
                "SELECT due_at FROM intercom WHERE id=? AND cancelled_at IS NULL",
                (int(row["wake_id"]),)).fetchone())) and limit[0] < iso(due):
            said += (" The time limit's notice wakes you first, "
                     f"{clock(datetime.fromisoformat(limit[0]), self.tz, now_)}.")
        return ToolResult(content=said, display={"due_at": iso(due), "why": why})
