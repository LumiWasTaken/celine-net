"""The agent's intercom tools: write to {{username}}, itself, its parent, its team, everyone
working, or a conversation, now or later; read its threads (and every thread between the
user and any of its conversations, and its jobs everywhere); cancel what it scheduled.
A message to itself with a time is a reminder. The agent's instructions list what waits for
it, the announcements standing now, what it scheduled, and its sub-agents."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from .. import homes
from ..attachments import draft_of, agent_file
from ..core.ids import clock, iso, parse_when, when
from ..skills import ToolContext, ToolResult, guide, tool, described, words
from ..clock import zone
from . import store as rows
from .addresses import GUARD, GUARD_NAMES, Refused
from .render import header, who

TEXT_MAX = 60       # characters of a scheduled message's text in its instructions
LISTED = 5          # scheduled messages listed there, the rest a count
PER_COUNTERPART = 3  # threads intercom__read lists per counterpart, the rest a count
AWAY = ("background", "mission")     # kinds where start_chat is offered
COVERS = timedelta(hours=24)    # a thread to the user from the same source this recent
ACTIONS = 6         # the source's last tool calls the new chat's opening names
WAIT_MAX = 600      # seconds send(wait=true) holds its turn for the user's answer at most


def _short(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > TEXT_MAX:
        text = text[:TEXT_MAX].rstrip() + "..."
    return text if text.endswith((".", "!", "?")) else text + "."


class IntercomSkill:
    name = "intercom"
    guide = guide("intercom")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg
        self.tz = zone(cfg)

    app = None      # the running app (core/services.py), from skills.build

    async def _scheduled(self, store, cid: str) -> list[dict]:
        """What this conversation sent with a time that has not landed, earliest first."""
        out = await store.read(lambda c: rows.listed(c, from_addr=f"agent:{cid}",
                                                     scheduled=True))
        return [r for r in out if r["state"] == "pending"]

    async def tail_part(self, ctx: ToolContext) -> str | None:
        """For the tail of a turn, each only when there is any: the thread the turn
        answers, the announcements standing now, what it scheduled from here, and its
        own sub-agents. (What waits for it is in the request itself already: the runner
        takes it at every step.)"""
        if ctx.store is None:
            return None
        cid = ctx.conversation_id
        me = f"agent:{cid}"
        now_ = datetime.now(timezone.utc)
        parts = []

        here = await ctx.store.read(lambda c: _here(c, cid, ctx.turn_id))
        if here:
            parts.append(self._here_line(here))

        standing = await ctx.store.read(rows.standing)
        if standing:
            items = [f"#{a['id']} from {who(a, self.cfg.user)}, until "
                     f"{clock(datetime.fromisoformat(a['until_at']), self.tz, now_)}: "
                     f"{_short(a['text'])}" for a in standing]
            parts.append("Announcements to everyone working: " + " ".join(items))

        pending = await self._scheduled(ctx.store, cid)
        if pending:
            items = []
            for r in pending[:LISTED]:
                to = f"{self.cfg.user}, in this chat" if r["data"].get("say") else \
                    "you" if r["to"] == me else r["to"]
                at = clock(datetime.fromisoformat(r["due_at"]), self.tz, now_)
                items.append(f"#{r['id']} {at} to {to}: {_short(r['text'])}")
            if len(pending) > LISTED:
                items.append(f"and {len(pending) - LISTED} more.")
            parts.append("Your scheduled messages: " + " ".join(items))

        team = await ctx.store.read(lambda c: _team_of(c, cid))
        if team:
            shown = [f"{t['name']} ({'working' if t['working'] else 'done'}"
                     f"{', on ' + t['device'] if t['device'] else ''}"
                     f"{', now ' + t['step'] if t['working'] and t['step'] else ''})"
                     for t in team[:LISTED]]
            if len(team) > LISTED:
                shown.append(f"and {len(team) - LISTED} more")
            parts.append("Your sub-agents: " + ", ".join(shown) + ".")
        return "\n".join(parts) or None

    def _here_line(self, here: dict) -> str:
        """The thread this turn answers, said once at the top of its intercom lines."""
        subject = f' "{here["subject"]}"' if here["subject"] else ""
        sender = who(here, self.cfg.user)
        line = (f"You are in thread #{here['thread']}{subject}: this turn answers "
                f"#{here['id']} from {sender}.")
        if here["from"] == "user" and not here["sub_agent"]:     # intercom/answer.py
            line += f" Your answer goes into that thread for {self.cfg.user}."
        if here["sub_agent"] and not here["from"].startswith("agent:"):
            return line         # it writes up its chain only (addresses._reach)
        to = "user" if here["from"] == "user" else here["from"]
        return line + (f' intercom__send(to="{to}") answers there by itself; '
                       "new_thread=true starts a new thread.")

    @tool(read_only=False, destructive=False, timeout=WAIT_MAX + 30)
    async def send(
        self,
        ctx: ToolContext,
        text: Annotated[str, "The message."],
        to: Annotated[str, '"user" for {{username}}, "me" for yourself, "parent", "team", '
                           '"all" for everyone working, "bastet", a sub-agent\'s or '
                           'mission\'s name, a conversation id, run:<id> for a Routine\'s run '
                           'or routine:<name> for its newest run. Several, comma '
                           'separated. '
                           'With reply_to and no to, it goes to the one who wrote that '
                           'message.'] = "",
        *,
        subject: Annotated[str | None, "A subject, like in mail."] = None,
        reply_to: Annotated[int | None, "The number of the message this answers."] = None,
        new_thread: Annotated[bool, "Start a new thread, not the one you are in."] = False,
        in_: Annotated[str | None, 'Lands later, like "30 min" or "1 h 30 min".'] = None,
        at: Annotated[str | None, 'Lands at a time today, like "02:00", tomorrow once it '
                                  'has passed; or on a day, like "2026-11-02 09:00".'] = None,
        until: Annotated[str | None, 'To "all": an announcement that stands until then, '
                                     'like "05:00" or "2 h". Everyone who starts work '
                                     'before then sees it.'] = None,
        wait: Annotated[bool, 'To "user": your turn holds here until {{username}} '
                              'answers, up to 10 minutes; the answer comes in as their '
                              'message. Not with in or at.'] = False,
        attachments: Annotated[list[str] | None, "Files it carries, by their short ids: "
                                                 "one on your own message (a voice note, a "
                                                 "file you sent) or one {{username}} "
                                                 "attached here."] = None,
    ) -> ToolResult:
        """Write to {{username}}, yourself, your parent, your team, everyone working,
        Bastet, or a sub-agent or mission by name. It answers in the thread you are in (the one
        your instructions name); reply_to answers another message in its thread,
        new_thread starts one. With in or at it lands
        later; to "me" that is a reminder that starts your turn then, or joins the one
        running. To "user" from a chat with them: what you write in the chat reaches
        them, and with in or at your text lands in the chat then, as your message. From a
        background task or a Routine's run: it starts a chat for them with your text as
        its first message (subject: its name), and their answer comes to you there."""
        if not text.strip():
            return ToolResult(content="text is the message.", is_error=True)
        if not to.strip() and reply_to is None:
            return ToolResult(content="to says who it goes to, or reply_to answers a "
                                      "message.", is_error=True)
        now_ = datetime.now(timezone.utc)
        due = until_at = None
        if in_ or at:
            due = parse_when(in_, at, self.tz, now_)
            if due is None:
                return ToolResult(content='in is like "30 min" or "1 h 30 min", or give '
                                          'at="02:00" or at="2026-11-02 09:00" still ahead.',
                                  is_error=True)
        if until:
            until_at = parse_when(until, until, self.tz, now_)
            if until_at is None:
                return ToolResult(content='until is like "05:00" or "2 h".', is_error=True)
        from .. import guard
        if not guard.enabled(self.cfg) and any(
                w.strip() in GUARD_NAMES for w in to.split(",")):
            return ToolResult(content="Bastet is off; nobody reads this.", is_error=True)
        files = []
        for ref in attachments or []:
            row = await agent_file(ctx.store, ref.strip(), ctx.conversation_id, self.cfg.user)
            if isinstance(row, str):
                return ToolResult(content=row, is_error=True)
            files.append(row["id"])
        words = [w.strip() for w in to.split(",") if w.strip()]
        told = await self._to_user(ctx, text, subject, due, files) \
            if "user" in words and reply_to is None else None
        if told is not None:
            words = [w for w in words if w != "user"]
            if not words:
                return told
            to = ", ".join(words)
        drafts = [await draft_of(ctx.store, aid) for aid in files]
        try:
            out = await self.app.intercom.send(
                f"agent:{ctx.conversation_id}", to, text, subject=subject,
                reply_to=reply_to, due_at=iso(due) if due else None,
                until_at=iso(until_at) if until_at else None,
                turn=None if new_thread else ctx.turn_id, attachments=drafts or None)
        except KeyError as e:
            return ToolResult(content=f"No conversation or message {e.args[0]}.",
                              is_error=True)
        except Refused as e:
            return ToolResult(content=str(e), is_error=True)
        except ValueError as e:
            return ToolResult(content=f"{e}. to is user, me, parent, team, all, bastet, a name, "
                                      f"or a conversation id.",
                              is_error=True)
        iid = out["intercom_id"]
        def sent(c):
            r = c.execute("SELECT thread, to_addr FROM intercom WHERE id=?", (iid,)).fetchone()
            names = ", ".join(rows.name_of(c, a) or "Everyone" for a in r[1].split(", "))
            return r[0], r[1], names
        tid, stored, to_name = await ctx.store.read(sent)
        if GUARD in stored.split(", "):
            from ..guard import talk         # Bastet answers, as background work
            await talk.heard(self.app, iid, ctx.conversation_id,
                             (due - now_).total_seconds() if due else None)
        where = "a new thread" if tid == iid else f"thread #{tid}"
        said = f"Sent #{iid} in {where}." if due is None else \
            f"#{iid} lands {clock(due, self.tz, now_)}, in {where}."
        if wait and due is None and "user" in [w.strip() for w in to.split(",")]:
            said += await self._wait_for_user(ctx, iid)
        if told is not None:
            said = f"{told.content} {said}"
        return ToolResult(content=said, display={
            "id": iid, "to": to, "to_name": to_name, "text": text,
            "due_at": iso(due) if due else None})

    async def _to_user(self, ctx: ToolContext, text: str, subject: str | None,
                       due: datetime | None, files: list[str]) -> ToolResult | None:
        """A message to the user, by where it is sent from. A chat is where the user reads
        it already: with no time, nothing is sent (its reply in the chat reaches them),
        unless its turn answers their reply in a thread (it goes into that thread);
        with one, the text lands in the chat at that time as its own message (store.SAY).
        A background task or a run starts a chat for them (_chat), titled with the
        subject or else the run's own title, so their answer comes to a chat. None: it goes as written (a mission's waits on the mission's page)."""
        cid, user = ctx.conversation_id, self.cfg.user
        kind = await ctx.store.read(lambda c: (c.execute(
            "SELECT kind FROM conversations WHERE id=?", (cid,)).fetchone() or [None])[0])
        shown = {"to": "user", "to_name": rows.NAMES["user"], "text": text,
                 "due_at": iso(due) if due else None}
        if kind == "chat" and due is None and ctx.turn_id and await ctx.store.read(
                lambda c: (rows.answering(c, cid, ctx.turn_id) or {"from_addr": ""})
                ["from_addr"] == "user"):
            return None         # this turn answers their reply in a thread: it goes there
        if kind == "chat" and due is None:
            return ToolResult(content=f"This chat is where {user} reads you: what you write "
                                      f"here reaches them, and their answer comes here.",
                              display=shown)
        if kind == "chat":
            drafts = [await draft_of(ctx.store, aid) for aid in files]
            out = await self.app.intercom.send(
                f"agent:{cid}", f"agent:{cid}", text, subject=subject, wake=False,
                due_at=iso(due), data={"say": True}, attachments=drafts or None)
            at = clock(due, self.tz, datetime.now(timezone.utc))
            return ToolResult(content=f"#{out['intercom_id']} lands {at} in this chat as "
                                      f"your message to {user}.",
                              display={**shown, "id": out["intercom_id"]})
        if kind == "background":
            # named after its source when the message has no subject ("Radar, Mon 9 Nov")
            title = subject or await ctx.store.read(lambda c: c.execute(
                "SELECT title FROM conversations WHERE id=?", (cid,)).fetchone()[0]) or ""
            new, iid, has = await self._chat(ctx, title, text, files, due)
            when = (f"; the chat is in their list now, and its notice reaches them "
                    f"{clock(due, self.tz, datetime.now(timezone.utc))} as #{iid}" if due
                    else f"; it waits for them as #{iid}")
            return ToolResult(content=f"Started a chat for {user} (/chat/{new}) with your "
                                      f"text as your first message there{when}, and their "
                                      f"answer comes to you in that chat.{self._had(has)}",
                              display={**shown, "id": iid, "chat": new})
        return None

    async def _chat(self, ctx: ToolContext, title: str, text: str, files=(),
                    due: datetime | None = None) -> tuple[str, int, list[str]]:
        """A chat for the user (start_chat, and send to "user" from a background task or
        a run): the agent's text is its first message, the chat knows where it came from
        (opening), and the text waits for the user on the intercom from that chat, so
        their answer lands there. -> (the chat, the message's number, what the user had
        from the same source in the last 24 h: covered)."""
        from ..conversations import store as conversations, tree
        src = ctx.conversation_id
        since = iso(datetime.now(timezone.utc) - COVERS)
        has = await ctx.store.read(lambda c: covered(c, src, since, self.tz))
        new = await conversations.create(ctx.store, title=title)
        mine = [await draft_of(ctx.store, aid) for aid in files]

        def first(c):
            # where it came from, for it in the new chat (turns/context.py opening)
            c.execute("INSERT INTO kv(k, v) VALUES(?, ?)",
                      (rows.OPENING + new, opening(c, src, ctx.tool_call_id, has, self.cfg.user)))
            tree.append(c, ctx.store, new, None, "assistant", text, "model", attachments=mine)
        await ctx.store.write(first)
        theirs = [await draft_of(ctx.store, aid) for aid in files]
        # from the new chat, so the user's answer lands there
        out = await self.app.intercom.send(f"agent:{new}", "user", text, subject=title or None,
                                           data={"chat": new, "from": src},
                                           due_at=iso(due) if due else None,
                                           attachments=theirs or None)
        return new, out["intercom_id"], has

    def _had(self, has: list[str]) -> str:
        return (f"\nIn the last 24 h {self.cfg.user} also got this from here:\n- "
                + "\n- ".join(has)) if has else ""

    async def _wait_for_user(self, ctx: ToolContext, iid: int) -> str:
        """send(wait=true): the turn holds until the user writes to this conversation
        after #iid (in the Intercom or the chat itself), or WAIT_MAX seconds pass. The
        call's row says who it waits for; the runner takes the answer in before its
        next step (deliver.take)."""
        user = self.cfg.user
        limit = WAIT_MAX if ctx.time_left is None else min(WAIT_MAX, max(int(ctx.time_left), 0))
        if ctx.emit is not None:
            await ctx.emit(f"Waiting for {user}")
        cid = ctx.conversation_id

        async def answer() -> int | None:
            r = await ctx.store.read(lambda c: c.execute(
                "SELECT i.id FROM intercom i JOIN intercom_deliveries d ON d.intercom_id = i.id "
                "WHERE d.conversation_id=? AND i.from_addr='user' AND i.id>? AND "
                "i.cancelled_at IS NULL ORDER BY i.id LIMIT 1", (cid, iid)).fetchone())
            return r[0] if r else None
        got = await self.app.intercom.wait_for(answer, limit)
        return f" {user} answered (#{got})." if got else \
            f" {user} did not answer within {limit} s; their answer comes in as a message."

    @tool()
    async def read(
        self,
        ctx: ToolContext,
        thread: Annotated[int | None, "A thread's number: the whole thread."] = None,
        unread: Annotated[bool, 'Only threads with messages waiting for you; with '
                                'with="user", those still waiting for {{username}}.'] = False,
        kind: Annotated[Literal["message", "brief", "report", "job", "notice", "trigger"]
                        | None, 'Only messages of this kind. "job": your jobs instead, '
                                'running and recently ended, in every conversation.'
                        ] = None,
        with_: Annotated[str | None, 'Only the threads with someone: a name, "user" '
                                     'for {{username}} (from every conversation of yours), '
                                     'or a conversation id.'] = None,
        limit: Annotated[int, "At most this many threads."] = 20,
    ) -> ToolResult:
        """Your threads by who they are with, newest activity first, or one whole thread
        by its number. with="user": every thread between {{username}} and any of your
        conversations, each with where it lives (kind, title, conversation id) and
        whether it waits for them. kind="job": your jobs, each with its conversation, its
        state and its result's first line."""
        cid = ctx.conversation_id
        if thread is not None:
            msgs = await ctx.store.read(lambda c: rows.thread(c, thread, cid))
            if not msgs:
                return ToolResult(content=f"There is no thread #{thread}.", is_error=True)
            out = []
            for m in msgs:
                head = header(m, self.cfg.user, self.tz) or \
                    f"[From {self.cfg.user} (#{m['id']})]\n"
                at = when(m["created_at"], self.tz)
                gone = " (cancelled)" if m["cancelled"] else ""
                out.append(f"[{at}] to {m['to']}{gone}\n{head}{m['text']}")
            return ToolResult(content="\n\n".join(out),
                              display={"thread": thread, "count": len(msgs)})
        limit = max(1, limit)
        if kind == "job":
            return await self._jobs(ctx, limit)
        if with_ == "user":
            return await self._with_user(ctx, unread, kind, limit)
        me = f"agent:{cid}"
        # one counterpart: its threads up to the limit; else each counterpart's newest few
        per = limit if with_ else PER_COUNTERPART

        def fn(c):
            found = rows.threads(c, me, unread, kind=kind, with_=with_)
            groups, _ = rows.grouped(c, found, per, len(found) + 1)
            return found, groups, (None if kind or with_ else rows.jobs(c, me))

        found, groups, jobs = await ctx.store.read(fn)
        lines, shown = [], 0
        for i, g in enumerate(groups):
            if shown >= limit:
                rest = groups[i:]
                lines.append("Also threads with " + ", ".join(
                    f"{r['name']} ({r['threads']})" for r in rest) + ".")
                break
            # one name over several addresses (the agent's chats): the group names them all and
            # each thread says which it is with
            many = len(g["addresses"]) > 1
            peer = f"{g['name']} ({', '.join(g['addresses'])})" if many else g["name"]
            waits = f", {g['waiting']} waiting" if g["waiting"] else ""
            lines.append(f"with {peer}: {g['threads']} thread"
                         f"{'s' if g['threads'] != 1 else ''}{waits}")
            for t in g["items"]:
                last = t["last"]
                subject = f' "{t["subject"]}"' if t["subject"] else ""
                waits = f", {t['unread']} waiting for you" if t["unread"] else ""
                at = f" with {', '.join(t['with'])}" if many else ""
                lines.append(f"  #{t['thread']}{subject}{at}: {t['count']} message"
                             f"{'s' if t['count'] != 1 else ''}{waits}, last from "
                             f"{who(last, self.cfg.user)} "
                             f"{when(last['created_at'], self.tz)}: {_short(last['text'])}")
            shown += len(g["items"])
            if g["more"]:
                key = g["name"] or (g["addresses"] or [""])[0]
                lines.append(f"  and {g['more']} more" + (
                    f": intercom__read(with=\"{key}\")." if key else "."))
        if jobs and jobs["count"]:
            lines.append(f"{jobs['count']} job result{'s' if jobs['count'] != 1 else ''}, "
                         f"newest #{jobs['newest']}: intercom__read(kind=\"job\").")
        if not lines:
            return ToolResult(content="No threads waiting for you." if unread
                              else "No threads yet.", display={"threads": 0})
        return ToolResult(content="\n".join(lines), display={"threads": len(found)})

    async def _with_user(self, ctx: ToolContext, unread: bool, kind: str | None,
                         limit: int) -> ToolResult:
        """read(with="user"): the threads between the user and any of the agent's
        conversations, newest activity first, each with its home and whether it waits
        for the user (`unread`: only those)."""
        user = self.cfg.user

        def fn(c):
            # its own filter: rows.threads' unread leaves out what waits on a mission
            found = [t for t in rows.threads(c, "user", kind=kind)
                     if any(a.startswith("agent:") for a in t["with"])
                     and (t["unread"] or not unread)]
            out = []
            for t in found[:limit]:
                home = next(a for a in t["with"] if a.startswith("agent:"))
                out.append({**rows.thread_view(c, t), "home": home[6:],
                            "src": rows.source_of(c, home) or {}})
            return len(found), out
        total, shown = await ctx.store.read(fn)
        if not shown:
            return ToolResult(content=f"No threads waiting for {user}." if unread
                              else f"No threads with {user} yet.", display={"threads": 0})
        lines = []
        for t in shown:
            last, src = t["last"], t["src"]
            subject = f' "{t["subject"]}"' if t["subject"] else ""
            by = "you" if last["from"].startswith("agent:") else who(last, user)
            waits = f", waits for {user}" if t["unread"] else ""
            lines.append(f"#{t['thread']}{subject} in {src.get('kind', 'conversation')} "
                         f"\"{src.get('title') or 'Untitled'}\" ({t['home']}): {t['count']} "
                         f"message{'s' if t['count'] != 1 else ''}{waits}, last from {by} "
                         f"{when(last['created_at'], self.tz)}: {_short(last['text'])}")
        if total > len(shown):
            lines.append(f"and {total - len(shown)} older.")
        return ToolResult(content="\n".join(lines), display={"threads": total})

    async def _jobs(self, ctx: ToolContext, limit: int) -> ToolResult:
        """read(kind="job"): the jobs of every conversation, running first, then the
        newest ended, each with its conversation and its result message's number."""

        def fn(c):
            got = []
            for j in c.execute(
                    "SELECT j.id, j.conversation_id, j.tool_name, j.state, j.result, "
                    "j.created_at, j.finished_at, tc.arguments FROM jobs j LEFT JOIN "
                    "tool_calls tc ON tc.id = j.tool_call_id ORDER BY (j.state = "
                    "'running') DESC, j.created_at DESC LIMIT ?", (limit,)).fetchall():
                said = c.execute("SELECT i.id FROM intercom i, json_each(i.data, '$.jobs') "
                                 "je WHERE i.kind = 'job' AND je.value = ? LIMIT 1",
                                 (j["id"],)).fetchone()
                got.append({**dict(j), "said": said[0] if said else None,
                            "src": rows.source_of(c, f"agent:{j['conversation_id']}") or {}})
            return got
        found = await ctx.store.read(fn)
        if not found:
            return ToolResult(content="No jobs yet.", display={"jobs": 0})
        lines = []
        for j in found:
            asked = " ".join((words.prompt_of(j["arguments"], j["tool_name"]) or "").split())
            if len(asked) > TEXT_MAX:
                asked = asked[:TEXT_MAX - 1].rstrip() + "\u2026"
            what = j["tool_name"] + (f" `{asked}`" if asked else "")
            src = j["src"]
            where = (f"{src.get('kind', 'conversation')} \"{src.get('title') or 'Untitled'}\" "
                     f"({j['conversation_id'][:8]})")
            if j["state"] == "running":
                lines.append(f"- job {j['id'][:8]} {what} in {where}: running since "
                             f"{when(j['created_at'], self.tz)}")
                continue
            first = next((x for x in (j["result"] or "").splitlines() if x.strip()), "")
            lines.append(f"- job {j['id'][:8]} {what} in {where}: {j['state']} "
                         f"{when(j['finished_at'] or j['created_at'], self.tz)}" +
                         (f", result #{j['said']}" if j["said"] else "") +
                         (f": {_short(first)}" if first else ""))
        return ToolResult(content="\n".join(lines), display={"jobs": len(found)})

    async def offered_in(self, store, cid: str) -> bool | set[str]:
        """start_chat only away from the user: a background task or run, a mission."""
        kind = await store.read(lambda c: (c.execute(
            "SELECT kind FROM conversations WHERE id=?", (cid,)).fetchone() or [None])[0])
        return True if kind in AWAY else {"send", "read", "cancel"}

    @tool(read_only=False, destructive=False)
    async def start_chat(
        self,
        ctx: ToolContext,
        title: Annotated[str, "The chat's name: its topic in 2 to 4 plain words, like "
                              "\"Shell wedge\" or \"Invoice due\". Not a sentence, not "
                              "a headline."],
        text: Annotated[str, "What {{username}} reads first: what happened and what it "
                             "needs from them, short and complete."],
    ) -> ToolResult:
        """Start a chat for {{username}} about something they need to act on or know now.
        Check intercom__read first: when they have it already (a thread, an open bug
        report), add to that thread instead. Your text is your first message in the new
        chat and waits for them on the intercom; when they answer, you go on there."""
        if not title.strip() or not text.strip():
            return ToolResult(content="title and text are both needed.", is_error=True)
        new, iid, has = await self._chat(ctx, title, text)
        return ToolResult(content=f"Started the chat \"{title}\" (/chat/{new}); it waits "
                                  f"for {self.cfg.user} as #{iid}.{self._had(has)}",
                          display={"chat": new, "id": iid})

    @tool(read_only=False, destructive=True)
    async def cancel(
        self,
        ctx: ToolContext,
        id: Annotated[int, "The number of a scheduled message."],
    ) -> ToolResult:
        """Cancel a message scheduled for later that has not landed yet. It stays listed
        as cancelled."""
        if not await self.app.intercom.cancel(id):
            return ToolResult(content=f"#{id} is not a scheduled message that is still "
                                      "waiting.", is_error=True)
        return ToolResult(content=f"Cancelled #{id}.", display={"cancelled": id})


def _here(c, cid: str, turn: str | None) -> dict | None:
    """The message in a thread this turn took in (store.answering), as store.view."""
    r = rows.answering(c, cid, turn) if turn else None
    if r is None:
        return None
    sub = c.execute("SELECT agent_of FROM conversations WHERE id=?", (cid,)).fetchone()
    return {**rows.view(c, r["id"], cid), "sub_agent": bool(sub and sub["agent_of"])}


def _team_of(c, cid: str) -> list[dict]:
    """This conversation's own sub-agents, newest first, whether each is working (not
    quiet: sub_agents/report.py, the one definition), and a drone's device and step."""
    from ..sub_agents.report import quiet
    return [{"name": r["agent_name"], "working": not quiet(c, r["id"]),
             "device": r["agent_device"], "step": r["agent_step"]} for r in c.execute(
        "SELECT id, agent_name, agent_device, agent_step FROM conversations "
        "WHERE agent_of=? ORDER BY created_at DESC", (cid,)).fetchall()]


def _sources(c, cid: str) -> set[str]:
    """The roots that count as the same source as `cid`: its root, and for a Routine's
    run every run of the same routine."""
    top = homes.root(c, cid)
    if top is None:
        return set()
    out = {top["id"]}
    key = top["trigger_key"] or ""
    if key.startswith("run:"):
        out |= {r[0] for r in c.execute(
            "SELECT conversation_id FROM runs WHERE conversation_id IS NOT NULL AND "
            "schedule = (SELECT schedule FROM runs WHERE id=?)", (key[4:],))}
    return out


def covered(c, cid: str, since: str, tz) -> list[str]:
    """What the user got from the source of `cid` since `since`, for it to judge: its bug
    reports still open, and its threads to them (sent from there, or a chat start_chat
    made there), each with what it says and whether they answered. -> one line each,
    like 'bug #2 "Shell wedge" (open, Fri 23 Oct 09:00): The shell hangs.'"""
    mine = _sources(c, cid)

    def ours(addr_or_cid: str | None) -> bool:
        top = homes.root(c, (addr_or_cid or "").removeprefix("agent:"))
        return top is not None and top["id"] in mine

    out, bug_threads = [], set()
    for r in c.execute("SELECT id, title, what, conversation_id, thread, created_at FROM "
                       "bugreports WHERE state='open' AND created_at >= ? ORDER BY id",
                       (since,)).fetchall():
        if ours(r["conversation_id"]):
            out.append(f'bug #{r["id"]} "{r["title"]}" (open, '
                       f'{when(r["created_at"], tz)}): {_short(r["what"])}')
            bug_threads.add(r["thread"])
    seen = set()
    for r in c.execute("SELECT thread, subject, text, from_addr, json_extract(data, '$.from') "
                       "AS src, created_at FROM intercom WHERE to_addr='user' AND "
                       "created_at >= ? ORDER BY id DESC", (since,)).fetchall():
        if r["thread"] in seen or r["thread"] in bug_threads:
            continue
        if ours(r["src"]) or (r["from_addr"].startswith("agent:") and ours(r["from_addr"])):
            seen.add(r["thread"])
            subject = f' "{r["subject"]}"' if r["subject"] else ""
            answered = c.execute("SELECT 1 FROM intercom WHERE thread=? AND from_addr='user'",
                                 (r["thread"],)).fetchone()
            out.append(f"thread #{r['thread']}{subject} ({when(r['created_at'], tz)}, "
                       f"{'answered' if answered else 'not answered yet'}): "
                       f"{_short(r['text'])}")
    return out


def opening(c, src: str, call_id: str, has: list[str], user: str) -> str:
    """What it reads before its first message in a chat it started for the user (_chat):
    where it came from with its link and the id past_conversations__read takes (a run's
    `run:<id>`, else the conversation's), that source's last tool calls, and what the
    user had from there already. Fixed when the chat starts, so the request stays the
    same."""
    s = rows.source_of(c, f"agent:{src}") or {}
    key = (c.execute("SELECT trigger_key FROM conversations WHERE id=?",
                     (src,)).fetchone() or [None])[0] or ""
    ref = key if key.startswith("run:") else src[:8]
    lines = [f"You started this chat from a {s.get('kind', 'conversation')} "
             f"\"{s.get('title') or 'Untitled'}\" ({s.get('link') or src}); "
             f"past_conversations__read(conversation=\"{ref}\") reads it. Your first "
             f"message below is what {user} read on the intercom."]
    calls = c.execute(
        "SELECT tc.name, tc.arguments FROM tool_calls tc JOIN messages m ON m.id = "
        "tc.message_id WHERE m.conversation_id=? AND tc.id <> ? ORDER BY tc.rowid DESC "
        "LIMIT ?", (src, call_id, ACTIONS)).fetchall()
    if calls:
        lines.append("Its last actions, newest first: " + "; ".join(
            f"{r['name']}({_short(r['arguments'] or '').rstrip('.')})" for r in calls) + ".")
    if has:
        lines.append(f"{user} had from there already: " + "; ".join(has) + ".")
    return "\n".join(lines)
