"""One turn, start to finish: stream, run tools, stream again, until it stops."""
from __future__ import annotations

import logging
import time

from ..conversations import tree
from ..backends.base import (
    ORIGIN, BackendError, Done, ReasoningDelta, TextDelta, ToolCallDelta,
)
import anyio

from ..core.store import Store
from ..errors import mask
from ..skills import words
from ..skills.gating import overrides_for
from ..intercom import deliver
from ..plans import flow as plan_flow
from . import context, loads, message_context, prompt, reply, tokens, waiting
from . import store as turns
from .compact import SummaryFailed, closing, stoppable
from .tools import run_tool_calls
from ..core.event_types import CONTEXT_EMERGENCY, ERROR, MESSAGE_REASONING, TOOL_FORMING

log = logging.getLogger("celine.turn")
DELTA_EVERY = 0.075        # seconds between two writes of a streaming reply
WAIT = "missions__wait"     # ran ok: the turn ends with its step, whatever else is open


async def run_turn(turn: dict, store: Store, cfg, backend, bus,
                   token=None, registry=None, jobs=None, clock=None) -> None:
    """`clock.deadline` (anyio time) is when the turn ends for good. The room of
    `last_step_seconds` before it is the step under way's: once the work time is up no
    new step starts, and the turn ends `capped` with the reason wall_clock."""
    from .cancel import Token
    tid = turn["id"]
    token = token if token is not None else Token(tid)
    if clock is None:
        # a turn's clock without the dispatcher (tests): the same limits,
        # kept by the runner alone (never entered). The dispatcher's clock is the cancel
        # scope that also stops a turn that hangs; the model queue moves its deadline by
        # the time spent waiting for a model.
        clock = anyio.CancelScope(deadline=anyio.current_time() +
                                  float(cfg.r("last_step_seconds")) +
                                  float(cfg.r("turn_wall_clock_seconds")))
    # what a wait for the user inside it needs (approvals.py): the user's own turn
    # waits for their answer, its clock held meanwhile
    waits = waiting.TURN.set(waiting.Turn(
        interactive=(turn.get("origin") or "user") == "user", clock=clock, token=token))
    try:
        await _run_turn(turn, store, cfg, backend, bus, token, registry, jobs, clock)
    except SummaryFailed as e:
        # the older messages had to become a summary and did not: the turn fails with
        # why, nothing is left out of its request instead (turns/context.py)
        await _failed(store, tid, turn["conversation_id"], "summary_failed",
                      f"The older messages could not be summarised: {e}", True)
    finally:
        waiting.TURN.reset(waits)


async def _run_turn(turn: dict, store: Store, cfg, backend, bus,
                    token, registry, jobs, clock) -> None:
    tid, cid = turn["id"], turn["conversation_id"]
    kbs = prompt.kbs(registry)
    last_step = float(cfg.r("last_step_seconds"))

    def time_left() -> float:
        """Seconds of work left in this turn; the last step's room is not counted."""
        return clock.deadline - last_step - anyio.current_time()

    if turn.get("abort_requested"):
        token.cancel()
    await turns.set_backend(store, tid, backend.name, backend.model)

    # the path, read once for the turn's start: what it carries by need, the user
    # messages it sends first, the tail and the first request
    chain = await store.read(lambda c: tree.path(c, cid))
    # this conversation's switches (skills/gating.py) and what it carries by need
    # (turns/loads.py), resolved once per turn: a config reload must not swap tools
    # under a running turn; only skills__load changes them, between steps
    overrides = await overrides_for(store, cid)
    carried = await loads.select(store, cfg, registry, cid, backend, when="start",
                                 turn_id=tid, path=chain)
    specs, read_only = await prompt.tool_specs(registry, store, cid, carried)
    tools_on = bool(registry) and getattr(backend, "supports_tools", False)
    wire_tools = prompt.wire_tools(specs, registry, overrides, cfg) if tools_on else []

    counter = tokens.counter(backend)
    win = tokens.window(backend)
    tool_turn = {"id": tid, "conversation_id": cid, "origin": turn.get("origin") or "user",
                 "root_message_id": turn["root_message_id"],
                 "max_output_tokens": tokens.result_cap(win), "read_only": read_only,
                 "time_left": time_left, "loaded": frozenset(carried.skills), "carried": carried,
                 "offered": frozenset(specs), "notes": _carried_notes(carried)}

    # where this turn stands: a fresh turn starts at its user message; a resumed or
    # continued one carries on from its own tail with the next free iteration number
    point = await turns.resume_point(store, tid)
    iteration = int(point["iteration"])
    parent = point["parent"] or turn["root_message_id"]
    reopen = point["reopen"]

    if token.cancelled:
        await _stopped(store, tid, cid, token)
        return

    if point["pending"]:
        # read-only steps that were in flight when the server stopped: same call ids.
        # A step with a side effect is never re-run here, whoever queued the turn.
        from .journal import restarted
        if why := restarted(point["pending"], turns.tool_flags(registry)):
            await turns.transition(store, tid, cid, "interrupted", reason_code="restart",
                                   reason=why)
            return
        if not await turns.transition(store, tid, cid, "tools"):
            return
        calls = [{"id": r["id"], "name": r["name"], "arguments": r["arguments"],
                  "message_id": r["message_id"]} for r in point["pending"]]
        log.info("rerunning steps", extra={"event": "turn.rerun", "turn": tid,
                                           "calls": len(calls)})
        await run_tool_calls(tool_turn, calls, store, cfg, registry, overrides,
                             token, jobs)
        if token.cancelled:
            await _stopped(store, tid, cid, token)
            return
        parent = await turns.step_tail(store, calls[-1]["message_id"])
        chain = await store.read(lambda c: tree.path(c, cid))

    pieces, tail = await prompt.request_text(store, cfg, registry, backend, cid, turn, carried,
                                      wire_tools, path=chain)
    system = "\n\n".join(pieces)

    async def build(mode: str, path: list[dict] | None = None):
        """The request from the context builder (turns/context.py), with the system,
        the tools and the tail of now."""
        return await context.build(store, cfg, backend, cid, system=system,
                                   tools=wire_tools, mode=mode, tail=tail, path=path)

    # the user messages this turn sends first get their context block, stored once
    await message_context.ensure(store, cfg, registry, cid, tid,
                                 message_context.unsent(chain), tool_turn["max_output_tokens"])
    # the context builder (turns/context.py): old results as stubs and older turns as
    # a summary when the conversation nears the window; decisions are persisted
    built = await build("start", chain)
    messages, hidden = built.messages, built.hidden
    if point.get("complete"):
        # the answer had fully streamed before the server stopped: finish it (the
        # citation check, without asking the model again), never write it a second time
        done_msg = point["complete"]
        await _verify_citations(store, cfg, cid, done_msg["message_id"], done_msg["text"],
                                done_msg["reasoning"], messages, kbs, hidden)
        await turns.transition(store, tid, cid, "done", honor_abort=False)
        return
    # a reply continued in place: the stored partial reply leaves the history, and the
    # prefill that continues it goes with its one step only. A model without a prefill
    # cannot continue it: the turn ends with that reason.
    reopen_tail: list[dict] = []
    if reopen and reopen.get("text"):
        if not prompt.supports_prefill(backend):
            await _failed(store, tid, cid, "no_prefill",
                          f"The reply was cut off, and {backend.model} cannot continue a "
                          f"reply where it stopped.", False)
            return
        messages, reopen_tail = _reopen_request(messages, reopen, cid, tid)

    # the instructions and tools alone do not fit the model's context: no request can
    # succeed, and compacting cannot help
    fixed = counter.request([{"role": "system", "content": system}], wire_tools)
    if not _fits(fixed, win):
        await _too_small(store, tid, cid, cfg, backend, fixed, win, instructions=True)
        return

    cap = int(cfg.r("max_tool_iterations"))
    # a quiet turn shows only its tool calls: its text is never streamed
    quiet = bool(turn.get("quiet"))
    grown = False                  # tool results appended since the last build
    emergency = False              # one emergency pass per run
    base = int(turn.get("cap_base") or 0)    # a Continue on a capped turn starts here
    while True:
        if cap and iteration - base >= cap:       # 0: only the wall clock
            await turns.transition(store, tid, cid, "capped", reason="max_tool_iterations",
                                   reason_code="max_tool_iterations")
            return
        if time_left() <= 0:
            await turns.transition(store, tid, cid, "capped", reason="wall_clock",
                                   reason_code="wall_clock")
            return
        if not await turns.transition(store, tid, cid, "streaming"):
            return
        # what reached this conversation since the last step joins the request here
        # (intercom/deliver.py); a reply continued in place takes it next step
        if not reopen:
            taken = await deliver.take(store, cid, tid, parent)
            if taken:
                await message_context.ensure(store, cfg, registry, cid, tid, taken,
                                             tool_turn["max_output_tokens"])
                context.add_user(messages, context.user_wire(cfg, taken, cid))
                parent = taken[-1]["id"]
                grown = True

        # between steps: the next request would pass 85 % of the window -> age this
        # turn's older results too (and summarise older turns if still needed)
        if grown and not context.fits(messages, wire_tools, backend,
                                      context.POLICY.turn_at):
            built = await build("turn")
            messages, hidden = built.messages, built.hidden
        grown = False

        step_tools, step = wire_tools, messages + reopen_tail
        if why := context.malformed(step, cid):
            # a request that breaks the rules is never sent (turns/context.py malformed)
            await _failed(store, tid, cid, "malformed_request",
                          f"This request is not well formed: {why}.", False)
            return
        estimate = counter.request(step, step_tools)
        await prompt.keep_request(cfg, cid, pieces, step, step_tools)
        out = await _stream_one(store, cid, tid, parent, backend, step, step_tools,
                                token, iteration, reopen=reopen, quiet=quiet)
        reopen, reopen_tail = None, []
        if isinstance(out, _Overflow) and emergency:
            # still too long after the emergency pass: nothing is left to shorten
            await _too_small(store, tid, cid, cfg, backend, out.n_prompt,
                             tokens.window(backend), instructions=False)
            return
        if isinstance(out, _Overflow):
            # the server says it does not fit: one emergency pass (age everything but
            # the last result, summarise down to this turn), then the same step again
            # into the same (empty) message. The server counted this request itself:
            # its count, over the estimate, is how far the estimates are off.
            emergency = True
            await store.append_event(cid, CONTEXT_EMERGENCY, {
                "turn_id": tid, "iteration": iteration, "estimate": estimate,
                "n_prompt_tokens": out.n_prompt, "n_ctx": out.n_ctx})
            ratio = out.n_prompt / estimate if out.n_prompt and estimate else 1.0
            win = tokens.window(backend)
            if not _fits(fixed * ratio, win):
                await _too_small(store, tid, cid, cfg, backend, int(fixed * ratio), win,
                                 instructions=True)
                return
            built = await build("emergency")
            messages, hidden = built.messages, built.hidden
            need = counter.request(messages, wire_tools) * ratio
            if not _fits(need, win):
                await _too_small(store, tid, cid, cfg, backend, int(need), win,
                                 instructions=False)
                return
            reopen = {"message_id": out.message_id, "text": ""}
            continue
        if out is None:                      # backend failed; already recorded
            return
        mid, text, thinking, calls, done = out
        if mid is None:                      # stopped before a word: nothing is kept
            await _stopped(store, tid, cid, token)
            return
        # a known secret in its arguments (a key, a vault password) is masked before the
        # call is stored or read (errors.mask)
        calls = [{**c, "arguments": mask(c["arguments"])} for c in calls]
        parent = mid
        await _record_usage(store, cid, tid, iteration, backend, counter, estimate, done,
                            mid)
        counter = tokens.counter(backend)      # the model that answered, from now on

        if token.cancelled:
            await _stopped(store, tid, cid, token)
            return
        if not calls:
            cut = "length" if done is not None and done.reason == "length" else None
            await _verify_citations(store, cfg, cid, mid, text, thinking, messages, kbs,
                                    hidden)
            # the answer had fully streamed: a Stop now only cancelled the check
            reason, code = ("cut off at the reply limit", "cut_length") if cut else \
                (None, None)
            await turns.transition(store, tid, cid, "done", cut=cut, reason=reason,
                                   reason_code=code, honor_abort=False)
            if not token.cancelled:
                # while the model is loaded and it reads: get the next turn's prompt
                # ready if the conversation is near the limit
                await context.after_turn(store, cfg, backend, cid, system=system,
                                         tools=wire_tools)
            return

        if not await turns.transition(store, tid, cid, "tools"):
            return                          # stopped: the calls are not run
        await turns.record_tool_calls(store, cid, mid, [_with_flags(c, registry) for c in calls])
        # the text before its calls is shown too (an answer, then a note): the same
        # check, after the calls are recorded so a restart during it keeps them
        text = await _verify_citations(store, cfg, cid, mid, text, thinking, messages, kbs,
                                       hidden)
        # `request`: what was just sent, the guard's view of the outside text in it
        texts = await run_tool_calls({**tool_turn, "root_message_id": mid, "request": step},
                                     [{**c, "message_id": mid} for c in calls],
                                     store, cfg, registry, overrides, token, jobs)
        if token.cancelled:
            await _stopped(store, tid, cid, token)
            return

        # where its plan stops it: a step marked ask was done (its text is the
        # question, the user's answer starts the next turn); a mission's wait (the wake
        # it booked starts the next one); its answer with only a note beside it while no
        # step is open
        plan = await plan_flow.current(store, cid)
        waits = [c["id"] for c in calls if c["name"] == WAIT]
        if plan_flow.stops(plan, calls) or (waits and await _all_ok(store, waits)) or (
                text.strip() and all(_aside(registry, c["name"]) for c in calls) and
                not plan_flow.keeps_going(plan) and
                await _all_ok(store, [c["id"] for c in calls])):
            await turns.transition(store, tid, cid, "done")
            return
        # as the context builder sends a stored reply: as stored, with its signed
        # thinking (the next step continues the tool loop)
        messages.append({"role": "assistant", "content": text,
                         **({"thinking": done.thinking} if done and done.thinking else {}),
                         "tool_calls": [_wire_call(c) for c in calls],
                         ORIGIN: context.origin(cid, turn=tid, message=mid)})
        for call in calls:
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": texts.get(call["id"], "result unavailable"),
                             ORIGIN: context.origin(cid, turn=tid, call=call["id"])})
        parent = await turns.step_tail(store, mid)
        iteration += 1
        grown = True
        if await loads.touched(store, cid) - tool_turn["loaded"]:
            # it loaded a skill (skills__load), or a tool did for it (a password field
            # loads the vault): its tools, its index line and its fragment from the next
            # step on (the request is built again from the stored path)
            carried = await loads.select(store, cfg, registry, cid, backend, when="step")
            specs, read_only = await prompt.tool_specs(registry, store, cid, carried)
            wire_tools = prompt.wire_tools(specs, registry, overrides, cfg) if tools_on else []
            tool_turn.update(read_only=read_only, loaded=frozenset(carried.skills), carried=carried,
                             offered=frozenset(specs), notes=_carried_notes(carried))
            pieces, tail = await prompt.request_text(store, cfg, registry, backend, cid, turn,
                                              carried, wire_tools)
            system = "\n\n".join(pieces)
            built = await build("turn")
            messages, hidden = built.messages, built.hidden
            grown = False



def _aside(registry, name: str) -> bool:
    """A call that needs no answer (skills.ToolSpec needs_answer False): a step with
    answer text whose calls are all such, and all ran ok, ends the turn with its step
    (the answer is not written a second time), unless a step of its plan is open
    (plans/flow.keeps_going): then it keeps going."""
    spec = registry.get(name) if registry is not None else None
    return spec is not None and not spec.needs_answer


async def _all_ok(store, ids: list[str]) -> bool:
    """Every one of these tool calls ran and ended ok."""
    marks = ",".join("?" * len(ids))
    return await store.read(lambda c: c.execute(
        f"SELECT count(*) FROM tool_calls WHERE state='ok' AND id IN ({marks})",
        ids).fetchone()[0]) == len(ids)


async def _stopped(store, tid, cid, token) -> None:
    """The token was cancelled. A Stop ends the turn `aborted` (a freeze's with the
    reason frozen, guard/freeze.py); a shutdown writes nothing here (the dispatcher ends
    the turn `interrupted`)."""
    if getattr(token, "shutdown", False):
        return
    if getattr(token, "reason", None) == "frozen":
        await turns.transition(store, tid, cid, "aborted", reason="frozen",
                               reason_code="frozen")
        return
    await turns.transition(store, tid, cid, "aborted")


def _carried_notes(carried) -> list[dict]:
    """The notes she carries in full this step, for Bastet's leak rule (guard/facts.py
    private)."""
    return [{"path": n["path"], "text": n["text"]} for n in carried.notes
            if n.get("state") == "included" and n.get("text")]


def _with_flags(call: dict, registry) -> dict:
    """What the tool is, recorded with the call: the startup sweep decides from it
    whether a step in flight may run again."""
    spec = registry.get(call["name"]) if registry is not None else None
    if spec is None:
        return call
    return {**call, "mutating": bool(spec.mutating), "mode": spec.mode}


def _reopen_request(messages: list[dict], reopen: dict, cid: str,
                    tid: str) -> tuple[list[dict], list[dict]]:
    """The request that continues a cut-off reply in the same message: -> (the history
    with the stored partial reply taken off, the partial reply as the prefill that ends
    the continuing step). The reply, once whole, joins the history as stored."""
    mid = reopen["message_id"]
    out = [m for m in messages if not (m.get("role") == "assistant" and not m.get(
        "tool_calls") and any(o.get("message") == mid for o in m.get(ORIGIN) or ()))]
    return out, [{"role": "assistant", "content": reopen["text"],
                  ORIGIN: context.origin(cid, turn=tid, message=mid)}]


async def _record_usage(store, cid, tid, iteration, backend, counter, estimate,
                        done, message_id=None) -> None:
    usage = getattr(done, "usage", None)
    prompt = int(getattr(usage, "input_tokens", 0) or 0)
    if prompt:
        counter.observe(estimate, prompt)
    await turns.record_usage(store, cid, tid, {
        "message_id": message_id, "iteration": iteration, "estimate": estimate,
        "prompt_tokens": prompt or None,
        "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0) or None,
        "cache_read": int(getattr(usage, "cache_read", 0) or 0) or None,
        "cache_write": int(getattr(usage, "cache_write", 0) or 0) or None,
        "window": tokens.window(backend),
        "finish_reason": getattr(done, "reason", None)},
        request=getattr(backend, "request_sent", None))




def _wire_call(c: dict) -> dict:
    return {"id": c["id"], "type": "function",
            "function": {"name": c["name"], "arguments": c["arguments"]}}



async def _verify_citations(store, cfg, cid, mid, text, thinking, messages, kbs,
                            hidden=()) -> str:
    """The reply as the user sees it (turns/reply.py), written once: `message.rewritten`
    when it differs from what the stream ended with. -> the text as stored."""
    final = await reply.stored(store, cfg, cid, mid, text, messages, hidden, kbs)
    if final != (text or ""):
        await tree.finish_assistant_message(store, cid, mid, final, thinking or None,
                                            rewrite=True)
    return final


def _forming_preview(arguments: str) -> str:
    """The end of the half-written JSON arguments, readable: escapes undone, no known
    secret in it."""
    tail = mask(arguments)[-400:]
    return tail.replace("\\n", "\n").replace('\\"', '"').replace("\\t", "  ")[-300:]


def _fits(need: float, win: int) -> bool:
    """A request of `need` tokens leaves the margin free in a window of `win`."""
    return need + win * tokens.MARGIN <= win


async def _too_small(store, tid, cid, cfg, backend, need: int | None, win: int, *,
                     instructions: bool) -> None:
    """The turn fails once, with one sentence that says what to do: this request does
    not fit the model's context, and nothing left to shorten would make it fit.
    Continue tries again, for when the model or its context changed."""
    model = backend.model
    kind = ((getattr(getattr(backend, "cfg", None), "raw", None) or {})
            .get("provider_kind"))
    name = cfg.name
    about = f"about {need:,}" if need else "more"
    if instructions:
        what = (f"{model} has room for {win:,} tokens, and {name}'s instructions and "
                f"tools alone take {about}.")
    else:
        what = (f"This turn needs {about} tokens even after compacting, and {model} has "
                f"room for {win:,}.")
    fix = " Choose a model with more context for this conversation"
    fix += (f", or give {model} a larger context size in Settings, Models."
            if kind == "ollama" else ".")
    await _failed(store, tid, cid, "context_too_small", what + fix, False)


async def _failed(store, tid, cid, code: str, why: str, retryable: bool) -> None:
    """The turn fails with its reason, said once: on the conversation's log and its
    row."""
    await store.append_event(cid, ERROR, {"code": code, "message": why, "turn_id": tid,
                                            "retryable": retryable})
    await turns.transition(store, tid, cid, "failed", reason=why, reason_code=code)


async def _left(store, cid: str, mid: str, parts, thinking) -> str | None:
    """A step a Stop or a shutdown ended before it said anything leaves no empty reply:
    it goes (tree.drop_empty). -> the message id, None when it went."""
    if "".join(parts) or "".join(thinking):
        return mid
    return None if await tree.drop_empty(store, cid, mid) else mid


_END = object()


async def _next_event(events, backend):
    """The adapter's next event, _END after its last. Anything else it raises (a reply
    it could not read, a bug in it) comes out as a BackendError: the reply ends and the
    turn fails with what it said, as for any model failure."""
    try:
        return await anext(events)
    except StopAsyncIteration:
        return _END
    except BackendError:
        raise
    except Exception as exc:
        log.error("model reply broke", exc_info=exc,
                  extra={"event": "turn.backend_broke", "backend": backend.name})
        e = BackendError(f"The reply from {backend.model} could not be read "
                         f"({type(exc).__name__}: {exc}).", retryable=False)
        e.raw = {"error": f"{type(exc).__name__}: {exc}"}
        raise e from exc


class _Overflow:
    """The request did not fit the window; nothing was recorded as a failure yet."""

    def __init__(self, message_id: str, n_prompt, n_ctx):
        self.message_id, self.n_prompt, self.n_ctx = message_id, n_prompt, n_ctx


async def _stream_one(store, cid, tid, parent, backend, messages, tools, token,
                      iteration=0, reopen: dict | None = None, quiet: bool = False):
    """-> (message id, text, thinking, calls, Done or None), or None when the backend
    failed (recorded), or _Overflow when the request did not fit (the caller compacts
    and tries again, or gives up). `reopen` continues an existing reply in place: its
    text is the prefix, new deltas append to the same message id. A `quiet` turn's
    text goes onto the row only, never out as message.delta."""
    prefix = ""
    if reopen:
        mid = reopen["message_id"]
        prefix = reopen.get("text") or ""
        await tree.reopen_message(store, cid, mid)
    else:
        mid = await tree.start_assistant_message(store, cid, parent, tid)
    if hasattr(backend, "for_message"):          # queue events name the message
        # a reply continued in place: a prefill, thinking off
        backend = backend.for_message(mid, thinking="off" if prefix else None)
    parts: list[str] = [prefix] if prefix else []
    thinking: list[str] = []
    acc: dict[int, dict] = {}
    shown: dict[int, tuple[float, int]] = {}  # per call: when tool.forming last went out, at what length

    async def show(i: int) -> None:
        slot = acc[i]
        shown[i] = (time.monotonic(), len(slot["arguments"]))
        await store.append_event(cid, TOOL_FORMING, {
            "message_id": mid, "index": i, "name": slot["name"],
            "chars": len(slot["arguments"]), "preview": _forming_preview(slot["arguments"]),
            "words": words.forming(slot["name"], slot["arguments"])})

    async def show_last(i: int) -> None:
        if acc[i]["name"] and shown.get(i, (0.0, -1))[1] != len(acc[i]["arguments"]):
            await show(i)

    done: Done | None = None
    # llama.cpp streams a prefilled reply back as its first delta: hold the start of the
    # stream until it is clear whether it repeats the prefix, and drop the repeat
    echo = prefix if (prefix and messages and messages[-1].get("role") == "assistant"
                      and messages[-1].get("content") == prefix) else ""
    held: list[str] = []
    # text deltas go out together, at most every DELTA_EVERY seconds: one write and one
    # event per flush, with the reply so far on the row
    pending: list[str] = []
    flushed_at = 0.0

    async def flush() -> None:
        nonlocal flushed_at
        flushed_at = time.monotonic()
        if pending:
            text = "".join(pending)
            pending.clear()
            await tree.append_delta(store, cid, mid, text, "".join(parts), quiet)

    async def emit(text: str) -> None:
        # note: checked as tokens arrive, no timer; a pause in the stream holds
        # the last tokens until the next one or the end
        if text:
            parts.append(text)
            pending.append(text)
            if time.monotonic() - flushed_at >= DELTA_EVERY:
                await flush()

    async def release(final: bool = False) -> None:
        nonlocal echo
        buf = "".join(held)
        if not final and len(buf) <= len(echo) and echo.startswith(buf):
            return                                   # still undecided
        held.clear()
        for cand in (echo, echo.rstrip()):
            if cand and buf.startswith(cand):
                buf = buf[len(cand):]
                break
        echo = ""
        await emit(buf)

    try:
        # aclosing: a queued backend frees its model slot as soon as we stop reading
        async with closing(stoppable(backend, messages, tools, token)) as events:
            while (ev := await _next_event(events, backend)) is not _END:
                if token.cancelled:
                    break
                if isinstance(ev, TextDelta):
                    if echo:
                        held.append(ev.text)
                        await release()
                    else:
                        await emit(ev.text)
                elif isinstance(ev, ReasoningDelta):
                    thinking.append(ev.text)
                    await store.append_event(cid, MESSAGE_REASONING,
                                             {"message_id": mid, "text": ev.text})
                elif isinstance(ev, ToolCallDelta):
                    # accumulate by index: providers send the name in the first delta,
                    # the last, or reuse an index (the call's id is ours, turns.call_id)
                    slot = acc.setdefault(ev.index, {"name": None, "arguments": ""})
                    if ev.name:
                        slot["name"] = ev.name
                    slot["arguments"] += ev.arguments
                    # the call is visible while it writes it, not only once it runs: twice a
                    # second, and once more with its last state when it moves on to the next
                    if slot["name"] and time.monotonic() - shown.get(ev.index, (0.0, 0))[0] >= 0.5:
                        await show(ev.index)
                    for i in acc:
                        if i != ev.index:
                            await show_last(i)
                elif isinstance(ev, Done):
                    for i in acc:
                        await show_last(i)
                    done = ev
                    break
        if held:
            await release(final=True)
        await flush()
    except BackendError as e:
        await flush()
        await tree.finish_assistant_message(store, cid, mid, "".join(parts),
                                            "".join(thinking) or None,
                                            raw=e.raw or {"error": str(e)})
        if token.cancelled:                  # stopped (maybe still in the queue): not a failure
            return await _left(store, cid, mid, parts, thinking), \
                "".join(parts), "".join(thinking), [], None
        code = getattr(e, "code", None) or ("backend_retryable" if e.retryable
                                            else "backend")
        if code == "context_overflow":
            tokens.learn_window(backend, getattr(e, "n_ctx", None))
            if not "".join(parts):
                return _Overflow(mid, getattr(e, "n_prompt_tokens", None),
                                 getattr(e, "n_ctx", None))
        await store.append_event(cid, ERROR, {"code": code, "message": str(e),
                                                "turn_id": tid, "message_id": mid,
                                                "retryable": e.retryable
                                                or code == "backend_retryable"})
        await turns.transition(store, tid, cid, "failed", reason=str(e), reason_code=code)
        return None

    await tree.finish_assistant_message(store, cid, mid, "".join(parts),
                                        "".join(thinking) or None,
                                        done.reason if done else None,
                                        raw=done.raw if done else None,
                                        thinking=done.thinking if done else None)
    if token.cancelled and not any(s["name"] for s in acc.values()):
        mid = await _left(store, cid, mid, parts, thinking)
    if done and not token.cancelled and not quiet and not "".join(parts) and \
            not thinking and not any(s["name"] for s in acc.values()):
        # (a quiet turn that ends saying nothing has simply done its calls)
        msg = "The model sent an empty reply."
        await store.append_event(cid, ERROR, {"code": "empty_reply", "message": msg,
                                                "turn_id": tid, "message_id": mid,
                                                "retryable": True})
        await turns.transition(store, tid, cid, "failed", reason=msg,
                               reason_code="empty_reply")
        return None
    calls = [{"id": turns.call_id(tid, iteration, index), "name": acc[index]["name"],
              "arguments": acc[index]["arguments"]}
             for index in sorted(acc) if acc[index]["name"]]
    return mid, "".join(parts), "".join(thinking), calls, done
