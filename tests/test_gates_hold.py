"""The gates it cannot talk its way out of,
one test per gate: closed gates are code (no model, no pass, no Watch), the house
enforces a freeze (no turn or work item leased, no tool, the model proxy and tokens
refused), only the owner's session unfreezes, passes and approves, the guard routes refuse every token, and every tool call has one way in."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest

from chat_fixtures import Script, until, user_says
from test_auth import all_routes
from test_bastet import OWNER, TURN, FakeAsk, Notes, Web, _app, _cfg
from test_guard import _call
from test_intercom import _gated_app

from celine import guard, work
from celine.approvals import Approvals
from celine.backends.base import Done, TextDelta
from celine.boot import load
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.core.services import Services
from celine.guard import banners, freeze, judge, passes, questions, rules
from celine.guard import store as decisions
from celine.guard.owner import Owner
from celine.settings.files import Invalid
from celine.skills import Registry, ToolResult, tool
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn

pytestmark = pytest.mark.anyio

SERVER = Path(__file__).resolve().parents[1] / "server" / "celine"
CLOSED = ("Not run: Bastet stopped it: that is private and does not leave the house. "
          "Carry on with what you were doing.")


def _sources():
    for p in sorted(SERVER.rglob("*.py")):
        yield p.relative_to(SERVER).as_posix(), p.read_text()


# ---- closed gates ------------------------------------------------------------------------

def test_the_gates_table_its_defaults_and_its_checks(tmp_path, monkeypatch):
    g = rules.current(load())["gates"]
    assert g["sensitive"] == {"public": "closed", "people": "judge", "house": "open"}
    for cls in ("informal", "nsfw", "unclassed"):
        assert g[cls] == {"public": "judge", "people": "judge", "house": "open"}, cls
    cfg = _cfg(gates={"nsfw": {"people": "closed"}, "sensitive": {"public": "judge"}})
    now = rules.current(cfg)
    assert rules.gate(now, "nsfw", "people") == "closed"
    assert rules.gate(now, "nsfw", "public") == "judge", "the rest of a class stays"
    assert rules.gate(now, "sensitive", "public") == "judge"
    assert rules.gate(now, "public", "public") == "open"
    for bad in ({"gates": "closed"}, {"gates": {"public": {"people": "closed"}}},
                {"gates": {"secret": {"public": "closed"}}},
                {"gates": {"sensitive": {"web": "closed"}}},
                {"gates": {"sensitive": {"public": "shut"}}},
                {"gates": {"sensitive": {"house": "judge"}}},
                {"gates": {"sensitive": "closed"}}):
        with pytest.raises(Invalid):
            rules.validate(bad)
    monkeypatch.setenv("CELINE_DATA_DIR", str(tmp_path / "var"))
    root = tmp_path / "var" / "settings"
    root.mkdir(parents=True, exist_ok=True)
    (root / "guard.toml").write_text('[gates]\nsensitive = { public = "ajar" }\n')
    assert any(p.file.endswith("guard.toml") and p.fatal for p in load().problems)


def _world(mode, **gates):
    cfg = _cfg(mode, **({"gates": gates} if gates else {}))
    cfg.skills = {**cfg.skills, "notebook": {"enabled": True}, "web": {"enabled": True}}
    reg = Registry()
    web = Web()
    reg.add(Notes())
    reg.add(web)
    return cfg, reg, web


async def _turn(store, cfg, reg, second=("web__search", {"query": "pizza near 12 Example Road"})):
    """The agent's turn: the sensitive note read, then `second` with its street address."""
    from celine.intercom import send
    backend = Script([_call(0, "notebook__read", {"path": "people/sam.md"}),
                      _call(1, *second), [TextDelta("done"), Done(reason="stop")]])
    cid = await conversations.create(store)
    await send(store, "user", f"agent:{cid}", "find me pizza")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), reg, None)
    return cid, await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT tc.id, tc.name, tc.state, tc.display, r.content FROM tool_calls tc "
        "JOIN messages r ON r.id = tc.result_message_id WHERE r.conversation_id=? "
        "ORDER BY tc.rowid", (cid,))])


@pytest.fixture
def no_judge(monkeypatch):
    """Any model Bastet would ask fails the test: a closed gate asks none."""
    async def never(*_a, **_k):
        raise AssertionError("a closed gate asks no model")
    monkeypatch.setattr(questions, "judge_for", lambda *a, **kw: never)
    monkeypatch.setattr(judge, "ask", never)


async def test_a_closed_gate_never_runs_in_watch_with_a_pass_or_allowed_once(store, no_judge):
    cfg, reg, web = _world("watch")
    _, rows = await _turn(store, cfg, reg)
    assert web.queries == [] and rows[1]["state"] == "gated"
    assert rows[1]["content"] == CLOSED
    [row] = await decisions.listing(store)
    assert (row["rule"], row["decision"], row["class"], row["sink"], row["piece"], row["p"]) \
        == ("gates", "closed", "sensitive", "public", "12 Example Road", None)
    # their pass on every rule everywhere, and the very call allowed once: still closed
    assert (await passes.make(store, by=OWNER, rule="all", minutes=60))["by"] == OWNER.via
    app = Services(cfg=cfg, store=store)
    from celine import events
    r = await store.read(lambda c: c.execute(events.SOURCES["guard"].sql).fetchone())
    assert events._guard(r, cfg)["outcome"] == "Stopped"      # not "No answer"
    b = await store.read(lambda c: decisions.banner(c, row["id"]))
    assert b["kind"] == "closed" and b["line"] == (
        'Stopped a web search. It sends "12 Example Road" (sensitive) to the public, and that '
        'gate is closed in guard.toml. A closed gate holds in Watch too.')
    for grant in ("once", "pass"):
        got = await banners.answer(app, row["id"], by=OWNER, said="let her", decided={
            "grant": grant, "rule": "all", "minutes": 60, "where": "here"})
        assert got["thread"][-1]["text"] == "That gate is closed in guard.toml. Only the " \
                                            "file opens it."
    _, rows = await _turn(store, cfg, reg)
    assert web.queries == [] and rows[1]["content"] == CLOSED


async def test_open_and_judge_gates_and_the_master_switch(store, monkeypatch):
    # the switch off: nothing of Bastet runs, the gate too (the master)
    cfg, reg, web = _world("off")
    await _turn(store, cfg, reg)
    assert web.queries == ["pizza near 12 Example Road"]
    # open: the leak rule is never asked; judge: it is
    ask = FakeAsk(0.9)
    cfg = _cfg(gates={"sensitive": {"public": "open"}})
    call = {"id": "k1", "name": "web__search", "message_id": None, "read_only": True,
            "args": {"query": "12 Example Road Springfield pizza"}, "description": "Search."}
    assert await guard.closed(TURN, call, [], mode="on", store=store, cfg=cfg) is None
    assert await guard.check(TURN, call, [], mode="on", judge=judge.ask, store=store,
                             ask=ask, cfg=cfg) is None and ask.seen == []
    cfg = _cfg(gates={"sensitive": {"public": "judge"}})
    assert await guard.closed(TURN, call, [], mode="on", store=store, cfg=cfg) is None
    v = await guard.check(TURN, call, [], mode="on", judge=judge.ask, store=store, ask=ask,
                          cfg=cfg)
    assert v.rule == "leak" and [set(q) for _, q in ask.seen] == [{"leak"}]


async def test_a_closed_house_gate_and_what_it_never_looks_at(store):
    cfg = _cfg(gates={"sensitive": {"house": "closed"}})
    write = {"id": "k1", "name": "notebook__write", "message_id": None, "read_only": False,
             "args": {"path": "x.md", "content": "Joe: 12 Example Road"}}
    v = await guard.closed(TURN, write, [], mode="on", store=store, cfg=cfg)
    assert v.decision == "stop" and v.sentence == CLOSED
    # image and music prompts (moderation is left to the ComfyUI server) and the vault are never looked at
    for name in ("image_generator__make", "music_generator__make", "vault__get"):
        assert await guard.closed(TURN, {**write, "name": name}, [], mode="on", store=store,
                                  cfg=cfg) is None, name
    assert await guard.closed(TURN, {**write, "args": {"content": "pizza"}}, [], mode="on",
                              store=store, cfg=cfg) is None


class Files:
    """An MCP server's skill: its block of mcp.toml says where it sends."""
    name = "files"
    description = "A drive."
    cfg = SimpleNamespace(sends="public", cls="sensitive")

    def __init__(self):
        self.sent = []

    @tool(read_only=False)
    async def share(self, ctx, text: str) -> ToolResult:
        """Share a text."""
        self.sent.append(text)
        return ToolResult(content="shared")


async def test_an_mcp_tool_goes_through_the_chokepoint_and_its_gate(store, no_judge):
    cfg, reg, _ = _world("watch")
    cfg.skills = {**cfg.skills, "files": {"enabled": True}}
    files = Files()
    reg.add(files)
    _, rows = await _turn(store, cfg, reg, ("files__share", {"text": "Joe, 12 Example Road"}))
    assert files.sent == [] and rows[1]["content"] == CLOSED


# ---- the freeze, enforced by the house -------------------------------------------------

async def _frozen(store):
    await freeze.freeze(Services(cfg=load(), store=store), by="user", why="a test")


async def test_frozen_no_turn_and_no_work_item_is_leased(store):
    cid = await conversations.create(store)
    await user_says(store, cid, "hello")
    wid = await work.put(store, "title", {"conversation_id": cid})
    await _frozen(store)
    assert await turns.claim(store, "w") is None
    assert await work.claim(store, "w", ["title"]) is None
    ran = []
    pool = work.Pool(store, workers=1)

    async def title(payload):
        ran.append(payload)
    pool.handle("title", (), title)
    app = Services(cfg=load(), store=store, dispatcher=SimpleNamespace(
        notify=lambda: None, work=pool))
    async with anyio.create_task_group() as tg:
        await pool.start(tg)
        await anyio.wait_all_tasks_blocked()
        assert ran == [] and await store.read(lambda c: c.execute(
            "SELECT state FROM work WHERE id=?", (wid,)).fetchone()[0]) == "queued"
        await freeze.unfreeze(app, by=OWNER)      # wakes the pool
        await until(lambda: ran)
        await pool.stop()
        tg.cancel_scope.cancel()
    assert (await turns.claim(store, "w"))["conversation_id"] == cid


async def test_frozen_no_tool_runs_even_in_a_turn_that_runs(store, no_judge):
    cfg, reg, web = _world("off")              # the freeze is the house's, any mode
    cid = await conversations.create(store)
    await user_says(store, cid, "find me pizza")
    turn = await turns.claim(store, "w")       # claimed, then frozen
    await _frozen(store)
    backend = Script([_call(0, "web__search", {"query": "pizza"}),
                      [TextDelta("done"), Done(reason="stop")]])
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), reg, None)
    [row] = await store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT tc.state, r.content FROM tool_calls tc JOIN messages r "
        "ON r.id = tc.result_message_id")])
    assert web.queries == [] and row == {
        "state": "gated", "content": "Not run: you are frozen. Nothing runs until User "
                                     "unfreezes you."}


async def test_frozen_the_model_proxy_and_a_token_only_read(store, stack_key):
    from fastapi import FastAPI
    from celine.api import llm_proxy
    from celine.intercom import tokens
    stack_key("shellbox-llm", "box-key")
    proxy = FastAPI()
    proxy.state.services = Services(cfg=load(), store=store, llm=SimpleNamespace())
    proxy.include_router(llm_proxy.router)
    token = await store.write(lambda c: tokens.make(c, "cli"))
    gated = _gated_app(store)
    key = {"authorization": "Bearer box-key"}
    bearer = {"authorization": f"Bearer {token}"}
    body = {"model": "nope", "messages": []}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy),
                                 base_url="http://t") as box, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=gated),
                              base_url="http://localhost:8766") as api:
        assert (await box.post("/llm/v1/chat/completions", headers=key, json=body)
                ).status_code != 403
        assert (await api.post("/api/intercom", headers=bearer, json={
            "to": "user", "text": "up"})).status_code == 200
        await _frozen(store)
        r = await box.post("/llm/v1/chat/completions", headers=key, json=body)
        assert r.status_code == 403 and "frozen" in r.json()["error"]["message"]
        r = await api.post("/api/intercom", headers=bearer, json={"to": "user", "text": "x"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "frozen"
        assert (await api.get("/api/intercom", headers=bearer)).status_code == 200


# ---- only the owner unfreezes, passes and approves ---------------------------------------

async def test_unfreeze_pass_and_approve_take_the_owner_and_nothing_else(store):
    app = Services(cfg=load(), store=store, approvals=Approvals(load()))
    await _frozen(store)
    with pytest.raises(TypeError):
        Owner("tool", object())
    for by in ("user", None, "guard", SimpleNamespace(via="session")):
        with pytest.raises(TypeError):
            await freeze.unfreeze(app, by=by)
        with pytest.raises(TypeError):
            await passes.make(store, by=by, rule="all", minutes=5)
        with pytest.raises(TypeError):
            app.approvals.decide("x", True, by)
        with pytest.raises(TypeError):
            await banners.answer(app, 1, by=by, said="ok", decided={"grant": "unfreeze"})
    assert await freeze.frozen(store) is not None
    assert await passes.listing(store) == []


def test_only_his_session_makes_an_owner():
    """By construction: an Owner comes from guard/owner.py's makers, and each is called
    in one place only (the precedent, an unfreeze on their own past unfreezes, in
    guard/learn.py). No tool, model path, intercom or Bastet's chat holds one."""
    makers = {"Owner(": set(), "_SEAL": set(), "owner.session(": set(),
              "owner.session_or_none(": set(), "precedent(c": set(), "second_look(c": set()}
    for path, text in _sources():
        for word in makers:
            if word in text:
                makers[word].add(path)
    assert makers == {"Owner(": {"guard/owner.py"}, "_SEAL": {"guard/owner.py"},
                      "owner.session(": {"guard/api.py", "api/approvals.py"},
                      "owner.session_or_none(": {"guard/consult.py"},
                      "precedent(c": {"guard/owner.py", "guard/learn.py"},
                      "second_look(c": {"guard/owner.py", "guard/chain.py"}}


# ---- the guard routes: a logged-in session only ------------------------------------------

async def test_a_token_reaches_the_intercom_only_and_the_box_key_nothing(store, stack_key):
    """A named token is for a host script on the intercom (auth/gate.py): every other
    route and every socket answers it as if it were no credential."""
    from celine.auth.gate import OPEN
    from celine.intercom import tokens
    stack_key("shellbox-llm", "box-key")
    token = await store.write(lambda c: tokens.make(c, "cli"))
    app = _gated_app(store)
    routes = [(m, p.replace("/x", "/1")) for p, r in all_routes(app.routes)
              if p.startswith("/api/") and not p.startswith("/api/intercom") for m in
              (getattr(r, "methods", None) or set()) - {"HEAD"} if (m, p) not in OPEN]
    sockets = [p.replace("/x", "/1") for p, r in all_routes(app.routes)
               if p.startswith("/ws/")]
    assert len(routes) >= 100 and sockets
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost:8766") as c:
        for method, path in routes:
            for key in (token, "box-key"):
                r = await c.request(method, path, headers={"authorization": f"Bearer {key}"},
                                    json={})
                assert r.status_code == 404, (method, path, r.status_code)
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    with TestClient(app, base_url="http://localhost:8766") as tc:
        for path in sockets:
            with pytest.raises(WebSocketDisconnect):
                with tc.websocket_connect(path, headers={
                        "authorization": f"Bearer {token}",
                        "origin": "http://localhost:8766"}) as ws:
                    ws.receive_text()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost:8766") as c:
        # their session counts even when a proxy adds an Authorization header
        from celine.auth import COOKIE
        app.state.auth.store.add_passkey(b"key", b"pub", 0)
        c.cookies.set(COOKIE, app.state.auth.store.new_session(b"key"))
        r = await c.get("/api/guard/freeze", headers={"authorization": "Bearer proxy"})
        assert r.status_code == 200, r.text
    # the login off (no gate): a request that carries a token or a key is no session
    # to the guard
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(store, load())),
                                 base_url="http://localhost:8766") as c:
        for headers in ({"authorization": f"Bearer {token}"}, {"x-api-key": "box-key"}):
            r = await c.delete("/api/guard/freeze", headers=headers)
            assert r.status_code == 403 and r.json()["error"]["code"] == "passkey_only"


def test_no_tool_writes_the_rule_file_the_database_or_bastet():
    """The agent's tools write only below their own roots: the notebook, Documents, attachments
    by hash, its shellbox over HTTP. None names var/settings, the database or the guard
    API, and none calls what changes a freeze, a pass or an approval."""
    tools = [(p, t) for p, t in _sources() if p.startswith(
        ("skills/", "sub_agents/", "intercom/", "missions/", "artifacts/", "browser/",
         "terminals/", "vault/")) or p in ("mcp.py", "box_files.py", "attachments.py")]
    assert len(tools) > 20
    for path, text in tools:
        assert "guard.toml" not in text and "bastet.md" not in text, path
        assert not re.search(r"/api/(guard|approvals)", text), path
        assert not re.search(r"(\bunfreeze|\bpasses\.make|\.decide|\bbanners\.answer)\(",
                             text), path


async def test_her_file_tools_never_reach_the_rule_file_bastet_or_the_database(store,
                                                                              tmp_path):
    """The notebook and Documents resolve a path, symlinks too, and refuse what lands
    outside their root (a `..` is test_notebook's and test_documents')."""
    from celine.kb.documents import DocumentsKB
    from celine.skills import ToolContext
    from celine.skills.notebook import Notebook
    cfg = load()
    settings = cfg.data_dir / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    rule, persona = settings / "guard.toml", settings / "bastet.md"
    rule.write_text("[gates]\n")
    persona.write_text("Bastet\n")
    nb = Notebook(cfg)
    await nb.seed(store)
    root = nb.root
    (root / "out").symlink_to(settings, target_is_directory=True)
    (root / "rule.md").symlink_to(rule)
    ctx = ToolContext("c", "t", "x", store, cfg, None)  # type: ignore[arg-type]

    async def refused(fn, *a) -> bool:
        try:
            return (await fn(ctx, *a)).is_error
        except ValueError:
            return True
    for path in (f"out/{rule.name}", "rule.md", str(persona)):      # a link out, a path out
        assert await refused(nb.write, path, "mine now", "x"), path
        assert await refused(nb.read, path), path
    docs = DocumentsKB(id="documents", title="Documents", root=tmp_path / "files",
                       path=tmp_path / "kb" / "documents.db", options={})
    docs.root.mkdir(parents=True, exist_ok=True)
    (docs.root / "out").symlink_to(settings, target_is_directory=True)
    (docs.root / "rule.md").symlink_to(rule)
    for path in ("out/bastet.md", "rule.md"):
        with pytest.raises(ValueError):
            docs.write(path, "mine now", author="celine")
    assert rule.read_text() == "[gates]\n" and persona.read_text() == "Bastet\n"


# ---- one chokepoint ----------------------------------------------------------------------

# a `.fn(` that calls no tool: the store's write closure, a connector's feed function
NOT_TOOLS = {"core/store.py", "kb/connectors.py"}


def test_every_tool_runs_through_the_chokepoint():
    """A tool's function is called in turns/tools.py, by the jobs it hands a step to
    (jobs/jobs.py, begun only there), and by the app itself in a routine: the note it
    writes with its answer and the partner's read-only reads (schedules/runs.py)."""
    calls = {p for p, t in _sources() if re.search(r"\.fn\(", t)} - NOT_TOOLS
    assert calls == {"turns/tools.py", "jobs/jobs.py", "schedules/runs.py"}
    begun = {p for p, t in _sources() if re.search(r"\bjobs\.begin\(", t)}
    assert begun == {"turns/tools.py"}


async def test_an_agent_cli_counts_as_outside_work(store):
    from celine.guard.sources import agent_read_outside, label
    cid = await conversations.create(store)
    await store.write(lambda c: c.execute("UPDATE conversations SET agent_cli='opencode' "
                                          "WHERE id=?", (cid,)))
    assert await store.read(lambda c: agent_read_outside(c, cid))
    got = label("sub_agents__wait", {}, ToolResult(content="its answer", display={
        "name": "coder", "outside": True}))
    assert got == {"source": "sub-agent coder", "by": None}


async def test_frozen_the_partner_of_a_routine_reads_nothing(store):
    from celine.schedules.runs import _call as partner_call
    ran = []

    async def read(ctx, **kw):
        ran.append(kw)
        return ToolResult(content="a note")
    ctx = SimpleNamespace(store=store)
    spec = SimpleNamespace(fn=read)
    assert await partner_call(spec, ctx, json.dumps({"path": "a.md"})) == "a note"
    await _frozen(store)
    assert (await partner_call(spec, ctx, "{}")).startswith("error: frozen")
    assert ran == [{"path": "a.md"}]
