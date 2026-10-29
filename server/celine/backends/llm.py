"""Model calls, queued and routed. The runner, titles and captions see a
`QueuedBackend`: the same `stream(messages, tools)` as any backend, but each call asks
the router (backends/router.py) which models of its task can answer now, waits for a
slot in the scheduler (backends/queue.py), and moves to the next model when one fails
before its first token.

A turn the user waits on (`fallback` False: their message in a chat or a mission)
runs on exactly its first model: when it fails, the
call fails with its error, and nothing waits for another model. When that model's
provider is seen down (backends/router.py), its state is asked for once more (2 s at
most, the state may be stale) and the call fails with the provider's sentence only
when it is still down. Only background work
(schedules, sub-agents, compaction, titles, summaries, captions, every aux role) walks
its task's list.

A busy provider (HTTP 429, 503 or 529) or a model that could not be loaded fails a call
the user waits on (priority interactive) with code `busy`: the chat offers Switch
provider and Keep waiting. Keep waiting (`keep_waiting`, a turn continued with
`wait`) runs that one model and waits like background work does, until Stop; so does
every model call made for that turn (a tool's vision, Writing or models__ask call, bound
with the turn's id). A wait for a model stops the turn's clock and the
time limit of the tool that waits (TOOL_CLOCKS).

Priority belongs to the caller: the user's turn is interactive, trigger and job turns
are agent, titles, descriptions and captions are background.

When no model of the task can answer (a call with fallback):
- an interactive or agent call waits: it says so once ("waiting" with reason
  `no_model` and the sentence why), and continues as soon as a model can answer
  (a provider comes back, a setting changes). Stop ends the wait at once. The time
  spent waiting does not count against the turn's wall clock.
- a background call raises `NoModel` at once; its work item is deferred until a host
  is back (work.py).

A failure before the first token is recorded on the router: the provider is
unreachable (the connection failed), the model could not be loaded (llama-swap was
starting it, or said the start failed), or the model answered with an error. Nothing
is remembered on a timer.

Every other call treats a busy provider as no failure and never moves to the next
model: it waits and asks the same model again, as often as it takes, until Stop. A Keep
waiting turn waits for the answer's Retry-After (at most RETRY_AFTER_MAX; else
RETRY_AFTER); background work (any priority but interactive) backs off: the Busy model
wait (Settings, Advanced) doubled at each try, the Longest busy model wait at most, and
never shorter than the Retry-After (`backoff`). A turn says so
("waiting" with reason `busy`, "Waiting for <model>.") and the wait does not count
against its wall clock. Only the status says busy: a load failure's text (out of
memory) is a could-not-load failure, never waited on.

A turn's Stop ends a call at once in every phase: waiting for a model, waiting for a
slot, waiting for the model to load, and streaming. The HTTP request is cancelled.

A GPU lease (`gpu_lease`), only for llama-swap: an image generation on a ComfyUI
provider that shares its GPU with a llama-swap provider takes that endpoint whole in
the scheduler. It waits until no request of this app runs there and holds new ones
("waiting" with reason `gpu_lease`), unloads llama-swap's models, and releases the
endpoint when the image is done, stopped or failed. Clients of llama-swap outside this
app are not held.

Config: `[queue]` in config/llm.toml (how long a call waits for a slot), the providers
and task models in Settings (slots per model, `max_concurrent` per provider).
"""
from __future__ import annotations

import contextlib
import contextvars
import itertools
import math
import logging
import time
from typing import Any, Awaitable, Callable

import anyio
import httpx

from .. import metrics
from .base import BackendError, Done
from .openai import has_images
from .queue import Position, Priority, QueueCancelled, Scheduler, Ticket
from .router import NoModel, Ordered, Router, app_router
from ..settings.kinds import SWAPS

QUEUE_KEYS = {              # config/llm.toml [queue]: here and backends/queue.py
    "wait_seconds": "num", "background_wait_seconds": "num", "gpu_idle_seconds": "num",
    "gpu_patience_seconds": "num",
}

log = logging.getLogger("celine.llm")

LOAD_POLL = 1.0          # seconds between /running looks while a model loads
RECHECK = 5.0            # a call waiting for a model looks again this often
UNLOAD_SECONDS = 60.0    # the most a lease waits for llama-swap to unload its models
RETRY_AFTER = 10.0       # seconds before a busy model is asked again, no Retry-After
RETRY_AFTER_MAX = 120.0  # the most a Retry-After header is waited for
LEASE_HELD = "Waiting: the GPU is making an image."
_leases = itertools.count(1)
_LOAD_FAILED = ("exited prematurely", "failed to start", "unable to load", "out of memory",
                "cudamalloc failed")

Emit = Callable[[dict[str, Any]], Awaitable[None]]


# the time limits of the tool calls running here (turns/tools.py): a wait for a model
# inside a tool moves them like the turn's clock
TOOL_CLOCKS: contextvars.ContextVar[tuple] = contextvars.ContextVar("tool_clocks",
                                                                    default=())


@contextlib.contextmanager
def paused(clock):
    """Time spent waiting for a model is not its work: while inside, `clock` (a turn's)
    and the running tools' limits do not run, and they end that much later after."""
    clocks = [c for c in (clock, *TOOL_CLOCKS.get()) if c is not None]
    t0, was = time.monotonic(), [c.deadline for c in clocks]
    for c in clocks:
        c.deadline = math.inf
    try:
        yield
    finally:
        spent = time.monotonic() - t0
        for c, d in zip(clocks, was):
            c.deadline = d + spent


def endpoint_key(backend) -> str:
    cfg = getattr(backend, "cfg", None)
    url = getattr(cfg, "base_url", None)
    return str(url).rstrip("/") if url else f"backend:{getattr(backend, 'name', 'unknown')}"


def provider_label(backend) -> str:
    return getattr(getattr(backend, "cfg", None), "label", None) or endpoint_key(backend)


def busy(e: BaseException) -> bool:
    """An HTTP 429, 503 or 529: the model is fine, the provider wants us to wait."""
    return getattr(e, "status", None) in (429, 503, 529)


def retry_after(e: BaseException) -> float:
    """Seconds to wait before asking again: the answer's Retry-After, capped, else
    RETRY_AFTER."""
    secs = getattr(e, "retry_after", None)
    return RETRY_AFTER if secs is None else min(float(secs), RETRY_AFTER_MAX)


def backoff(e: BaseException, tries: int, cfg) -> float:
    """Seconds background work waits before it asks a busy model again after `tries`
    waits: the Busy model wait doubled at each, the Longest busy model wait at most
    (Settings, Advanced), never shorter than the answer's Retry-After (capped)."""
    first, most = float(cfg.r("busy_retry_seconds")), float(cfg.r("busy_retry_max_seconds"))
    secs = min(most, first * 2 ** min(tries, 32))
    given = getattr(e, "retry_after", None)
    return secs if given is None else max(secs, min(float(given), RETRY_AFTER_MAX))


class LLM:
    """The app's one scheduler and its router. Implements queue.Limits."""

    def __init__(self, cfg, publish: Callable[[dict], None] | None = None, *,
                 build: Callable[[Any], Any] | None = None, router: Router | None = None):
        self.cfg = cfg
        self.publish = publish
        self._build = build
        self.router = router or app_router(cfg, publish)
        try:                                 # events leave from the server's loop
            import asyncio
            self.router.bind(asyncio.get_running_loop())
        except RuntimeError:
            pass
        self.scheduler = Scheduler(self, on_change=self._on_change,
                                   settings=lambda: self.queue)
        self.turn_priority: dict[str, Priority] = {}
        self.turn_clock: dict[str, Any] = {}       # a turn's wall clock, credited for waits
        self.turn_waits: dict[str, Any] = {}       # Keep waiting turns: their Stop token

    # ---- settings -------------------------------------------------------------------

    @property
    def queue(self) -> dict[str, Any]:
        return dict((getattr(self.cfg, "llm", None) or {}).get("queue") or {})

    def _provider_at(self, endpoint: str) -> dict[str, Any] | None:
        s = getattr(self.cfg, "settings", None)
        for p in (s.providers.values() if s is not None else ()):
            if str(p.get("base_url", "")).rstrip("/") == endpoint:
                return p
        return None

    # ---- queue.Limits -----------------------------------------------------------------

    def slots(self, endpoint: str, model: str) -> int:
        """The model's `slots` in Settings (a vision twin's own, else its base
        model's); else one request at a time."""
        p = self._provider_at(endpoint)
        if p is None:
            return 0
        models = p.get("models") or {}
        base = next((m for m in models.values() if m.get("vision_twin") == model), {})
        n = (models.get(model) or {}).get("slots") or base.get("slots")
        return int(n) if n else 1

    def endpoint_max(self, endpoint: str) -> int:
        p = self._provider_at(endpoint)
        return int((p.get("options") or {}).get("max_concurrent", 0)) if p else 0

    def exclusive(self, endpoint: str) -> bool:
        """llama-swap runs one model of a group at a time: asking for another one
        unloads the first and ends its requests."""
        p = self._provider_at(endpoint)
        return bool(p and p.get("kind") in SWAPS)

    # ---- building ---------------------------------------------------------------------

    def make(self, ref: str, thinking: str | None = None, voice: dict | None = None):
        """The backend for one model. `voice`: the call speaks in its voice, so its
        Banned words go into its requests (settings/voice.py)."""
        bc = self.router.backend_config(ref)
        if bc is None:
            return None
        if thinking:
            from ..settings import thinking as thinking_mod
            s = self.router.s
            offered = thinking_mod.levels(s.providers.get(s.split(ref)[0]) or {},
                                          s.model(ref) or {})
            bc = thinking_mod.apply(bc, thinking_mod.fit(thinking, offered), offered)
        if voice:
            from ..settings import voice as voice_mod
            bc = voice_mod.apply(bc, voice)
        if self._build is not None:
            return self._build(bc)
        from .openai import build
        return build(bc, float((bc.raw or {}).get("timeout", 600.0)))

    def thinking_for(self, role: str, conversation_id: str | None) -> str | None:
        """The conversation's own thinking level (chat turns only), else None."""
        if role != "chat" or not conversation_id:
            return None
        s = getattr(self.cfg, "settings", None)
        return (getattr(s, "conversation_thinking", None) or {}).get(conversation_id)

    def bind(self, backend=None, *, role: str = "chat", priority: Priority = Priority.AGENT,
             conversation_id: str | None = None, turn_id: str | None = None, token=None,
             emit: Emit | None = None, lane: str | None = None, clock=None,
             body: dict[str, Any] | None = None,
             thinking: str | None = None, images=None,
             wait: float | None = None,
             voice: bool = True, fallback: bool = True, keep_waiting: bool = False,
             label: str | None = None) -> "QueuedBackend":
        """A backend for one task. `backend` given (a test double): that
        backend alone, queued but not routed. Else the task's models from Settings.
        `thinking`: the call's own level (settings/thinking.py), e.g. "off" for a
        background request; a chat turn takes its conversation's. `images`: a turn's
        attachments.Images, which fits the images of each request to the model that
        answers it. `wait`: the most seconds a call waits for a model, else the queue's; an interactive call ignores it and never times out waiting. `voice`
        False: no voice bans (a summary, a memory). `fallback` False: a turn the user
        waits on, exactly its first model. `keep_waiting`: the user chose Keep waiting,
        a busy provider is waited for (QueuedBackend._attempt). `label`: who the calls
        are for in the queue (the conversation's title, a schedule's name); else the
        role's name. A call for a turn (`turn_id`: a tool's vision, Writing or
        models__ask call) credits its waits to the turn's clock, and in a Keep waiting
        turn it waits like the turn, until the turn's Stop."""
        if turn_id:
            self.turn_priority[turn_id] = priority
            if clock is not None:
                self.turn_clock[turn_id] = clock
            clock = self.turn_clock.get(turn_id)
            if keep_waiting:
                self.turn_waits[turn_id] = token
            elif turn_id in self.turn_waits:
                keep_waiting = True
                token = token if token is not None else self.turn_waits[turn_id]
        return QueuedBackend(self, backend, role=role, priority=priority,
                             conversation_id=conversation_id, turn_id=turn_id,
                             token=token, emit=emit,
                             lane=lane or conversation_id or turn_id or role,
                             clock=clock, body=body, thinking=thinking, images=images,
                             wait=wait, voice=voice, fallback=fallback,
                             keep_waiting=keep_waiting, label=label or _role_label(role))

    def forget_turn(self, turn_id: str) -> None:
        self.turn_priority.pop(turn_id, None)
        self.turn_clock.pop(turn_id, None)
        self.turn_waits.pop(turn_id, None)

    # ---- the GPU lease ----------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def gpu_lease(self, pid: str, *, label: str, conversation_id: str | None = None,
                        turn_id: str | None = None,
                        on_wait: Callable[[Position], Awaitable[None]] | None = None):
        """Provider `pid` (llama-swap) whole, for work that needs its GPU: waits until
        no request of this app runs there, holds new ones, unloads its models, and
        releases on exit, also after an error, a Stop or a timeout. Raises QueueTimeout
        after the queue's wait."""
        p = self.router.provider(pid)
        if p is None:
            yield
            return
        prio = self.turn_priority.get(turn_id or "", Priority.AGENT)
        ticket = Ticket(endpoint=str(p["base_url"]).rstrip("/"), model="",
                        priority=prio, lane=f"lease-{next(_leases)}", role=label,
                        label=label,
                        conversation_id=conversation_id, turn_id=turn_id, exclusive=True)
        with paused(self.turn_clock.get(turn_id or "")):
            await self.scheduler.acquire(ticket, timeout=self.wait_timeout(prio),
                                         on_wait=on_wait)
        try:
            await self._unload(pid)
            yield
        finally:
            self.scheduler.release(ticket)

    async def _unload(self, pid: str) -> None:
        """llama-swap unloads every model (POST /api/models/unload); then its /running
        is watched until it is empty. An unreachable server holds no GPU memory."""
        from ..settings import kinds
        from ..settings.registry import provider_headers
        p = self.router.provider(pid) or {}
        root = kinds.root_of(str(p.get("base_url", "")))
        headers = provider_headers(self.router.s, pid)
        try:
            async with httpx.AsyncClient(timeout=UNLOAD_SECONDS,
                                         transport=self.router.transport) as c:
                r = await c.post(f"{root}/api/models/unload", headers=headers)
                r.raise_for_status()
                with anyio.move_on_after(UNLOAD_SECONDS) as waited:
                    while (await c.get(f"{root}/running", headers=headers)).json().get(
                            "running"):
                        await anyio.sleep(LOAD_POLL / 2)
            log.info("models unloaded", extra={"event": "llm.unloaded", "provider": pid,
                                                "complete": not waited.cancelled_caught})
        except (httpx.HTTPError, ValueError) as e:
            log.info("unload failed", extra={"event": "llm.unload_failed", "provider": pid,
                                             "error": str(e) or type(e).__name__})
        await self.router.probe(pid, max_age=0)

    def wait_timeout(self, priority: Priority) -> float:
        """The most seconds a call of `priority` waits for a slot; 0: no limit. The user
        waits on an interactive call: it waits as long as it takes."""
        q = self.queue
        if priority == Priority.INTERACTIVE:
            return 0.0
        if priority == Priority.BACKGROUND:
            return float(q.get("background_wait_seconds", 120))
        return float(q.get("wait_seconds", 0))

    def _on_change(self, snap: dict) -> None:
        if self.publish is not None:
            self.publish(snap)

    def _note(self, ref: str, e: BackendError, loaded_then, saw_starting: bool) -> str:
        """Record why `ref` failed before its first token. Returns the kind."""
        pid = ref.partition("/")[0]
        msg = str(e)
        if getattr(e, "unreachable", False):
            self.router.note_unreachable(pid, msg)
            return "unreachable"
        if saw_starting or any(w in msg.lower() for w in _LOAD_FAILED):
            self.router.note_failed(ref, "could_not_load", msg, loaded_then)
            return "could_not_load"
        self.router.note_failed(ref, "error", msg)
        return "error"


def _role_label(role: str) -> str:
    from ..settings.registry import ROLES
    return str((ROLES.get(role) or {}).get("label") or role)


class _Moved(Exception):
    """This model failed before its first token; try the next one."""

    def __init__(self, kind: str, error: BackendError):
        self.kind = kind
        self.error = error


class _Busy(Exception):
    """The provider answered 429, 503 or 529 before the first token; the same model
    again, after a wait."""

    def __init__(self, error: BackendError):
        self.error = error


def _request_of(backend, messages, tools, extra, model: str) -> dict:
    """The body a backend sends, built the way its stream builds it (request_body is
    pure), so what is kept is what went out."""
    build = getattr(backend, "request_body", None)
    body = build(messages, tools, extra, model=model) if build else \
        {"messages": messages, "tools": tools, **(extra or {})}
    return {"provider": str(getattr(backend, "name", "")), "model": str(model),
            "body": body}


class QueuedBackend:
    """One task's view of the models for one turn (or one background call)."""

    def __init__(self, llm: LLM, fixed, *, role: str, priority: Priority,
                 conversation_id: str | None, turn_id: str | None, token, emit: Emit | None,
                 lane: str, message_id: str | None = None, clock=None,
                 body: dict[str, Any] | None = None, thinking: str | None = None,
                 images=None, shared: dict | None = None, wait: float | None = None,
                 voice: bool = True, fallback: bool = True, keep_waiting: bool = False,
                 label: str = ""):
        self.llm = llm
        self.label = label
        self.fixed = fixed
        self.role = role
        self.priority = priority
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.token = token
        self.emit = emit
        self.lane = lane
        self.message_id = message_id
        self.clock = clock
        self.body = body
        self.thinking = thinking
        self.images = images
        self.wait = wait
        self.voice = voice
        self.fallback = fallback
        self.keep_waiting = keep_waiting
        # shared by a turn's per-message copies: the backend that answered last
        self._shared = shared if shared is not None else {"answered": None}

    # ---- the backend surface the runner and attachments read ------------------------

    def _head(self):
        """The backend of the task's first model (what a turn is "on" before it runs)."""
        if self.fixed is not None:
            return self.fixed
        chain = self.llm.router.chain(self.role, self.conversation_id)
        return self.llm.make(chain[0]) if chain else None

    @property
    def name(self) -> str:
        h = self._head()
        return getattr(h, "name", "") if h is not None else ""

    @property
    def model(self) -> str:
        h = self._head()
        return getattr(h, "model", "") if h is not None else ""

    @property
    def supports_tools(self) -> bool:
        h = self._head()
        return True if h is None else bool(getattr(h, "supports_tools", False))

    @property
    def cfg(self):
        return getattr(self._head(), "cfg", None)

    @property
    def answered(self):
        """The backend that answered the last call of this turn."""
        return self._shared["answered"]

    @property
    def request_sent(self) -> dict | None:
        """The last call's request as it went out: {provider, model, body}
        (turns/store.record_usage keeps it with the reply)."""
        return self._shared.get("request")

    def for_message(self, message_id: str | None,
                    thinking: str | None = None) -> "QueuedBackend":
        """The same task for one message; `thinking` in place of the task's (a reply
        continued in place goes as a prefill, thinking off)."""
        return QueuedBackend(self.llm, self.fixed, role=self.role, priority=self.priority,
                             conversation_id=self.conversation_id, turn_id=self.turn_id,
                             token=self.token, emit=self.emit, lane=self.lane,
                             message_id=message_id, clock=self.clock, body=self.body,
                             thinking=thinking or self.thinking, images=self.images,
                             shared=self._shared,
                             wait=self.wait, voice=self.voice, fallback=self.fallback,
                             keep_waiting=self.keep_waiting, label=self.label)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.emit is None:
            return
        try:
            await self.emit({"turn_id": self.turn_id, "message_id": self.message_id,
                             "role": self.role, **payload})
        except Exception:
            log.warning("queue event failed", extra={"event": "llm.queue.emit_failed"})

    def _cancelled(self) -> bool:
        return self.token is not None and self.token.cancelled

    # ---- the call ---------------------------------------------------------------------

    async def stream(self, messages, tools):
        if self.fixed is not None:
            one = self._attempt(None, messages, tools, None, None, head=None, why=None)
            try:
                async for ev in one:
                    yield ev
            finally:                        # a consumer that stops early frees the slot
                await one.aclose()
            return
        router = self.llm.router
        thinking = self.thinking or self.llm.thinking_for(self.role, self.conversation_id)
        from ..settings import voice as voice_mod
        voice = voice_mod.of(self.llm.cfg, self.role) if self.voice else None
        # one model only, and its failure is the call's (fallback False, no Keep waiting)
        only = not self.fallback
        fails = only and not self.keep_waiting
        failed_here: set[str] = set()
        last_error: BackendError | None = None
        said: str | None = None
        head_why: str | None = None         # why the conversation's own model is not used
        asked_again = False
        started = anyio.current_time()
        while True:
            if self._cancelled():
                raise QueueCancelled("stopped")
            pick = router.s.conversation_models.get(self.conversation_id or "")
            if self.role == "chat" and pick and router.s.model(pick) is None:
                raise NoModel(f"This chat's model {pick} no longer exists. "
                              f"Pick another model for this chat.")
            ordered: Ordered = await router.order(self.role,
                                                  conversation_id=self.conversation_id)
            if only:
                ordered = ordered.head()
            todo = [r for r in ordered.refs if r not in failed_here]
            if not todo:
                if last_error is not None:
                    raise last_error
                sentence = ordered.sentence()
                if fails:
                    # a provider seen down is asked once more (the state may be stale:
                    # a host back, one failed request); still down fails the call
                    pid = ordered.chain[0].partition("/")[0] if ordered.chain else ""
                    if pid and not asked_again and \
                            router.provider_state(pid).reachable is False:
                        asked_again = True
                        st = await router.probe(pid, max_age=0)
                        if st.reachable is not False:
                            continue
                        sentence = st.error or sentence
                    raise NoModel(sentence)
                if self.priority == Priority.BACKGROUND:
                    raise NoModel(sentence)
                left = None if self.wait is None or self.priority == Priority.INTERACTIVE \
                    else self.wait - (anyio.current_time() - started)
                if left is not None and left <= 0:
                    raise NoModel(sentence)
                if sentence != said:
                    said = sentence
                    await self._send({"state": "waiting", "reason": "no_model",
                                      "message": sentence,
                                      "priority": self.priority.name.lower()})
                with anyio.move_on_after(left if left is not None else float("inf")):
                    await self._wait_for_model()
                # every model tried here failed as unreachable or could not load, and the
                # router leaves those out while the cause holds: what it offers after the
                # wait is worth a new try
                failed_here.clear()
                continue
            head = ordered.chain[0] if ordered.chain else None
            head_why = dict(ordered.skipped).get(head, head_why)
            for ref in todo:
                attempt = self._attempt(ref, messages, tools, thinking, voice,
                                        head=head, why=head_why)
                try:
                    async for ev in attempt:
                        yield ev
                    return
                except _Moved as m:
                    if fails:
                        if m.kind == "could_not_load" and self._interactive():
                            m.error.code = "busy"
                        raise m.error from None
                    failed_here.add(ref)
                    if ref == head:
                        head_why = str(m.error)
                    if m.kind == "error":
                        last_error = m.error
                finally:                # a consumer that stops early frees the slot
                    await attempt.aclose()

    def _interactive(self) -> bool:
        """The user waits on this call and did not choose Keep waiting."""
        return self.priority == Priority.INTERACTIVE and not self.keep_waiting

    async def _attempt(self, ref: str, messages, tools, thinking, voice, *,
                       head: str | None, why: str | None):
        """One model until it answers. A busy provider is waited for and the same model
        asked again, until Stop; a call the user waits on fails with code `busy`
        instead. Raises _Moved when the model failed before its first token. `ref`
        None: the fixed backend."""
        backend = self.fixed if ref is None else self.llm.make(ref, thinking, voice)
        tries = 0                           # busy waits so far: background backs off
        while True:
            loaded = None if ref is None else \
                self.llm.router.provider_state(ref.partition("/")[0]).loaded
            one = self._one(backend, ref, messages, tools, head=head, why=why,
                            loaded_then=loaded)
            try:
                async for ev in one:
                    yield ev
                return
            except _Busy as b:
                if self._interactive():
                    b.error.code = "busy"
                    raise b.error from None
                await self._wait_busy(b.error, backend, tries)
                tries += 1
            finally:                        # a consumer that stops early frees the slot
                await one.aclose()

    async def _wait(self, until: Callable[[], Awaitable[None]]) -> None:
        """Until `until` returns, or Stop. The wait is not counted against the turn's
        wall clock."""
        with paused(self.clock):
            async with anyio.create_task_group() as tg:
                async def wake() -> None:
                    await until()
                    tg.cancel_scope.cancel()
                tg.start_soon(wake)
                if self.token is not None:
                    async def stop() -> None:
                        await self.token.wait()
                        tg.cancel_scope.cancel()
                    tg.start_soon(stop)
        if self._cancelled():
            raise QueueCancelled("stopped")

    async def _wait_for_model(self) -> None:
        """Until the router says something changed, a recheck is due, or Stop."""
        await self._wait(lambda: self.llm.router.wait_change(RECHECK))

    async def _wait_busy(self, e: BackendError, backend, tries: int) -> None:
        """The provider's Retry-After (else RETRY_AFTER) for a Keep waiting turn, the
        backoff after `tries` waits for background work; or Stop. The UI hears why."""
        await self._send({"state": "waiting", "reason": "busy",
                          "message": f"Waiting for {backend.model}.",
                          "model": backend.model, "endpoint": provider_label(backend),
                          "priority": self.priority.name.lower()})
        secs = retry_after(e) if self.priority == Priority.INTERACTIVE else \
            backoff(e, tries, self.llm.cfg)
        await self._wait(lambda: anyio.sleep(secs))

    async def _one(self, backend, ref: str | None, messages, tools, *, head: str | None,
                   loaded_then, why: str | None = None):
        """One model: a slot, then the stream. Raises _Moved when a routed model fails
        before its first event in a way the next model may not, _Busy when its
        provider is busy. `why`: why the head model is not the one answering."""
        sched = self.llm.scheduler
        endpoint = endpoint_key(backend)
        label = provider_label(backend)
        is_fallback = ref is not None and head is not None and ref != head
        if self.images is not None:
            # before this model's slot: a description may need a model on the same server
            messages = await self._fit_images(messages, backend, ref)
        model = await self._model_for(backend, ref, messages, loaded_then)
        ticket = Ticket(endpoint=endpoint, model=model, priority=self.priority,
                        lane=self.lane, role=self.role, label=self.label,
                        conversation_id=self.conversation_id, turn_id=self.turn_id)
        waited = False

        async def on_wait(pos: Position) -> None:
            nonlocal waited
            waited = True
            held = {"reason": "gpu_lease", "message": LEASE_HELD} if pos.lease else {}
            await self._send({"state": "waiting", "ahead": pos.ahead,
                              "running": pos.running, "model": model,
                              "endpoint": label, "priority": self.priority.name.lower(),
                              "holder": sched.holder(endpoint), **held})

        try:
            with paused(self.clock):
                await sched.acquire(ticket, token=self.token,
                                    timeout=self.llm.wait_timeout(self.priority)
                                    if self.wait is None or self.priority == Priority.INTERACTIVE
                                    else self.wait, on_wait=on_wait)
        except BackendError as e:
            await self._send({"state": "left", "model": model, "endpoint": label,
                              "reason": "cancelled" if self._cancelled() else "timeout",
                              "message": str(e)})
            raise
        metrics.QUEUE_WAIT.labels(self.role, label).observe(ticket.waited_ms / 1000)
        if self.llm.exclusive(endpoint) and sched.streak.get(endpoint) == (model, 1) \
                and model not in (loaded_then or ()):
            metrics.MODEL_SWAPS.labels(label, model).inc()
        yielded = False
        watch: dict[str, Any] = {"starting": False, "t0": time.monotonic()}
        outcome, usage = "cancelled", None
        try:
            self._shared["answered"] = backend
            if waited or is_fallback or self.emit is not None:
                head_model = self.llm.router.s.split(head)[1] if is_fallback else None
                await self._send({"state": "running", "model": model,
                                  "backend": backend.name, "endpoint": label,
                                  "fallback": is_fallback,
                                  "instead_of": head_model, "why": why if is_fallback else None,
                                  "waited_ms": ticket.waited_ms})
            if is_fallback:
                log.info("model call on the next model", extra={
                    "event": "llm.fallback", "role": self.role, "model": model})
            # a model in the list without the tools capability gets no tool list
            sent = tools if backend.supports_tools else []
            self._shared["request"] = _request_of(backend, messages, sent, self.body, model)
            kw: dict[str, Any] = {"body": self.body} if self.body else {}
            if model != backend.model:
                kw["model"] = model
            inner = backend.stream(messages, sent, **kw)
            try:
                while True:
                    try:
                        ev = await self._next(inner, model, ref, label, watch,
                                              watching=not yielded)
                    except StopAsyncIteration:
                        break
                    if not yielded:
                        metrics.MODEL_FIRST_TOKEN.labels(self.role, label, model).observe(
                            time.monotonic() - watch["t0"])
                    if not yielded and ref is not None:
                        self.llm.router.note_ok(ref)
                        # a load the watch saw start but not end (the first token came
                        # first): timed to that token
                        if watch["starting"] and not watch.get("timed"):
                            self.llm.router.note_load(f"{ref.partition('/')[0]}/{model}",
                                                      time.monotonic() - watch["t0"])
                    yielded = True
                    if isinstance(ev, Done):
                        outcome, usage = "ok", ev.usage
                        sched.release(ticket)      # free before the consumer moves on
                        yield ev
                        return
                    yield ev
            finally:
                aclose = getattr(inner, "aclose", None)
                if aclose is not None:
                    await aclose()
        except BackendError as e:
            if self._cancelled():
                raise QueueCancelled("stopped") from None
            outcome = "error"
            # a request the model cannot take (too long, an image) is the same for the
            # next one; a missing model, a rejected key, no credits are this model's
            if yielded or e.code in ("context_overflow", "unsupported_input"):
                raise
            if busy(e):                     # the model is fine: nothing to note
                raise _Busy(e) from None
            if ref is None:
                raise
            kind = self.llm._note(ref, e, loaded_then, watch["starting"])
            raise _Moved(kind, e) from None
        finally:
            sched.release(ticket)
            metrics.MODEL_REQUESTS.labels(self.role, label, model, outcome).inc()
            metrics.MODEL_SECONDS.labels(self.role, label, model).observe(
                time.monotonic() - watch["t0"])
            if usage is not None:
                metrics.MODEL_TOKENS.labels(self.role, label, model, "in").inc(
                    usage.input_tokens)
                metrics.MODEL_TOKENS.labels(self.role, label, model, "out").inc(
                    usage.output_tokens)

    async def _model_for(self, backend, ref: str | None, messages, loaded_then) -> str:
        """The name the request goes out under. The vision twin (Settings) answers a
        request with an image, and every request while it is loaded, so the server
        never swaps back and forth between the two."""
        cfg = getattr(backend, "cfg", None)
        twin = cfg.raw.get("vision_twin") if cfg is not None else None
        if not twin:
            return backend.model
        if has_images(messages):
            return twin
        loaded = (await self.llm.router.probe(ref.partition("/")[0])).loaded \
            if ref is not None else None
        return twin if twin in (loaded or loaded_then or ()) else backend.model

    async def _fit_images(self, messages, backend, ref: str | None):
        """The request's images as this model takes them (attachments.Images), raced
        against the turn's Stop: a photo description can take a while."""
        from ..attachments import label
        out: dict[str, Any] = {}
        async with anyio.create_task_group() as tg:
            async def fit() -> None:
                out["messages"] = await self.images.wire(
                    messages, backend,
                    label(self.llm.router.s, ref) if ref else backend.model)
                tg.cancel_scope.cancel()
            tg.start_soon(fit)
            if self.token is not None:
                async def stop() -> None:
                    await self.token.wait()
                    tg.cancel_scope.cancel()
                tg.start_soon(stop)
        if "messages" not in out:
            raise QueueCancelled("stopped")
        return out["messages"]

    async def _next(self, inner, model: str, ref: str | None, label: str, watch: dict,
                    *, watching: bool):
        """The next event of `inner`, raced against the turn's Stop (the request is
        cancelled, not waited for). Before the first event, a model llama-swap is
        still loading is announced as "loading"."""
        out: dict[str, Any] = {}
        async with anyio.create_task_group() as tg:
            if self.token is not None:
                async def stop() -> None:
                    await self.token.wait()
                    out.setdefault("why", "stopped")
                    tg.cancel_scope.cancel()
                tg.start_soon(stop)
            if watching and ref is not None:
                tg.start_soon(self._watch_load, model, ref, label, watch)
            try:
                out["ev"] = await inner.__anext__()
            except StopAsyncIteration:
                out["end"] = True
            except Exception as e:          # raised after the group, unwrapped
                out["err"] = e
            tg.cancel_scope.cancel()
        if "ev" in out:
            return out["ev"]
        if "err" in out:
            raise out["err"]
        if out.get("end"):
            raise StopAsyncIteration
        raise QueueCancelled("stopped")

    async def _watch_load(self, model: str, ref: str, label: str, watch: dict) -> None:
        """While the request waits for its first event on llama-swap: say "loading"
        once the server shows the model starting. No deadline: the server ends a
        failed load with an error itself."""
        router = self.llm.router
        pid = ref.partition("/")[0]
        if (router.provider(pid) or {}).get("kind") not in SWAPS:
            return
        while True:
            await anyio.sleep(LOAD_POLL)
            st = await router.probe(pid, max_age=LOAD_POLL / 2)
            if model in (st.loaded or ()):
                if watch["starting"] and not watch.get("timed"):   # a load we saw: to the poll
                    watch["timed"] = True
                    router.note_load(f"{pid}/{model}", time.monotonic() - watch["t0"])
                return                  # loaded: the prompt is being read
            if model in st.starting and not watch["starting"]:
                watch["starting"] = True
                await self._send({"state": "loading", "model": model,
                                  "endpoint": label})

