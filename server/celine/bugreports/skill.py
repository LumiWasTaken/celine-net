"""The agent's tool for app problems: bugreport__file, any time, anywhere (guides/bugreport.md).
The rows are bugreports/store.py's; each opens its Intercom thread to the user at once,
and Events lists them."""
from __future__ import annotations

from typing import Annotated, Literal

from ..skills import ToolContext, ToolResult, guide, tool, described
from . import store as reports

LISTED = 10         # its other open reports the result names, the newest


class BugReport:
    name = "bugreport"
    guide = guide("bugreport")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg

    app = None

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
