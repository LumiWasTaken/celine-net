"""The agent's sub-agents: a brief handed to a new conversation of its own, so its context stays
small.

A sub-agent is a conversation of kind `agent` (it works in its notebook) on the tasks
role (settings/roles.py), carrying what any conversation carries (turns/loads.py: the
always notes, every tool offered at its place), with four extra columns: `agent_of`
(the conversation that started it), `agent_name` (the name it gave it, unique among
the sub-agents of that conversation, its handle for it), `notebook_folder` (the
notebook folder it is limited to; NULL is the whole notebook, enforced in
skills/notebook.py) and `access` ("read": only read-only tools and the intercom,
enforced in turns/runner.py and turns/tools.py; "work": every tool it carries).
Its brief is an intercom `brief` from its conversation; a follow-up is any intercom
message to it, and `start` with the name of one that is done sends the new brief to it.
Its turns run at agent priority on their own dispatcher worker. It gets its persona,
not the parent conversation. When it is done, its report comes back as an intercom
`report` (report.py). `start` returns at once; `read` looks at one, and waits for it
when asked. `model` picks its model among its own (settings/her_models.py), kept as its
conversation's model; a cloud model (her_models.cloud) waits for the user's approval at
every start. `cli` hands its briefs to an agent CLI in its shellbox instead (`agent_cli`,
cli.py).

A sub-agent starts sub-agents of its own, down to MAX_DEPTH levels below the top of
the chain (conversations/store.depth reads the agent_of chain); each reports to the one
that started it.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Literal

import anyio

from .. import homes, metrics, prompts
from ..conversations import store as conversations, tree
from ..core.ids import now
from ..skills import ToolContext, ToolResult, guide, knob, tool, described
from ..settings import her_models
from ..skills import words
from ..skills.gating import overrides_for
from . import report
from ..guard.sources import agent_read_outside
from .cli import CLIS
from ..core.event_types import AGENT_STATE

MIN_NAME = 5
MAX_NAME = 60
MAX_WAIT = 600      # seconds `read` waits for a sub-agent at most
MAX_DEPTH = 3       # levels of sub-agents below the top conversation
BRIEF_FACT = 160    # characters of the brief on a cloud model's approval
CLOUD_ASK = "start the sub-agent {name} on the cloud model {model}"
# Settings, Advanced, read at use (cfg.r)
KNOBS = (
    knob("sub_agents.max_running", "Sub-agents at once", None, 1, 1000, 4,
         "Per conversation; a start over it waits its turn."),
    knob("sub_agents.cli_minutes", "Agent CLI time limit", "minutes", 1, 100_000, 120,
         "One sub-agent that works through an agent CLI in its shellbox."),
)


async def model_for_sub_agent(store, parent_cid: str) -> dict:
    """The model settings a new sub-agent's conversation starts with ({"model"}, as in
    settings/conversations): the parent conversation's own model when it has one
    (a sub-agent runs on the mode it was started with). Its role and
    thinking are its mode's."""
    from ..settings import conversations as conv
    own = await conv.get(store, parent_cid) or {}
    return {"model": own.get("model")}


async def reads_only(store, cid: str) -> bool:
    """A sub-agent started in read mode: only read-only tools are in its list, and a
    call to any other is refused."""
    row = await store.read(lambda c: c.execute(
        "SELECT agent_of, access FROM conversations WHERE id=?", (cid,)).fetchone())
    return row is not None and bool(row["agent_of"]) and row["access"] == "read"


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


class SubAgents:
    name = "sub_agents"
    guide = guide("sub_agents")
    description = described()

    app = None      # the running app (core/services.py), from skills.build

    def __init__(self, cfg):
        self.cfg = cfg
        # checking the limit and names, then queueing a turn, is one step: several
        # calls of one step run at once (turns/tools.py)
        self._queueing = anyio.Lock()

    @property
    def dispatcher(self):
        return self.app.dispatcher if self.app is not None else None

    async def offered_in(self, store, cid: str) -> bool:
        """Above MAX_DEPTH: the deepest starts, reads and stops none."""
        return store is None or await store.read(
            lambda c: conversations.depth(c, cid)) < MAX_DEPTH

    @tool(read_only=False, destructive=False)
    async def start(
        self,
        ctx: ToolContext,
        brief: Annotated[str, "Everything it needs: what to do, where to look, and the "
                              "exact form of the report. It does not see this "
                              "conversation."],
        name: Annotated[str, "A name of at least 5 characters, unique in this "
                             "conversation. The name of one that is done gives it this "
                             "brief."],
        access: Annotated[Literal["read", "work"] | None,
                          "read: it changes nothing (in a browser it looks and "
                          "scrolls, and cannot open or act). work: it may write and act. Default: read, "
                          "or work when you give a folder."] = None,
        folder: Annotated[str | None, "A notebook folder, such as agents/scout. It sees "
                                      "and writes only that folder."] = None,
        model: Annotated[str | None, "One of your models (models__list). Default: the "
                                     "model this conversation runs on."] = None,
        cli: Annotated[Literal[*CLIS] | None,
                       "An agent CLI in your shellbox that works on the brief instead, "
                       "in ~/agents/<its terminal>."] = None,
    ) -> ToolResult:
        """Start a sub-agent: a new conversation with your personality and notebook, and
        one brief. For work that would fill your context: many searches, many notes,
        long files, a website to go through. Not for one lookup that one call does.
        Example: sub_agents__start(brief="Which of my notes mention the beach trip?
        Only the paths, one per line.", name="trip finder")
        Returns at once. End your turn to wait, or sub_agents__read with wait."""
        store = ctx.store
        sub, refusal, resumed, brief_id = await self.begin(
            store, ctx.conversation_id, brief, name, access, folder, model, cli, asker=ctx)
        if refusal:
            return _error(refusal)
        [row] = await store.read(lambda c: agents_of(c, agent=sub))
        said = (f"resumed {row['name']} (brief #{brief_id})." if resumed else
                f"{row['name']} started (brief #{brief_id}).")
        if row["queued"]:
            said = (f"{row['name']} is queued (brief #{brief_id}): {max_running(self.cfg)} of yours "
                    f"are working, and it starts when one of them is done.")
        return ToolResult(content=f"{said} Its report comes here as a message.",
                          display={"name": row["name"], "conversation_id": sub,
                                   "link": row["link"], "access": row["access"], "folder": row["folder"],
                                   "model": row["model"], "cli": row["cli"],
                                   "resumed": resumed})

    @tool(timeout=MAX_WAIT + 30)
    async def read(
        self,
        ctx: ToolContext,
        name: Annotated[str, "The name of the sub-agent."],
        wait: Annotated[int, "Seconds to wait for it to be done, at most 600. 0 looks "
                             "at once."] = 0,
    ) -> ToolResult:
        """Look at one of your sub-agents: working or done, the step it is on, and its
        latest report (or, before one, its last words). With wait, first wait until it
        is done or the seconds run out."""
        sub, refusal = await _find(ctx, name)
        if refusal:
            return refusal
        limit = min(max(int(wait or 0), 0), MAX_WAIT)
        if ctx.time_left is not None:
            limit = min(limit, max(int(ctx.time_left), 0))
        waited = 0
        # note: a look every second, the store answers it from one indexed read
        while waited < limit and (await ctx.store.read(
                lambda c: _look(c, sub["id"])))["working"]:
            await anyio.sleep(1)
            waited += 1
        f = await ctx.store.read(lambda c: _look(c, sub["id"]))
        outside = await ctx.store.read(lambda c: agent_read_outside(c, sub["id"]))
        working = f["working"]
        state = "queued" if f["queued"] else "working" if working else "done"
        lines = [f"{sub['name']}: {state}."]
        if limit:
            lines.append(f"It finished after {waited} s of waiting." if not working else
                         f"The wait of {limit} s ran out.")
        if working and f["step"]:
            lines.append(f"Now: {f['step']}.")
        if f["report"] is not None:
            lines.append(f"Its latest report (#{f['report']['id']}):\n\n"
                         f"{f['report']['text']}")
        elif f["last"]:
            lines.append(f"No report yet. Its last words:\n\n{f['last']}")
        else:
            lines.append("No report yet.")
        return ToolResult(content="\n".join(lines), display={
            "name": sub["name"], "conversation_id": sub["id"], "link": sub["link"],
            "state": state,
            # it read text others wrote: so does it now (guard/sources.py)
            **({"outside": True} if outside else {})})

    @tool(read_only=False, destructive=True)
    async def stop(
        self,
        ctx: ToolContext,
        name: Annotated[str, "The name of the sub-agent."],
    ) -> ToolResult:
        """Stop one of your sub-agents: its turn, its jobs, and the messages it
        scheduled that have not landed. Its report then says what it did until then."""
        sub, refusal = await _find(ctx, name)
        if refusal:
            return refusal
        await self.stop_agent(sub["id"])
        return ToolResult(content=f"Stopped {sub['name']}.",
                          display={"name": sub["name"], "conversation_id": sub["id"],
                                   "link": sub["link"]})

    async def _cloud_approval(self, ctx: ToolContext, brief: str, name: str,
                              model: str | None, known: dict | None) -> str | None:
        """The user's approval when the sub-agent would run on a cloud model: a resumed
        one's own, else the one given, else this conversation's (_settings), else the
        first of its role's chain (settings/roles.py: tasks, an empty one chat's). None
        when it may start, else the sentence for its result."""
        from ..settings.roles import role_of
        s = self.cfg.settings
        if known is not None:
            model = known["model"]
        elif model is None:
            model = (await model_for_sub_agent(ctx.store, ctx.conversation_id))["model"]
        model = model or next(iter(s.chain(role_of("agent")) or s.chain("chat")), None)
        if not model or not her_models.cloud(s, model):
            return None
        approvals = getattr(self.app, "approvals", None)
        if approvals is None:
            return "Nobody can approve a cloud model here: it did not start."
        action = CLOUD_ASK.format(name=" ".join(name.split()),
                                  model=her_models.label(s, model))
        summary = " ".join(brief.split())
        if len(summary) > BRIEF_FACT:
            summary = summary[:BRIEF_FACT - 1].rstrip() + "\u2026"
        return await approvals.ask(action, [f"brief: {summary}"], ctx.conversation_id,
                                   short=action, kind="model",
                                   call=ctx.tool_call_id or None)


    def _folder(self, folder: str, create: bool) -> tuple[str | None, str | None]:
        """A relative notebook folder; one that does not exist only for a sub-agent that
        writes (`create`), made by begin. (folder, refusal)."""
        root = self.cfg.her.notebook.resolve()
        rel = str(Path(folder.strip().rstrip("/")))
        p = (root / rel).resolve()
        if rel in ("", ".") or not p.is_relative_to(root) or p == root:
            return None, (f"{folder!r} is not a folder inside your notebook. Leave "
                          f"`folder` out for the whole notebook.")
        if rel == "tools" or rel.startswith("tools/"):
            return None, "tools/ is not part of your notebook."
        if p.exists() and not p.is_dir():
            return None, f"{rel} is a file. `folder` is a folder."
        if not p.exists() and not create:
            return None, (f"There is no folder {rel} in your notebook to read. "
                          f"Give it access work to let it write there.")
        return rel, None

    async def _check(self, store, parent: str, name: str, given: dict, access: str,
                     folder: str | None) -> tuple[list, dict | None, str | None, str | None]:
        """What begin refuses once it knows the parent's sub-agents, reading only:
        -> (them, the one called `name` or None, the folder, None) or the refusal last."""
        mine = await store.read(lambda c: agents_of(c, parent))
        known = next((a for a in mine if _same(a["name"], name)), None)
        if known is not None and known["working"]:
            return mine, known, None, (
                f"{known['name']} is still working. Write to it with "
                f"intercom__send(to={known['name']!r}), or pick another name.")
        if known is not None and any(v is not None and v != known[k]
                                     for k, v in given.items()):
            where = f" and the folder {known['folder']}" if known["folder"] else ""
            where += f", the model {known['model']}" if known["model"] else ""
            where += f", the CLI {known['cli']}" if known["cli"] else ""
            return mine, known, None, (
                f"{known['name']} exists with {known['access']} access{where}; start it "
                f"under another name or leave access, folder, model and cli out.")
        if known is not None:
            return mine, known, None, None
        if (refusal := await self._over_cap(store, parent, len(mine))):
            return mine, None, None, refusal
        if folder is None:
            return mine, None, None, None
        folder, refusal = self._folder(folder, create=access == "work")
        return mine, None, folder, refusal

    async def begin(self, store, parent: str, brief: str, name: str,
                    access: str | None = None, folder: str | None = None,
                    model: str | None = None, cli: str | None = None,
                    asker: ToolContext | None = None
                    ) -> tuple[str | None, str | None, bool]:
        """Start a sub-agent of `parent`, in the parent's mode: its conversation,
        settings and brief; the name of one that is done gives that one the brief,
        refused when a given access or folder is not its own. The agent's tool and the API
        share it; the tool's call (`asker`) asks the user first for a cloud model
        (_cloud_approval). -> (its conversation id, None, resumed, its brief's number) or
        (None, the refusal, False, None)."""
        name = " ".join(name.split())
        given = {"access": access,
                 "folder": str(Path(folder.strip().rstrip("/"))) if folder else None,
                 "model": model, "cli": cli}
        if len(name) < MIN_NAME:
            return None, (f"Give it a name of at least {MIN_NAME} characters, so you "
                          f"can keep your sub-agents apart."), False, None
        if len(name) > MAX_NAME:
            return None, f"Keep the name under {MAX_NAME} characters.", False, None
        if not brief.strip():
            return None, "The brief is empty. Say what it must do.", False, None
        access = access or ("work" if folder is not None else "read")
        if access not in ("read", "work"):
            return None, "access is read or work.", False, None
        if model is not None and model not in her_models.allowed(self.cfg.settings):
            yours = ", ".join(her_models.allowed(self.cfg.settings)) or "none"
            return None, f"{model} is not one of your models. Yours: {yours}.", False, None
        if cli is not None and cli not in CLIS:
            return None, f"cli is one of {', '.join(CLIS)}.", False, None
        if (deep := await store.read(lambda c: conversations.depth(c, parent))) >= MAX_DEPTH:
            return None, self.cfg.substitute(
                f"Sub-agents go at most {MAX_DEPTH} levels below {{{{name}}}}, and this "
                f"conversation is {deep} below: it starts none."), False, None
        if self.dispatcher is None:         # tests run turns without one
            return None, ("Sub-agents cannot run here: nothing is there to run their "
                          "turns."), False, None
        _, known, _, refusal = await self._check(store, parent, name, given, access, folder)
        if refusal:
            return None, refusal, False, None
        # the user's answer may take minutes: asked outside the lock, checked again in it
        if asker is not None and (no := await self._cloud_approval(
                asker, brief, name, model, known)):
            return None, no, False, None
        async with self._queueing:
            mine, known, folder, refusal = await self._check(
                store, parent, name, given, access, folder)
            if refusal:
                return None, refusal, False, None
            if known is not None:
                sub = known["id"]
            else:
                if folder is not None and access == "work":
                    (self.cfg.her.notebook.resolve() / folder).mkdir(parents=True,
                                                                     exist_ok=True)
                sub = await conversations.create(
                    store, title=name, agent_of=parent, agent_name=name,
                    notebook_folder=folder, access=access, agent_cli=cli)
                await self._settings(store, parent, sub, model)
            # the brief lands without waking it; release starts it now, or, over
            # max_running, when one of the parent's sub-agents is done
            sent = await self.app.intercom.send(f"agent:{parent}", f"agent:{sub}", brief,
                                                kind="brief", wake=False)
            await release(self.app, parent)
        await announce(self.app, sub)
        metrics.SUB_AGENTS_STARTED.labels("cli" if (cli or (known or {}).get("cli"))
                                          else "model").inc()
        return sub, None, known is not None, int(sent["intercom_id"]) if sent else None

    async def _over_cap(self, store, parent: str, started: int) -> str | None:
        """The refusal when `parent` is a mission that started its Sub-agents per mission
        (Settings, Advanced; 0 none) already, else None."""
        cap = self.cfg.r("mission_max_sub_agents")
        if not cap or started < cap or not await store.read(lambda c: c.execute(
                "SELECT 1 FROM conversations WHERE id=? AND kind='mission'",
                (parent,)).fetchone()):
            return None
        return (f"This mission has started its {cap} sub-agent{'' if cap == 1 else 's'}, "
                f"the most it may. Give one that is done a new brief under its name.")

    async def stop_agent(self, sub: str) -> None:
        """Its turn, its jobs, the messages it scheduled that have not landed, and the
        queued briefs of it and every sub-agent under it, so no release starts one."""
        def fn(c: sqlite3.Connection) -> None:
            for one in conversations.family(c, sub):
                c.execute("UPDATE intercom SET cancelled_at=? WHERE id IN (SELECT i.id FROM "
                          "intercom i JOIN intercom_deliveries d ON d.intercom_id = i.id "
                          f"WHERE {queued_sql()})", (now(), one))
        await self.app.store.write(fn)
        await self.app.intercom.cancel_scheduled(sub)
        await self.dispatcher.abort_conversation(sub)
        await announce(self.app, sub)

    async def _settings(self, store, parent: str, sub: str, model: str | None = None) -> None:
        """Its model (the one it picked, else the parent's, model_for_sub_agent), the
        parent's automatic context switch (api/context.py) and the parent's tool
        switches, so a tool that is off in the parent conversation is off for its
        sub-agents too."""
        from ..settings import conversations as conv
        from ..conversations.store import inherit_auto_context
        await store.write(lambda c: inherit_auto_context(c, parent, sub))
        picked = {"model": model} if model else await model_for_sub_agent(store, parent)
        clean = {k: v for k, v in picked.items() if v is not None}
        tools = await overrides_for(store, parent)
        if tools:
            clean["tools"] = tools
        if clean:
            await conv.apply(store, sub, clean)

    async def prompt_fragment(self, ctx: ToolContext) -> str | None:
        """Only in a sub-agent's own conversation: who it is, who started it, what it is
        for, that its last message is its report and how much of it goes up."""
        row = await ctx.store.read(lambda c: c.execute(
            "SELECT s.agent_of, s.agent_name, s.notebook_folder, s.access, "
            "p.agent_name AS parent FROM conversations s LEFT JOIN conversations p "
            "ON p.id = s.agent_of WHERE s.id=?", (ctx.conversation_id,)).fetchone())
        if row is None or not row["agent_of"]:
            return None
        cap = await ctx.store.read(lambda c: report.cap(c, row["agent_of"]))
        folder = row["notebook_folder"]
        if row["access"] == "read":
            can = ("You were started to look things up: you have only tools that read, "
                   "and you change nothing.")
            where = (f"You see only `{folder}` of {{{{name}}}}'s notebook." if folder
                     else "You see {{name}}'s whole notebook.")
        else:
            can = "You may write where the brief asks you to."
            where = (f"Your notebook here is {{{{name}}}}'s notebook, limited to "
                     f"`{folder}`: you see and write only that folder." if folder else
                     "Your notebook here is {{name}}'s whole notebook. Write what the "
                     "brief asks for and leave the rest as it is.")
        return prompts.fill(self.cfg, __file__, "fragment.md", agent_name=row["agent_name"],
                            parent=row["parent"] or "{{name}}", cap=f"{cap:,}",
                            can=can, where=where)


def max_running(cfg) -> int:
    """How many sub-agents of one parent run at once; the others wait queued."""
    return int(cfg.r("sub_agents.max_running"))


def agents_of(c: sqlite3.Connection, parent: str | None = None, *,
              agent: str | None = None) -> list[dict]:
    """Sub-agents, newest first, as the overview shows them: who, where, its `home` and
    `link` (homes.of: where it opens), working (report.quiet says), the step at work,
    the latest report, its last words. Those of `parent`, the one `agent`, or (neither
    given) every one not in the archive."""
    if agent:
        where, args = "WHERE s.id = ? AND s.agent_of IS NOT NULL", (agent,)
    elif parent:
        where, args = "WHERE s.agent_of = ?", (parent,)
    else:
        where, args = "WHERE s.agent_of IS NOT NULL AND s.archived_at IS NULL", ()
    rows = c.execute(
        f"SELECT s.id, s.agent_of AS parent, s.agent_name AS name, s.access, "
        f"s.notebook_folder AS folder, s.model_id AS model, s.agent_cli AS cli, "
        f"s.created_at, s.updated_at FROM conversations s "
        f"{where} ORDER BY s.created_at DESC, s.id DESC", args).fetchall()
    return [{**dict(r), **_look(c, r["id"]), **homes.of(c, r["id"])} for r in rows]


ENDED = {"aborted": "stopped", "failed": "failed", "interrupted": "failed",
         "capped": "paused"}     # how a done one's last turn ended, in the tree's words


def tree_of(c: sqlite3.Connection, cid: str) -> dict | None:
    """Every sub-agent under the top of `cid`'s chain, each before its own and siblings
    oldest first: id, parent, depth, name, state (queued, working, done, stopped, failed,
    paused), step_words, model (the newest that answered, else its own), what all its
    turns took (report.work) and link. None: no such conversation."""
    top = homes.root(c, cid)
    if top is None:
        return None
    out: list[dict] = []

    def walk(parent: str, depth: int) -> None:
        for r in c.execute("SELECT id, agent_name, model_id FROM conversations WHERE "
                           "agent_of=? ORDER BY created_at, id", (parent,)).fetchall():
            f, w = _look(c, r["id"]), report.work(c, r["id"], whole=True)
            last = c.execute("SELECT state FROM turns WHERE conversation_id=? ORDER BY "
                             "created_at DESC LIMIT 1", (r["id"],)).fetchone()
            state = "queued" if f["queued"] else "working" if f["working"] else \
                ENDED.get(last[0] if last else "", "done")
            out.append({"id": r["id"], "parent": parent, "depth": depth,
                        "name": r["agent_name"], "state": state,
                        "step_words": f["step_words"] if f["working"] else None,
                        "model": (w["models"] or [r["model_id"]])[-1],
                        "prompt_tokens": w["prompt_tokens"],
                        "completion_tokens": w["completion_tokens"],
                        "seconds": w["seconds"], "link": homes.of(c, r["id"])["link"]})
            walk(r["id"], depth + 1)
    walk(top["id"], 1)
    return {"top": top["id"], "agents": out}


def queued_sql(conversation: str = "?") -> str:
    """A queued brief, over intercom i and intercom_deliveries d: landed in
    `conversation` (a placeholder or a column) without waking its sub-agent, and no turn
    answers it yet."""
    return (f"d.conversation_id = {conversation} AND i.kind='brief' AND i.wake=0 AND "
            "i.cancelled_at IS NULL AND d.state <> 'cancelled' AND d.turn_id IS NULL AND "
            "d.message_id IS NOT NULL")


def waiting(c: sqlite3.Connection, sub: str) -> tuple[int, str] | None:
    """`sub`'s queued brief: (its intercom id, its message id), or None."""
    r = c.execute(f"SELECT i.id, d.message_id FROM intercom i JOIN intercom_deliveries d "
                  f"ON d.intercom_id = i.id WHERE {queued_sql()} ORDER BY i.id LIMIT 1",
                  (sub,)).fetchone()
    return (r["id"], r["message_id"]) if r else None


async def release(app, parent: str) -> None:
    """Start the queued sub-agents of `parent`, oldest brief first, while fewer than
    max_running of them run. One write: two releases never start one twice."""
    limit = max_running(app.cfg)

    def fn(c: sqlite3.Connection) -> list[str]:
        mine = agents_of(c, parent)
        free = limit - sum(1 for a in mine if a["working"] and not a["queued"])
        queued = sorted((waiting(c, a["id"]), a["id"]) for a in mine if a["queued"])
        started = []
        for (iid, mid), sub in queued[:max(free, 0)]:
            tid, _ = tree.queue_turn(c, app.store, sub, mid, origin="intercom")
            c.execute("UPDATE intercom_deliveries SET turn_id=? WHERE intercom_id=? AND "
                      "conversation_id=?", (tid, iid, sub))
            started.append(sub)
        return started
    started = await app.store.write(fn)
    if started:
        app.dispatcher.notify()
    for sub in started:
        await announce(app, sub)


async def release_all(app) -> None:
    """At start: every parent with a queued sub-agent gets its free places filled."""
    parents = await app.store.read(lambda c: [r[0] for r in c.execute(
        f"SELECT DISTINCT o.agent_of FROM conversations o JOIN intercom_deliveries d ON "
        f"d.conversation_id = o.id JOIN intercom i ON i.id = d.intercom_id WHERE "
        f"o.agent_of IS NOT NULL AND {queued_sql('o.id')}")])
    for parent in parents:
        await release(app, parent)


async def announce(app, sub: str) -> None:
    """`agent.state` on the global feed: a client keeps its overview without a refetch.
    Sent when a sub-agent starts, stops, and whenever one of its turns or jobs ends."""
    rows = await app.store.read(lambda c: agents_of(c, agent=sub))
    if rows:
        app.store.emit_global({"type": AGENT_STATE, **rows[0]})


def _look(c: sqlite3.Connection, sub: str) -> dict:
    """What `read` shows of sub-agent `sub`: whether it is quiet (report.quiet), the
    tool its live turn runs, its latest report and its last words."""
    step = c.execute(
        "SELECT tc.name FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
        "WHERE m.conversation_id=? AND tc.state IN ('pending', 'running', 'detached') "
        "ORDER BY tc.started_at DESC LIMIT 1", (sub,)).fetchone()
    rep = c.execute("SELECT id, text FROM intercom WHERE from_addr=? AND kind='report' "
                    "ORDER BY id DESC LIMIT 1", (f"agent:{sub}",)).fetchone()
    quiet = report.quiet(c, sub)
    queued = waiting(c, sub) is not None
    # a queued one is working too: it has a brief to do (release)
    return {"quiet": quiet, "queued": queued, "working": queued or not quiet,
            "step": step["name"] if step else None,
            "step_words": words.step(step["name"]) if step else None, "report": dict(rep) if rep else None,
            "last": report.last_answer(c, sub)}


def _error(text: str) -> ToolResult:
    return ToolResult(content=text, is_error=True)


async def _find(ctx: ToolContext, name: str) -> tuple[dict | None, ToolResult | None]:
    """The sub-agent of this conversation called `name`, or the error that names its own."""
    name = " ".join(name.split())
    mine = await ctx.store.read(lambda c: agents_of(c, ctx.conversation_id))
    sub = next((a for a in mine if _same(a["name"], name)), None)
    if sub is None:
        return None, _error(f"No sub-agent called {name!r} here." + (
            f" Yours: {', '.join(a['name'] for a in mine)}." if mine else ""))
    return sub, None
