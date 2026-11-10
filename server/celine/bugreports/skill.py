"""The agent's tools for app problems, in its own conversations (a chat, a mission, a
routine's run): bugreport__file, any time, and bugreport__list, its reports with their
state and the last word on each (guides/bugreport.md). A sub-agent keeps to its brief and
is offered neither: what blocks it goes in its report to its parent.
The rows are bugreports/store.py's; each opens its Intercom thread to the user at once,
and Events lists them."""
from __future__ import annotations

from typing import Annotated, Literal

from ..skills import ToolContext, ToolResult, guide, tool, described
from . import store as reports

LISTED = 10         # its other open reports the result names, the newest
ANSWER_CHARS = 300  # of the last word on a report, in the list


def closed_line(rid: int, why: str) -> str:
    """The app's line in a report's thread when the user closes it with a reason."""
    return f"Bug #{rid} closed: {why}"


class BugReport:
    name = "bugreport"
    guide = guide("bugreport")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg

    app = None

    async def offered_in(self, store, cid: str) -> bool:
        """In the agent's own conversations; in a sub-agent's, nothing."""
        return store is None or not await store.read(lambda c: (c.execute(
            "SELECT agent_of FROM conversations WHERE id=?", (cid,)).fetchone() or [None])[0])

    @tool(read_only=False, destructive=False)
    async def file(
        self,
        ctx: ToolContext,
        title: Annotated[str, "The problem in a few words, like: past_conversations__read "
                              "cuts long tool results."],
        severity: Annotated[Literal["high", "medium", "low"],
                            "high: it stops your work. medium: you work around it. low: "
                            "a small thing."],
        what: Annotated[str, "What happened and what you expected instead, with the error "
                             "text if there was one."],
        where: Annotated[str, "Where you saw it: the tool, the routine or the conversation "
                              "and message number."] = "",
        wish: Annotated[str, "What would fix it or what you want instead."] = "",
    ) -> ToolResult:
        """File a problem with the app itself for {{username}} to review later: a tool
        that fails or misleads, a limit that is wrong, something you miss. One report
        per problem. It goes to {{username}} at once as an intercom message from this
        conversation; their reply comes here.
        Returns: its number and your other open reports."""
        title, what = " ".join(title.split()), what.strip()
        if not title or not what:
            return ToolResult(content="Nothing filed: a report needs a title and what "
                                      "happened.", is_error=True)
        if severity not in reports.SEVERITIES:
            return ToolResult(content="Nothing filed: severity is high, medium or low.",
                              is_error=True)
        where, wish = where.strip(), wish.strip()
        if len(title) > reports.TITLE_MAX:
            return ToolResult(content=f"Nothing filed: the title is at most "
                                      f"{reports.TITLE_MAX} characters.", is_error=True)
        if max(len(what), len(where), len(wish)) > reports.TEXT_MAX:
            return ToolResult(content=f"Nothing filed: what, where and wish are at most "
                                      f"{reports.TEXT_MAX:,} characters each.", is_error=True)
        row = await ctx.store.write(lambda c: reports.file(
            c, ctx.store, ctx.conversation_id, title, severity, what, where, wish))
        others = [r for r in await ctx.store.read(lambda c: reports.listing(c, "open"))
                  if r["id"] != row["id"]]
        lines = [f"Filed bug report #{row['id']} ({severity}): {row['title']}. It went to "
                 f"{self.cfg.user} as message #{row['thread']}."]
        if others:
            lines.append("Your other open reports: " + "; ".join(
                f"#{r['id']} {r['title']}" for r in others[:LISTED])
                + (f"; and {len(others) - LISTED} more" if len(others) > LISTED else "")
                + ".")
        return ToolResult(content="\n".join(lines),
                          display={"bugreport": {"id": row["id"], "title": row["title"],
                                                 "severity": severity}})

    @tool(read_only=True, destructive=False)
    async def list(
        self,
        ctx: ToolContext,
        state: Annotated[Literal["open", "closed", "all"],
                         "open: what {{username}} has not closed. closed: what they closed, "
                         "with why. all: both."] = "open",
    ) -> ToolResult:
        """Your bug reports with their state and the last word on each from {{username}}
        or the app. Read it before filing: a problem that is listed is known, and a
        closed one with a reason is done.
        Returns: one line per report, newest first."""
        want = None if state == "all" else state
        def fn(c):
            return [(r, reports.answer(c, r["thread"])) for r in reports.listing(c, want)]
        rows = await ctx.store.read(fn)
        if not rows:
            return ToolResult(content=f"No {'' if state == 'all' else state + ' '}reports.")
        lines = []
        for r, a in rows:
            line = f"#{r['id']} {r['state']}, {r['severity']}: {r['title']} ({r['created_at'][:10]})"
            if a:
                who = self.cfg.user if a["from"] == "user" else "the app"
                said = " ".join(a["text"].split())
                if len(said) > ANSWER_CHARS:
                    said = said[:ANSWER_CHARS].rstrip() + "..."
                line += f"\n  {who}, {a['created_at'][:10]}: {said}"
            lines.append(line)
        return ToolResult(content="\n".join(lines))
