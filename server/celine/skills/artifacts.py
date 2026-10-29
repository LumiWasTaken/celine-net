"""The agent's artifacts: pages it makes for the user (server/celine/artifacts). A page is one
self-contained HTML file, a document one markdown text; every write adds a version,
nothing is replaced. The user opens
it beside the chat or as a page of its own, where it runs sandboxed (artifacts/page.py).

Every word it reads is composed here, next to the guide (guides/artifacts.md)."""
from __future__ import annotations

from typing import Annotated, Literal

from ..artifacts import lint
from ..artifacts import store as artifacts
from . import ToolContext, ToolResult, guide, tool, described
from ..core.event_types import ARTIFACT_UPDATED


class Artifacts:
    name = "artifacts"
    guide = guide("artifacts")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg

    @tool(read_only=False, destructive=False)
    async def write(
        self,
        ctx: ToolContext,
        title: Annotated[str, "What it is called, like Budget 2026. A new artifact needs one; "
                              "with an id, empty keeps the title."],
        content: Annotated[str, "The whole page: HTML with its style and script inline. For "
                                "markdown, the whole document."],
        id: Annotated[str, "Empty: a new artifact. An artifact's id, like 7f3a9c: a new "
                           "version of it with this whole content."] = "",
        note: Annotated[str, "What changed, in a few words, for the version list."] = "",
        kind: Annotated[Literal["html", "markdown"], "html: a page that runs. markdown: a "
                                                     "document to read."] = "html",
    ) -> ToolResult:
        """Make a page {{username}} opens beside the chat: an app, a puzzle, a chart, a table,
        a dashboard, or a document. Every write keeps the old versions.
        Returns: the id and version. Show it with [[artifact:<id>]] in your reply, never the
        page's text."""
        aid = id
        if kind not in artifacts.KINDS:
            return _refused(f"kind is html or markdown, not \"{kind}\".")
        if problem := _refusal(content, title, aid):
            return _refused(problem)
        if not aid and (same := await artifacts.find(ctx.store, ctx.conversation_id, title)):
            return _refused(f"this conversation already has \"{title}\" as artifact {same}. "
                            f"To change it, call artifacts__write with id=\"{same}\". For a "
                            f"second page, give it another title.")
        try:
            v = await artifacts.add_version(ctx.store, aid or None, kind, title, content,
                                            note, ctx.conversation_id,
                                            ctx.tool_call_id)
        except artifacts.Refused as e:
            return _refused(f"There is no artifact {aid}. Leave id empty for a new one."
                            if e.code == "unknown" else
                            f"artifact {aid} is not {kind}; a version keeps the artifact's "
                            f"kind. For {kind}, leave id empty and give a new title.")
        await ctx.store.append_event(ctx.conversation_id, ARTIFACT_UPDATED,
                                     {"id": v["id"], "n": v["n"]})
        lines = [f"Wrote \"{v['title']}\" as artifact {v['id']}, version {v['n']}, "
                 f"{v['bytes']:,} bytes. Show it with [[artifact:{v['id']}]]."]
        if kind == "html":
            lines += [f"hint: {h}" for h in lint.hints(content)]
        return ToolResult(content="\n".join(lines), display={"artifact": _card(v)})


    @tool()
    async def list(
        self,
        ctx: ToolContext,
        where: Annotated[Literal["here", "all"], "here: this conversation's pages. all: "
                                                "every page you made."] = "all",
    ) -> ToolResult:
        """List your pages, newest change first: id, title, kind, version and when."""
        rows = await artifacts.listing(ctx.store, ctx.conversation_id if where == "here"
                                       else None, False)
        if not rows:
            return ToolResult(content="No artifacts" + (" here." if where == "here" else "."),
                              display={"artifacts": 0})
        lines = [f"- {r['id']}: \"{r['title']}\" ({r['kind']}, version {r['n']}, "
                 f"{r['updated_at'][:16].replace('T', ' ')} UTC)" for r in rows[:LIST_MAX]]
        if len(rows) > LIST_MAX:
            lines.append(f"{len(rows) - LIST_MAX} older ones not listed.")
        return ToolResult(content="\n".join(lines), display={"artifacts": len(rows)})

    @tool()
    async def read(
        self,
        ctx: ToolContext,
        id: Annotated[str, "The artifact's id, like 7f3a9c."],
        version: Annotated[int | None, "A version number; empty: the newest."] = None,
    ) -> ToolResult:
        """Read a page whole: its title, its versions and the content of one version."""
        aid = id
        row = await artifacts.get(ctx.store, aid) if artifacts.ID.match(aid) else None
        v = await artifacts.version(ctx.store, aid, version) if row else None
        if v is None:
            return ToolResult(content=f"There is no artifact {aid}" + (
                f" with a version {version}." if row else "."), is_error=True)
        head = [f"\"{v['title']}\" ({v['kind']}), version {v['n']} of {row['n']}" +
                (f": {v['note']}" if v["note"] else "."),
                "Versions: " + ", ".join(f"{x['n']} ({x['created_at'][:10]})"
                                         for x in row["versions"]) + "."]
        if row["trashed_at"]:
            head.append("It is in the Trash.")
        return ToolResult(content="\n".join(head) + "\n\n" + v["content"],
                          display={"artifact": _card({**v, "id": aid})})


LIST_MAX = 50


def _refusal(content: str, title: str, aid: str) -> str | None:
    """What makes a write impossible, in its words, or None."""
    if not content.strip():
        return "content is empty."
    if aid and not artifacts.ID.match(aid):
        return f"\"{aid}\" is no artifact id: an id is 6 letters and digits, like 7f3a9c."
    if not aid and not title.strip():
        return "a new artifact needs a title."
    try:
        size = len(content.encode())
    except UnicodeEncodeError as e:
        return _broken(content, e.start)
    if "�" in content:
        return _broken(content, content.index("�"))
    if size > artifacts.MAX_BYTES:
        return (f"the page is {size:,} bytes; an artifact holds at most "
                f"{artifacts.MAX_BYTES:,}.")
    return None


def _broken(content: str, at: int) -> str:
    line = content.count("\n", 0, at) + 1
    column = at - (content.rfind("\n", 0, at) + 1) + 1
    return (f"line {line}, column {column} holds a broken character (half of a pair from "
            f"a broken escape, or U+FFFD). Write that character again.")


def _refused(text: str) -> ToolResult:
    return ToolResult(content=f"Nothing written: {text}", is_error=True)


def _card(v: dict) -> dict:
    return {"id": v["id"], "n": v["n"], "title": v["title"], "kind": v["kind"]}
