"""The single chokepoint every tool call resolves through."""
from __future__ import annotations

import inspect
import json
import keyword
import logging
from dataclasses import replace

import anyio

from .. import guard, metrics
from ..errors import mask, mask_all
from ..guard import calls as bastet_calls, facts as guard_facts, mark
from ..guard import page as guard_page, passes as guard_passes, sources
from ..skills import CORE, ToolContext, ToolResult
from ..skills.gating import (OFF, READ_ONLY_REFUSAL, down_sentence, is_enabled,
                             read_mode_has)
from .cancel import until_stop
from . import store as turns

log = logging.getLogger("celine.tools")


async def run_tool_calls(turn, calls, store, cfg, registry, overrides,
                         token, jobs=None) -> dict[str, str]:
    """-> {call_id: result text}, what the model is told for each call.

    The calls of one reply run concurrently when each is read-only and blocking, or
    hands work that only adds (not destructive) to a job, like several sub-agents
    started at once. Results are written in call order either way. Every call runs.
    Each call's arguments are parsed once, here (`args`: the object, or None when they
    are not one)."""
    tid, cid = turn["id"], turn["conversation_id"]
    calls = [{**c, "args": parsed(c["arguments"])} for c in calls]
    texts: dict[str, str] = {}
    guarded = guard.enabled(cfg, cid)

    async def write(call, out) -> None:
        state, result, withheld = out
        result = await checked(received(result, turn, call, guarded, registry), turn,
                               call, store, cfg, getattr(registry, "app", None))
        withheld = mask(withheld or "") or None
        await turns.finish_tool_call(store, cid, call["message_id"], call["id"],
                                     call["name"], result, state, withheld)
        texts[call["id"]] = marked(result, guarded)
        _seen(registry, cid, call["name"])
        known = registry is not None and registry.get(call["name"]) is not None
        metrics.TOOL_CALLS.labels(call["name"] if known else "unknown", state).inc()
        log.info("tool done", extra={"event": "tool.done", "turn": tid,
                                     "tool": call["name"], "state": state})
    if _concurrent(calls, registry):
        done: dict[str, tuple] = {}

        async def one(call):
            done[call["id"]] = await _resolve(
                turn, call, store, cfg, registry, overrides, token, jobs)
        async with anyio.create_task_group() as tg:
            for call in calls:
                tg.start_soon(one, call)
        for call in calls:
            if done.get(call["id"]) is not None:     # None: shutdown before it started
                await write(call, done[call["id"]])
        return texts
    for call in calls:
        out = await _resolve(turn, call, store, cfg, registry, overrides, token, jobs)
        if out is None:                            # shutdown: left pending, settled interrupted
            break
        await write(call, out)
    return texts


def parsed(arguments: str | None) -> dict | None:
    """A call's arguments as the object they must be; None when they are not one."""
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return None
    return args if isinstance(args, dict) else None


def _seen(registry, cid: str, name: str) -> None:
    """A result landed: the approvals name the last one that read from outside
    (celine/approvals.py)."""
    approvals = getattr(getattr(registry, "app", None), "approvals", None)
    if approvals is not None:
        approvals.seen(cid, name)


def received(result: ToolResult, turn, call: dict, guarded: bool,
             registry=None) -> ToolResult:
    """A result as the runner receives it, from its turn or a job: no known secret in
    it, its text or its row (errors.mask: a key, a vault password), then capped, its
    class written (display "class", guard/facts.py: who may see it), and, while the
    guard is on (`guarded`: Watch or On), labelled when someone else wrote it
    (guard/sources.py: display "outside"). With the guard off no result is labelled.
    `call["args"]`: its arguments as run_tool_calls parsed them."""
    result = _capped(replace(result, content=mask(result.content),
                             display=mask_all(result.display)), turn)
    cls = guard_facts.result_class(call["name"], result.display or {}, registry)
    if cls and not result.is_error:
        result = replace(result, display={**(result.display or {}), "class": cls})
    if not guarded:
        if "outside" in (result.display or {}):     # a skill's own flag (shellboxd's)
            result = replace(result, display={k: v for k, v in result.display.items()
                                              if k != "outside"})
        return result
    if (result.display or {}).get("raised"):
        return result
    label = sources.label(call["name"], call.get("args") or {}, result)
    if label is None:
        return result
    return replace(result, display={**(result.display or {}), "outside": label})


async def checked(result: ToolResult, turn, call: dict, store, cfg, app) -> ToolResult:
    """Bastet's page check on a received result (guard/page.py), while the guard is on
    (Watch or On); with it off the result as it is. In its turn or from a job (`app`:
    the running services, None in a bare runner: no judge, the patterns alone)."""
    gm = guard.mode(cfg, turn["conversation_id"])
    if gm == "off":
        return result
    return await guard_page.check(result, turn=turn, call=call, store=store, cfg=cfg,
                                  llm=getattr(app, "llm", None), mode=gm, app=app)


def marked(result: ToolResult, guarded: bool) -> str:
    """The result's text as it reads it (guard/mark.py shown)."""
    return mark.shown(result.content, result.display, guarded)


def _capped(result: ToolResult, turn) -> ToolResult:
    """The backstop for tools that ignore ctx.max_output_tokens (MCP, shell): one result
    never takes more than the cap (tokens.result_cap). Its start and its end are kept,
    the cut line between them says how much is left out and how to get it."""
    cap = turn.get("max_output_tokens")
    if not cap or not result.content:
        return result
    from . import tokens
    head, tail, shown, total = tokens.cut_middle(result.content, int(cap))
    if not tail:
        return result
    return replace(result, content=f"{head}\n\n[cut: {total - shown:,} of {total:,} tokens "
                                   f"left out here, between the start and the end. Page "
                                   f"through it, or read the part you need from its "
                                   f"file.]\n\n{tail}")


def _concurrent(calls, registry) -> bool:
    if len(calls) < 2 or registry is None:
        return False
    for call in calls:
        spec = registry.get(call["name"])
        if spec is None:
            continue                     # answered with an error, touches nothing
        if spec.mode == "blocking" and not spec.mutating:
            continue
        if spec.mode == "detach" and not spec.annotations.destructive:
            continue
        return False
    return True


BACKGROUND_AFTER = 5  # seconds a call it asked into the background runs before it
                      # detaches: a quick one, or one that fails at once, still answers
                      # directly. Nothing detaches unasked: a call runs in its turn to
                      # its end or its time.
TIME_UP = "Not run: the time for this turn is up."


# Every path returns a result: an unanswered tool_call makes the conversation
# unusable on the next request.
async def _resolve(turn, call, store, cfg, registry, overrides, token, jobs):
    """-> (state, ToolResult, withheld_output), or None when the server is shutting
    down and the step has not started (it stays pending until its turn ends
    interrupted). Never raises. The steps, in order: stopped, replayed, frozen (the
    house refuses), offered, valid, Bastet, then the call itself."""
    if token.cancelled:
        return None if token.shutdown else ("cancelled", ToolResult(content="cancelled"),
                                            None)
    if (done := await _replayed(store, turn, call)) is not None:
        return done
    if (shut := await bastet_calls.frozen(store, cfg, call["name"])) is not None:
        return (*shut, None)
    spec = registry.get(call["name"])
    if (no := _not_offered(turn, call["name"], spec, registry, overrides, cfg)) is not None:
        return no
    args = call["args"]
    if (wrong := _invalid(args, call["arguments"], spec, cfg)) is not None:
        # a tool sent by name: its parameters with the error
        full = "" if spec.skill in CORE else \
            "\nThe tool: " + json.dumps(spec.as_wire()["function"], ensure_ascii=False)
        return "error", ToolResult(content=f"invalid arguments: {wrong}{full}",
                                   is_error=True), None

    async def progress(note: str) -> None:
        await turns.tool_progress(store, turn["conversation_id"], call["id"], note)
    # Bastet (guard/calls.py), before the time check: her wait for the judge counts on
    # its clock
    gate = await bastet_calls.at_call(turn, call, spec, args, store=store, cfg=cfg,
                                      registry=registry, token=token, jobs=jobs,
                                      progress=progress)
    if gate.pending:
        return None
    if gate.refusal is not None:
        return (*gate.refusal, None)
    out = await _run_call(turn, call, spec, args, store, cfg, token, jobs)
    if out is None:
        await guard_passes.retell(store, gate.told)   # shutdown: its next result tells them
        return out
    if not gate.lines:
        return out
    # after the result, outside any <outside> wrapper and the cap (marked())
    state, result, withheld = out
    return state, replace(result, display={**(result.display or {}),
                                           "bastet": gate.lines}), withheld


async def _replayed(store, turn, call):
    """Journal replay: the call id is deterministic, so a crash-repeated step reads its
    old result."""
    done = await turns.existing_tool_result(store, call["id"])
    if done is None:
        return None
    log.info("replayed from journal", extra={"event": "tool.replayed",
                                             "turn": turn["id"], "tool": call["name"]})
    return done["state"], ToolResult(content=done["content"]), None


def _not_offered(turn, name: str, spec, registry, overrides, cfg):
    """The refusal of a call to a tool this step does not have: made up, not offered
    here, outside a read-only sub-agent's tools, of a skill that is down or off. None
    when it has it."""
    offered = turn["offered"]
    skill = name.split("__")[0]
    if spec is None:
        # a made-up tool of a real skill: name the ones it has
        real = sorted(t for t in offered if t.startswith(skill + "__"))
        return "error", ToolResult(content=(
            f"There is no tool {name}. The {skill} skill has {', '.join(real)}. "
            f"skills__read_guide(name=\"{skill}\") shows how to use them."
            if real else f"There is no tool {name}."), is_error=True), None
    if name not in offered:
        here = ", ".join(sorted(t for t in offered if t.startswith(skill + "__")))
        if turn.get("read_only") and not read_mode_has(spec):
            text = READ_ONLY_REFUSAL.format(skill=skill, reads=here or "none")
        else:
            text = (f"{name} is not in this conversation; its {skill} tools are: {here}."
                    if here else f"{name} is not in this conversation; it has no {skill} "
                                 f"tools.")
        return "gated", ToolResult(content=text, display={"gated": True, "tool": name}), None
    if why := registry.down(spec.skill):
        return "gated", ToolResult(content=cfg.substitute(down_sentence(spec.skill, why)),
                                   display={"gated": True, "tool": name}), None
    if not is_enabled(spec, overrides, cfg):
        whole = not any(is_enabled(s, overrides, cfg) for s in registry.tools.values()
                        if s.skill == spec.skill)
        return "gated", ToolResult(content=OFF.format(name=spec.skill if whole else name),
                                   display={"gated": True, "tool": name}), None
    return None


def _invalid(args: dict | None, arguments: str | None, spec, cfg) -> str | None:
    """What is wrong with the arguments for `spec`'s schema; None when they fit. Nothing
    is changed: a value of the wrong type fails the call. `arguments`: as it sent them,
    read again only to say why they are not an object."""
    if args is None:
        try:
            json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            return str(e)
        return "arguments must be an object"
    props = spec.schema.get("properties", {})
    unknown = set(args) - set(props)
    missing = set(spec.schema.get("required", [])) - set(args)
    if unknown or missing:
        parts = []
        for m in sorted(missing):
            opts = (props.get(m) or {}).get("enum")
            desc = (props.get(m) or {}).get("description", "")
            hint = f" (one of {', '.join(map(str, opts))})" if opts else ""
            parts.append(f"missing {m}{hint}" + (f": {cfg.substitute(desc)}" if desc else ""))
        if unknown:
            parts.append(f"unexpected {', '.join(sorted(unknown))}")
        return "; ".join(parts)
    return mistyped(args, spec.schema)


# lists and objects are left to the tools: several read a string as one (a comma list,
# a JSON list in a string)
_FITS = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}
_WORD = {"string": "a string", "integer": "a whole number", "number": "a number",
         "boolean": "true or false"}


def _kind(prop: dict) -> str | None:
    """A parameter's one JSON type: `type`, or the one not null of a type list or an
    anyOf (an MCP server's optional `str | None`); None when it is several."""
    kinds = prop.get("type") or [k.get("type") for k in prop.get("anyOf") or ()]
    kinds = [kinds] if isinstance(kinds, str) else [k for k in kinds if k != "null"]
    return kinds[0] if len(kinds) == 1 else None


def mistyped(args: dict, schema: dict) -> str | None:
    """The arguments whose value is not of their schema's plain type or not in its
    enum ("find is a string", "what is one of view, locate"), or None. Nothing is
    converted. A null for a parameter that is not required is its default."""
    props, required = schema.get("properties", {}), set(schema.get("required", ()))
    wrong = []
    for key, v in args.items():
        kind = _kind(props.get(key) or {})
        if kind not in _FITS or (v is None and key not in required):
            continue
        if not isinstance(v, _FITS[kind]) or (isinstance(v, bool) and kind != "boolean"):
            wrong.append(f"{key} is {_WORD[kind]}")
        elif (opts := (props.get(key) or {}).get("enum")) and v not in opts:
            wrong.append(f"{key} is one of {', '.join(map(str, opts))}")
    return "; ".join(wrong) or None


def _takes_any(fn) -> bool:
    return any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in inspect.signature(fn).parameters.values())


async def _run_call(turn, call, spec, args, store, cfg, token, jobs):
    """The call itself, after every check: the turn's time, then the tool, in its turn
    or handed to a job."""
    cid, tid, name = turn["conversation_id"], turn["id"], call["name"]
    # the turn's time: nothing starts after it is up, and nothing runs past it
    left = turn["time_left"]() if turn.get("time_left") else None
    if left is not None and left <= 0:
        return "error", ToolResult(content=TIME_UP, is_error=True,
                                   display={"time_up": True}), None

    # a parameter named like a Python keyword is `in` on the wire and `in_` in the
    # coroutine (skills.derive_schema); the schema check above saw the
    # wire form, the call below gets the Python one. A tool that takes **args (MCP)
    # passes them on as they came.
    wire = args
    if not _takes_any(spec.fn):
        args = {k + "_" if keyword.iskeyword(k) else k: v for k, v in args.items()}

    async def emit(note: str, **data) -> None:
        await turns.tool_progress(store, cid, call["id"], note, **data)

    ctx = ToolContext(conversation_id=cid, turn_id=tid, tool_call_id=call["id"],
                      store=store, config=cfg, emit=emit,
                      max_output_tokens=turn.get("max_output_tokens"), time_left=left)

    asked = bool(spec.background and args.get(spec.background) is True)
    timeout = float(spec.timeout or cfg.r("tool_timeout_seconds"))
    expired = f"timed out after {timeout:.0f}s"
    if left is not None and left < timeout:
        timeout, expired = left, "Stopped: the time for this turn is up."
    handoff = spec.mode if jobs is not None else "blocking"
    await turns.tool_started(store, cid, call["id"])

    if handoff == "blocking":
        with metrics.TOOL_SECONDS.labels(name).time():
            return await _bounded(spec, ctx, args, timeout, token, name, expired)

    # a detach tool starts once, in the job nursery, so that a call it asked into the
    # background can be handed over without running its side effect twice; unasked, it
    # runs in its turn to its end or its time, and is stopped then
    handle = jobs.begin(spec, ctx, args, call, turn)
    with anyio.move_on_after(min(BACKGROUND_AFTER, timeout) if asked else timeout):
        await handle.done.wait()

    if not handle.done.is_set() and not asked:
        await jobs.stop_handle(handle)
        await handle.done.wait()
        return "error", ToolResult(content=expired, is_error=True), None

    if handle.done.is_set():
        # no await between the done check and the claim
        if handle.owner is None:
            handle.owner = "turn"
        out = handle.result
        assert out is not None
        if handle.state == "cancelled" or token.cancelled:
            return "cancelled", ToolResult(content="cancelled"), out.content
        return ("error" if out.is_error else "ok"), out, None

    handle.owner = "job"
    job_id = await jobs.promote(handle)
    display = {"job_id": job_id, "tool": name, **(spec.detached_display or {})}
    placeholder = spec.detached or ("running in the background as job {job}. "
                                    "You'll get the result when it finishes.")
    return "detached", ToolResult(content=placeholder.format_map({**wire, "job": job_id[:8]}),
                                  display=display), None


async def _bounded(spec, ctx, args, limit, token, name, expired: str):
    """Run with a deadline; `expired` is the result when it passes. A Stop ends the
    step at once; a shutdown lets it finish (it is kept)."""
    from ..backends.llm import TOOL_CLOCKS
    stopped, out = False, None
    try:
        with anyio.move_on_after(limit) as scope:
            # a wait for a model inside the tool is not its time (backends/llm.credit)
            clocks = TOOL_CLOCKS.set((*TOOL_CLOCKS.get(), scope))
            try:
                stopped, out = await until_stop(lambda: spec.fn(ctx, **args), token,
                                                shutdown_stops=False)
            finally:
                TOOL_CLOCKS.reset(clocks)
        if stopped:
            return "cancelled", ToolResult(content="cancelled"), None
        if scope.cancelled_caught:
            return "error", ToolResult(content=expired, is_error=True), None
    except Exception as e:
        log.warning("tool raised", exc_info=True,
                    extra={"event": "tool.raised", "tool": name,
                           "error": type(e).__name__})
        # the app's own crash: its text is the app's, never outside text (received)
        return "error", ToolResult(content=f"failed: {type(e).__name__}: {e}",
                                   is_error=True, display={"raised": True}), None

    # a shutdown is not a Stop: work that finished is kept as it is
    if token.cancelled and not token.shutdown:
        return "cancelled", ToolResult(content="cancelled"), out.content

    return ("error" if out.is_error else "ok"), out, None
