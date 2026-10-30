"""Drones (sub_agents/drone.py): a sub-agent whose turns run in `opencode serve` on a
device, here a fake one. Its brief goes to its own session with the model it runs on,
the proxy's models into the device's config first; its tools come back as thinking lines
and its "Now:"; the reply is opencode's answer as it came, its tokens the turn's usage,
the files its messages changed in the report. The same name carries on in the same
session; a waking message ends the turn without stopping the drone; a Stop aborts it."""
from __future__ import annotations

import base64
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import pytest

from celine import boot
from celine.backends.base import Done, ReasoningDelta, TextDelta
from celine.conversations import store as conversations, tree
from celine.core.services import Services
from celine.devices import Device
from celine.intercom.service import Intercom
from celine.settings import conversations as conv
from celine.skills import Registry
from celine.sub_agents import drone, skill as skill_mod
from celine.sub_agents.report import head, work
from celine.sub_agents.skill import SubAgents, agents_of
from chat_fixtures import until
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

PROVIDERS = {"main": {"kind": "llama-swap", "base_url": "http://main:8080/v1",
                      "models": {"big": {"caps": ["chat", "tools"], "context": 65536},
                                 "small": {"caps": ["chat"], "context": 32768}}}}
ROLES = {"chat": {"chain": ["main/big"]}}
PW = "drone-pw"


class Fake(BaseHTTPRequestHandler):
    """opencode serve: config, sessions, status, messages, diff, prompt_async, abort and
    the event stream (what `events` holds goes out, in order)."""
    seen: list[tuple[str, str, dict | None]] = []
    config: dict = {}
    busy: set[str] = set()
    messages: dict[str, list[dict]] = {}
    diffs: dict[str, list[dict]] = {}
    events: queue.Queue = queue.Queue()
    n = 0

    def _auth(self) -> bool:
        if self.headers.get("Authorization") == \
                "Basic " + base64.b64encode(f"opencode:{PW}".encode()).decode():
            return True
        self.send_response(401)
        self.end_headers()
        return False

    def _json(self, data, status=200):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self._auth():
            return
        path, _, query = self.path.partition("?")
        Fake.seen.append(("GET", self.path, None))
        if path == "/event":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            while True:
                try:
                    ev = Fake.events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if ev is None:
                    return
                try:
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
                except OSError:
                    return
        if path == "/global/config":
            return self._json(Fake.config)
        if path == "/session/status":
            return self._json({s: {"type": "busy"} for s in Fake.busy})
        parts = path.strip("/").split("/")
        if len(parts) == 2:
            return self._json({"id": parts[1], "directory": "/Users/celine"})
        if parts[2] == "message":
            return self._json(Fake.messages.get(parts[1], []))
        if parts[2] == "diff":
            return self._json(Fake.diffs.get(query.partition("messageID=")[2], []))
        self._json({}, 404)

    def _body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_PATCH(self):
        if not self._auth():
            return
        body = self._body()
        Fake.seen.append(("PATCH", self.path, body))
        Fake.config = body
        self._json(body)

    def do_POST(self):
        if not self._auth():
            return
        body = self._body()
        Fake.seen.append(("POST", self.path, body))
        parts = self.path.partition("?")[0].strip("/").split("/")
        if parts == ["session"]:
            Fake.n += 1
            return self._json({"id": f"ses_{Fake.n}", "title": body.get("title")})
        sid = parts[1]
        if parts[2] == "prompt_async":
            Fake.busy.add(sid)
            msgs = Fake.messages.setdefault(sid, [])
            msgs.append({"info": {"id": f"msg_{len(msgs):03}", "role": "user"},
                         "parts": [{"type": "text", "text": body["parts"][0]["text"]}]})
            self.send_response(204)
            self.end_headers()
            return
        if parts[2] == "abort":
            was = sid in Fake.busy
            Fake.busy.discard(sid)
            return self._json(was)
        self._json({}, 404)

    def log_message(self, *args):
        pass


def answer(sid: str, text: str, model="main/small", tokens=(1000, 50, 200)) -> None:
    """The fake's session answered: one complete assistant message, then idle."""
    msgs = Fake.messages[sid]
    msgs.append({"info": {"id": f"msg_{len(msgs):03}", "role": "assistant", "modelID": model,
                          "time": {"created": 1, "completed": 2},
                          "tokens": {"input": tokens[0], "output": tokens[1], "reasoning": 0,
                                     "cache": {"read": tokens[2], "write": 0}}},
                 "parts": [{"type": "step-start"}, {"type": "text", "text": text}]})
    Fake.busy.discard(sid)
    Fake.events.put({"type": "session.idle", "properties": {"sessionID": sid}})


def tool(sid: str, call: str, name: str, title: str) -> None:
    Fake.events.put({"type": "message.part.updated", "properties": {"sessionID": sid, "part": {
        "type": "tool", "callID": call, "tool": name,
        "state": {"status": "running", "input": {}, "title": title}}}})


@pytest.fixture
def fake():
    Fake.seen, Fake.config, Fake.busy, Fake.messages, Fake.diffs = [], {}, set(), {}, {}
    Fake.events, Fake.n = queue.Queue(), 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    for _ in range(8):
        Fake.events.put(None)
    srv.shutdown()


@pytest.fixture
def app(tmp_path, monkeypatch, store, fake):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=ROLES)
    cfg = boot.load()
    mac = Device("mac", "Her Mac", fake, PW, "dev-key", "~/oc.log", None)
    monkeypatch.setattr(drone, "devices", lambda cfg: {"mac": mac})
    monkeypatch.setattr(skill_mod, "all_devices", lambda cfg: {"mac": mac})
    monkeypatch.setattr(drone, "POLL", 0.05)
    reg = Registry()
    agents = SubAgents(cfg)
    reg.add(agents)
    services = Services(cfg=cfg, store=store, registry=reg, dispatcher=object())
    agents.app = services
    return services


async def _drone(store, brief="make pac-man and test it"):
    parent = await conversations.create(store)
    sub = await conversations.create(store, title="arcade", agent_of=parent,
                                     agent_name="arcade", access="work", agent_cli="opencode",
                                     agent_device="mac")
    await conv.apply(store, sub, {"model": "main/small"})
    await _brief(store, sub, brief)
    return sub


async def _brief(store, sub, text):
    def fn(c):
        leaf = c.execute("SELECT id FROM messages WHERE conversation_id=? ORDER BY rowid DESC "
                         "LIMIT 1", (sub,)).fetchone()
        m = tree.append(c, store, sub, leaf[0] if leaf else None, "user", text, "intercom")
        tree.append(c, store, sub, m["id"], "assistant", "", "model")
    await store.write(fn)


async def _run(backend, during=None) -> list:
    """The stream's events; `during(event)` is called after each."""
    out = []
    async for ev in backend.stream([], []):
        out.append(ev)
        if during is not None:
            during(ev)
    return out


async def _row(store, sub):
    return await store.read(lambda c: dict(c.execute(
        "SELECT agent_session, agent_sent, agent_seen, agent_step FROM conversations "
        "WHERE id=?", (sub,)).fetchone()))


async def test_a_brief_goes_to_its_session_and_the_answer_comes_back_as_it_came(app, store):
    sub = await _drone(store)
    b = drone.DroneBackend(app, sub, "opencode", "mac")
    steps = []

    def during(ev):
        if isinstance(ev, ReasoningDelta) and ev.text.startswith("opencode on"):
            tool("ses_1", "c1", "bash", "Runs the Playwright test")
        elif isinstance(ev, ReasoningDelta):
            steps.append(ev.text)
            answer("ses_1", "pac-man runs at 60 fps, the test passes.")
    Fake.diffs["msg_000"] = [{"path": "pacman/index.html", "status": "added"}]
    evs = await _run(b, during)
    assert [e.text for e in evs if isinstance(e, TextDelta)] == [
        "pac-man runs at 60 fps, the test passes."]
    assert steps == ["bash: Runs the Playwright test\n"]
    done = evs[-1]
    assert isinstance(done, Done) and done.reason == "stop"
    assert (done.usage.input_tokens, done.usage.output_tokens, done.usage.cache_read) == \
        (1200, 50, 200)
    assert done.raw["files"] == ["pacman/index.html"] and done.raw["session"] == "ses_1"
    assert b.request_sent["provider"] == "main/small"
    prompt = next(body for m, p, body in Fake.seen if p.startswith("/session/ses_1/prompt_async"))
    assert prompt == {"model": {"providerID": "celine", "modelID": "main/small"},
                      "parts": [{"type": "text", "text": "make pac-man and test it"}]}
    patch = next(body for m, p, body in Fake.seen if m == "PATCH")
    assert patch["provider"]["celine"]["models"]["main/small"] == {
        "name": "main/small", "limit": {"context": 32768, "output": 8192}}
    row = await _row(store, sub)
    assert (row["agent_session"], row["agent_seen"], row["agent_step"]) == \
        ("ses_1", "msg_001", None)

    # the same name carries on: the next message goes to the same session, the config
    # already holds the models
    await _brief(store, sub, "now add the ghosts")
    Fake.seen.clear()
    evs = await _run(b, lambda ev: isinstance(ev, ReasoningDelta) and answer(
        "ses_1", "ghosts added", tokens=(10, 5, 0)))
    assert [e.text for e in evs if isinstance(e, TextDelta)] == ["ghosts added"]
    assert evs[-1].usage.input_tokens == 10, "only what it answered since the last count"
    assert [(m, p) for m, p, _ in Fake.seen if m != "GET"] == [
        ("POST", "/session/ses_1/prompt_async")]
    assert Fake.messages["ses_1"][2]["parts"][0]["text"] == "now add the ghosts"


async def test_a_waking_message_ends_the_turn_and_the_next_hands_it_over(app, store):
    sub = await _drone(store)
    b = drone.DroneBackend(app, sub, "opencode", "mac")
    parent = await store.read(lambda c: c.execute(
        "SELECT agent_of FROM conversations WHERE id=?", (sub,)).fetchone()[0])

    # its turn is live: the message waits to be delivered
    await store.write(lambda c: c.execute(
        "INSERT INTO turns(id, conversation_id, state, created_at) VALUES('t1', ?, "
        "'streaming', '2026-10-30T10:00:00.000+00:00')", (sub,)))

    async def steer():
        await Intercom(store, lambda: None).send(f"agent:{parent}", f"agent:{sub}",
                                                 "use canvas, not DOM")
    evs = []
    async with anyio.create_task_group() as tg:
        async def go():
            evs.extend(await _run(b))
        tg.start_soon(go)
        await until(lambda: Fake.busy)
        await steer()
    assert not [e for e in evs if isinstance(e, TextDelta)], "it still works"
    assert "ses_1" in Fake.busy, "not aborted"
    assert not any(p.endswith("/abort") for _, p, _ in Fake.seen)


async def test_a_stop_aborts_the_session(app, store):
    sub = await _drone(store)
    b = drone.DroneBackend(app, sub, "opencode", "mac")
    async with anyio.create_task_group() as tg:
        tg.start_soon(_run, b)
        await until(lambda: Fake.busy)
        tg.cancel_scope.cancel()
    assert any(p == "/session/ses_1/abort" for _, p, _ in Fake.seen)
    assert (await _row(store, sub))["agent_step"] is None


async def test_the_report_names_the_files_a_drone_changed(store):
    parent = await conversations.create(store)
    sub = await conversations.create(store, "d", agent_of=parent, agent_name="d")

    def rows(c):
        c.execute("INSERT INTO turns(id, conversation_id, state, created_at, finished_at) "
                  "VALUES('t0', ?, 'done', '2026-10-30T10:00:00.000+00:00', "
                  "'2026-10-30T10:01:00.000+00:00')", (sub,))
        m = tree.append(c, store, sub, None, "assistant", "done", "model", turn_id="t0")
        c.execute("INSERT INTO raw_responses(message_id, body) VALUES(?, ?)",
                  (m["id"], json.dumps({"files": ["a.py", "b.py"]})))
    await store.write(rows)
    w = await store.read(lambda c: work(c, sub))
    assert w["files"] == ["a.py", "b.py"] and w["seconds"] == 60
    assert head("d", "done", w) == "[d, done; changed a.py, b.py]"


async def test_a_device_must_be_one_that_is_on(app, store):
    parent = await conversations.create(store)
    agents = app.registry.skills["sub_agents"]
    _, refusal, _, _ = await agents.begin(store, parent, "x", "arcade", device="pc")
    assert refusal == "pc is not one of your devices. Yours: mac."
    rows = await store.read(lambda c: agents_of(c, parent))
    assert rows == []
