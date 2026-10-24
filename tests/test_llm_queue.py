"""The LLM request queue (backends/queue.py) and routing (backends/llm.py), with fakes:
the user's request never times out while it waits and a wait names who holds the model."""
from __future__ import annotations

from chat_fixtures import until, user_says

import copy
from dataclasses import dataclass, field
from types import SimpleNamespace

import anyio
import pytest

from celine.conversations import store as conversations, view
from celine.backends.base import BackendError, Done, TextDelta
from fake_backend import FakeBackend
from celine.backends.llm import LLM
from celine.backends.queue import Priority, QueueCancelled, QueueTimeout, Scheduler, Ticket
from celine.boot import load
from celine.core.bus import EventBus
from celine.turns.cancel import Token
from celine.turns import store as turns

pytestmark = pytest.mark.anyio

I, A, B = Priority.INTERACTIVE, Priority.AGENT, Priority.BACKGROUND
settle = anyio.wait_all_tasks_blocked     # every other task waits: the queue did its part


@dataclass
class StaticLimits:
    default_slots: int = 4
    exclusive_endpoints: set[str] = field(default_factory=set)

    def slots(self, endpoint, model):
        return self.default_slots

    def endpoint_max(self, endpoint):
        return 0

    def exclusive(self, endpoint):
        return endpoint in self.exclusive_endpoints


def tk(prio=I, lane="c1", model="m", endpoint="e"):
    return Ticket(endpoint=endpoint, model=model, priority=prio, lane=lane)


async def hold(s: Scheduler, t: Ticket) -> Ticket:
    return await s.acquire(t, timeout=1)


# ---- scheduler -----------------------------------------------------------------------

async def test_priority_order_interactive_agent_background():
    s = Scheduler(StaticLimits(default_slots=1))
    first = await hold(s, tk(I, "x"))
    bg, ag, it = tk(B, "t"), tk(A, "a"), tk(I, "u")
    order: list[Ticket] = []

    async def one(t):
        await s.acquire(t, timeout=5)
        order.append(t)
    async with anyio.create_task_group() as tg:
        for t in (bg, ag, it):
            tg.start_soon(one, t)
            await settle()
        s.release(first)
        while len(order) < 3:
            await settle()
            if s.running:
                s.release(s.running[0])
    assert order == [it, ag, bg]


async def test_exclusive_endpoint_runs_one_model_at_a_time():
    s = Scheduler(StaticLimits(default_slots=4, exclusive_endpoints={"swap"}))
    a1 = await hold(s, tk(I, "x", model="A", endpoint="swap"))
    b = tk(I, "y", model="B", endpoint="swap")
    a2 = tk(B, "z", model="A", endpoint="swap")    # would fit A's slots, but B is first
    async with anyio.create_task_group() as tg:
        tg.start_soon(s.acquire, b)
        await settle()
        tg.start_soon(s.acquire, a2)
        await settle()
        assert not b.granted and not a2.granted
        s.release(a1)
        assert b.granted and not a2.granted
        s.release(b)
        assert a2.granted
        s.release(a2)


async def test_two_scenes_on_two_models_swap_after_two_of_one_not_every_request():
    """The model the endpoint holds goes first, two in a row at most, then the other:
    round-robin alone would swap on every request."""
    s = Scheduler(StaticLimits(default_slots=1, exclusive_endpoints={"swap"}))
    a0 = await hold(s, tk(I, "one", model="A", endpoint="swap"))
    waiting = [tk(I, "two", "B", "swap"), tk(I, "one", "A", "swap"),
               tk(I, "two", "B", "swap"), tk(I, "one", "A", "swap")]
    ran = [a0]

    async def one(t):
        await s.acquire(t, timeout=5)
        ran.append(t)
    async with anyio.create_task_group() as tg:
        for t in waiting:
            tg.start_soon(one, t)
            await settle()
        assert [t.model for t in s.order()] == ["A", "B", "A", "B"]
        while len(ran) < 5 or s.running:
            await settle()
            if s.running:
                s.release(s.running[0])
    assert [t.model for t in ran] == ["A", "A", "B", "B", "A"]


async def _run_all(s: Scheduler, first: Ticket, waiting: list[Ticket]) -> list[Ticket]:
    ran = [await hold(s, first)]

    async def one(t):
        await s.acquire(t, timeout=5)
        ran.append(t)
    async with anyio.create_task_group() as tg:
        for t in waiting:
            tg.start_soon(one, t)
            await settle()
        while len(ran) <= len(waiting) or s.running:
            await settle()
            if s.running:
                s.release(s.running[0])
    return ran


async def test_affinity_keeps_a_lane_in_its_order():
    s = Scheduler(StaticLimits(default_slots=1, exclusive_endpoints={"swap"}))
    waiting = [tk(I, "one", m, "swap") for m in "BABA"]
    ran = await _run_all(s, tk(I, "x", "A", "swap"), waiting)
    assert ran[1:] == waiting


async def test_text_goes_first_and_an_image_lease_holds_the_gpu_alone():
    """Image A holds the GPU: nothing starts while it runs. Image B waits behind every
    text request, whatever its priority; text that came while A ran goes before B, and
    B holds nothing while it waits."""
    s = Scheduler(StaticLimits(default_slots=4), settings=lambda: {"gpu_idle_seconds": 0})
    a = await hold(s, Ticket(endpoint="e", model="", priority=A, lane="la", exclusive=True))
    b = Ticket(endpoint="e", model="", priority=I, lane="lb", exclusive=True)
    chat, agent, title = tk(I, "you"), tk(A, "trigger"), tk(B, "titles")
    async with anyio.create_task_group() as tg:
        for t in (b, chat, agent, title):
            tg.start_soon(s.acquire, t)
            await settle()
        assert not any(t.granted for t in (b, chat, agent, title))
        s.release(a)
        assert chat.granted and agent.granted and title.granted and not b.granted
        late = tk(B, "late")
        tg.start_soon(s.acquire, late)
        await settle()
        assert late.granted, "a waiting lease holds nothing"
        for t in (chat, agent, title):
            s.release(t)
        assert not b.granted, "text still runs"
        s.release(late)
        assert b.granted
        s.release(b)


async def test_a_lease_waits_for_the_gpu_to_go_idle_then_loses_patience(clock, monkeypatch):
    from celine.backends import queue
    monkeypatch.setattr(queue, "time", SimpleNamespace(monotonic=clock))
    gpu = {"gpu_idle_seconds": 0.3, "gpu_patience_seconds": 0.5}
    s = Scheduler(StaticLimits(default_slots=4), settings=lambda: gpu)
    text = await hold(s, tk(I, "you"))
    s.release(text)
    img = Ticket(endpoint="e", model="", priority=A, lane="l1", exclusive=True, created=clock())
    async with anyio.create_task_group() as tg:
        tg.start_soon(s.acquire, img)
        await settle()
        clock.advance(0.1)
        s._dispatch()               # what the lease's own look every second does
        assert not img.granted, "text ended a moment ago: idle gate"
        clock.advance(0.3)
        s._dispatch()
        assert img.granted, "idle long enough"
        s.release(img)
    # past its patience a lease holds new text and goes next
    busy = await hold(s, tk(I, "you"))
    img2 = Ticket(endpoint="e", model="", priority=A, lane="l2", exclusive=True, created=clock())
    async with anyio.create_task_group() as tg:
        tg.start_soon(s.acquire, img2)
        await settle()
        clock.advance(0.6)
        more = tk(I, "more")
        tg.start_soon(s.acquire, more)
        await settle()
        assert not more.granted, "held: the lease waited past its patience"
        s.release(busy)
        assert img2.granted and not more.granted
        s.release(img2)
        assert more.granted
        s.release(more)


async def test_a_stopped_or_timed_out_wait_leaves_the_queue():
    s = Scheduler(StaticLimits(default_slots=1))
    first = await hold(s, tk(I, "x"))
    tok, t = Token("t1"), tk(I, "u")

    async def wait():
        with pytest.raises(QueueCancelled):
            await s.acquire(t, token=tok, timeout=5)
    async with anyio.create_task_group() as tg:
        tg.start_soon(wait)
        await settle()
        assert s.waiting == [t]
        tok.cancel()
    assert s.waiting == [] and not t.granted
    with pytest.raises(QueueTimeout):
        await s.acquire(tk(B, "t"), timeout=0.05)
    assert s.waiting == []
    s.release(first)
    assert s.running == []


# ---- routing + the queued backend ----------------------------------------------------

class Net:
    """A fake model with a base_url that counts its calls."""

    def __init__(self, name, url, model):
        self.name, self.model = name, model
        self.cfg = SimpleNamespace(id=name, base_url=url, raw={}, label=name)
        self.supports_tools, self.supports_images = True, False
        self.script = [TextDelta("a"), TextDelta("b"), Done(reason="stop")]
        self.calls, self.fail, self.models = 0, None, []

    async def stream(self, messages, tools, body=None, model=None):
        self.calls += 1
        self.models.append(model or self.model)
        if self.fail:
            raise self.fail
        for ev in self.script:
            yield ev


PROVIDERS = {
    "main": {"kind": "llama-swap", "base_url": "http://main:11434/v1",
             "models": {"big": {"caps": ["chat", "tools"], "slots": 1}}},
    "far": {"kind": "ollama", "base_url": "http://far:11434/v1",
            "models": {"small": {"caps": ["chat"]}}},
}


def routed(tmp_path, monkeypatch, roles, providers=PROVIDERS):
    """An LLM over `providers` and `roles`, one Net per model ref."""
    from settings_fixtures import scratch
    scratch(tmp_path, monkeypatch, providers=providers, roles=roles)
    cfg = load()
    cfg.llm = {"queue": {"wait_seconds": 5, "background_wait_seconds": 5}}
    nets = {f"{pid}/{name}": Net(f"{pid}/{name}", p["base_url"], name)
            for pid, p in providers.items() for name in p["models"]}
    return LLM(cfg, build=lambda bc: nets[bc.id]), nets


async def collect(backend, content="hi"):
    events = backend.stream([{"role": "user", "content": content}], [])
    try:
        return [ev async for ev in events]
    finally:
        await events.aclose()


async def test_streaming_frees_the_slot_at_done_or_when_left_early(tmp_path, monkeypatch):
    llm, nets = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}})
    assert llm.exclusive("http://main:11434/v1"), "llama-swap: one model on the GPU"
    out = []
    async for ev in llm.bind(role="chat", priority=I, conversation_id="c1").stream([], []):
        out.append(ev)
        if isinstance(ev, Done):
            assert llm.scheduler.running == []     # freed before the consumer goes on
            break
    assert [type(e).__name__ for e in out] == ["TextDelta", "TextDelta", "Done"]

    nets["main/big"].script = [TextDelta(str(i)) for i in range(50)] + [Done(reason="stop")]
    events = llm.bind(role="chat", priority=I, conversation_id="c1").stream([], [])
    async for _ in events:
        break
    assert len(llm.scheduler.running) == 1
    await events.aclose()
    assert llm.scheduler.running == []


async def test_an_error_moves_on_to_the_next_model(tmp_path, monkeypatch):
    llm, nets = routed(tmp_path, monkeypatch, {"small": {"chain": ["main/big", "far/small"]}})
    nets["main/big"].fail = BackendError("llama-swap returned 503. busy")
    out = await collect(llm.bind(role="small", priority=B))
    assert isinstance(out[-1], Done) and nets["far/small"].calls == 1
    nets["main/big"].fail = None
    await collect(llm.bind(role="small", priority=B))
    assert nets["main/big"].calls == 2               # tried first again


async def test_a_missing_model_or_a_rejected_key_moves_on_but_an_overflow_does_not(
        tmp_path, monkeypatch):
    from celine.backends.openai import http_error
    llm, nets = routed(tmp_path, monkeypatch, {"small": {"chain": ["main/big", "far/small"]}})
    for status in (404, 401):
        nets["main/big"].fail = http_error("Main", "big", status, "")
        assert not nets["main/big"].fail.retryable
        out = await collect(llm.bind(role="small", priority=B))
        assert isinstance(out[-1], Done)
    assert nets["far/small"].calls == 2
    llm, nets = routed(tmp_path, monkeypatch, {"small": {"chain": ["main/big", "far/small"]}})
    nets["main/big"].fail = http_error("Main", "big", 400,
                                           '{"error": {"code": "context_length_exceeded"}}')
    with pytest.raises(BackendError):
        await collect(llm.bind(role="small", priority=B))


async def test_a_chat_whose_model_is_gone_says_which_and_to_pick_another(
        tmp_path, monkeypatch):
    from celine.backends.router import NoModel
    llm, nets = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}})
    llm.cfg.settings.conversation_models["c1"] = "main/renamed"
    with pytest.raises(NoModel, match=r"main/renamed no longer exists.*another model"):
        await collect(llm.bind(role="chat", priority=I, conversation_id="c1", fallback=False))
    assert nets["main/big"].calls == 0


async def test_the_vision_twin_answers_an_image_and_everything_while_loaded(
        tmp_path, monkeypatch):
    """One name for the ticket, the queue events and the request: the twin for an
    image, and for text too while it is loaded, so llama-swap never swaps back."""
    from celine.backends.router import ProviderState
    providers = copy.deepcopy(PROVIDERS)
    providers["main"]["models"]["big"].update(slots=2, vision_twin="big-vision")
    llm, nets = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}}, providers)
    big = nets["main/big"]
    big.cfg.raw = {"vision_twin": "big-vision"}
    loaded: set[str] = set()

    async def probe(pid, **_):
        return ProviderState(reachable=True, loaded=frozenset(loaded))
    monkeypatch.setattr(llm.router, "probe", probe)
    granted: list[str] = []
    grant = llm.scheduler._grant
    llm.scheduler._grant = lambda t: (granted.append(t.model), grant(t))[1]
    sent: list[dict] = []

    async def emit(ev):
        sent.append(ev)

    async def ask(content):
        b = llm.bind(role="chat", priority=I, conversation_id="c1", emit=emit)
        return await collect(b, content)

    image = [{"type": "text", "text": "look"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]
    await ask("hi")
    await ask(image)
    loaded.add("big-vision")
    await ask("and now?")
    assert big.models == ["big", "big-vision", "big-vision"]
    assert granted == big.models
    assert [e["model"] for e in sent if e.get("state") == "running"] == big.models
    assert llm.slots("http://main:11434/v1", "big-vision") == 2, "the base model's slots"


# ---- the whole thing: dispatcher + runner + durable store ----------------------------

class Slow(FakeBackend):
    """Shared counters across every instance the factory makes; on `main`'s address."""
    active = peak = 0
    log: list[str] = []

    def __init__(self):
        super().__init__([TextDelta("x") for _ in range(5)] + [Done(reason="stop")],
                         delay=0.01)
        self.cfg = SimpleNamespace(base_url="http://main:11434/v1", raw={}, label="main")

    async def stream(self, messages, tools):
        sys = messages[0]["content"] if messages else ""
        Slow.log.append("title" if sys.startswith("Name the topic") else "turn")
        Slow.active += 1
        Slow.peak = max(Slow.peak, Slow.active)
        try:
            async for ev in super().stream(messages, tools):
                yield ev
        finally:
            Slow.active -= 1


async def test_three_conversations_a_trigger_and_titles_share_one_slot(
        store, tmp_path, monkeypatch):
    from celine.turns.dispatcher import Dispatcher
    from settings_fixtures import scratch
    scratch(tmp_path, monkeypatch, providers={"main": {
        "kind": "llama-swap", "base_url": "http://main:11434/v1",
        "models": {"fake-1": {"caps": ["chat"], "slots": 1}}}}, roles={})
    cfg = copy.copy(load())
    cfg.settings.general.update({"max_concurrent_turns": 4})
    cfg.llm = {"queue": {"wait_seconds": 20, "background_wait_seconds": 20}}
    Slow.active = Slow.peak = 0
    Slow.log = []
    llm = LLM(cfg, lambda e: None)
    violations: list[str] = []
    grant = llm.scheduler._grant

    def checked(t):
        # never start a ticket while a higher-priority one for the same model waits
        violations.extend(f"{t.label} before {w.label}" for w in llm.scheduler.order()
                          if w is not t and w.model == t.model and w.priority < t.priority)
        grant(t)
    llm.scheduler._grant = checked

    d = Dispatcher(store, cfg, EventBus(), Slow, None, llm=llm)
    cids = [await conversations.create(store) for _ in range(4)]
    async with anyio.create_task_group() as tg:
        await d.start(tg)
        for cid in cids[:3]:
            await user_says(store, cid, "hello there")
        from celine.intercom import send
        await send(store, "system:schedule", f"agent:{cids[3]}", "scheduled check",
                   kind="trigger")
        d.notify()

        async def live(cid):
            turn = (await view.snapshot(store, cid))["turn"]
            return turn and turn["state"] in ("preparing", "streaming", "tools")

        async def stop_third():            # while its turn waits (or streams)
            await until(lambda: live(cids[2]))
            await turns.request_abort(store, cids[2])
            await d.abort_conversation(cids[2])
        tg.start_soon(stop_third)

        async def settled():
            snaps = [await view.snapshot(store, c) for c in cids]
            return all(s["turn"]["state"] in ("done", "aborted") for s in snaps) and all(
                snaps[i]["conversation"]["title"] for i in (0, 1, 3)) \
                and not llm.scheduler.running
        await until(settled, 10)
        states = [(await view.snapshot(store, c))["turn"]["state"] for c in cids]
        await d.stop()
        tg.cancel_scope.cancel()

    assert states == ["done", "done", "aborted", "done"]
    assert Slow.peak == 1 and not violations and "title" in Slow.log
    assert llm.scheduler.waiting == [] and llm.scheduler.running == []
    for cid in cids:
        events = (await view.resume(store, cid, 0))["events"]
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))   # durable order intact
        assert not [e for e in events if e["type"] == "error"]    # a stop is not a failure


def _busy(status=429):
    e = BackendError(f"main returned {status}.")
    e.status, e.retry_after = status, 0     # Retry-After: 0, the wait takes no time
    return e


async def test_busy_is_the_status_alone():
    from celine.backends.llm import busy
    assert busy(_busy(503)) and busy(_busy(529)) and busy(_busy(429))
    oom = BackendError("llama-swap returned 500. CUDA out of memory: Insufficient VRAM")
    oom.status = 500
    assert not busy(oom), "a real out of memory is reported, never waited on"


async def test_the_users_turn_fails_busy_and_keep_waiting_waits(tmp_path, monkeypatch):
    llm, nets = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big", "far/small"]}})
    nets["main/big"].fail = _busy(429)
    with pytest.raises(BackendError) as e:
        await collect(llm.bind(role="chat", priority=I, conversation_id="c1", fallback=False))
    assert e.value.code == "busy" and nets["main/big"].calls == 1, "asked once, no wait"
    assert nets["far/small"].calls == 0

    seen = []

    async def emit(d):
        seen.append(d)
        if d.get("reason") == "busy":
            nets["main/big"].fail = None    # the provider is free again
    out = await collect(llm.bind(role="chat", priority=I, conversation_id="c1",
                                 fallback=False, keep_waiting=True, emit=emit))
    assert isinstance(out[-1], Done) and nets["main/big"].calls == 3
    assert {"reason": "busy", "message": "Waiting for big."}.items() <= \
        next(d for d in seen if d.get("reason") == "busy").items()


async def test_a_keep_waiting_turns_tool_calls_wait_too(tmp_path, monkeypatch):
    """A tool's model call for a Keep waiting turn (vision, Writing, models__ask: bound
    with the turn's id) waits for a busy provider like the turn, routed or fixed, and
    credits the turn's clock; the same call for another turn fails busy."""
    llm, nets = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}})
    token, clock = Token("t1"), SimpleNamespace(deadline=100.0)
    llm.bind(role="chat", priority=I, conversation_id="c1", turn_id="t1", token=token,
             clock=clock, fallback=False, keep_waiting=True)
    tool = llm.bind(role="chat", priority=I, conversation_id="c1", turn_id="t1",
                    fallback=False)
    assert tool.keep_waiting and tool.token is token and tool.clock is clock

    async def emit(d):
        if d.get("reason") == "busy":
            nets["main/big"].fail = None
    for fixed in (None, nets["main/big"]):
        nets["main/big"].fail, nets["main/big"].calls = _busy(429), 0
        out = await collect(llm.bind(fixed, role="chat", priority=I, conversation_id="c1",
                                     turn_id="t1", fallback=False, emit=emit))
        assert isinstance(out[-1], Done) and nets["main/big"].calls == 2

    nets["main/big"].fail = _busy(429)
    for tid in ("t2", "t1"):
        if tid == "t1":
            llm.forget_turn("t1")           # the turn ended: nothing waits for it
        with pytest.raises(BackendError) as e:
            await collect(llm.bind(nets["main/big"], role="chat", priority=I,
                                   turn_id=tid, fallback=False))
        assert e.value.code == "busy"


async def test_stop_ends_a_tool_call_waiting_for_a_keep_waiting_turn(tmp_path, monkeypatch):
    llm, nets = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}})
    nets["main/big"].fail = _busy(429)
    nets["main/big"].fail.retry_after = 60      # the wait would be long: Stop ends it
    token = Token("t1")
    llm.bind(role="chat", priority=I, turn_id="t1", token=token, keep_waiting=True)

    async def emit(d):
        if d.get("reason") == "busy":
            token.cancel()
    with pytest.raises(QueueCancelled):
        await collect(llm.bind(role="chat", priority=I, turn_id="t1", fallback=False,
                               emit=emit))
    assert nets["main/big"].calls == 1


async def test_a_wait_for_a_model_stops_the_turns_clock_and_the_tools_limits():
    import math
    from celine.backends.llm import TOOL_CLOCKS, paused
    turn, tool = SimpleNamespace(deadline=100.0), SimpleNamespace(deadline=50.0)
    held = TOOL_CLOCKS.set((tool,))
    try:
        with paused(turn):
            assert (turn.deadline, tool.deadline) == (math.inf, math.inf)
    finally:
        TOOL_CLOCKS.reset(held)
    assert 100.0 <= turn.deadline < 101.0 and 50.0 <= tool.deadline < 51.0


async def test_a_tool_is_not_timed_out_while_it_waits_for_a_slot():
    from celine.backends.llm import TOOL_CLOCKS, paused
    with anyio.move_on_after(0.05) as scope:
        held = TOOL_CLOCKS.set((scope,))
        try:
            with paused(None):
                await anyio.sleep(0.15)
        finally:
            TOOL_CLOCKS.reset(held)
    assert not scope.cancelled_caught


async def test_a_load_failure_fails_the_users_turn_busy(tmp_path, monkeypatch):
    llm, nets = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}})
    nets["main/big"].fail = BackendError("llama-swap returned 500. failed to start")
    with pytest.raises(BackendError) as e:              # a load failure is busy too
        await collect(llm.bind(role="chat", priority=I, conversation_id="c1", fallback=False))
    assert e.value.code == "busy"


async def test_background_work_on_one_model_waits_for_a_busy_provider(tmp_path, monkeypatch):
    llm, nets = routed(tmp_path, monkeypatch, {"small": {"chain": ["main/big"]}})
    nets["main/big"].fail = _busy(503)

    async def emit(d):
        if d.get("reason") == "busy":
            nets["main/big"].fail = None
    out = await collect(llm.bind(role="small", priority=B, fallback=False, emit=emit))
    assert isinstance(out[-1], Done) and nets["main/big"].calls == 2


# ---- the user's request never times out, a wait names the holder -----------------------

def _short_wait_llm():
    cfg = load()
    cfg.llm = {"queue": {"wait_seconds": 0.2, "background_wait_seconds": 0.2}}
    llm = LLM(cfg)
    llm.slots = lambda endpoint, model: 1            # one request at a time
    return llm


class Held(FakeBackend):
    """Holds its slot until the test sets `go`."""

    def __init__(self):
        super().__init__([TextDelta("a"), Done(reason="stop")])
        self.go = anyio.Event()

    async def stream(self, messages, tools, body=None):
        await self.go.wait()
        async for ev in super().stream(messages, tools, body):
            yield ev


async def _drain(backend):
    return [ev async for ev in backend.stream([{"role": "user", "content": "hi"}], [])]


async def test_the_users_request_waits_past_every_limit_and_names_the_holder():
    llm = _short_wait_llm()
    slow, waits, limits = Held(), [], []
    acquire = llm.scheduler.acquire

    async def recorded(t, **kw):
        limits.append((t.priority, kw.get("timeout")))
        return await acquire(t, **kw)
    llm.scheduler.acquire = recorded

    async def emit(data):
        waits.append(data)
    async with anyio.create_task_group() as tg:
        tg.start_soon(_drain, llm.bind(slow, role="chat", priority=Priority.AGENT,
                                       label="World news"))
        await until(lambda: llm.scheduler.running)
        snap = llm.scheduler.snapshot()
        assert snap["running"][0]["label"] == "World news"
        mine = llm.bind(FakeBackend([TextDelta("b"), Done(reason="stop")]), role="chat",
                        priority=Priority.INTERACTIVE, conversation_id="c1", wait=0.1,
                        emit=emit, label="Our chat")
        out = []

        async def ask():
            out.extend(await _drain(mine))
        tg.start_soon(ask)
        await until(lambda: any(w["state"] == "waiting" for w in waits))
        slow.go.set()
    assert isinstance(out[-1], Done)
    prio, limit = limits[-1]
    assert prio == Priority.INTERACTIVE and not limit, "no limit, not even its own wait"
    assert limits[0][0] == Priority.AGENT and limits[0][1], "everyone else's wait has one"
    wait = next(w for w in waits if w["state"] == "waiting")
    assert wait["holder"] == "World news"


async def test_the_snapshot_says_which_reply_another_model_wrote(store):
    """llm.queue with fallback true on a message of the path: `switched` names it."""
    from celine.conversations import tree
    from celine.core.event_types import LLM_QUEUE
    cid = await conversations.create(store)

    def fn(c):
        m = tree.append(c, store, cid, None, "assistant", "hi", "model")
        n = tree.append(c, store, cid, m["id"], "assistant", "again", "model")
        for one, fallback in ((m, True), (n, False)):
            store.event(c, cid, LLM_QUEUE, {"message_id": one["id"], "fallback": fallback,
                                            "model": "small", "instead_of": "big"})
        return m["id"]
    mid = await store.write(fn)
    assert (await view.snapshot(store, cid))["switched"] == {
        mid: {"model": "small", "instead_of": "big"}}
