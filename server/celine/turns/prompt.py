"""The prompt of a turn: the system prompt's pieces (system_blocks), the tail of now
(tail_text, stored once on the message the turn answers), the tools a request carries (specs, wire_tools) and the last request as
it went out (keep_request, last_request: /usage). The turn loop is turns/runner.py; the
Context tab, /compact, /usage and impersonate read the same assembly from here."""
from __future__ import annotations

import json

import anyio

from .. import clock, devices, guard
from ..plans import flow as plan_flow
from ..skills import CORE, ToolContext
from ..skills.gating import OFF, down_sentence, is_enabled, overrides_for
from . import legend, loads

def supports_prefill(backend) -> bool:
    """Whether a final assistant message is continued by the server (llama.cpp with
    thinking off; a queued backend turns thinking off for that step, _stream_one)."""
    p = getattr(backend, "supports_prefill", None)
    if p is not None:
        return bool(p)
    cfg = getattr(backend, "cfg", None)
    if cfg is None:
        return False
    raw = getattr(cfg, "raw", None) or {}
    if getattr(cfg, "kind", "") != "llama-server":
        return False
    kwargs = (raw.get("llama") or {}).get("chat_template_kwargs") or {}
    return hasattr(backend, "for_message") or not bool(kwargs.get("enable_thinking", False))


async def tool_specs(registry, store, cid: str, carried, without=frozenset()
                 ) -> tuple[dict, bool]:
    """The tools this conversation has, and whether it may only read: the tools its
    skills offer here (turns/loads.py: a skill with offered_in() offers its tools
    only where that says so, missions in a mission's own conversation; it may also name
    the tools it offers there, as a set), in registry order, but those named in
    `without`; a sub-agent started with read access is offered only what it may call
    (loads.offered; turns/tools.py refuses the rest)."""
    if not registry:
        return {}, False
    here = await loads.offered(registry, store, cid)
    names = {s.wire_name for n in [*carried.skills, *carried.others]
             for s in here.get(n, ())} - set(without)
    tools = {k: s for k, s in registry.tools.items() if k in names}
    from ..sub_agents.skill import reads_only
    return tools, await reads_only(store, cid)


def wire_tools(specs, registry, overrides, cfg) -> list[dict]:
    """The tools it gets in this request: those of `specs` that are on (skills/
    gating.py) and whose skill is up, a core skill's with their parameters, every
    other by name and one line (skills.ToolSpec.as_name). Loads never change it: only
    the switches and a skill going down or up do. A tool that is off or down is not
    sent at all (an empty stub got called with no arguments in a loop): the skill index
    says so (unusable), and a call to it is refused with why (turns/tools.py).
    Descriptions are filled in at registration (skills.Registry.add_specs)."""
    return [s.as_wire() if s.skill in CORE else s.as_name()
            for s in specs.values()
            if registry.down(s.skill) is None and is_enabled(s, overrides, cfg)]


def unusable(specs, registry, overrides, cfg) -> list[str]:
    """One line per skill offered here that sends no tool now: down with its reason, or
    off. In the tail (tail_text)."""
    out = []
    for skill in dict.fromkeys(s.skill for s in specs.values()):
        mine = [s for s in specs.values() if s.skill == skill]
        if why := registry.down(skill):
            out.append(cfg.substitute(down_sentence(skill, why)))
        elif not any(is_enabled(s, overrides, cfg) for s in mine):
            out.append(OFF.format(name=skill))
    return out


async def request_parts(store, cfg, registry, backend, cid: str):
    """The system prompt's pieces, named (system_blocks), the tool list a request of
    this conversation carries now and what it carries (turns/loads.py), built as a turn
    builds them (for /compact, /usage and what it sees)."""
    overrides = await overrides_for(store, cid)
    carried = await loads.select(store, cfg, registry, cid, backend)
    specs, _ = await tool_specs(registry, store, cid, carried)
    tools = (wire_tools(specs, registry, overrides, cfg)
             if registry and getattr(backend, "supports_tools", False) else [])
    blocks = await system_blocks(cfg, registry, store, cid, "", carried,
                                 getattr(backend, "role", None))
    return blocks, tools, carried


def _request_file(cfg, cid: str):
    return cfg.data_dir / "requests" / f"{cid}.json"


def request_rows(pieces: list[str], messages: list[dict], tools: list[dict]) -> list[dict]:
    """A request as it reads it (/usage shows it): the system prompt's pieces and what
    the build added to them, the tools' names, then the messages."""
    system = "\n\n".join(pieces)
    head = messages[0]["content"] if messages and messages[0]["role"] == "system" else None
    added = head[len(system):].strip() if isinstance(head, str) else ""
    rows = [{"role": "system", "name": None, "text": x} for x in [*pieces, added] if x]
    if tools:
        rows.append({"role": "tools", "name": None,
                     "text": "\n".join(t["function"]["name"] for t in tools)})
    return rows + seen(messages[1 if head is not None else 0:])


async def keep_request(cfg, cid: str, pieces: list[str], messages: list[dict],
                       tools: list[dict]) -> None:
    """The request as it went out, the newest one per conversation (off the event loop:
    it is written before every step)."""
    path, text = _request_file(cfg, cid), json.dumps(request_rows(pieces, messages, tools))

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    await anyio.to_thread.run_sync(write)


def last_request(cfg, cid: str) -> list[dict] | None:
    """What keep_request kept for this conversation; None before its first request."""
    try:
        return json.loads(_request_file(cfg, cid).read_text())
    except FileNotFoundError:
        return None


def seen(messages: list[dict]) -> list[dict]:
    """Request messages as it reads them: the role, the tool's name on a result, and
    the text (an image as [image]; its calls as name(arguments))."""
    names: dict[str, str] = {}
    out = []
    for m in messages:
        c = m.get("content")
        text = c if isinstance(c, str) else "\n".join(
            p.get("text", "") if p.get("type") == "text" else "[image]" for p in c or [])
        calls = []
        for t in m.get("tool_calls") or []:
            names[t["id"]] = t["function"]["name"]
            calls.append(f"{t['function']['name']}({t['function']['arguments']})")
        out.append({"role": m["role"], "name": names.get(m.get("tool_call_id") or ""),
                    "text": "\n".join(x for x in [text, *calls] if x)})
    return out


def kbs(registry):
    """The app's knowledge bases, through the registry's app; None in a bare test."""
    app = registry.app if registry is not None else None
    return app.kbs if app is not None else None


async def _chat_line(store, cid: str) -> str | None:
    """Who is on the other side of this conversation, and where. None for a sub-agent
    (the sub_agents skill tells it who it is and where its brief came from) and a
    mission (its brief says who reads)."""
    row = await store.read(lambda c: c.execute(
        "SELECT kind FROM conversations WHERE id=?", (cid,)).fetchone())
    if row is None or row["kind"] in ("agent", "mission"):
        return None
    if row["kind"] != "chat":
        return ("{{username}} is not here: this runs in the background, and nobody reads "
                "along live.")
    return "You are talking with {{username}} in the app right now."


async def tail_text(store, cfg, registry, backend, cid: str, carried,
                    tid: str = "") -> str:
    """The tail of turn `tid`: what holds now and changes from turn to turn, stored once
    on the user message the turn answers (turns/message_context.py put_tail) so it never
    moves: the date and time now, the model it runs on when that is not the first,
    where else it works, its plan, the skills it cannot use now and the ones it keeps
    notes on, and the core skills' live state (`tail_part`: the intercom's thread and
    announcements, a mission's status, a sub-agent's report room, the notebook's
    index)."""
    from ..conversations import here
    parts = [clock.today(cfg), clock.now_line(cfg), await fallback_line(store, backend, cid),
             await store.read(lambda c: here.busy(c, cid))]
    plan = plan_flow.block(await plan_flow.current(store, cid))
    parts.append(cfg.substitute(plan) if plan else None)
    if registry:
        from ..skills.meta import skill_note_path
        specs, _ = await tool_specs(registry, store, cid, carried)
        parts += unusable(specs, registry, await overrides_for(store, cid), cfg)
        noted = [n for n in [*carried.skills, *carried.others]
                 if skill_note_path(cfg, n).is_file()]
        if noted:
            parts.append(f"Your notes on skills (skills/<name>.md, with skills__read_guide "
                         f"or skills__load): {', '.join(noted)}.")
        ctx = ToolContext(conversation_id=cid, turn_id=tid, tool_call_id="", store=store,
                          config=cfg, emit=None,  # type: ignore[arg-type]
                          loaded_notes=carried.loaded_notes)
        parts += await registry.tail_parts(ctx, (set(carried.skills) & CORE) | {"sub_agents"})
    return "\n\n".join(p for p in parts if p)


FALLBACK = "You are running in fallback mode. Instead of {first} you are running on {current}."


def answered_ref(backend) -> str | None:
    """The model that answered this turn's last call (backends/llm.py), None before."""
    return getattr(getattr(getattr(backend, "answered", None), "cfg", None), "id", None)


def _last_answered(c, cid: str, role: str) -> str | None:
    """The model of the conversation's newest `llm.queue` running event of `role`."""
    r = c.execute(
        "SELECT json_extract(data, '$.backend') FROM events WHERE conversation_id=? AND "
        "type='llm.queue' AND json_extract(data, '$.state')='running' AND "
        "json_extract(data, '$.role')=? ORDER BY seq DESC LIMIT 1", (cid, role)).fetchone()
    return r[0] if r else None


async def fallback_line(store, backend, cid: str) -> str | None:
    """While the model that answers the turn's role is not its chain's first
    (backends/llm.py walks the list): the fixed FALLBACK sentence, what the app knows of
    that model (model_facts), then why each one before it is left out. The model is the
    one that answered this turn's last call; before the turn's first call, the one that
    answered the role last in this conversation (its `llm.queue` running event). None on
    the first choice, before any model answered, and for a turn that runs on the first
    model alone."""
    llm = getattr(backend, "llm", None)
    if llm is None or not getattr(backend, "fallback", False):
        return None
    current = answered_ref(backend) or await store.read(
        lambda c: _last_answered(c, cid, backend.role))
    if current is None:
        return None
    ordered = await llm.router.order(backend.role, conversation_id=cid)
    passed = ordered.passed_over(current)
    if not passed:
        return None
    return " ".join([FALLBACK.format(first=ordered.chain[0], current=current),
                     *model_facts(llm.make(current)), passed])


def model_facts(backend) -> list[str]:
    """What the app knows of one model, one plain sentence each: its context window
    (the live one, or its setting), whether it sees images and whether it calls tools
    (its caps: what its server states, else its settings; settings/kinds.py caps_of).
    A fact not known is not said."""
    from . import tokens
    if backend is None:
        return []
    out = []
    if (n := tokens.known_window(backend)) is not None:
        out.append(f"Its context window is {n:,} tokens.")
    for attr, yes, no in (("supports_images", "It sees images.", "It does not see images."),
                          ("supports_tools", "It calls tools.", "It does not call tools.")):
        if (v := getattr(backend, attr, None)) is not None:
            out.append(yes if v else no)
    return out


def about_user(cfg) -> str:
    """What the user wrote about themselves (General settings), placeholders filled;
    empty when they wrote nothing."""
    return cfg.substitute(f"About {{{{user}}}}:\n{cfg.bio}") if cfg.bio else ""


async def system_blocks(cfg, registry, store, cid, tid, carried, role: str | None = None
                        ) -> list[tuple[str, str]]:
    """The system prompt's pieces, each named for what it is (persona, talk, about,
    notes, tools), the most stable first: its persona, the legend, who it is talking
    to, where it is (conversations/here.py where, from ids; `role` its model role), how
    it talks (its `talk`), what the user wrote about themselves, the skill index, what
    each core skill adds for the whole conversation (its live state goes in the tail:
    `tail_part`), and last the notes it always carries (turns/loads.py), the one piece
    her own writes change. Nothing here changes from turn to turn unless she writes an
    always note: what does is in the tail (tail_text), so the start of the request stays
    the same for the model server's cache. A chat, a mission and a background run or
    task get all of it; what the kind adds (a brief, a trigger's prompt) comes as its
    messages. A sub-agent gets its own, lean one (agent_blocks)."""
    from ..skills.notebook import notes_block
    row = await store.read(lambda c: c.execute(
        "SELECT agent_of, agent_cli FROM conversations WHERE id=?", (cid,)).fetchone())
    ctx = ToolContext(conversation_id=cid, turn_id=tid, tool_call_id="",
                      store=store, config=cfg, emit=None,  # type: ignore[arg-type]
                      loaded_notes=carried.loaded_notes)
    if row is not None and row["agent_of"]:
        return await agent_blocks(cfg, registry, store, ctx, carried, role,
                                  bool(row["agent_cli"]))
    her = getattr(cfg, "her", None)
    parts = [("persona", cfg.substitute(her.persona).strip())] \
        if her is not None and her.persona.strip() else []
    # what the app's marks in its context mean (turns/legend.py), right under who it is
    parts.append(("persona", legend.legend(cfg, guard.enabled(cfg, cid))))
    line = await _chat_line(store, cid)
    if line:
        parts.append(("talk", cfg.substitute(line)))
    from ..conversations import here
    where = await store.read(lambda c: here.where(c, cid, role))
    if where:
        parts.append(("talk", cfg.substitute(where)))
    parts.append(("talk", devices.about(cfg) or ""))
    if her is not None:
        parts.append(("talk", cfg.substitute(her.talk_text)))
    parts.append(("about", about_user(cfg)))
    if registry:
        parts.append(("tools", registry.index(cfg, carried)))
        # the core skills' parts; another skill's comes with skills__load
        parts.extend(("tools", f) for f in await registry.prompt_fragments(
            ctx, set(carried.skills) & CORE))
    if "notebook" in carried.skills:
        parts.append(("notes", notes_block(cfg, carried.notes)))
    return [(name, text) for name, text in parts if text]


async def agent_blocks(cfg, registry, store, ctx, carried, role, cli: bool
                       ) -> list[tuple[str, str]]:
    """A sub-agent's system prompt: its agent template's body when it was started from
    one (sub_agents/templates.py), who it is and how it reports (the sub_agents
    fragment), where it is, then the legend and its skills. Not her persona, talk, notes
    or the user's words about themselves: a template that wants them says so. A CLI or a
    drone (`cli`) has none of her tools: the template, how it reports and where it is
    (drone.py hands it to opencode as the system prompt)."""
    from ..conversations import here
    from ..sub_agents import templates
    cid = ctx.conversation_id
    t = await store.read(lambda c: templates.of(c, cid, cfg))
    parts = [("persona", cfg.substitute(t.body) if t is not None else "")]
    if registry:
        parts.extend(("persona", f) for f in await registry.prompt_fragments(
            ctx, {"sub_agents"}))
    where = await store.read(lambda c: here.where(c, cid, role))
    parts.append(("talk", cfg.substitute(where) if where else ""))
    parts.append(("talk", devices.about(cfg) or ""))
    if not cli:
        parts.append(("persona", legend.legend(cfg, guard.enabled(cfg, cid))))
        if registry:
            parts.append(("tools", registry.index(cfg, carried)))
    return [(name, text) for name, text in parts if text]


async def turn_parts(services, cid: str):
    """The backend, the system prompt's pieces, named (request_parts), the tool list a
    turn of this conversation would use now and what it carries (turns/loads.py),
    for the estimates; /compact keeps only the latest exchange anyway."""
    from ..backends.queue import Priority
    backend = services.dispatcher.bound(cid, role="chat", priority=Priority.INTERACTIVE)
    pieces, tools, carried = await request_parts(services.store, services.cfg,
                                                 services.registry, backend, cid)
    return backend, pieces, tools, carried
