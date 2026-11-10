"""A mission: a brief from the user that it works on in a conversation of its own
(missions/store.py holds the rows, missions/api.py starts, edits and stops one). It
plans first, executes with sub-agents, reviews the result against the brief, delivers
an artifact and closes it with a note (missions/steps.md). The brief sits in its instructions there; its
plan is the conversation's (plans/: plan__set, plan__step, a step's model);
missions__update records where it stands and what it delivered. missions__list and
missions__read are offered everywhere, missions__start everywhere but in a sub-agent, the
rest only in the mission's own conversation. missions__start on a cloud model the user did not pick waits
for their approval (agent_models.ask_cloud)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from .. import prompts
from ..conversations import store as conversations
from ..core import ids
from ..core.ids import clock, iso, parse_when, when
from . import store as missions
from ..plans.flow import chat_models, step_lines
from ..skills import ToolContext, ToolResult, guide, tool, described
from ..clock import zone
from ..turns.compact import one_line

STATUS = {"Active": "active", "Needs you": "needs_you", "Done": "done"}
WORDS = {"active": "active", "needs_you": "waiting for you", "done": "done",
         "stopped": "stopped by {{username}}"}
ARTIFACT = re.compile(r"(artifact|documents):\S.*")
ASK = "start a mission on a cloud model"   # the approval card's action (agent_models.ask_cloud)
BRIEF_LINE = 120        # characters of a brief in missions__list
REPORT_LINE = 300       # characters of a sub-agent's report in missions__read
TALK_LINE = 300         # characters of a message in missions__read
TALK_SHOWN = 10         # the newest messages between a mission and the user it reads
# how a mission goes: missions/steps.md


def _talk(c, cid: str) -> list[dict]:
    """The newest TALK_SHOWN messages between mission `cid` and the user, oldest first:
    what it sent them and what they wrote to it (its brief left out), each with
    `waits` while it waits for them."""
    rows = c.execute(
        "SELECT i.id, i.from_addr, i.text, i.created_at, EXISTS (SELECT 1 FROM "
        "intercom_deliveries d WHERE d.intercom_id = i.id AND d.to_addr = 'user' AND "
        "d.state = 'pending') AS waits FROM intercom i WHERE i.cancelled_at IS NULL AND "
        "i.kind <> 'brief' AND ((i.from_addr = ? AND EXISTS (SELECT 1 FROM "
        "intercom_deliveries d WHERE d.intercom_id = i.id AND d.to_addr = 'user')) OR "
        "(i.from_addr = 'user' AND EXISTS (SELECT 1 FROM intercom_deliveries d WHERE "
        "d.intercom_id = i.id AND d.conversation_id = ?))) ORDER BY i.id DESC LIMIT ?",
        (f"agent:{cid}", cid, TALK_SHOWN)).fetchall()
    return [dict(r) for r in reversed(rows)]


async def record(app, store, cid: str, word: str, note: str | None) -> dict | None:
    """The one way a mission's status changes (its missions__update, the watch, the
    user's Stop; the user's message starts a stopped one again inside its send, with nothing to
    follow): the row and the feed (missions.set_status_sync); done or stopped cancels what
    it scheduled; any status but active is reported in the conversation it reports to, and a stop
    (the user's, or the app's at its time limit) in the conversation that started it. ->
    the row, None when there is no such mission."""
    row = await store.write(lambda c: missions.set_status_sync(c, store, cid, word, note))
    if row is None or app is None:
        return row
    if word in ("done", "stopped"):
        await app.intercom.cancel_scheduled(cid)
        await missions.update(store, cid, {"wake_id": None})
    to = row["report_to"]
    if word == "stopped" and not to and row["started_by"] not in (None, "user"):
        to = row["started_by"]          # a stop: the chat that started it hears
    if word != "active" and to:
        # a report the user reads in that conversation; it starts no turn there
        from ..intercom.addresses import Refused
        said = "stopped" if word == "stopped" and note else WORDS[word]   # not by them
        try:
            await app.intercom.send(
                f"agent:{cid}", f"agent:{to}", app.cfg.substitute(
                    f"Mission {row['name']} {said}" + (f": {note}" if note else ".")),
                kind="report", wake=False, data={"mission": cid})
        except Refused:
            pass                       # that chat is locked: it takes no report
    return await missions.get(store, cid)


async def stop(app, cid: str, note: str | None, word: str = "stopped") -> None:
    """The mission ends from outside: stopped (the user's Stop, note None, or its
    ceiling, the note why) or done (the user's Mark as done). Its status, then its
    turns and sub-agents aborted, then what it asked the user read, so nothing of it
    waits for them; its terminals and tabs closed, and the missions started under it
    stopped (`stop_started`)."""
    from ..core.event_types import INTERCOM_STATE
    from ..intercom import store as intercom_rows
    await record(app, app.store, cid, word, note)
    await app.dispatcher.abort_conversation(cid)

    def read(c) -> None:
        for iid in intercom_rows.read_from(c, f"agent:{cid}"):
            app.store.global_event(c, INTERCOM_STATE, {"intercom_id": iid,
                                                       "conversation_id": None})
    await app.store.write(read)
    from .. import leftovers             # no turn ran: nothing else closes its terminals
    await leftovers.on_end(app, cid)
    await stop_started(app, cid)


async def stop_started(app, cid: str) -> None:
    """Every open mission that `cid` or a sub-agent of it started stops as the user's
    Stop does (no note, so the one Stop raises one notice), and so on down."""
    def fn(c) -> list[str]:
        family = conversations.family(c, cid)
        return [r[0] for r in c.execute(
            "SELECT conversation_id FROM missions WHERE status IN ('active', 'needs_you') "
            f"AND started_by IN ({','.join('?' * len(family))})", family)]
    for started in await app.store.read(fn):
        await stop(app, started, None)


async def end_under(app, cid: str) -> None:
    """Mission `cid` is done: what still runs under it ends, its sub-agents' turns and
    the missions started under it. Its own turn, which said so, runs on."""
    subs = await app.store.read(lambda c: [r[0] for r in c.execute(
        "SELECT id FROM conversations WHERE agent_of=?", (cid,))])
    for sub in subs:
        await app.intercom.cancel_scheduled(sub)
        await app.dispatcher.abort_conversation(sub)
    await stop_started(app, cid)


async def report_title(store, row: dict) -> str | None:
    """The title of the conversation a mission reports in, None when it reports here."""
    if not row.get("report_to"):
        return None
    hit = await store.read(lambda c: c.execute(
        "SELECT title FROM conversations WHERE id=?", (row["report_to"],)).fetchone())
    return str(hit["title"]) if hit else None


def brief_lines(row: dict, title: str | None, tz, only: set[str] | None = None,
                now_: datetime | None = None) -> list[str]:
    """The brief, where it reports, the time limit and the minimum time, {{username}}
    unsubstituted.
    Every one with a value, or with `only` exactly those columns, an empty one as
    "none"."""
    now_ = now_ or datetime.now(timezone.utc)
    out = []
    if only is None or "brief" in only:
        out.append(f"Instructions:\n{row['brief'].strip()}")
    if only is None or "report_to" in only:
        if row.get("report_to") or only is not None:
            out.append(f"Result goes to: {title or 'this mission'}.")
    if only is None or "until" in only:
        if row.get("until"):
            out.append(f"Time limit: {clock(datetime.fromisoformat(row['until']), tz, now_)}.")
        elif only is not None:
            out.append("Time limit: none.")
    if only is None or "min_until" in only:
        if row.get("min_until"):
            out.append("Minimum time: until "
                       f"{clock(datetime.fromisoformat(row['min_until']), tz, now_)}.")
        elif only is not None:
            out.append("Minimum time: none.")
    return out


def head(row: dict) -> str:
    by = "{{name}}" if row.get("started_by") else "{{username}}"
    return f"Mission {row['name']}, from {by}."


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
        """Everything in the mission's own conversation; elsewhere list, read and start,
        in a sub-agent list and read (a sub-agent working in a mission is not the
        mission itself)."""
        if store is None:
            return {"list", "read", "start"}
        if await missions.get(store, cid) is not None:
            return True
        sub = await store.read(lambda c: c.execute(
            "SELECT agent_of FROM conversations WHERE id=?", (cid,)).fetchone())
        # sub-agents start no missions, only the agent does
        return {"list", "read"} if sub and sub[0] else {"list", "read", "start"}

    async def prompt_fragment(self, ctx: ToolContext) -> str | None:
        """The brief, where it reports and the models a step can run on, only in the
        mission's own conversation. Its state goes in the tail (tail_part)."""
        if ctx.store is None:
            return None
        row = await missions.get(ctx.store, ctx.conversation_id)
        if row is None:
            return None
        title = await report_title(ctx.store, row)
        lines = [head(row), *brief_lines(row, title, self.tz, only={"brief"})]
        if row.get("report_to"):
            lines += brief_lines(row, title, self.tz, only={"report_to"})
        lines += model_line(chat_models(getattr(self.cfg, "settings", None)))
        lines.append(prompts.text(__file__, "steps.md"))
        return self.cfg.substitute("\n".join(lines))

    async def tail_part(self, ctx: ToolContext) -> str | None:
        """The mission's state now, for the tail of a turn in its own conversation: its
        time limit and minimum time, its status, whether it has a plan, what it
        delivered (its plan is in the tail too, plans/flow.block)."""
        if ctx.store is None:
            return None
        row = await missions.get(ctx.store, ctx.conversation_id)
        if row is None:
            return None
        lines = [*brief_lines(row, None, self.tz)[1:], f"Status: {WORDS[row['status']]}."]
        lines = [x for x in lines if not x.startswith("Result goes to:")]
        if not row["steps"]:
            lines.append("No plan yet.")
        if row["artifact"]:
            lines.append(f"Delivered: {row['artifact']}.")
        return self.cfg.substitute("\n".join(lines))

    @tool()
    async def list(
        self,
        ctx: ToolContext,
        status: Annotated[Literal["all", "active", "needs_you", "done", "stopped"],
                          "Only the missions in this status."] = "all",
    ) -> ToolResult:
        """List the missions, newest first: name, status, the step of the plan it is on,
        its conversation id (an intercom address) and the first line of its brief.
        missions__read gives one whole."""
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
                         f"{where}" + (f". {r['note']}" if r.get("note") else "") +
                         f"\n  brief: {one_line(r['brief'], BRIEF_LINE)}")
        return ToolResult(content=self.cfg.substitute("\n".join(lines)),
                          display={"missions": len(rows)})

    @tool()
    async def read(
        self,
        ctx: ToolContext,
        mission: Annotated[str, "Its name, as missions__list gives it, or its "
                                "conversation id."],
    ) -> ToolResult:
        """Read one mission, any status, from anywhere.
        Returns: its status and note, the brief, where it reports, its time limits,
        what it delivered, its plan with each step's state, its sub-agents with their
        latest report, and the newest messages between it and {{username}} (what it
        asked, what they answered)."""
        from ..sub_agents.skill import agents_of
        store = ctx.store

        def fn(c):
            found = missions.find_sync(c, mission)
            if len(found) != 1:
                return found, None, None, None
            row = found[0]
            cid = row["conversation_id"]
            where = c.execute("SELECT title FROM conversations WHERE id=?",
                              (row["report_to"],)).fetchone() if row["report_to"] else None
            return found, where, agents_of(c, cid, own=store.tool_words), _talk(c, cid)
        found, where, subs, talk = await store.read(fn)
        if len(found) != 1:
            what = "matches more than one mission" if found else "is no mission"
            return ToolResult(content=f"{mission!r} {what}. missions__list gives the names.",
                              is_error=True)
        row = found[0]
        cid, tz = row["conversation_id"], self.tz
        lines = [f"{head(row)[:-1]} ({cid}): {WORDS[row['status']]}" +
                 (f". {row['note']}" if row.get("note") else "."),
                 *[x for x in brief_lines(row, None, tz) if not x.startswith("Result goes to:")],
                 f"Reports in: \"{where['title'] or 'Untitled'}\" ({row['report_to']})."
                 if where else "Reports to {{username}}."]
        if row["artifact"]:
            lines.append(f"Delivered: {row['artifact']}.")
        lines.append("Plan:" if row["steps"] else "No plan yet.")
        lines += step_lines(row["steps"])
        if subs:
            lines.append("Sub-agents:")
            for a in subs:
                said = f"report #{a['report']['id']}: {one_line(a['report']['text'], REPORT_LINE)}" \
                    if a["report"] else f"last words: {one_line(a['last'], REPORT_LINE)}" \
                    if a["last"] else "no report yet"
                lines.append(f"- {a['name']} ({a['state']}, conversation {a['id'][:8]}): {said}")
        if talk:
            lines.append("With {{username}}, newest last:")
            for m in talk:
                by = "{{username}}" if m["from_addr"] == "user" else "you"
                waits = " (waits for {{username}})" if m["waits"] else ""
                lines.append(f"- #{m['id']} {when(m['created_at'], tz)} {by}: "
                             f"{one_line(m['text'], TALK_LINE)}{waits}")
        lines.append(f"Its conversation: past_conversations__read(conversation=\"{cid[:8]}\").")
        return ToolResult(content=self.cfg.substitute("\n".join(lines)),
                          display={"mission": cid, "name": row["name"]})

    @tool(read_only=False, destructive=False)
    async def start(
        self,
        ctx: ToolContext,
        brief: Annotated[str, "What the mission is to do and deliver."],
        name: Annotated[str | None, "Its name; without one it is named from the "
                                    "brief."] = None,
        model: Annotated[str | None, "The model it runs on, a ref from models__list; "
                                     "without one the default."] = None,
        time_limit: Annotated[str | None, 'When it is due, like "2 h" or "08:00".'] = None,
        min_time: Annotated[str | None, 'How long it runs at least, like "2 h" or '
                                        '"08:00".'] = None,
        check_in: Annotated[str | None, 'How often a line on where it stands goes where it '
                                        'reports, like "30 min".'] = None,
        report_to: Annotated[str | None, "The conversation id it reports in; without one "
                                         "its reports go to {{username}}."] = None,
    ) -> ToolResult:
        """Start a mission: it works on the brief in a conversation of its own, with its
        own plan and sub-agents, until it is done.
        Returns: its name and its conversation id (an intercom address)."""
        from ..errors import CelineError
        from ..settings import agent_models
        from .api import MissionIn, start, title_of
        if no := await agent_models.ask_cloud(
                self.cfg, self.app, ctx, model, action=ASK,
                facts=[("Mission", " ".join((name or "").split()) or title_of(brief))],
                detail=("Instructions", brief)):
            return ToolResult(content=no, is_error=True)
        body = MissionIn(brief=brief, name=name, model=model, time_limit=time_limit,
                         min_time=min_time, check_in=check_in, report_to=report_to)
        try:
            cid = await start(self.app, body, by=ctx.conversation_id)
        except CelineError as e:
            return ToolResult(content=e.message, is_error=True)
        row = await missions.get(ctx.store, cid)
        return ToolResult(content=f"Started the mission {row['name']} ({cid}).",
                          display={"mission": cid, "name": row["name"]})

    @tool(read_only=False, destructive=False)
    async def update(
        self,
        ctx: ToolContext,
        status: Literal["Active", "Needs you", "Done"],
        note: Annotated[str, "One line on where you are."] = "",
        artifact: Annotated[str | None, "With Done: what you delivered, "
                                        "artifact:<id> for a page or documents:<path> "
                                        "for a file in Documents."] = None,
        force: Annotated[bool, "With Done: close it before its minimum time."] = False,
    ) -> ToolResult:
        """Record where the mission stands: status, a short note, and with Done the
        artifact you delivered. Your steps are plan__step's. Done before the mission's
        minimum time takes force=true."""
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
        now_ = datetime.fromisoformat(ids.now())
        if word == "done" and not force and row["min_until"] and \
                (least := datetime.fromisoformat(row["min_until"])) > now_:
            from ..events import _took
            return ToolResult(
                content=f"The minimum time is until {clock(least, self.tz, now_)}, "
                        f"{_took(iso(now_), row['min_until'])} from now. Done before it "
                        "takes force=true.", is_error=True)
        if artifact:
            await missions.update(ctx.store, ctx.conversation_id, {"artifact": artifact})
        await record(self.app, ctx.store, ctx.conversation_id, word, note)
        if word == "done" and self.app is not None:
            await end_under(self.app, ctx.conversation_id)
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
