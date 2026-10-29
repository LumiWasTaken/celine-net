"""The prompt of a turn: the system prompt's pieces (system_blocks), the tail of now
(request_text), the tools a request carries (specs, wire_tools) and the last request as
it went out (keep_request, last_request: /usage). The turn loop is turns/runner.py; the
Context tab, /compact, /usage and impersonate read the same assembly from here."""
from __future__ import annotations

import json

import anyio

from .. import clock, guard
from ..plans import flow as plan_flow
from ..skills import ToolContext
from ..skills.gating import OFF, down_sentence, is_enabled, overrides_for
from . import context, legend, loads

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
    """The tools this conversation has, and whether it may only read: the tools the
    loaded skills offer here (turns/loads.py: a skill with offered_in() offers its tools
    only where that says so, missions in a mission's own conversation; it may also name
    the tools it offers there, as a set), in registry order, but those named in
    `without`; a sub-agent started with read access gets the read-only tools and the
    intercom (turns/tools.py refuses the rest)."""
    if not registry:
        return {}, False
    here = await loads.offered(registry, store, cid)
    names = {s.wire_name for n in carried.skills for s in here.get(n, ())} - set(without)
    tools = {k: s for k, s in registry.tools.items() if k in names}
    from ..skills.gating import read_mode_has
    from ..sub_agents.skill import reads_only
    if await reads_only(store, cid):
        return {k: s for k, s in tools.items() if read_mode_has(s)}, True
    return tools, False


def wire_tools(specs, registry, overrides, cfg) -> list[dict]:
    """The tools it gets in this request: those of `specs` that are on (skills/
    gating.py) and whose skill is up. A tool that is off or down is not sent at all
    (an empty stub got called with no arguments in a loop): the skill index says so
    (unusable), and a call to it is refused with why (turns/tools.py). Descriptions
    are filled in at registration (skills.Registry.add_specs)."""
    return [s.as_wire() for s in specs.values()
            if registry.down(s.skill) is None and is_enabled(s, overrides, cfg)]


def unusable(specs, registry, overrides, cfg) -> list[str]:
    """One line per loaded skill that sends no tool now: down with its reason, or off
    here. Under the skill index."""
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
    this conversation carries now and what it carries by need (turns/loads.py, nothing
    saved), built as a turn builds them (for /compact, /usage and what it sees)."""
    overrides = await overrides_for(store, cid)
    carried = await loads.select(store, cfg, registry, cid, backend)
    specs, _ = await tool_specs(registry, store, cid, carried)
    tools = (wire_tools(specs, registry, overrides, cfg)
             if registry and getattr(backend, "supports_tools", False) else [])
    blocks = await system_blocks(cfg, registry, store, cid, "", carried)
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


async def request_text(store, cfg, registry, backend, cid: str, turn: dict, carried,
                       tools: list[dict] = (), touch: bool = True,
                       path: list[dict] | None = None
                       ) -> tuple[list[str], list[context.Part]]:
    """(the system prompt's pieces, the tail's parts) of a turn: system_blocks (the
    notes it always carries in the system prompt) and the notes it carries by need in
    the tail. `touch` False: a look that changes nothing (the Context tab). `path`: the
    conversation's path when the turn read it already."""
    pieces = [t for _, t in await system_blocks(cfg, registry, store, cid, turn["id"],
                                                carried)]
    # what changes from turn to turn, in the tail: the time now, the model it runs on
    # when that is not the first, where else it works, its plan, its notes by need. The tail is built once at a turn's start (again only
    # when a skill load rebuilds the request), so the time of step 0 holds for the
    # whole turn: good enough, and the steps of one turn share their prefix
    from ..conversations import here
    plan = plan_flow.block(await plan_flow.current(store, cid))
    return pieces, [context.Part(t) for t in [
        clock.now_line(cfg), await fallback_line(backend, cid),
        await store.read(lambda c: here.busy(c, cid)),
        cfg.substitute(plan) if plan else None, notes_tail(cfg, carried)] if t]


FALLBACK = "You are running in fallback mode. Instead of {first} you are running on {current}."


async def fallback_line(backend, cid: str) -> str | None:
    """While the turn's role runs on a model that is not its chain's first
    (backends/llm.py walks the list): the fixed FALLBACK sentence, what the app knows of
    the model it runs on (its context window, whether it sees images and calls tools,
    from its settings' caps and the live window; a fact it does not know is left out),
    then why each one before it is left out. None on the first choice, and for a turn
    that runs on the first model alone."""
    llm = getattr(backend, "llm", None)
    if llm is None or not getattr(backend, "fallback", False):
        return None
    ordered = await llm.router.order(backend.role, conversation_id=cid)
    current = ordered.refs[0] if ordered.refs else None
    passed = ordered.passed_over(current) if current else None
    if not passed:
        return None
    return " ".join([FALLBACK.format(first=ordered.chain[0], current=current),
                     *model_facts(llm.make(current)), passed])


def model_facts(backend) -> list[str]:
    """What the app knows of one model, one plain sentence each: its context window
    (the live one, or its setting), whether it sees images and whether it calls tools
    (its caps). A fact not known is not said."""
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


def notes_tail(cfg, carried) -> str | None:
    """The notes it carries by need, the ones over the share and the ones left out
    here: what changes between turns, out of the cached system prompt."""
    from ..skills.notebook import notes_block
    return notes_block(cfg, [n for n in carried.notes if not n.get("always")],
                       head=False) if "notebook" in carried.skills else None


def about_user(cfg) -> str:
    """What the user wrote about themselves (General settings), placeholders filled;
    empty when they wrote nothing."""
    return cfg.substitute(f"About {{{{user}}}}:\n{cfg.bio}") if cfg.bio else ""


async def system_blocks(cfg, registry, store, cid, tid, carried
                        ) -> list[tuple[str, str]]:
    """The system prompt's pieces, each named for what it is (persona, talk, about,
    notes, tools): its persona, who it is talking to and where, how it talks (it
    `talk`), what the user wrote about themselves, the notes it always
    carries (turns/loads.py; the ones by need go in the tail, notes_tail), the skill
    index, then what each loaded skill adds every turn (the Messages archive's sources, the
    notebook index), then the date. What changes from turn to turn comes last, so the
    start of the prompt stays the same for the model server's cache. Who a sub-agent is
    (the sub_agents fragment) is told in every conversation. Every kind gets all of it:
    a chat, a mission, a sub-agent and a background run or task alike;
    what the kind adds (a brief, a trigger's prompt) comes as its messages."""
    from ..skills.notebook import notes_block
    her = getattr(cfg, "her", None)
    parts = [("persona", cfg.substitute(her.persona).strip())] \
        if her is not None and her.persona.strip() else []
    # what the app's marks in its context mean (turns/legend.py), right under who it is
    parts.append(("persona", legend.legend(cfg, guard.enabled(cfg, cid))))
    line = await _chat_line(store, cid)
    if line:
        parts.append(("talk", cfg.substitute(line)))
    from ..conversations import here
    where = await store.read(lambda c: here.where(c, cid))
    if where:
        parts.append(("talk", where))
    if her is not None:
        parts.append(("talk", cfg.substitute(her.talk_text)))
    parts.append(("about", about_user(cfg)))
    ctx = ToolContext(conversation_id=cid, turn_id=tid, tool_call_id="",
                      store=store, config=cfg, emit=None,  # type: ignore[arg-type]
                      loaded_notes=carried.loaded_notes)
    if "notebook" in carried.skills:
        # the notes it always carries; the ones by need go in the tail (notes_tail)
        parts.append(("notes", notes_block(cfg, [n for n in carried.notes
                                                 if n.get("always")])))
    if registry:
        specs, _ = await tool_specs(registry, store, cid, carried)
        lines = unusable(specs, registry, await overrides_for(store, cid), cfg)
        parts.append(("tools", "\n".join(x for x in [registry.index(cfg, carried), *lines]
                                         if x)))
        parts.extend(("tools", f) for f in await registry.prompt_fragments(
            ctx, set(carried.skills) | {"sub_agents"}))
    parts.append(("tools", clock.today(cfg)))
    return [(name, text) for name, text in parts if text]



async def turn_parts(services, cid: str):
    """The backend, the system prompt's pieces, named (request_parts), the tool list a
    turn of this conversation would use now and what it carries by need (turns/loads.py),
    for the estimates; /compact keeps only the latest exchange anyway."""
    from ..backends.queue import Priority
    backend = services.dispatcher.bound(cid, role="chat", priority=Priority.INTERACTIVE)
    pieces, tools, carried = await request_parts(services.store, services.cfg,
                                                 services.registry, backend, cid)
    return backend, pieces, tools, carried
