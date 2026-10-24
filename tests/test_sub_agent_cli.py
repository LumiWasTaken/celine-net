"""A sub-agent's model and its agent CLI (sub_agents/skill.py, sub_agents/cli.py): the
model is one of its own and stays its own, and a CLI sub-agent's brief runs as `agent` in
its own shellbox terminal, its answer.md the reply."""
from __future__ import annotations

import json

import anyio
import httpx
import pytest

from celine import boot
from celine.backends.base import TextDelta
from celine.conversations import store as conversations
from celine.core.services import Services
from celine.settings import conversations as conv
from celine.skills import Registry, ToolContext
from celine.skills.shell import Shell
from celine.sub_agents import cli as cli_mod
from celine.sub_agents.skill import SubAgents
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio

PROVIDERS = {"main": {"kind": "llama-swap", "base_url": "http://main:8080/v1",
                      "models": {"big": {"caps": ["chat", "tools"]},
                                 "small": {"caps": ["chat"]},
                                 "other": {"caps": ["embeddings"]}}}}
ROLES = {"chat": {"chain": ["main/big"]}, "small": {"chain": ["main/small"]}}


class Box:
    """The agent's shellbox's /file, /run, /read and /stop: keeps files, answers each run with
    `states` in turn (each a run's end), and keeps every call."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self.states = [{"state": "done", "exit": 0}]
        self.answer: bytes | None = b"three bugs fixed\n"

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/file" and request.method == "PUT":
            self.files[request.url.params["path"]] = request.content
            return httpx.Response(200, json={"path": request.url.params["path"]})
        if path == "/file":
            if self.answer is None:
                return httpx.Response(404, json={"error": "no such file"})
            return httpx.Response(200, content=self.answer)
        body = json.loads(request.content)
        self.calls.append((path, body))
        if path == "/stop":
            return httpx.Response(200, json={"state": "done"})
        res = {"terminal": body["terminal"], "tail": "the last lines", **self.states.pop(0)}
        if path == "/run":
            return httpx.Response(200, content=b'{"progress": "working"}\n'
                                  + json.dumps(res).encode() + b"\n")
        return httpx.Response(200, json=res)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, providers=PROVIDERS, roles=ROLES)
    monkeypatch.setenv("CELINE_SHELL_URL", "http://box")
    monkeypatch.setattr(cli_mod, "POLL", 0)
    return boot.load()


@pytest.fixture
def box():
    return Box()


def _services(cfg, store, box) -> Services:
    reg = Registry()
    reg.add(Shell(cfg, transport=httpx.MockTransport(box)))
    agents = SubAgents(cfg)
    reg.add(agents)
    app = Services(cfg=cfg, store=store, registry=reg, dispatcher=object())
    agents.app = app
    return app


async def _sub(store, cli="opencode", model=None):
    parent = await conversations.create(store)
    sub = await conversations.create(store, title="bug fixer", agent_of=parent,
                                     agent_name="bug fixer", access="work", agent_cli=cli)
    if model:
        await conv.apply(store, sub, {"model": model})
    from celine.conversations import tree

    def earlier(c):
        """An earlier brief and its answer; the new brief's turn already has its reply,
        still empty, as a running turn does."""
        m = tree.append(c, store, sub, None, "user", "the earlier brief", "intercom")
        m = tree.append(c, store, sub, m["id"], "assistant", "the earlier answer", "model")
        m = tree.append(c, store, sub, m["id"], "user", "fix the three bugs in ~/proj",
                        "intercom")
        tree.append(c, store, sub, m["id"], "assistant", "", "model")
    await store.write(earlier)
    return sub


async def _reply(backend) -> str:
    return "".join([ev.text async for ev in backend.stream([], []) if isinstance(ev, TextDelta)])


async def test_the_brief_runs_as_agent_in_its_own_terminal(cfg, store, box):
    app = _services(cfg, store, box)
    sub = await _sub(store, model="main/small")
    b = cli_mod.CliBackend(app, sub, "opencode")
    assert (b.name, b.model) == ("cli:opencode", "main/small")
    assert await _reply(b) == "three bugs fixed"
    [(path, run)] = box.calls
    term = run["terminal"]
    assert path == "/run" and term.startswith("bug-fixer-")
    assert box.files == {f"agents/{term}/brief.md": b"fix the three bugs in ~/proj"}
    assert run["command"] == (f"cd ~/agents/{term} && rm -f answer.md && agent opencode -m "
                              f"'main/small' --minutes 120 -f brief.md -o answer.md")


async def test_a_long_run_is_followed_and_its_end_says_how_it_went(cfg, store, box):
    app = _services(cfg, store, box)
    sub = await _sub(store, cli="opencode")
    box.states = [{"state": "running"}, {"state": "running"}, {"state": "done", "exit": 124}]
    box.answer = None
    b = cli_mod.CliBackend(app, sub, "opencode")
    assert b.model == "default", "no model of its own: the proxy's default"
    text = await _reply(b)
    assert text == "opencode ran out of its 120 minutes.\n\nthe last lines"
    assert [p for p, _ in box.calls] == ["/run", "/read", "/read"]


async def test_a_stop_presses_ctrl_c_in_its_terminal(cfg, store, box, monkeypatch):
    app = _services(cfg, store, box)
    sub = await _sub(store)
    box.states = [{"state": "running"}] * 1000
    monkeypatch.setattr(cli_mod, "POLL", 0.01)
    with anyio.move_on_after(0.3):
        await _reply(cli_mod.CliBackend(app, sub, "opencode"))
    assert box.calls[-1][0] == "/stop"


async def test_start_takes_one_of_her_models_and_a_cli(cfg, store, box):
    app = _services(cfg, store, box)
    agents = app.registry.skills["sub_agents"]
    parent = await conversations.create(store)
    sent = []

    class Intercom:
        async def send(self, *a, **kw):
            sent.append(a)
            return {"intercom_id": len(sent)}
    app.intercom = Intercom()

    async def emit(*a, **kw):
        pass
    ctx = ToolContext(parent, "t", "x", store, cfg, emit)
    bad = await agents.start(ctx, brief="x", name="picky one", model="main/other")
    assert bad.is_error and "main/big, main/small" in bad.content
    ok = await agents.start(ctx, brief="go", name="picky one", model="main/small", cli="opencode")
    assert not ok.is_error and ok.display["model"] == "main/small" and ok.display["cli"] == "opencode"
    row = await store.read(lambda c: c.execute(
        "SELECT model_id, agent_cli FROM conversations WHERE id=?",
        (ok.display["conversation_id"],)).fetchone())
    assert tuple(row) == ("main/small", "opencode")
    plain = await agents.start(ctx, brief="go", name="plain one")
    assert plain.display["model"] is None and plain.display["cli"] is None, "as today"
