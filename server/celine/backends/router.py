"""What the app knows about its model servers, and which model does a task now.

Everything here comes from the servers' own APIs, nothing from guesses or timers.

- **Provider state**: reachable (up) or unreachable (down, with the last error
  sentence), `since` when it last changed, and `loaded`: the models loaded now
  (llama-swap `/running`, Ollama `/api/ps`, llama-server `/models`; None when the
  server does not say, as OpenRouter and ComfyUI (`/system_stats`) do not). Refreshed
  by every request outcome, by a probe (a GET, cached a few seconds) whenever a
  decision needs it, and by the host ping. "Check again" probes uncached.
- **Host ping** (`watch`, in the app): every provider is probed every PING_EVERY
  seconds with a PROBE_TIMEOUT timeout, so a call never waits for a host that is down
  to time out, and a host that comes back is seen within PING_EVERY. The state is in
  GET /api/status (`hosts`), in `models.state` and in the metrics
  (celine_host_up).
- **Paused**: while the ping sees every model of the chat and tasks roles on a host
  that is down, it is paused (`paused_since`, event `her.hold`, celine_paused): none
  of her turns is claimed (hold.py) until a host is back; a sub-agent's or a drone's
  whose own model answers still is (`answering`).
- **Model state**: ok, or failed with `kind` could_not_load (the server could not
  start it) or error (it answered with an error), a sentence, when, and `loaded_then`:
  what the server had loaded when the load was asked for.

`order(task)` gives the task's models in the order to try:
1. chat: the conversation's own model first, then the chat task's list;
2. a model whose provider needs a key that is not saved is left out;
3. a model whose provider is down is left out, as one that failed: no call waits for
   its timeout (a call that finds it down marks it, the ping brings it back);
4. a model that could not be loaded is left out while everything that was loaded
   then is still loaded (nothing was freed, so the load would fail the same way).
   When nothing was loaded then, the memory is held outside the server (an image
   being made, a game) and the server cannot say when it is free: the model is tried
   again after `RETRY_OUTSIDE_MS`.
   A failure is the model's state only while it is current: every probe forgets a
   could-not-load failure once its cause is gone (the model is loaded now, or what
   was loaded then was freed), a later answer from the model forgets any failure,
   and "Check again" forgets them all. So a failure from hours ago never shows as
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
from dataclasses import dataclass, field
from typing import Any, Callable

import anyio
import httpx

from ..core.ids import now_ms
from ..settings.kinds import SWAPS
from .base import BackendError
from ..core.event_types import HER_HOLD, MODELS_STATE, MODEL_LOADED

log = logging.getLogger("celine.router")

PROBE_TTL = 3.0          # seconds a probe result answers for
PROBE_TIMEOUT = 2.0
SLOW_PINGS = 3              # timeouts in a row before an up host counts as down


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
    since: int = 0                         # ms: when `reachable` last changed

    def public(self) -> dict[str, Any]:
        return {"reachable": self.reachable,
                "error": self.error, "since": self.since,
                "loaded": sorted(self.loaded) if self.loaded is not None else None,
                "starting": sorted(self.starting)}


@dataclass
class ModelState:
    kind: str                              # could_not_load | error
    message: str
    at: int
    loaded_then: frozenset[str] | None = None
    sig: str = ""                          # the model's settings when it failed

    def public(self) -> dict[str, Any]:
        return {"state": "failed", "kind": self.kind, "message": self.message, "at": self.at}


@dataclass
class Ordered:
    """A task's models to try now, and a sentence for each one left out."""
    role: str
    refs: list[str]
    chain: list[str]
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def head(self) -> "Ordered":
        """Only the first model of the chain (a call that runs on exactly it)."""
        first = self.chain[:1]
        return Ordered(self.role, [r for r in self.refs if r in first], first,
                       [(r, why) for r, why in self.skipped if r in first])

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
PING_EVERY = 10.0           # seconds between two pings of every host
HER_ROLES = ("chat", "tasks")   # no host of these answers: it pauses


def _cause_gone(ms: ModelState, name: str, loaded: frozenset[str] | None) -> bool:
    """A could-not-load failure no longer holds: the model is loaded now, or the
    server freed something that was loaded when the load failed. When nothing of ours
    was loaded and the load still failed, the memory is held outside this server (an
    image being made, a game); that can change without the server saying so, so the
    model gets another try after a while instead of never."""
    if ms.kind != "could_not_load" or loaded is None:
        return False
    if name in loaded or ms.loaded_then is None:
        return name in loaded
    if ms.loaded_then:
        return not loaded >= ms.loaded_then
    return now_ms() - ms.at >= RETRY_OUTSIDE_MS


def _hm(ms: int) -> str:
    return time.strftime("%H:%M", time.localtime(ms / 1000)) if ms else ""


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
        self._pinging = False                  # watch() runs: the ping owns down hosts
        self._slow: dict[str, int] = {}        # probe timeouts in a row per provider
        self.paused_since: int | None = None   # ms; None: not paused
        self._on_reachable: list[Callable[[str], None]] = []
        self._metered: set[str] = set()        # providers with a celine_host_up label
        self._discovered: dict[str, float] = {}  # provider -> monotonic time of its last list

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
        `loaded` (it, or its vision twin, is loaded now) and `load_seconds` (its last
        load, measured; null before one). A new chat never starts on another model for
        it: the chooser shows it."""
        s = self.s
        models: list[dict[str, Any]] = []
        for pid, p in (s.providers.items() if s is not None else ()):
            now = self.provider_state(pid).loaded
            if now is None:
                continue
            for name, m in (p.get("models") or {}).items():
                ref = f"{pid}/{name}"
                models.append({"ref": ref, "loaded": name in now or m.get("vision_twin") in now,
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
        """Called after a reload: a waiting turn looks at the new task lists."""
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

    def hosts(self) -> dict[str, dict[str, Any]]:
        """{provider: {state: up | down | None (not asked yet), since}}, for the config
        status."""
        s = self.s
        out: dict[str, dict[str, Any]] = {}
        for pid in (s.providers if s is not None else {}):
            st = self.provider_state(pid)
            out[pid] = {"state": {True: "up", False: "down"}.get(st.reachable),
                        "since": st.since or None}
        return out

    def hold(self) -> dict[str, Any] | None:
        """{state: "paused", since} while it is paused, else None."""
        return None if self.paused_since is None else \
            {"state": "paused", "since": self.paused_since}

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

    def _parse(self, pid: str, status: int, data: Any) -> tuple[frozenset | None,
                                                                 frozenset, str | None]:
        """(loaded, starting, error) from a probe answer. Any answer means the host is
        up; only a rejected key is an error. Down is a refused connection or a timeout."""
        p = self.provider(pid) or {}
        if status in (401, 403):
            return None, frozenset(), f"{p.get('label', pid)} rejected the API key."
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

    async def probe(self, pid: str, *, max_age: float = PROBE_TTL) -> ProviderState:
        """The provider's state, asked again when older than `max_age` seconds
        (0: always, which is "Check again")."""
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
                loaded, starting, error = self._parse(pid, r.status_code, _json(r))
                if (props := self._props(pid, url, loaded)) is not None:
                    self._windows(pid, {m: n for m, u in props.items() if (
                        n := _n_ctx(_json(await c.get(u, headers=headers))))})
                self._ps_windows(pid, r.status_code, _json(r))
            return self._apply(pid, loaded, starting, error, fresh=max_age == 0)
        except httpx.HTTPError as e:
            return self._failed(pid, e)

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
                loaded, starting, error = self._parse(pid, r.status_code, _json(r))
                if (props := self._props(pid, url, loaded)) is not None:
                    self._windows(pid, {m: n for m, u in props.items() if (
                        n := _n_ctx(_json(c.get(u, headers=headers))))})
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

    def _ask(self, pid: str) -> bool:
        """Probe before a decision: what a running server has loaded. One last seen down
        is the ping's to bring back (no decision waits for its timeout); without the
        ping (a script, a test) it is asked again once its state is PING_EVERY old."""
        if not self._down(pid):
            return True
        return not self._pinging and \
            time.monotonic() - self.provider_state(pid).checked >= PING_EVERY

    async def _look(self, pid: str) -> None:
        if self._ask(pid):
            await self.probe(pid)

    def _look_sync(self, pid: str) -> None:
        if self._ask(pid):
            self.probe_sync(pid)

    # ---- the host ping and the pause ---------------------------------------------------

    def on_reachable(self, fn: Callable[[str], None]) -> None:
        """`fn(provider)` on the server's loop whenever a provider comes back up."""
        self._on_reachable.append(fn)

    def _came_back(self, pid: str) -> None:
        for fn in self._on_reachable:
            self._on_loop(lambda fn=fn: fn(pid))

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
        turn may still run on while she is paused (turns/dispatcher.py)."""
        s = self.s
        if s is None:
            return set()
        return {f"{pid}/{name}" for pid, p in s.providers.items()
                if not s.key_missing(pid) and not self._down(pid)
                for name in (p.get("models") or {})}

    async def ping_once(self) -> None:
        """Every provider probed once, side by side; then the pause looked at."""
        from .. import metrics
        s = self.s
        pids = [pid for pid, p in (s.providers.items() if s is not None else ())
                if p.get("base_url") and not s.key_missing(pid)]
        async with anyio.create_task_group() as tg:
            for pid in pids:
                tg.start_soon(self._ping, pid)
        for pid in pids:
            metrics.HOST_UP.labels(pid).set(1 if self.provider_state(pid).reachable else 0)
        for pid in self._metered - set(pids):      # left Settings (or lost its key)
            metrics.HOST_UP.remove(pid)
        self._metered = set(pids)
        self._pause_check()

    async def _ping(self, pid: str) -> None:
        try:
            st = await self.probe(pid, max_age=PING_EVERY / 2)   # a probe just now is as good
            if st.reachable:
                await self.discover(pid)
        except Exception:
            log.debug("ping failed", exc_info=True)

    async def discover(self, pid: str, *, force: bool = False) -> list[dict[str, Any]] | None:
        """The provider's own model list (settings/kinds.py), at most every
        `discover_every` seconds of its kind (settings/kinds.py) unless forced ("Check
        provider"). The context it states for each model is the window turns budget
        with (turns/tokens.py), over the number in Settings; not Ollama's (Settings is
        its num_ctx, the list says the model's most: /api/ps says what a loaded one
        runs with). The caps it states are the model's tools and vision
        (settings/kinds.py caps_of), over the saved ones. None when not asked or it did
        not answer."""
        from ..settings import kinds
        from ..settings.registry import provider_key
        from ..turns import tokens
        p = self.provider(pid)
        every = (kinds.KINDS.get((p or {}).get("kind")) or {}).get("discover_every")
        if p is None or every is None or not p.get("base_url"):
            return None
        if not force and time.monotonic() - self._discovered.get(pid, -every) < every:
            return None
        self._discovered[pid] = time.monotonic()
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

    async def watch(self) -> None:
        """The host ping, for the app's life (__main__)."""
        self._pinging = True
        try:
            while True:
                await self.ping_once()
                await anyio.sleep(PING_EVERY)
        finally:
            self._pinging = False

    def _pause_check(self) -> None:
        from .. import metrics
        paused = self._pinging and self.all_down(HER_ROLES)
        if paused == (self.paused_since is not None):
            return
        self.paused_since = now_ms() if paused else None
        metrics.PAUSED.set(1 if paused else 0)
        log.info("paused" if paused else "resumed",
                 extra={"event": "her.paused" if paused else "her.resumed"})
        if self.publish is not None:
            event = {"type": HER_HOLD, "hold": self.hold()}
            self._on_loop(lambda: self.publish(event))
        self._changed()

    # ---- state changes --------------------------------------------------------------

    def _apply(self, pid: str, loaded, starting, error: str | None,
               *, fresh: bool = False) -> ProviderState:
        if error:
            return self._set_unreachable(pid, error)
        changed = False
        with self._lock:
            self._slow.pop(pid, None)
            old = self._providers.get(pid) or ProviderState()
            back = old.reachable is not True
            st = ProviderState(reachable=True, error=None, loaded=loaded, starting=starting,
                               checked=time.monotonic(),
                               since=now_ms() if back else old.since)
            self._providers[pid] = st
            changed = back or old.loaded != loaded or old.starting != starting
            cleared = []
            for ref in [r for r in self._models if r.partition("/")[0] == pid]:
                # a server that came back (maybe restarted), or "Check again": its
                # models get another chance to load; otherwise only the failures
                # whose cause is gone
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
            self._came_back(pid)
        for ref in cleared:
            log.info("model state cleared", extra={"event": "router.model_ok", "ref": ref})
        if changed or cleared:
            self._changed()
        if old.loaded != loaded:
            self._loaded_changed()
        return st

    def _failed(self, pid: str, e: Exception) -> ProviderState:
        """A probe that got no answer. A refused connection is down at once; a busy host
        (a model loading, one stream on its only slot) can be slow to list its models,
        so an up host counts as down only after SLOW_PINGS timeouts in a row."""
        with self._lock:
            old = self._providers.get(pid)
            slow = self._slow.get(pid, 0) + 1 if isinstance(e, httpx.TimeoutException) else 0
            self._slow[pid] = slow
            if old is not None and old.reachable and 0 < slow < SLOW_PINGS:
                old.checked = time.monotonic()
                return old
        return self._set_unreachable(pid, self._unreachable_sentence(pid, e))

    def _set_unreachable(self, pid: str, sentence: str) -> ProviderState:
        with self._lock:
            old = self._providers.get(pid) or ProviderState()
            flip = old.reachable is not False
            st = ProviderState(reachable=False, error=sentence, loaded=None,
                               checked=time.monotonic(),
                               since=now_ms() if flip else old.since)
            self._providers[pid] = st
        if flip or old.error != sentence:
            if flip:
                log.info("provider unreachable", extra={"event": "router.unreachable",
                                                        "provider": pid,
                                                        "error": sentence})
            self._changed()
        if old.loaded is not None:
            self._loaded_changed()
        return st

    def note_unreachable(self, pid: str, sentence: str) -> None:
        self._set_unreachable(pid, sentence)

    def note_ok(self, ref: str) -> None:
        pid = ref.partition("/")[0]
        with self._lock:
            had = self._models.pop(ref, None)
            st = self._providers.get(pid)
            flip = st is None or st.reachable is not True
            if flip:
                self._providers[pid] = ProviderState(
                    reachable=True, loaded=st.loaded if st else None,
                    starting=st.starting if st else frozenset(), checked=0.0,
                    since=now_ms())
        if had is not None:
            log.info("model works again", extra={"event": "router.model_ok", "ref": ref})
        if flip:
            self._came_back(pid)
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

    # ---- choosing ---------------------------------------------------------------------

    def _check(self, ref: str, st: ProviderState) -> str | None:
        """None when `ref` may be tried now, else the sentence why not."""
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
            return f"{label} could not load at {_hm(ms.at)}."
        if st.reachable is False:
            return f"{p.get('label', pid)} does not answer."
        return None

    def chain(self, role: str, conversation_id: str | None = None) -> list[str]:
        s = self.s
        if s is None:
            return []
        return [r for r in s.task_chain(role, conversation_id)
                if s.model(r) is not None]

    async def order(self, role: str, *, conversation_id: str | None = None) -> Ordered:
        chain = self.chain(role, conversation_id)
        for pid in self._keyed(chain):
            await self._look(pid)
        return self._ordered(role, chain)

    def _keyed(self, chain: list[str]) -> list[str]:
        """The providers of `chain`, once each, that have their key."""
        return [pid for pid in dict.fromkeys(self.s.split(r)[0] for r in chain)
                if not self.s.key_missing(pid)]

    def _ordered(self, role: str, chain: list[str]) -> Ordered:
        out = Ordered(role, [], chain)
        for ref in chain:
            why = self._lacks(role, ref) or self._check(ref, self.provider_state(self.s.split(ref)[0]))
            (out.skipped.append((ref, why)) if why else out.refs.append(ref))
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

    async def can_answer(self, role: str) -> str | None:
        """None when a model of `role` can answer now, else the sentence why not, a
        provider named by its label only (its address and error are in the log when it
        goes down). Unlike `order`, a provider last seen down is asked again (a probe,
        cached PROBE_TTL), so work that waits for it (a scheduled run) starts once it is
        back."""
        chain = self.chain(role)
        if not chain:
            return Ordered(role, [], chain).sentence()
        why: list[str] = []
        for ref in chain:
            pid = self.s.split(ref)[0]
            st = ProviderState() if self.s.key_missing(pid) else await self.probe(pid)
            if (no := self._check(ref, st)) is None:
                return None
            why.append(no)
        return " ".join(dict.fromkeys(why))

    def order_sync(self, role: str) -> Ordered:
        chain = self.chain(role)
        for pid in self._keyed(chain):
            self._look_sync(pid)
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

