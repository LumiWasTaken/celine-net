"""What the app knows about its model servers, and which model does a task now.

Everything here comes from the servers' own answers, nothing from guesses or timers.

- **Health from requests**: what a server is like is learned from the requests the
  app sends anyway. A request that lands says up; a refused connection, a timeout or
  a proxy's 502 says down; a busy answer or an error is the model's state. A server is
  asked directly (`probe`, a GET with a PROBE_TIMEOUT timeout) only:
  1. once each at startup (`check_all`, __main__);
  2. on Check provider (Settings, Models), and once when a new provider is saved;
  3. once after a request landed on a model the server did not list as loaded (a
     swap: `note_ok`), so what it has loaded is known again;
  4. when a request got no event for FIRST_EVENT seconds on a server that says what it
     loads (backends/llm.py `_first_event`), strict: a timeout counts as down then;
     again each FIRST_EVENT while the server shows the model starting, and once more
     when it shows it loaded (the prompt is being read);
  5. once when a call that runs on exactly one model finds its provider seen down
     (backends/llm.py, `asked_again`), since that state may be stale;
  6. when work needs a server known to be failing (down, or a model of its chain
     could not load) and that state is RETRY_DOWN old (`_ask`):
     - a model call picks its model (`order`, `order_sync`);
     - while the agent is paused and a turn of its own waits for the pause (hold.any_held), the
       dispatcher's `revisit`, at most every HOLD_RECHECK (turns/dispatcher.py);
     - a work item is claimed, a deferred one that came due among them (work.py
       `revisit` for its roles);
     - a run waits with its slot come: the scheduler's tick asks `can_answer` (probe
       on) for the first model of its role (schedules/runs.py).
  Nothing polls: no heartbeat, no timer, no clock of how long anything was down.
- **Provider state**: reachable (up) or unreachable (down, with the server's or the
  connection's sentence), and `loaded`: the models loaded now (llama-swap `/running`,
  Ollama `/api/ps`, llama-server `/models`; None when the server does not say, as
  OpenRouter and ComfyUI (`/system_stats`) do not). It is in `models.state`, GET
  /api/status (`hosts`) and the metrics (celine_host_up, celine_host_down_total). A
  probe that times out keeps a server last seen up as up: a busy box lists its models
  late, and only a strict probe or a failed request marks it down.
- **Paused**: while every model of the chat and tasks roles is on a server seen down,
  it is paused (`paused`, event `agent.hold`, celine_paused): none of the agent's turns is
  claimed (hold.py) until a server is back; a sub-agent's or a drone's whose own model
  answers still is (`answering`). Looked at on every change of a server's state.
- **Model state**: ok, or failed with `kind` could_not_load (the server could not
  start it), error (it answered with an error) or busy (it asked to be asked later), the
  server's sentence, and `loaded_then`: what the server had loaded when the load was
  asked for. `problem` gives the one sentence a model row shows.
- **Requests per hour**: each request to a provider is counted as it goes out
  (`take`, keyed by the provider); while a provider used its Requests per hour
  (Settings, Models), `full` says so with the sentence "<provider> reached its <n>
  requests this hour.", and its models show it (`problem`). The request that uses the
  hour up publishes `models.state`, and sets the one timer here: it publishes again the
  moment the hour has room.

`order(task)` gives the task's models in the order to try:
1. chat: the conversation's own model first, then the chat task's list;
2. a model whose provider needs a key that is not saved is left out;
3. a model whose provider is down is left out, as one that failed: no call waits for
   its timeout (a call that finds it down marks it, `_ask` asks it again);
4. a model whose provider used its Requests per hour is passed over while another
   model of the list can be tried, unless the provider's Next model at the hourly limit is off
   (`next_when_full`); with none, or with it off, it is tried, and the call waits for
   the hour or fails with the sentence (backends/llm.py). A call with no fallback
   (`order(fallback=False)`: a turn the user waits on, a Routine's run) gets only the
   first model, whatever its hour;
5. a model that could not be loaded is left out while everything that was loaded
   then is still loaded (nothing was freed, so the load would fail the same way).
   When nothing was loaded then, the memory is held outside the server (an image
   being made, a game) and the server cannot say when it is free; a server that does
   not say what it has loaded cannot say either: the model is tried again after
   `RETRY_OUTSIDE_MS`.
   A failure is the model's state only while it is current: every probe forgets a
   could-not-load failure once its cause is gone (the model is loaded now, or what
   was loaded then was freed), a later answer from the model forgets any failure,
   and Check provider forgets them all. So a failure from hours ago never shows as
   the model's state now.

Images never change the order: the model that answers gets them as it can take them
(attachments.py).

Where work runs follows only from the task lists in Settings. The router logs one line
when a provider changes state and one when a model enters or leaves `failed`, and
publishes `models.state` on the global feed.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import anyio
import httpx

from ..core.ids import HOUR, hour_room, now_ms, reached
from ..settings.kinds import SWAPS
from .base import BackendError
from ..core.event_types import AGENT_HOLD, MODELS_STATE, MODEL_LOADED

log = logging.getLogger("celine.router")

PROBE_TTL = 3.0          # seconds a probe result answers for
PROBE_TIMEOUT = 2.0
RETRY_DOWN = 10.0        # seconds before a server known to be failing is asked again


class NoModel(BackendError):
    """No model of the task can answer now. `sentence` says why, for the UI."""

    def __init__(self, sentence: str):
        super().__init__(sentence, retryable=False)
        self.sentence = sentence
        self.code = "no_model"            # the turn's reason_code: continuable once one answers


@dataclass
class ProviderState:
    reachable: bool | None = None          # None: not asked yet
    error: str | None = None               # why it is unreachable
    loaded: frozenset[str] | None = None   # None: the server does not say
    starting: frozenset[str] = frozenset()
    checked: float = 0.0                   # monotonic time of the last probe

    def public(self) -> dict[str, Any]:
        return {"reachable": self.reachable,
                "error": self.error,
                "loaded": sorted(self.loaded) if self.loaded is not None else None,
                "starting": sorted(self.starting)}


@dataclass
class ModelState:
    kind: str                              # could_not_load | error | busy
    message: str
    at: int
    loaded_then: frozenset[str] | None = None
    sig: str = ""                          # the model's settings when it failed

    def public(self) -> dict[str, Any]:
        return {"state": "failed", "kind": self.kind, "message": self.message}


@dataclass
class Ordered:
    """A task's models to try now, and a sentence for each one left out."""
    role: str
    refs: list[str]
    chain: list[str]
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def passed_over(self, ref: str) -> str | None:
        """The models of the chain before `ref`, each with why it is left out now
        (`ref` answers in their place); None while `ref` is the chain's first."""
        if ref not in self.chain or self.chain[0] == ref:
            return None
        why = dict(self.skipped)
        return " ".join(f"{r}: {why[r]}" if r in why else f"{r}: left out."
                        for r in self.chain[:self.chain.index(ref)])

    def sentence(self) -> str:
        from ..settings.registry import ROLES
        what = ROLES.get(self.role, {}).get("label", self.role)
        if not self.chain:
            return (f"No model is set for {what.lower()}. Choose one in Settings, Models."
                    if self.role != "chat" else
                    "No chat model is set. Choose one in Settings, Models.")
        seen: list[str] = []
        for _, why in self.skipped:
            if why not in seen:
                seen.append(why)
        return " ".join(seen) or f"No model for {what.lower()} can answer now."


RETRY_OUTSIDE_MS = 60_000   # a load that failed with nothing of ours loaded: try again after
AGENT_ROLES = ("chat", "tasks")   # no host of these answers: it pauses


def _cause_gone(ms: ModelState, name: str, loaded: frozenset[str] | None) -> bool:
    """A could-not-load failure no longer holds: the model is loaded now, or the
    server freed something that was loaded when the load failed. When nothing of ours
    was loaded and the load still failed, the memory is held outside this server (an
    image being made, a game); that can change without the server saying so, so the
    model gets another try after RETRY_OUTSIDE_MS instead of never. A server that does
    not say what it has loaded (`loaded` None) gets the same retry."""
    if ms.kind != "could_not_load":
        return False
    if loaded is None:
        return now_ms() - ms.at >= RETRY_OUTSIDE_MS
    if name in loaded or ms.loaded_then is None:
        return name in loaded
    if ms.loaded_then:
        return not loaded >= ms.loaded_then
    return now_ms() - ms.at >= RETRY_OUTSIDE_MS


class Router:
    def __init__(self, cfg, publish: Callable[[dict], None] | None = None, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sync_transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.publish = publish
        self.transport = transport
        self.sync_transport = sync_transport
        self._providers: dict[str, ProviderState] = {}
        self._models: dict[str, ModelState] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        # model ref -> seconds its last load on llama-swap took (backends/llm.py measures)
        # note: kept in memory, a restart forgets them until the next load
        self.load_seconds: dict[str, float] = {}
        self.paused = False                    # no host of its chat and tasks models answers
        self._on_reachable: list[Callable[[str], None]] = []
        self._metered: set[str] = set()        # providers with a celine_host_up label
        self._discovering: dict[str, asyncio.Task] = {}   # provider -> its list being read
        self._tasks: set[asyncio.Task] = set()  # probes and lists started from a state change
        # provider -> when each of its requests of the last hour went out (take)
        # ponytail: in memory, a restart resets the hour
        self.sent: dict[str, deque] = {}
        self.now: Callable[[], float] = time.monotonic     # tests: a fixed clock

    # ---- settings ---------------------------------------------------------------------

    @property
    def s(self):
        return getattr(self.cfg, "settings", None)

    def provider(self, pid: str) -> dict[str, Any] | None:
        s = self.s
        return None if s is None else s.providers.get(pid)

    def _sig(self, ref: str) -> str:
        s = self.s
        if s is None:
            return ""
        from ..settings.files import version
        pid, _ = s.split(ref)
        p = s.providers.get(pid) or {}
        return version({"url": p.get("base_url"), "kind": p.get("kind"),
                        "model": s.model(ref)})

    # ---- the loop, events, waiting ---------------------------------------------------

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _on_loop(self, fn: Callable[[], None]) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            here = asyncio.get_running_loop() is loop
        except RuntimeError:
            here = False
        if here:
            fn()
        elif not loop.is_closed():
            loop.call_soon_threadsafe(fn)

    def _changed(self) -> None:
        """Wake whoever waits for a model, and tell the UI."""
        def fire() -> None:
            ev, self._event = self._event, None
            if ev is not None:
                ev.set()
            if self.publish is not None:
                try:
                    self.publish({"type": MODELS_STATE, **self.snapshot()})
                except Exception:
                    log.debug("publish failed", exc_info=True)
        self._on_loop(fire)

    def loaded_view(self) -> dict[str, Any]:
        """What GET /api/loaded-models and the `model.loaded` event say, from the last
        probes: every model of a provider whose server says what it has loaded, with
        `loaded` (it, or its vision twin, is loaded now), `loading` (its server says it is
        starting) and `load_seconds` (its last load, measured; null before one). A new chat never starts on another model for
        it: the chooser shows it."""
        s = self.s
        models: list[dict[str, Any]] = []
        for pid, p in (s.providers.items() if s is not None else ()):
            st = self.provider_state(pid)
            now = st.loaded
            if now is None:
                continue
            for name, m in (p.get("models") or {}).items():
                ref = f"{pid}/{name}"
                models.append({"ref": ref, "loaded": name in now or m.get("vision_twin") in now,
                               "loading": name in st.starting,
                               "load_seconds": self.load_seconds.get(ref)})
        return {"models": models}

    def note_load(self, ref: str, seconds: float) -> None:
        """A load of `ref` on llama-swap took `seconds` (backends/llm.py)."""
        self.load_seconds[ref] = round(seconds, 1)
        self._loaded_changed()

    def _loaded_changed(self) -> None:
        def fire() -> None:
            if self.publish is not None:
                try:
                    self.publish({"type": MODEL_LOADED, **self.loaded_view()})
                except Exception:
                    log.debug("publish failed", exc_info=True)
        self._on_loop(fire)

    def settings_changed(self) -> None:
        """Called after a reload: a waiting turn looks at the new task lists, and a
        provider that left Settings leaves the metrics."""
        from .. import metrics
        s = self.s
        for pid in self._metered - set(s.providers if s is not None else ()):
            metrics.HOST_UP.remove(pid)
            self._metered.discard(pid)
        self._changed()

    async def wait_change(self, timeout: float) -> None:
        """Until a provider or model changes state or the settings change, or
        `timeout` seconds (a provider coming back only shows in a probe)."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if self._event is None:
            self._event = asyncio.Event()
        ev = self._event
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"providers": {pid: st.public() for pid, st in self._providers.items()},
                    "models": {ref: m.public() for ref, m in self._models.items()}}

    def hosts(self) -> dict[str, str | None]:
        """{provider: up | down | None (not asked yet)}, for the config status."""
        s = self.s
        return {pid: {True: "up", False: "down"}.get(self.provider_state(pid).reachable)
                for pid in (s.providers if s is not None else {})}

    def hold(self) -> str | None:
        """"paused" while it is paused, else None."""
        return "paused" if self.paused else None

    def provider_state(self, pid: str) -> ProviderState:
        with self._lock:
            return self._providers.get(pid) or ProviderState()

    def server_context(self, pid: str, model: str, pinned: int | None) -> int | None:
        """The context the server says `model` runs with (turns/tokens.py); None when
        no server says."""
        from ..turns import tokens
        return tokens.server_window(str((self.provider(pid) or {}).get("base_url") or ""),
                                    model, pinned)

    def model_state(self, ref: str) -> ModelState | None:
        with self._lock:
            m = self._models.get(ref)
        if m is not None and m.sig != self._sig(ref):
            return None                     # its settings changed since it failed
        return m

    # ---- probing ---------------------------------------------------------------------

    def _probe_request(self, pid: str) -> tuple[str, dict[str, str]] | None:
        from ..settings import kinds
        from ..settings.registry import provider_key
        p = self.provider(pid)
        if p is None:
            return None
        path = kinds.KINDS[p["kind"]]["probe"]
        url = f"{kinds.root_of(p['base_url'])}{path}" if path.startswith("/") else \
            f"{p['base_url'].rstrip('/')}/{path}"
        key = provider_key(pid, getattr(self.s, "secrets_root", None))
        return url, kinds.headers(p["kind"], key)

    def _parse(self, pid: str, status: int, data: Any,
               body: bytes = b"") -> tuple[frozenset | None, frozenset, str | None]:
        """(loaded, starting, error) from a probe answer. Any answer but a 502 means the
        host is up; a rejected key is an error, and so is a 502: a proxy answers it when
        the server behind it is down (llm.LLM._note counts it unreachable too). Down is
        a refused connection or a timeout. An error is worded as a request's error is
        (openai.http_error): the server's own words behind its status."""
        p = self.provider(pid) or {}
        if status in (401, 403, 502):
            from .openai import _http_message
            return None, frozenset(), _http_message(
                p.get("label", pid), "", status, body.decode(errors="replace"))
        if status >= 400:       # a list endpoint it lacks (404, 405), a hiccup (429, 5xx)
            return None, frozenset(), None
        from ..settings.kinds import KINDS
        listed = (KINDS.get(p.get("kind")) or {}).get("loaded")
        if listed == "running":
            rows = (data or {}).get("running") or []
            loaded = frozenset(str(r.get("model")) for r in rows
                               if r.get("state") not in ("starting", "stopping"))
            starting = frozenset(str(r.get("model")) for r in rows
                                 if r.get("state") == "starting")
            return loaded, starting, None
        if listed == "ps":
            return frozenset(str(m.get("name") or m.get("model"))
                             for m in (data or {}).get("models") or []), frozenset(), None
        if listed == "models":
            # llama-server's router mode states each model's status (loaded, sleeping,
            # unloaded, loading); a single-model server lists the one it serves, with none
            rows = [m for m in (data or {}).get("data") or [] if isinstance(m, dict)]
            status = {str(m.get("id")): (m.get("status") or {}).get("value")
                      if isinstance(m.get("status"), dict) else "loaded" for m in rows}
            return frozenset(m for m, v in status.items() if v == "loaded"), \
                frozenset(m for m, v in status.items() if v == "loading"), None
        return None, frozenset(), None

    def _unreachable_sentence(self, pid: str, e: Exception) -> str:
        from urllib.parse import urlsplit
        p = self.provider(pid) or {}
        host = urlsplit(str(p.get("base_url", ""))).netloc or pid
        what = f"{p.get('label', pid)} at {host}"
        if isinstance(e, httpx.TimeoutException):
            return f"{what} did not answer within {PROBE_TIMEOUT:g} s."
        if isinstance(e, httpx.ConnectError):
            return f"{what} refused the connection."
        return f"{what} is unreachable ({type(e).__name__})."

    def _fresh(self, pid: str, max_age: float) -> ProviderState | None:
        with self._lock:
            st = self._providers.get(pid)
        if st is not None and st.reachable is not None and \
                time.monotonic() - st.checked < max_age:
            return st
        return None

    async def probe(self, pid: str, *, max_age: float = PROBE_TTL,
                    strict: bool = False) -> ProviderState:
        """The provider's state, asked again when older than `max_age` seconds
        (0: always, as Check provider asks). `strict`: a timeout marks it down even when
        it was up (`_failed`)."""
        if (st := self._fresh(pid, max_age)) is not None:
            return st
        req = self._probe_request(pid)
        if req is None:
            return ProviderState()
        url, headers = req
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT,
                                         transport=self.transport) as c:
                r = await c.get(url, headers=headers)
                loaded, starting, error = self._parse(pid, r.status_code, _json(r),
                                                      r.content)
                if (props := self._props(pid, url, loaded)) is not None:
                    got = {}
                    for m, u in props.items():
                        try:                # a window is extra: its answer never fails the probe
                            got[m] = _n_ctx(_json(await c.get(u, headers=headers)))
                        except httpx.HTTPError:
                            continue
                    self._windows(pid, {m: n for m, n in got.items() if n})
                self._ps_windows(pid, r.status_code, _json(r))
            return self._apply(pid, loaded, starting, error, fresh=max_age == 0)
        except httpx.HTTPError as e:
            return self._failed(pid, e, strict)

    def probe_sync(self, pid: str, *, max_age: float = PROBE_TTL) -> ProviderState:
        """`probe` for worker threads (embeddings, the reranker)."""
        if (st := self._fresh(pid, max_age)) is not None:
            return st
        req = self._probe_request(pid)
        if req is None:
            return ProviderState()
        url, headers = req
        try:
            with httpx.Client(timeout=PROBE_TIMEOUT, transport=self.sync_transport) as c:
                r = c.get(url, headers=headers)
                loaded, starting, error = self._parse(pid, r.status_code, _json(r),
                                                      r.content)
                if (props := self._props(pid, url, loaded)) is not None:
                    got = {}
                    for m, u in props.items():
                        try:                # a window is extra: its answer never fails the probe
                            got[m] = _n_ctx(_json(c.get(u, headers=headers)))
                        except httpx.HTTPError:
                            continue
                    self._windows(pid, {m: n for m, n in got.items() if n})
                self._ps_windows(pid, r.status_code, _json(r))
            return self._apply(pid, loaded, starting, error, fresh=max_age == 0)
        except httpx.HTTPError as e:
            return self._failed(pid, e)

    def _props(self, pid: str, url: str, loaded: frozenset | None) -> dict[str, str] | None:
        """{model: its /props url} for each model llama-swap lists as loaded at `url`
        (its /running); None for another kind of provider, or no answer."""
        if not self._swap(pid) or loaded is None:
            return None
        return {m: f"{url[:-len('/running')]}/upstream/{m}/props" for m in loaded}

    def _ps_windows(self, pid: str, status: int, data: Any) -> None:
        """The context each model Ollama has loaded runs with (/api/ps
        `context_length`), the same way as a llama-swap model's /props."""
        if (self.provider(pid) or {}).get("kind") != "ollama" or status >= 400:
            return
        self._windows(pid, {str(m.get("name") or m.get("model")): m["context_length"]
                            for m in (data or {}).get("models") or []
                            if isinstance(m, dict) and isinstance(m.get("context_length"), int)
                            and m["context_length"] > 0})

    def _swap(self, pid: str) -> bool:
        return (self.provider(pid) or {}).get("kind") in SWAPS

    def _windows(self, pid: str, windows: dict[str, int]) -> None:
        """The context each loaded model runs with: a llama-swap model's /props, asked
        only for a model /running lists as loaded (/props of an unloaded one would load
        it), Ollama's /api/ps. A --fit load sizes its context by the VRAM free at the
        time, so only the server itself can say (turns/tokens.py)."""
        from ..turns import tokens
        tokens.running_windows(str((self.provider(pid) or {}).get("base_url", "")).rstrip("/"),
                               windows)

    def _down(self, pid: str) -> bool:
        with self._lock:
            st = self._providers.get(pid)
        return st is not None and st.reachable is False

    def _ask(self, pid: str, chain: list[str]) -> bool:
        """Whether a decision asks the server first: only when it was never asked (a
        script, a test: no check_all), or what the app knows says it fails (it is down,
        or a model of `chain` on it could not load) and that is RETRY_DOWN old. A server
        seen up is never asked: its requests say how it is."""
        st = self.provider_state(pid)
        if st.reachable is None:
            return True
        failing = st.reachable is False or any(
            (ms := self.model_state(r)) is not None and ms.kind == "could_not_load"
            for r in chain if self.s.split(r)[0] == pid)
        return failing and time.monotonic() - st.checked >= RETRY_DOWN

    async def _look(self, pid: str, chain: list[str]) -> None:
        if self._ask(pid, chain):
            await self.probe(pid)

    def _look_sync(self, pid: str, chain: list[str]) -> None:
        if self._ask(pid, chain):
            self.probe_sync(pid)

    # ---- asking again, and the pause --------------------------------------------------

    def on_reachable(self, fn: Callable[[str], None]) -> None:
        """`fn(provider)` on the server's loop whenever a provider comes back up."""
        self._on_reachable.append(fn)

    def _came_back(self, pid: str) -> None:
        """A provider answers (again): its listeners hear it, and its model list is
        read once (`discover`)."""
        for fn in self._on_reachable:
            self._on_loop(lambda fn=fn: fn(pid))
        self._on_loop(lambda: self._discovery(pid))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _discovery(self, pid: str) -> asyncio.Task:
        """The one read of `pid`'s model list in flight, started when none is."""
        task = self._discovering.get(pid)
        if task is None or task.done():
            task = self._discovering[pid] = self._spawn(self._discover(pid))
        return task

    def all_down(self, roles) -> bool:
        """Every model of `roles` (with its key) is on a provider seen down; False when
        the roles have no such model."""
        s = self.s
        if s is None:
            return False
        pids = {s.split(r)[0] for role in roles for r in self.chain(role)}
        pids = {p for p in pids if not s.key_missing(p)}
        return bool(pids) and all(self._down(p) for p in pids)

    def answering(self) -> set[str]:
        """Every model (its ref) with its key whose provider is not seen down: what a
        turn may still run on while the agent is paused (turns/dispatcher.py)."""
        s = self.s
        if s is None:
            return set()
        return {f"{pid}/{name}" for pid, p in s.providers.items()
                if not s.key_missing(pid) and not self._down(pid)
                for name in (p.get("models") or {})}

    async def check_all(self) -> None:
        """Every provider with an address and its key asked once, side by side: at
        startup (__main__). One that answers has its model list read (`_came_back`)."""
        s = self.s
        pids = [pid for pid, p in (s.providers.items() if s is not None else ())
                if p.get("base_url") and not s.key_missing(pid)]

        async def one(pid: str) -> None:
            try:
                await self.probe(pid, max_age=0)
            except Exception:
                log.debug("check failed", exc_info=True)
        async with anyio.create_task_group() as tg:
            for pid in pids:
                tg.start_soon(one, pid)
        self._pause_check()

    async def revisit(self, roles) -> None:
        """Waiting work asks again (a held turn, a claimed work item): each server of
        `roles`' models that `_ask` allows, once. The one re-ask policy
        for a server known to be failing; when it answers, everything that waits on
        `wait_change` wakes."""
        if self.s is None:
            return
        chain = [r for role in roles for r in self.chain(role)]
        for pid in self._keyed(chain):
            await self._look(pid, chain)

    async def discover(self, pid: str) -> list[dict[str, Any]] | None:
        """The provider's own model list (settings/kinds.py), read now: Check provider,
        and once each time it comes back (`_came_back`); a read already in flight is
        shared. The context it states for each model is the window turns budget with
        (turns/tokens.py), over the number in Settings; not Ollama's (Settings is its
        num_ctx, the list says the model's most: /api/ps says what a loaded one runs
        with). The caps it states are the model's tools and vision (settings/kinds.py
        caps_of), over the saved ones. None when its kind has no list or it did not
        answer."""
        return await asyncio.shield(self._discovery(pid))   # one caller's Stop ends only its wait

    async def _discover(self, pid: str) -> list[dict[str, Any]] | None:
        from ..settings import kinds
        from ..settings.registry import provider_key
        from ..turns import tokens
        p = self.provider(pid)
        if p is None or not p.get("base_url") or \
                not (kinds.KINDS.get(p.get("kind")) or {}).get("lists"):
            return None
        try:
            found = await kinds.discover(p["kind"], p["base_url"],
                                         provider_key(pid, getattr(self.s, "secrets_root", None)),
                                         refresh=True, transport=self.transport)
        except (httpx.HTTPError, ValueError):
            return None
        mine = p.get("models") or {}
        twins = {m.get("vision_twin") for m in mine.values()} - {None}
        kinds.reported_outputs(p["base_url"], {m["id"]: m["output"] for m in found
                                                if m["id"] in mine and m.get("output")})
        windows = tokens.reported_windows(p["base_url"], {
            m["id"]: m["context"] for m in found
            if m["id"] in mine and m.get("context") and p["kind"] != "ollama"})
        caps = kinds.stated_caps(p["base_url"], {m["id"]: m["caps"] for m in found
                                                 if m["id"] in set(mine) | twins
                                                 and not m.get("guessed")})
        if windows or caps:
            self._changed()                 # Settings, Models shows the new facts
        return found

    def _pause_check(self) -> None:
        from .. import metrics
        paused = self.all_down(AGENT_ROLES)
        if paused == self.paused:
            return
        self.paused = paused
        metrics.PAUSED.set(1 if paused else 0)
        log.info("paused" if paused else "resumed",
                 extra={"event": "agent.paused" if paused else "agent.resumed"})
        if self.publish is not None:
            event = {"type": AGENT_HOLD, "hold": self.hold()}
            self._on_loop(lambda: self.publish(event))
        self._changed()

    def _meter(self, pid: str, up: bool) -> None:
        from .. import metrics
        metrics.HOST_UP.labels(pid).set(1 if up else 0)
        self._metered.add(pid)

    # ---- state changes --------------------------------------------------------------

    def _apply(self, pid: str, loaded, starting, error: str | None,
               *, fresh: bool = False) -> ProviderState:
        if error:
            return self._set_unreachable(pid, error)
        changed = False
        with self._lock:
            old = self._providers.get(pid) or ProviderState()
            back = old.reachable is not True
            st = ProviderState(reachable=True, error=None, loaded=loaded, starting=starting,
                               checked=time.monotonic())
            self._providers[pid] = st
            changed = back or old.loaded != loaded or old.starting != starting
            cleared = []
            for ref in [r for r in self._models if r.partition("/")[0] == pid]:
                # a server that came back (maybe restarted), or a probe asked fresh
                # (Check provider): its models get another chance to load; otherwise
                # only the failures whose cause is gone
                if back or fresh or _cause_gone(self._models[ref], ref.partition("/")[2],
                                                loaded):
                    cleared.append(ref)
                    del self._models[ref]
        url = str((self.provider(pid) or {}).get("base_url", ""))
        if (back or fresh) and url:
            from ..kb.embed import forget_fingerprints
            forget_fingerprints(url)
        if back and old.reachable is False:
            log.info("provider reachable", extra={"event": "router.reachable",
                                                  "provider": pid})
        if back:
            self._meter(pid, True)
            self._came_back(pid)
            self._pause_check()
        for ref in cleared:
            log.info("model state cleared", extra={"event": "router.model_ok", "ref": ref})
        if changed or cleared:
            self._changed()
        if old.loaded != loaded:
            self._loaded_changed()
        return st

    def _failed(self, pid: str, e: Exception, strict: bool = False) -> ProviderState:
        """A probe that got no answer. A refused connection is down at once. A timeout
        keeps a provider last seen up as up: a busy server (a model loading, one stream
        on its only slot) lists its models late, and its requests say whether it
        answers. A `strict` probe (a request with no event for FIRST_EVENT) counts a
        timeout as down."""
        if isinstance(e, httpx.TimeoutException) and not strict:
            with self._lock:
                old = self._providers.get(pid)
                if old is not None and old.reachable:
                    old.checked = time.monotonic()
                    return old
        return self._set_unreachable(pid, self._unreachable_sentence(pid, e))

    def _set_unreachable(self, pid: str, sentence: str) -> ProviderState:
        with self._lock:
            old = self._providers.get(pid) or ProviderState()
            flip = old.reachable is not False
            st = ProviderState(reachable=False, error=sentence, loaded=None,
                               checked=time.monotonic())
            self._providers[pid] = st
        if flip:
            from .. import metrics
            self._meter(pid, False)
            if old.reachable:               # a real flip from up, not a first look
                metrics.HOST_DOWN.labels(pid).inc()
            log.info("provider unreachable", extra={"event": "router.unreachable",
                                                    "provider": pid, "error": sentence})
            self._pause_check()
        if flip or old.error != sentence:
            self._changed()
        if old.loaded is not None:
            self._loaded_changed()
        return st

    def note_unreachable(self, pid: str, sentence: str) -> None:
        self._set_unreachable(pid, sentence)

    def note_ok(self, ref: str, swapped: bool = False) -> None:
        """`ref` answered its first token. `swapped`: the server had not listed it as
        loaded, so it loaded it now and unloaded others: one probe learns what is
        loaded."""
        pid = ref.partition("/")[0]
        with self._lock:
            had = self._models.pop(ref, None)
            st = self._providers.get(pid)
            flip = st is None or st.reachable is not True
            if flip:
                self._providers[pid] = ProviderState(
                    reachable=True, loaded=st.loaded if st else None,
                    starting=st.starting if st else frozenset(), checked=0.0)
        if had is not None:
            log.info("model works again", extra={"event": "router.model_ok", "ref": ref})
        if flip:
            self._meter(pid, True)
            self._came_back(pid)
            self._pause_check()
        if swapped:
            self._on_loop(lambda: self._spawn(self.probe(pid, max_age=0)))
        if had is not None or flip:
            self._changed()

    def note_failed(self, ref: str, kind: str, message: str,
                    loaded_then: frozenset[str] | None = None) -> None:
        m = ModelState(kind=kind, message=message, at=now_ms(),
                       loaded_then=loaded_then, sig=self._sig(ref))
        with self._lock:
            old = self._models.get(ref)
            self._models[ref] = m
        if old is None or (old.kind, old.message) != (m.kind, m.message):
            log.warning("model failed", extra={"event": "router.model_failed", "ref": ref,
                                               "kind": kind, "error": m.message})
        self._changed()

    # ---- requests per hour -----------------------------------------------------------

    def per_hour(self, pid: str) -> int:
        """The provider's Requests per hour (Settings, Models); 0 is no limit."""
        return int(((self.provider(pid) or {}).get("options") or {}).get("max_per_hour", 0))

    def take(self, pid: str) -> float:
        """0 when one more request to provider `pid` fits its Requests per hour, and it is
        counted now; else the seconds until one fits. The request that uses the hour up
        publishes `models.state`, and so does the moment the hour has room again. Any
        thread."""
        cap, now = self.per_hour(pid), self.now()
        with self._lock:
            sent = self.sent.setdefault(pid, deque())
            secs = hour_room(sent, cap, now)
            room = sent[-cap] + HOUR - now if cap and not secs and len(sent) >= cap else 0
        if room:
            self._changed()
            self._on_loop(lambda: self._loop.call_later(room, self._changed))
        return secs

    def moves_on(self, pid: str) -> bool:
        """The provider's Next model at the hourly limit: past its hour, work moves to the next
        model in its list (True) or waits for it (False)."""
        return bool(((self.provider(pid) or {}).get("options") or {}).get("next_when_full", True))

    def full(self, pid: str) -> str | None:
        """The sentence while provider `pid` used its Requests per hour, else None."""
        cap, now = self.per_hour(pid), self.now()
        if not cap:
            return None
        with self._lock:
            n = sum(1 for t in self.sent.get(pid, ()) if now - t < HOUR)
        return reached((self.provider(pid) or {}).get("label", pid), cap) if n >= cap \
            else None

    # ---- choosing ---------------------------------------------------------------------

    def _check(self, ref: str, st: ProviderState) -> str | None:
        """None when `ref` may be tried now, else the sentence why not: a missing key,
        the server's words for a load that failed, the provider's own error."""
        s = self.s
        pid, name = s.split(ref)
        p = s.providers.get(pid) or {}
        m = s.model(ref) or {}
        label = m.get("label") or name
        if s.key_missing(pid):
            return f"{p.get('label', pid)} has no API key. Add it in Settings, Models."
        ms = self.model_state(ref)
        if ms is not None and ms.kind == "could_not_load" and \
                not _cause_gone(ms, name, st.loaded):
            return f"{label} could not load. {ms.message}"
        if st.reachable is False:
            return st.error or f"{p.get('label', pid)} does not answer."
        return None

    def problem(self, role: str | None, ref: str) -> str | None:
        """The one sentence why `ref` (for `role`, when given) does not answer now, as
        the app last learned it: a capability it lacks, why it is left out, its provider's
        used-up hour, else the words its last failed answer carried (busy, an error);
        None when nothing is known against it. Settings, Models and the chooser show it
        (settings/views.py)."""
        if self.s is None or self.s.model(ref) is None:
            return None
        pid = self.s.split(ref)[0]
        why = (self._lacks(role, ref) if role else None) or \
            self._check(ref, self.provider_state(pid)) or self.full(pid)
        if why:
            return why
        ms = self.model_state(ref)
        return ms.message if ms is not None else None

    def chain(self, role: str, conversation_id: str | None = None) -> list[str]:
        s = self.s
        if s is None:
            return []
        return [r for r in s.task_chain(role, conversation_id)
                if s.model(r) is not None]

    async def order(self, role: str, *, conversation_id: str | None = None,
                    fallback: bool = True) -> Ordered:
        """The task's models in the order to try now (the list above). `fallback` False:
        only the chain's first model, for a call that runs on exactly it."""
        chain = self.chain(role, conversation_id)
        if not fallback:
            chain = chain[:1]
        for pid in self._keyed(chain):
            await self._look(pid, chain)
        return self._ordered(role, chain)

    def _keyed(self, chain: list[str]) -> list[str]:
        """The providers of `chain`, once each, that have their key."""
        return [pid for pid in dict.fromkeys(self.s.split(r)[0] for r in chain)
                if not self.s.key_missing(pid)]

    def _ordered(self, role: str, chain: list[str]) -> Ordered:
        out, full = Ordered(role, [], chain), []
        for ref in chain:
            pid = self.s.split(ref)[0]
            why = self._lacks(role, ref) or self._check(ref, self.provider_state(pid))
            if why:
                out.skipped.append((ref, why))
            elif (hour := self.full(pid)) and self.moves_on(pid):
                full.append((ref, hour))
            else:
                out.refs.append(ref)
        if out.refs:                # a used-up hour is passed over while another model can answer
            out.skipped += full
        else:
            out.refs = [ref for ref, _ in full]
        return out

    def _lacks(self, role: str, ref: str) -> str | None:
        """The sentence why `ref` cannot do `role` now: a capability the role needs that
        its server says it has not (registry.lacks), None when it has them all."""
        from ..settings.registry import ROLES, lacks
        cap = lacks(self.s, role, ref)
        if cap is None:
            return None
        label = (self.s.model(ref) or {}).get("label") or self.s.split(ref)[1]
        return f"{label} cannot do {ROLES[role]['label']}: it has no '{cap}' capability."

    async def can_answer(self, role: str, *, probe: bool = True) -> str | None:
        """None when the first model of `role` can answer now, else the sentence why
        not (its provider's used-up hour too). Work that waits for it (a scheduled run)
        runs on that model alone. `probe`:
        a server known to be failing is asked again as `revisit` asks it (`_ask`), so a
        waiting run starts once it is back; False reads the state as it stands."""
        chain = self.chain(role)
        if not chain:
            return Ordered(role, [], chain).sentence()
        ref = chain[0]
        pid = self.s.split(ref)[0]
        if self.s.key_missing(pid):
            st = ProviderState()
        else:
            if probe:
                await self._look(pid, chain[:1])
            st = self.provider_state(pid)
        return self._lacks(role, ref) or self._check(ref, st) or self.full(pid)

    def order_sync(self, role: str) -> Ordered:
        chain = self.chain(role)
        for pid in self._keyed(chain):
            self._look_sync(pid, chain)
        return self._ordered(role, chain)

    def backend_config(self, ref: str):
        from ..settings.registry import backend_config
        return backend_config(self.s, ref)


def _n_ctx(props) -> int | None:
    """n_ctx of one slot from llama-server's /props."""
    try:
        n = (props or {}).get("default_generation_settings", {}).get("n_ctx")
        return int(n) if n and int(n) > 0 else None
    except (AttributeError, TypeError, ValueError):
        return None


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return None


_standalone: dict[int, Router] = {}


def app_router(cfg, publish: Callable[[dict], None] | None = None) -> Router:
    """The one router for this config. Schedulers start before the app's LLM exists and
    wait on it; the LLM adopts it, so a settings change wakes them too."""
    r = _standalone.get(id(cfg))
    if r is None or r.cfg is not cfg:
        r = Router(cfg, publish)
        _standalone[id(cfg)] = r
    elif publish is not None:
        r.publish = publish
    return r

