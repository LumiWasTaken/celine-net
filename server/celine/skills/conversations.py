"""The agent's own past conversations in this app: search them, list the newest or a day of
them (of one kind or all), read one (by id, run:<id>, a mission's or a trigger's name),
read one tool call raw. A whole day fits its context this way: the day is an index
first (one entry per conversation), a read is a slice of one conversation with each
tool call one line, and a raw call is read only when it matters."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
import datetime as dt
from typing import Annotated, Literal

from ..conversations import store as conversations, tree
from ..core.ids import iso
from ..conversations.store import locked_sql
from ..turns.compact import one_line
from . import ToolContext, ToolResult, guide, knob, parts, tool, described
from ..clock import zone

# Settings, Advanced, read at use (cfg.r)
KNOBS = (
    knob("past_conversations.max_results", "Matches per search", None, 1, 1000, 20,
         "The most matches one search of past conversations brings."),
    knob("past_conversations.snippet_chars", "Snippet length", "characters", 40, 10_000, 240,
         "How much text each match shows around the words found."),
)
READ_TOKENS = 8000          # one read, before the result cap
LINE = 120                  # characters of a first or last line in the day's index
LISTED = 50                 # conversations past_conversations__list gives at most

# a conversation's kind as the agent reads it: chat, mission, run (a Routine's), task (a
# background task, a trigger's), sub-agent
KIND_SQL = ("(CASE WHEN c.agent_of IS NOT NULL THEN 'sub-agent' WHEN c.kind = 'background' "
            "THEN CASE WHEN c.trigger_key LIKE 'run:%' THEN 'run' ELSE 'task' END "
            "ELSE c.kind END)")
Kind = Literal["chat", "mission", "run", "task", "sub-agent"]
KIND_HELP = "Only conversations of this kind: chat, mission, run (a Routine's), task (a " \
            "background task, one per trigger), sub-agent."


def _items(c: sqlite3.Connection, cid: str) -> list[dict]:
    """The conversation's active path as it reads it: one item per user message and
    per answer with words or tools, numbered from 1, each with its tool calls."""
    chain = tree.path(c, cid)
    ids = [m["id"] for m in chain if m["role"] == "assistant"]
    calls: dict[str, list[dict]] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in c.execute(
                f"SELECT tc.id, tc.message_id, tc.name, tc.state, tc.arguments, "
                f"m.content AS result FROM tool_calls tc LEFT JOIN messages m ON "
                f"m.id = tc.result_message_id WHERE tc.message_id IN ({marks}) "
                f"ORDER BY tc.rowid", ids):
            calls.setdefault(r["message_id"], []).append(dict(r))
    out: list[dict] = []
    for m in chain:
        mine = calls.get(m["id"], []) if m["role"] == "assistant" else []
        if m["role"] in ("user", "assistant") and ((m["content"] or "").strip() or mine):
            out.append({"n": len(out) + 1, "message": m, "calls": mine})
    return out


DRAFT = "(a draft, not a source)"
CUT = ("failed", "interrupted", "aborted")


def _ended(c: sqlite3.Connection, cid: str, key: str | None) -> str:
    """How a conversation ended, when that makes its words a draft: a run's is "run World
    news, failed: Stopped at its time limit of 3 hours. (a draft, not a source)" (or "..., done";
    a run that wrote nothing gets no draft mark). Any other one whose last turn ended
    failed, interrupted or aborted is "last turn failed", no more: a chat the user
    stopped holds their words and its own, real ones (such chats can be whole talks). "" for
    the rest."""
    last = c.execute("SELECT state FROM turns WHERE conversation_id=? "
                     "ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
    cut = bool(last and last["state"] in CUT)
    parts = (key or "").split(":")
    if len(parts) == 2 and parts[0] == "run":
        run = c.execute("SELECT label, state, error FROM runs WHERE id=?",
                        (parts[1],)).fetchone()
        if run is not None:
            how = f"failed: {run['error']}" if run["state"] == "failed" else run["state"]
            wrote = c.execute("SELECT 1 FROM messages WHERE conversation_id=? AND "
                              "role='assistant' AND content != '' LIMIT 1", (cid,)).fetchone()
            # a run that died on a 502 before a word has nothing to call a draft
            draft = f" {DRAFT}" if wrote and (cut or run["state"] == "failed") else ""
            return f"run {run['label']}, {how}{draft}"
    return f"last turn {last['state']}" if cut else ""


def _archived(row, lock: bool) -> str:
    """", archived" after the id of a conversation the user archived, while that locks
    it (`lock`, conversations/store.lock_archived): its tasks are not its own to pick up."""
    return ", archived" if lock and row["archived_at"] else ""


class PastConversations:
    name = "past_conversations"
    guide = guide("past_conversations")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg

    def _who(self, role: str, author: str | None = None) -> str:
        if author == "intercom":
            return "the intercom"           # an agent, a job, a notice: not the user's words
        return self.cfg.user if role == "user" else "you"

    def _at(self, iso: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        """A stored UTC time in the app's time zone, as list_day takes days."""
        if not iso:
            return ""
        return dt.datetime.fromisoformat(iso).astimezone(zone(self.cfg)).strftime(fmt)

    @tool()
    async def search(
        self,
        ctx: ToolContext,
        query: Annotated[str, "The words. FTS5 syntax works: quotes, OR, prefix*."],
        limit: Annotated[int | None, "The most matches, default 20."] = None,
    ) -> ToolResult:
        """Search your earlier conversations with {{username}} in this app, by word search.
        The current conversation is left out. Do not use it for their WhatsApp or Discord
        chats: use chat_archive. Returns: one line per match: date, title, conversation
        id, author, snippet. An archived conversation is over: leave its tasks."""
        most = int(self.cfg.r("past_conversations.max_results"))
        n = min(int(limit or most), most)
        chars = int(self.cfg.r("past_conversations.snippet_chars"))
        hits = await conversations.search(
            ctx.store, query, exclude=ctx.conversation_id, limit=n,
            snippet_tokens=max(8, min(64, chars // 5)))
        if not hits:
            return ToolResult(content=f"No earlier conversation contains {query!r}. For "
                                      f"their chats, use the Messages archive.")
        lines = []
        lock = await ctx.store.read(conversations.lock_archived)
        for h in hits:
            day = self._at(h["created_at"], "%Y-%m-%d")
            lines.append(f"[{day}] {h['title'] or 'Untitled'} (conversation "
                         f"{h['conversation_id'][:8]}{_archived(h, lock)}), "
                         f"{self._who(h['role'], h['author'])}: ...{h['snippet']}...")
        return ToolResult(content="\n".join(lines), display={"hits": len(hits)})

    @tool()
    async def list_day(
        self,
        ctx: ToolContext,
        date: Annotated[str, "The day, such as 2026-01-09, in the app's time zone."],
        kind: Annotated[Kind | None, KIND_HELP] = None,
    ) -> ToolResult:
        """Every conversation of yours with messages on one day, as an index: id, kind,
        title, the message numbers of that day, turns, the tool calls with their names
        and the ones that failed, a run's try with its outcome (a failed one is a draft,
        not a source), a last turn that failed or was cut, the first and the last line.
        The current conversation is left out, and the archived ones while Lock archived
        chats is on. Read one further with past_conversations__read."""
        try:
            day = dt.date.fromisoformat(str(date).strip())
        except ValueError:
            return ToolResult(content=f"{date!r} is not a day. Use 2026-01-09.",
                              is_error=True)
        tz = zone(self.cfg)
        lo, hi = (iso(dt.datetime.combine(d, dt.time(), tz))
                  for d in (day, day + dt.timedelta(days=1)))
        rows = await ctx.store.read(lambda c: self._day(c, lo, hi, ctx.conversation_id, kind))
        if not rows:
            of = f" {kind}" if kind else ""
            return ToolResult(content=f"No{of} conversation of yours has messages on {day}.")
        out = [f"{day}: {len(rows)} conversation{'s' if len(rows) != 1 else ''}"]
        for r in rows:
            calls = r["calls"]
            names = Counter(c["name"] for c in calls)
            failed = [c for c in calls if c["state"] in ("error", "interrupted")]
            line = (f"- {r['id'][:8]} {r['kind']} \"{r['title'] or 'Untitled'}\": "
                    f"#{r['first']} to #{r['last']}, {r['turns']} turns, "
                    f"{len(calls)} tool calls")
            if names:
                line += f" ({', '.join(f'{n} {k}' if k > 1 else n for n, k in names.most_common())})"
            if failed:
                line += f", {len(failed)} failed ({', '.join(c['name'] for c in failed)})"
            if r["ended"]:
                line += f", {r['ended']}"
            out += [line, f"  first: {r['first_line']}", f"  last: {r['last_line']}"]
        text = "\n".join(out)
        from ..turns import tokens
        budget = int(ctx.max_output_tokens or READ_TOKENS)
        cut, shown, total = tokens.cut_to(text, budget, "prose")
        if shown < total:
            cut += "\n[the rest of the day did not fit: give parts of it to sub-agents]"
        return ToolResult(content=cut, display={"day": str(day), "conversations": len(rows)})

    @tool()
    async def list(
        self,
        ctx: ToolContext,
        kind: Annotated[Kind | None, KIND_HELP] = None,
        limit: Annotated[int, f"How many, at most {LISTED}."] = 10,
    ) -> ToolResult:
        """Your newest conversations by their last activity, of every kind or one.
        Returns: one line each: id, kind, title, when it started and when it last moved,
        a mission's status, a run's outcome, a last turn that failed or was cut, and its
        last line. The current conversation is left out, and the archived ones while Lock
        archived chats is on. Read one with past_conversations__read."""
        from ..conversations.store import last_line
        n = min(max(int(limit or 10), 1), LISTED)

        def fn(c):
            rows = c.execute(
                f"SELECT c.id, c.title, {KIND_SQL} AS kind, c.trigger_key, c.created_at, "
                "c.updated_at, c.active_leaf_id, mi.status FROM conversations c LEFT JOIN "
                "missions mi ON mi.conversation_id = c.id WHERE c.id != ? AND NOT "
                f"{locked_sql()} AND {KIND_SQL} = coalesce(?, {KIND_SQL}) AND "
                "c.active_leaf_id IS NOT NULL ORDER BY c.updated_at DESC, c.id DESC LIMIT ?",
                (ctx.conversation_id, kind, n)).fetchall()
            return [{**dict(r), "ended": _ended(c, r["id"], r["trigger_key"]),
                     "last": last_line(c, r["active_leaf_id"])} for r in rows]
        rows = await ctx.store.read(fn)
        if not rows:
            return ToolResult(content=f"No {kind + ' ' if kind else ''}conversations yet.",
                              display={"conversations": 0})
        lines = []
        for r in rows:
            facts = [f"started {self._at(r['created_at'])}", f"last {self._at(r['updated_at'])}"]
            facts += [f"mission {r['status'].replace('_', ' ')}"] if r["status"] else []
            facts += [r["ended"]] if r["ended"] else []
            lines.append(f"- {r['id'][:8]} {r['kind']} \"{r['title'] or 'Untitled'}\": "
                         f"{', '.join(facts)}" + (f"\n  last: {r['last']}" if r["last"] else ""))
        return ToolResult(content=self.cfg.substitute("\n".join(lines)),
                          display={"conversations": len(rows)})

    def _day(self, c: sqlite3.Connection, lo: str, hi: str, here: str,
             kind: str | None = None) -> list[dict]:
        out = []
        for r in c.execute(
                f"SELECT c.id, c.title, {KIND_SQL} AS kind, c.trigger_key FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "WHERE m.created_at >= ? AND m.created_at < ? "
                "AND m.role IN ('user', 'assistant') AND c.id != ? "
                f"AND NOT {locked_sql()} AND {KIND_SQL} = coalesce(?, {KIND_SQL}) "
                "GROUP BY c.id "
                "ORDER BY min(m.created_at)", (lo, hi, here, kind)).fetchall():
            items = [i for i in _items(c, r["id"])
                     if lo <= i["message"]["created_at"] < hi]
            if not items:
                continue                      # that day's messages are off the path
            said = [i for i in items if (i["message"]["content"] or "").strip()]
            turns = c.execute("SELECT count(*) FROM turns WHERE conversation_id = ? AND "
                              "created_at >= ? AND created_at < ?",
                              (r["id"], lo, hi)).fetchone()[0]

            def line(i) -> str:
                m = i["message"]
                return f"{self._who(m['role'], m['author'])}: {one_line(m['content'], LINE)}"
            out.append({**dict(r), "first": items[0]["n"], "last": items[-1]["n"],
                        "ended": _ended(c, r["id"], r["trigger_key"]),
                        "turns": turns, "calls": [k for i in items for k in i["calls"]],
                        "first_line": line(said[0]) if said else "(tools only)",
                        "last_line": line(said[-1]) if said else "(tools only)"})
        return out

    async def _find(self, ctx: ToolContext, key: str):
        """The conversation `key` names: run:<id> (a Routine's run), a mission's name, a
        trigger's name (its background task), else the start of a conversation id."""
        key = str(key or "").strip()
        cols = ("SELECT id, title, created_at, archived_at, trigger_key FROM conversations "
                "WHERE trashed_at IS NULL AND ")

        def fn(c):
            if key.lower().startswith("run:"):
                return c.execute(cols + "id IN (SELECT conversation_id FROM runs WHERE id "
                                 "LIKE ? || '%') LIMIT 2", (key[4:].strip(),)).fetchall()
            name = " ".join(key.split())          # a mission's name, as missions__list shows it
            ids = [r[0] for r in c.execute(
                "SELECT m.conversation_id FROM missions m JOIN conversations c ON c.id = "
                "m.conversation_id WHERE c.trashed_at IS NULL AND (lower(m.name) = lower(?) "
                "OR lower(c.title) = lower(?))",
                (name, name))] or [
                r[0] for r in c.execute("SELECT id FROM conversations WHERE trigger_key = ?",
                                        (f"trigger:{key}",))]
            if ids:
                return c.execute(cols + f"id IN ({','.join('?' * len(ids))}) LIMIT 2",
                                 ids).fetchall()
            return c.execute(cols + "id LIKE ? || '%' LIMIT 2", (key,)).fetchall()
        rows = await ctx.store.read(fn) if key else []
        if len(rows) == 1:
            return rows[0], None
        what = "matches more than one conversation" if rows else "names no conversation"
        return None, ToolResult(
            content=f"{key!r} {what}. It takes a conversation id (past_conversations__list, "
                    f"past_conversations__search, past_conversations__list_day), run:<id> "
                    f"(routines__read), a mission's name (missions__list) or a trigger's "
                    f"name (routines__list).", is_error=True)

    @tool()
    async def read(
        self,
        ctx: ToolContext,
        conversation: Annotated[str, "A conversation id (8 characters are enough), "
                                     "run:<id> for a Routine's run, a mission's name, or "
                                     "a trigger's name for its background task."],
        start: Annotated[int | None, "The first message number to read, reading on from "
                                     "it. Left out: the end of the conversation, read "
                                     "back from `end`."] = None,
        end: Annotated[int | None, "The last message number to read. The `earlier:` line "
                                   "gives it."] = None,
        tools: Annotated[Literal["names", "full"], "names: each tool call one line with "
                         "its id and how it ended. full: with its arguments and its "
                         "result."] = "names",
    ) -> ToolResult:
        """Read one earlier conversation in order: {{username}}'s messages, your answers,
        and the tools you used. A long one comes in slices: `earlier:` and `later:` lines
        read on. A tool call's id reads it raw with past_conversations__read_tool_call."""
        from ..turns import tokens
        conv, wrong = await self._find(ctx, conversation)
        if wrong:
            return wrong
        cid = conv["id"]
        items, ended, lock = await ctx.store.read(
            lambda c: (_items(c, cid), _ended(c, cid, conv["trigger_key"]),
                       conversations.lock_archived(c)))
        lines = [self._item(i, tools) for i in items]
        budget = min(READ_TOKENS, int(ctx.max_output_tokens or READ_TOKENS))
        lo = max(1, int(start)) if start else None
        hi = min(len(items), int(end)) if end else len(items)
        picked: list[int] = []
        spent = 0
        order = range(lo, hi + 1) if lo else range(hi, 0, -1)
        for n in order:
            cost = tokens.estimate(lines[n - 1], "prose")
            if picked and spent + cost > budget:
                break
            picked.append(n)
            spent += cost
        picked.sort()
        out = [f"conversation {cid[:8]}{_archived(conv, lock)}: {conv['title'] or 'Untitled'}, "
               f"started {self._at(conv['created_at'], '%Y-%m-%d')}, {len(items)} messages"
               + (f", {ended}" if ended else "")]
        out += [lines[n - 1] for n in picked] or ["(no messages)"]
        if picked and picked[0] > 1:
            out.append(f"earlier: past_conversations__read(conversation=\"{cid[:8]}\", "
                       f"end={picked[0] - 1})")
        if picked and picked[-1] < len(items):
            out.append(f"later: past_conversations__read(conversation=\"{cid[:8]}\", "
                       f"start={picked[-1] + 1})")
        text, _, _ = tokens.cut_to("\n".join(out), budget + 200, "prose")
        return ToolResult(content=text, display={"conversation": cid,
                                                 "title": conv["title"] or "Untitled",
                                                 "messages": len(picked)})

    def _item(self, i: dict, tools: str) -> str:
        m = i["message"]
        text = (m["content"] or "").strip()
        head = (f"#{i['n']} {self._at(m['created_at'])} "
                f"{self._who(m['role'], m['author'])}")
        out = [f"{head}: {text}" if text else head]
        for k in i["calls"]:
            out.append(f"    call {k['id']} {k['name']} {k['state']}")
            if tools == "full":
                out.append(f"      arguments: {k['arguments']}")
                out.append("      result: " + (k["result"] or "(none)").replace(
                    "\n", "\n      "))
        return "\n".join(out)

    @tool()
    async def read_tool_call(
        self,
        ctx: ToolContext,
        id: Annotated[str, "A tool call's id, from a `call <id>` line of "  # noqa: A002
                           "past_conversations__read."],
        part: Annotated[int | None, "Which part of a long call, from 1; the last line says "
                                    "when another follows. Default 1."] = None,
    ) -> ToolResult:
        """One tool call of an earlier conversation, raw: its tool, how it ended, its
        arguments and its whole result, a long one in parts that joined are the call."""
        r = await ctx.store.read(lambda c: c.execute(
            "SELECT tc.id, tc.name, tc.state, tc.arguments, tc.started_at, tc.display, "
            "m.content AS result, a.conversation_id FROM tool_calls tc JOIN messages a ON "
            "a.id = tc.message_id JOIN conversations cv ON cv.id = a.conversation_id "
            "LEFT JOIN messages m ON m.id = tc.result_message_id "
            "WHERE tc.id = ? AND cv.trashed_at IS NULL", (str(id).strip(),)).fetchone())
        if r is None:
            return ToolResult(content=f"There is no tool call {id!r}. The `call <id>` lines "
                                      f"of past_conversations__read give the ids.",
                              is_error=True)
        try:
            args = json.dumps(json.loads(r["arguments"] or "{}"), indent=1,
                              ensure_ascii=False)
        except ValueError:
            args = r["arguments"]
        text = (f"call {r['id']} {r['name']} {r['state']}, conversation "
                f"{r['conversation_id'][:8]}, {self._at(r['started_at'])}"
                f"\narguments:\n{args}\nresult:\n{r['result'] or '(none)'}")
        cut, _, _ = parts.read(text, part, ctx, cap=READ_TOKENS,
                               call=f"past_conversations__read_tool_call(id=\"{r['id']}\"")
        # an outside call read again is outside text again, with its label (guard/)
        label = json.loads(r["display"] or "{}").get("outside")
        return ToolResult(content=cut, display={"tool_call": r["id"], "name": r["name"],
                                                **({"outside": label} if label else {})})
