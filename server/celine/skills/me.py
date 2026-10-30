"""The agent's self: `me__read` shows its persona, its
talk and its slop list (its row, celine/her.py); `me__edit` changes its persona, its
talk and its slop list (banned words, phrases, patterns). They are in every chat, so
every change waits for the user's approval first (celine/approvals.py, kind `her`),
whatever the guard is. Every change goes through
her.write_sync as written by it, so it is in the text's History (her:persona, her:talk,
her:voice; Settings, It) and in Events (the `her` source, events.py).
`me__whoami` says where it is, every line read when it is called (whoami_lines)."""
from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from .. import her
from ..settings.files import Invalid
from . import ToolContext, ToolResult, guide, tool, described
from ..core.event_types import HER_CHANGED

LISTS = {"banned": "banned", "phrases": "phrases", "patterns": "patterns"}
# what an edit changes, as Settings and Events name it (the approval's words)
NAMES = {"persona": "Persona", "talk": "Example lines", "banned": "Banned words",
         "phrases": "Banned words", "patterns": "Banned words"}
ASK = "change the {name}"
WHY = "It changes how {{name}} comes across in every chat from now on."
NOBODY = "Nothing was changed: nobody is here to approve it."
FACT = 200          # characters of one argument in the request


def _numbered(items: list[str]) -> str:
    return "\n".join(f"- {x!r}" if x != x.strip() else f"- {x}" for x in items) or "(none)"


def read_text(cfg) -> str:
    """The agent's persona, talk and slop list as it reads them."""
    h = cfg.her
    v = h.voice
    return "\n\n".join([
        "## persona (who you are; yours to change, and you tell {{username}} when you do)\n"
        + her.shown(cfg, h).strip(),
        "## talk (yours: how you talk, in every chat)\n" + h.talk_text,
        f"## slop list (yours; {'on' if v['on'] else 'off'})\n"
        f"banned words, steered away from as you write ({len(v['banned'])}):\n"
        + _numbered(v["banned"])
        + f"\n\nphrases, found in what you write ({len(v['phrases'])}):\n"
        + _numbered(v["phrases"])
        + f"\n\npatterns, regexes found in what you write ({len(v['patterns'])}):\n"
        + _numbered(v["patterns"])])


_PLACEHOLDER = re.compile(r"\{\{\w+\}\}")


def replaced(cfg, kept: str, old: str, new: str) -> str:
    """`kept` with `old`, found once in it as it reads it (cfg.substitute), replaced by
    `new`. A placeholder wholly outside `old` stays as typed, one inside goes with it,
    one cut by its edge keeps the part outside as it reads."""
    pieces, at = [], 0                  # (as kept, as it reads)
    for m in _PLACEHOLDER.finditer(kept):
        pieces += [(kept[at:m.start()],) * 2, (m.group(), cfg.substitute(m.group()))]
        at = m.end()
    pieces.append((kept[at:],) * 2)
    start = "".join(r for _, r in pieces).index(old)
    end = start + len(old)
    head, tail, pos = "", "", 0
    for k, r in pieces:
        a, b = pos, pos + len(r)
        pos = b
        if b <= start:
            head += k
        elif a >= end:
            tail += k
        else:
            head += r[:max(0, start - a)]
            tail += r[max(0, end - a):] if b > end else ""
    return head + new + tail


def edited(cfg, what: str, old: str, new: str, add: list[str],
           remove: list[str]) -> tuple[dict, str] | str:
    """(the body for her.write_sync, what changed in words), or the sentence why not.
    The persona and the talk are matched as it reads them, the placeholders filled in,
    and changed in the text as kept: what it writes is stored as written, and a
    placeholder outside the change stays one."""
    h = cfg.her
    if what in ("persona", "talk"):
        text = h.talk_text if what == "talk" else h.persona
        if add or remove:
            return f"For the {what}, use old and new, not add or remove."
        if not old:
            if not new.strip():
                return f"new is the text to add at the end of your {what}."
            text = text.rstrip() + "\n\n" + new.strip() + "\n"
            said = f"Added to your {what}."
        elif (n := cfg.substitute(text).count(old)) != 1:
            return (f"old is in your {what} {n} times; it has to be there once, word for "
                    f"word, as me__read shows it.")
        else:
            text, said = replaced(cfg, text, old, new), f"Changed your {what}."
        if what == "persona":
            said += f" Tell {cfg.user} in one line."
        return {what: text}, said
    if old or new:
        return f"For {what}, use add and remove, not old and new."
    items = list(h.voice[LISTS[what]])
    missing = [x for x in remove if x not in items]
    if missing:
        return f"Not in your {what}: {', '.join(repr(x) for x in missing)}."
    items = [x for x in items if x not in remove]
    items += [x for x in add if x not in items]
    if not add and not remove:
        return "add or remove names the entries to change."
    return ({"voice": {**h.voice, LISTS[what]: items}},
            f"Your {what}: {len(add)} added, {len(remove)} removed, {len(items)} now.")


def feature_words(p: dict) -> str:
    """A feature's probe as she reads it: the probe's own words for her (`why`), its
    detail while it is ready, else its state; a page's detail tells the user what to
    type, and that is not hers."""
    if p.get("why"):
        return p["why"]
    if p["state"] == "ready":
        return p["detail"] or "ready."
    return "not set up." if p["state"] == "unset" else "down."


async def whoami_lines(app, cfg, ctx: ToolContext) -> list[str]:
    """Where it is, one fact per line, from what the app reads for the UI: the turn's
    running event (backends/llm.py) and the router for its model and the chain, the
    newest usage event for the window, the conversation row (conversations/here.py),
    the features' probes and the MCP supervisor for what answers, the guard switch and
    the hold."""
    from .. import hold
    from ..conversations import here
    from ..guard import switch
    from ..settings import features
    from ..turns import store as turns
    from ..turns.prompt import model_facts
    store, cid, tid = ctx.store, ctx.conversation_id, ctx.turn_id

    def facts(c):
        turn = c.execute("SELECT origin FROM turns WHERE id=?", (tid,)).fetchone()
        ev = c.execute(
            "SELECT data FROM events WHERE conversation_id=? AND type='llm.queue' AND "
            "json_extract(data, '$.turn_id')=? AND json_extract(data, '$.state')='running' "
            "ORDER BY seq DESC LIMIT 1", (cid, tid)).fetchone()
        return turn, json.loads(ev["data"]) if ev else None, here.where(c, cid)
    turn, running, where = await store.read(facts)
    llm = getattr(app, "llm", None)
    lines = []
    if running and llm is not None:
        ref, role = running["backend"], running["role"]
        line = f"Model: {ref} for {role}, "
        if running.get("fallback"):
            ordered = await llm.router.order(role, conversation_id=cid)
            line += "a fallback. " + (ordered.passed_over(ref) or
                                      f"{running.get('instead_of')}: {running.get('why')}")
        else:
            line += "the first choice."
        lines.append(" ".join([line, *model_facts(llm.make(ref))]))
    usage = await turns.last_usage(store, cid)
    if usage and usage.get("window"):
        used = usage.get("prompt_tokens") or usage.get("estimate") or 0
        lines.append(f"Context: {used:,} of {usage['window']:,} tokens.")
    if where:
        lines.append(where)
    if turn:
        lines.append("This turn: " + ("{{username}}'s message." if turn["origin"] == "user"
                                      else "a message through the intercom."))
    probes = await features.probe_all(cfg, app)
    for f in features.catalog():
        p = probes.get(f.id)
        if p and p["state"] != "none" and features.on(cfg, f.id) and f.id != "guard":
            lines.append(f"{f.label}: {feature_words(p)}")
    mcp = getattr(app, "mcp", None)
    owned = features.connector_owners()        # a probed feature's line says it already
    for s in (mcp.skills.values() if mcp is not None else ()):
        if s.name not in owned or not features.get(owned[s.name]).probe:
            lines.append(f"{s.name} (MCP): {s.status}" + (f", {s.error}" if s.error else ""))
    guard_probe = probes.get("guard") or {}
    lines.append(f"Guard: {switch.mode(cfg, cid)}." + (
        f" {guard_probe['detail']}" if guard_probe.get("state") == "ready" else ""))
    if held := await hold.her_hold(store, llm):
        lines.append(f"House: {held}.")
    return [cfg.substitute(x) for x in lines]


class Me:
    name = "me"
    guide = guide("me")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg
        self.app = None

    @tool()
    async def read(self, ctx: ToolContext) -> ToolResult:
        """Read all of yourself as the app keeps it: your persona (who you are), your
        talk (how you talk in every chat) and your slop list (banned words, phrases,
        patterns). Returns: the three, as they are now."""
        return ToolResult(content=self.cfg.substitute(read_text(self.cfg)))

    @tool()
    async def whoami(self, ctx: ToolContext) -> ToolResult:
        """Where you are right now, every line read as you call it: the model and
        provider answering this turn, for which role, the first choice or a fallback
        and why, its context window, whether it sees images and calls tools; the
        context used of its window; the conversation you work in and
        who started this turn; which devices and servers answer, with the reason when
        one is down; the guard's mode; a freeze or pause when there is one. For when
        you are lost. Returns: one fact per line."""
        return ToolResult(content="\n".join(await whoami_lines(self.app, self.cfg, ctx)))

    @tool(read_only=False, destructive=False)
    async def edit(
        self,
        ctx: ToolContext,
        what: Annotated[Literal["persona", "talk", "banned", "phrases", "patterns"],
                        "persona: who you are. talk: how you talk. banned: words kept out "
                        "of what you write, "
                        "one word each, with a leading space when it starts a word "
                        "(\" delve\"). phrases: plain text kept out of what you write. "
                        "patterns: regexes found in what you write."],
        old: Annotated[str, "persona, talk: the exact text to replace, once in the "
                            "text; empty adds new at the end."] = "",
        new: Annotated[str, "persona, talk: the text in its place (empty deletes old), "
                            "or the text to add."] = "",
        add: Annotated[list[str], "banned, phrases, patterns: entries to add."] = [],  # noqa: B006
        remove: Annotated[list[str], "banned, phrases, patterns: entries to "
                                     "remove."] = [],  # noqa: B006
    ) -> ToolResult:
        """Change your persona, your talk or your slop list. Every change waits until
        {{username}} approves it; denied or unanswered, nothing changes. Every change is
        kept in its History, and {{username}} sees it in Settings and in Events; when you
        change your persona, tell {{username}} in one line. Example:
        me__edit(what="phrases", add=["you were right"]), or me__edit(what="talk",
        old="short.", new="short, lowercase.").
        Returns: what changed."""
        from .. import events
        got = edited(self.cfg, what, old, new, list(add or []), list(remove or []))
        if isinstance(got, str):
            return ToolResult(content=got, is_error=True)
        body, said = got
        store, cfg = ctx.store, self.cfg
        if no := await self._approved(ctx, what, old, new, list(add or []),
                                      list(remove or [])):
            return ToolResult(content=no, is_error=True)

        def fn(c) -> None:
            her.write_sync(c, cfg, body, "her")
            events.added(store, c, "her")
        try:
            await store.write(fn)
        except Invalid as e:
            return ToolResult(content=str(e), is_error=True)
        await her.refresh(store, cfg)
        store.emit_global({"type": HER_CHANGED})
        return ToolResult(content=said, display={"what": what})

    async def _approved(self, ctx: ToolContext, what: str, old: str, new: str,
                        add: list[str], remove: list[str]) -> str | None:
        """The user's answer to this edit (celine/approvals.py): None when approved, else
        the sentence for its result. Its words are fixed and the call's own data."""
        approvals = getattr(self.app, "approvals", None)
        if approvals is None:
            return NOBODY
        facts = [f"{k}: {v[:FACT]}" for k, v in (("old", old), ("new", new)) if v] + \
            [f"{k}: {', '.join(v)[:FACT]}" for k, v in (("add", add), ("remove", remove))
             if v]
        action = ASK.format(name=NAMES[what])
        return await approvals.ask(action, facts, ctx.conversation_id,
                                   self.cfg.substitute(WHY), action, kind="her",
                                   call=ctx.tool_call_id or None)
