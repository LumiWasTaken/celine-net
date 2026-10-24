"""shellboxd's /code/* routes and the opencode backend (shellbox/agents/opencode.py)
against a fake opencode server: what the box sends it, with its password, and the facts it
hands the app. No docker, no tmux: only the HTTP handler."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import threading
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
types_ns = types.SimpleNamespace


def box_agent():
    spec = importlib.util.spec_from_file_location("oc_w", ROOT / "shellbox" / "agents" / "opencode.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m
KEY = "test-key-" + "k" * 16
PW = "opencode-pw"
NOW = 1792779829922


class FakeOpencode(BaseHTTPRequestHandler):
    """Answers like opencode serve: sessions, status, messages, diff, abort."""
    seen: list[tuple[str, str, dict | None]] = []
    busy: set[str] = set()
    messages: dict[str, list[dict]] = {}
    sessions: dict[str, dict] = {}
    diff: list[dict] = []
    broken: dict | None = None      # GET /session answers 500 with this

    def _auth(self) -> bool:
        want = "Basic " + base64.b64encode(f"opencode:{PW}".encode()).decode()
        if self.headers.get("Authorization") == want:
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
        FakeOpencode.seen.append(("GET", self.path, None))
        parts = path.strip("/").split("/")
        if parts == ["session", "status"]:
            return self._json({s: {"type": "busy"} for s in self.busy})
        if parts == ["session"]:
            if self.broken:
                return self._json(self.broken, 500)
            return self._json(list(self.sessions.values()))
        sid = parts[1]
        if sid not in self.sessions:
            return self._json({"name": "NotFoundError", "data": {"message": "no"}}, 404)
        if len(parts) == 2:
            return self._json(self.sessions[sid])
        if parts[2] == "message":
            return self._json(self.messages.get(sid, []))
        if parts[2] == "diff":
            return self._json(self.diff)
        self._json({}, 404)

    def do_POST(self):
        if not self._auth():
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw or b"{}")
        FakeOpencode.seen.append(("POST", self.path, body))
        path = self.path.partition("?")[0]
        parts = path.strip("/").split("/")
        if parts == ["session"]:
            sid = f"ses_{len(self.sessions) + 1}"
            d = self.path.partition("directory=")[2]
            self.sessions[sid] = {"id": sid, "title": body.get("title"),
                                  "directory": urllib.request.unquote(d),
                                  "time": {"created": NOW, "updated": NOW}}
            return self._json(self.sessions[sid])
        sid = parts[1]
        if sid not in self.sessions:
            return self._json({"name": "NotFoundError"}, 404)
        if parts[2] == "prompt_async":
            self.busy.add(sid)
            self.messages.setdefault(sid, []).append(
                {"info": {"role": "user", "time": {"created": NOW}},
                 "parts": [{"type": "text", "text": body["parts"][0]["text"]}]})
            self.send_response(204)
            self.end_headers()
            return
        if parts[2] == "abort":
            was = sid in self.busy
            self.busy.discard(sid)
            return self._json(was)
        self._json({}, 404)

    def log_message(self, *args):
        pass


def answered(sid: str, text: str, error: dict | None = None, tools: list[str] = ()) -> None:
    """The fake's session `sid` got an assistant answer (done, or failed with `error`)."""
    FakeOpencode.busy.discard(sid)
    info = {"role": "assistant", "time": {"created": NOW, "completed": NOW + 500}}
    if error:
        info["error"] = error
    parts = [{"type": "step-start"}]
    parts += [{"type": "tool", "tool": "bash", "state": {"status": "completed", "output": t}}
              for t in tools]
    if text:
        parts.append({"type": "text", "text": text})
    FakeOpencode.messages.setdefault(sid, []).append({"info": info, "parts": parts})


@pytest.fixture
def box(tmp_path):
    FakeOpencode.seen, FakeOpencode.busy = [], set()
    FakeOpencode.messages, FakeOpencode.sessions, FakeOpencode.diff = {}, {}, []
    FakeOpencode.broken = None
    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpencode)
    fake.daemon_threads = True
    threading.Thread(target=fake.serve_forever, args=(0.01,), daemon=True).start()
    spec = importlib.util.spec_from_file_location("shellboxd_c", ROOT / "shellbox" / "shellboxd.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    d.KEY_HASH = hashlib.sha256(KEY.encode()).hexdigest()
    d.AGENTS_DIR = ROOT / "shellbox" / "agents"
    agent = d.load_agent("opencode")
    agent.URL = f"http://127.0.0.1:{fake.server_address[1]}"
    agent.PASSWORD = PW
    agent.HOME = tmp_path
    agent._state["models"] = dict.fromkeys(["local/small", "openrouter/anthropic/claude-opus-5.5"], {})
    (tmp_path / "proj").mkdir()
    d.CODE = agent
    d.watch_session = lambda session: {}        # its terminal needs tmux: tested below
    d.STATUS["code"] = {"agent": "opencode", "error": None}
    server = ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()

    class Box:
        daemon, home, port = d, tmp_path, server.server_address[1]

        def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}{path}", method=method,
                data=None if body is None else json.dumps(body).encode(),
                headers={"Authorization": f"Bearer {KEY}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
    yield Box()
    server.shutdown()
    fake.shutdown()


def test_start_opens_a_session_in_her_folder_and_prompts_it_with_the_model(box):
    code, res = box.call("POST", "/code/session", {"dir": "proj", "task": "fix the tests",
                                                   "model": "openrouter/anthropic/claude-opus-5.5"})
    assert code == 200 and res == {"session": "ses_1", "state": "running",
                                   "dir": str(box.home / "proj"),
                                   "model": "openrouter/anthropic/claude-opus-5.5"}
    create = next(s for s in FakeOpencode.seen if s[0] == "POST" and s[1].startswith("/session?"))
    assert create[1] == "/session?directory=" + urllib.request.quote(str(box.home / "proj"), safe="")
    assert create[2] == {"title": "fix the tests"}
    prompt = next(s for s in FakeOpencode.seen if "prompt_async" in s[1])
    assert prompt[2] == {"model": {"providerID": "celine",
                                   "modelID": "openrouter/anthropic/claude-opus-5.5"},
                         "parts": [{"type": "text", "text": "fix the tests"}]}
    code, res = box.call("GET", "/code/session/ses_1")
    assert code == 200 and res["state"] == "running" and res["agent"] == "opencode"
    assert res["dir"] == str(box.home / "proj") and res["title"] == "fix the tests"


def test_the_attach_command_shows_a_session_live_without_the_password_in_argv(box):
    agent = box.daemon.CODE
    agent._state["cfg"] = box.home / ".config" / "opencode"
    cmd = agent.attach("ses_1")
    assert cmd.endswith(f"opencode attach {agent.URL} --session ses_1")
    assert cmd.startswith("OPENCODE_SERVER_PASSWORD=$(cat ") and PW not in cmd


def test_a_session_gets_one_terminal_named_after_it_and_reuses_it(box, monkeypatch):
    typed, open_now = [], set()
    spec = importlib.util.spec_from_file_location("shellboxd_w", ROOT / "shellbox" / "shellboxd.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    w.CODE = types_ns(attach=lambda s: f"attach {s}")
    w.exists = lambda n: n in open_now
    w.type_call = lambda b: (typed.append(b), open_now.add(b["terminal"]))
    w.known = lambda n: types_ns(id="abc123")
    assert w.watch_session("ses_2fA9-b3c1d4e5") == {"terminal": "code-b3c1d4e5",
                                                    "terminal_id": "abc123"}
    assert typed == [{"terminal": "code-b3c1d4e5", "text": "attach ses_2fA9-b3c1d4e5",
                      "owner": "celine"}]
    w.watch_session("ses_2fA9-b3c1d4e5")
    assert len(typed) == 1


def test_websearch_changes_restart_opencode_and_allow_the_tool(box, monkeypatch):
    agent = box.daemon.CODE
    agent._state["cfg"] = box.home / ".config" / "opencode"
    stopped, written = [], []
    agent._state["proc"] = types_ns(terminate=lambda: stopped.append(1))
    monkeypatch.setattr(agent, "_write_config", lambda *a: written.append(agent._state["websearch"]))
    body = {"dir": "proj", "task": "x", "model": "local/small", "websearch": True}
    code, res = box.call("POST", "/code/session", body)
    assert code == 503 and "web search setting changed" in res["error"]
    assert stopped == [1] and written == [True]
    code, res = box.call("POST", "/code/session", body)      # restarted: no change now
    assert code == 200 and stopped == [1]


def test_the_config_allows_websearch_only_when_it_is_on(tmp_path, monkeypatch):
    agent = box_agent()
    monkeypatch.setattr(agent, "_put", lambda path, text, mode="644": written.append(text))
    written: list[str] = []
    agent._state["cfg"] = tmp_path
    for on, want in ((False, "allow"), (True, {"*": "allow", "websearch": "allow"})):
        agent._state["websearch"] = on
        agent._write_config(tmp_path, "http://x", "k", {})
        assert json.loads(written[-1])["permission"] == want


def test_a_missing_folder_an_empty_task_and_an_unknown_model_are_refused(box):
    assert box.call("POST", "/code/session", {"dir": "nope", "task": "x", "model": "local/small"}) \
        == (400, {"error": "No folder nope in the home folder."})
    assert box.call("POST", "/code/session", {"dir": "proj", "task": " ", "model": "local/small"}) \
        == (400, {"error": "The task is empty."})
    code, res = box.call("POST", "/code/session", {"dir": "proj", "task": "x", "model": "m/x"})
    assert (code, res["error"]) == (400, "m/x is not one of the models. The models: local/small, "
                                         "openrouter/anthropic/claude-opus-5.5.")
    code, res = box.call("POST", "/code/session", {"dir": "proj", "task": "x", "model": ""})
    assert code == 400 and res["error"].startswith("Name the model")
    assert not any("prompt_async" in s[1] for s in FakeOpencode.seen)


def test_a_model_added_after_the_box_started_restarts_opencode_on_it(box, monkeypatch):
    agent = box.daemon.CODE
    agent._state["models"] = {}
    code, res = box.call("POST", "/code/session", {"dir": "proj", "task": "x", "model": "p/new"})
    assert code == 503 and "Settings, Models" in res["error"]
    written, stopped = [], []
    monkeypatch.setattr(agent, "_models", lambda url, key: {"p/new": {}})
    monkeypatch.setattr(agent, "_write_config", lambda *a: written.append(a[-1]))
    agent._state["proc"] = type("P", (), {"terminate": lambda self: stopped.append(1)})()
    code, res = box.call("POST", "/code/session", {"dir": "proj", "task": "x", "model": "p/new"})
    assert code == 503 and "restarts on the new model list" in res["error"]
    assert written == [{"p/new": {}}] and stopped == [1]


def test_a_second_start_carries_on_in_the_session_unless_it_is_busy(box):
    box.call("POST", "/code/session", {"dir": "proj", "task": "a", "model": "local/small"})
    code, res = box.call("POST", "/code/session", {"dir": "proj", "task": "b",
                                                   "model": "local/small", "session": "ses_1"})
    assert (code, res["error"]) == (409, "Session ses_1 is still working; wait, or abort it.")
    answered("ses_1", "done a")
    code, res = box.call("POST", "/code/session", {"dir": "proj", "task": "b",
                                                   "model": "local/small", "session": "ses_1"})
    assert code == 200 and res["session"] == "ses_1"
    assert len(FakeOpencode.sessions) == 1
    assert [s[2]["parts"][0]["text"] for s in FakeOpencode.seen if "prompt_async" in s[1]] == ["a", "b"]


def test_status_and_result_follow_the_last_answer(box):
    box.call("POST", "/code/session", {"dir": "proj", "task": "a", "model": "local/small"})
    answered("ses_1", "Changed p.py; 3 passed.", tools=["$ pytest\n3 passed in 0.2s"])
    FakeOpencode.diff = [{"path": "p.py", "status": "modified", "additions": 2, "deletions": 1,
                          "patch": "..."}]
    code, res = box.call("GET", "/code/session/ses_1")
    assert code == 200 and res["state"] == "done" and res.get("error") is None
    code, res = box.call("GET", "/code/session/ses_1/result")
    assert code == 200
    assert res["text"] == "Changed p.py; 3 passed." and res["state"] == "done"
    assert res["tool_output"] == "$ pytest\n3 passed in 0.2s"
    assert res["files"] == [{"path": "p.py", "status": "modified", "additions": 2, "deletions": 1}]
    # a failed answer
    box.call("POST", "/code/session", {"dir": "proj", "task": "b", "model": "local/small"})
    answered("ses_2", "", error={"name": "MessageAbortedError", "data": {"message": "Aborted"}})
    code, res = box.call("GET", "/code/session/ses_2")
    assert (res["state"], res["error"]) == ("failed", "MessageAbortedError: Aborted")
    # the model never answered: idle with its task last
    box.call("POST", "/code/session", {"dir": "proj", "task": "c", "model": "local/small"})
    FakeOpencode.busy.discard("ses_3")
    code, res = box.call("GET", "/code/session/ses_3")
    assert res["state"] == "failed" and res["error"].startswith("No answer came")


def test_abort_list_and_unknown_sessions(box):
    box.call("POST", "/code/session", {"dir": "proj", "task": "a", "model": "local/small"})
    assert box.call("POST", "/code/session/ses_1/abort", {}) == (200, {"session": "ses_1",
                                                                       "aborted": True})
    assert box.call("POST", "/code/session/ses_1/abort", {}) == (200, {"session": "ses_1",
                                                                       "aborted": False})
    code, res = box.call("GET", "/code/sessions")
    assert code == 200 and res == {"sessions": [{"session": "ses_1", "title": "a",
                                                 "dir": str(box.home / "proj"), "updated": NOW}]}
    assert box.call("GET", "/code/session/ses_9") == (
        404, {"error": "No such session (opencode answered 404: NotFoundError: no)."})
    assert box.call("GET", "/code/nothing") == (404, {"error": "No such endpoint."})


def test_without_a_backend_every_code_call_says_why(box):
    box.daemon.CODE = None
    box.daemon.STATUS["code"] = {"agent": "nope", "error": "FileNotFoundError: nope.py"}
    assert box.call("GET", "/code/sessions") == (503, {"error": (
        "The coding agent (opencode) did not start in the box: FileNotFoundError: nope.py. "
        "`sudo reboot` in a terminal starts the box and it again.")})


def test_opencode_errors_come_back_in_its_own_words_with_its_log(box):
    """A crashed server says so with its exit code; an HTTP error carries opencode's
    status and words; a failed diff is said, not dropped; no models yet is said."""
    agent = box.daemon.CODE
    box.call("POST", "/code/session", {"dir": "proj", "task": "a", "model": "local/small"})
    answered("ses_1", "done")
    real = agent._call

    def broken(method, path, body=None, query="", timeout=30):
        if path.endswith("/diff"):
            raise agent.Problem("opencode answered 500: UnknownError: disk full. Its log: "
                                f"{agent.LOG}.", 502)
        return real(method, path, body, query, timeout)
    agent._call = broken
    code, res = box.call("GET", "/code/session/ses_1/result")
    assert code == 200 and res["files"] == [] and "disk full" in res["files_error"]
    agent._call = real

    gone = type("P", (), {"poll": lambda self: 1, "returncode": 1})()
    agent._state["proc"], agent.URL = gone, "http://127.0.0.1:9"
    code, res = box.call("GET", "/code/sessions")
    assert code == 503 and "`opencode serve` exited with 1" in res["error"]
    assert str(agent.LOG) in res["error"]

    agent._state["models"], agent._state["llm_url"] = None, "http://celine:8766/llm"
    code, res = box.call("POST", "/code/session", {"dir": "proj", "task": "a",
                                                   "model": "local/small"})
    assert code == 503 and "http://celine:8766/llm/v1/models" in res["error"]


def test_an_http_error_of_opencode_is_its_status_and_words(box):
    FakeOpencode.broken = {"name": "ProviderInitError", "data": {"message": "no provider"}}
    assert box.call("GET", "/code/sessions") == (502, {"error": (
        f"opencode answered 500: ProviderInitError: no provider. Its log: "
        f"{box.daemon.CODE.LOG}.")})


def test_the_boot_writes_her_config_and_password_once(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("opencode_t", ROOT / "shellbox" / "agents" / "opencode.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    monkeypatch.setattr(agent, "_models", lambda url, key: {
        "a/b": {"context": 262144, "output": 65536}, "default": {"context": 32768, "output": 8192}})
    monkeypatch.setattr(agent.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    home = tmp_path / "home"
    home.mkdir()
    agent.boot(home, {}, {}, "http://celine:8766/llm", "llmkey")
    cfg = home / ".config" / "opencode"
    pw = (cfg / "server.password").read_text().strip()
    assert len(pw) >= 24 and agent.PASSWORD == pw
    conf = json.loads((cfg / "opencode.json").read_text())
    assert conf["provider"]["celine"]["options"] == {"baseURL": "http://celine:8766/llm/v1",
                                                     "apiKey": "llmkey"}
    assert conf["provider"]["celine"]["npm"] == "@ai-sdk/openai-compatible"
    assert conf["provider"]["celine"]["models"] == {
        "a/b": {"name": "a/b", "limit": {"context": 262144, "output": 65536}},
        "default": {"name": "default", "limit": {"context": 32768, "output": 8192}}}
    assert conf["permission"] == "allow" and conf["instructions"] == [str(cfg / "celine.md")]
    assert conf["mcp"] == {"rea": {"type": "local", "command": ["rea", "mcp"], "enabled": True}}
    assert "short" in (cfg / "celine.md").read_text()
    mine = '{\n  "provider": {"celine": {"models": {"a/b": {"limit": {"output": 1000}}}}}\n}\n'
    (cfg / "opencode.jsonc").write_text(mine)
    agent.boot(home, {}, {}, "http://celine:8766/llm", "llmkey")
    assert (cfg / "server.password").read_text().strip() == pw, "kept across boots"
    assert (cfg / "opencode.jsonc").read_text() == mine, "its settings survive the boot"
    agent._write_config(cfg, "http://celine:8766/llm", "llmkey", {})
    assert (cfg / "opencode.jsonc").read_text() == mine, "and a new model list"


def test_the_limits_are_the_proxys_sizes(tmp_path, monkeypatch):
    """limit.context is the proxy's context_length; limit.output its max_output_tokens,
    else a quarter of the context. A model it does not size gets CONTEXT, said in LOG."""
    spec = importlib.util.spec_from_file_location("opencode_t4", ROOT / "shellbox" / "agents" / "opencode.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    monkeypatch.setattr(agent, "LOG", tmp_path / "opencode.log")
    assert agent._limits([
        {"id": "q", "context_length": 262144},
        {"id": "c", "context_length": 200000, "max_output_tokens": 64000},
        {"id": "x"}]) == {
        "q": {"context": 262144, "output": 65536},
        "c": {"context": 200000, "output": 64000},
        "x": {"context": agent.CONTEXT, "output": agent.CONTEXT // 4}}
    assert "no context for x" in (tmp_path / "opencode.log").read_text()


def test_boot_writes_every_file_as_her(tmp_path, monkeypatch):
    """The agent's browser shares this home: nothing in ~/.config may be root's, so every file
    (and the folders on the way) is made by it, never chowned after."""
    spec = importlib.util.spec_from_file_location("opencode_t2", ROOT / "shellbox" / "agents" / "opencode.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    real, seen = agent.subprocess.run, []

    def run(cmd, **k):                  # as it in the box; here as whoever runs the test
        seen.append((cmd[0], k.pop("user", None), k.pop("group", None)))
        k.pop("extra_groups", None)
        return real(cmd, **k)
    monkeypatch.setattr(agent, "_models", lambda url, key: [])
    monkeypatch.setattr(agent.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(agent.subprocess, "run", run)
    home = tmp_path / "home"
    home.mkdir()
    agent.boot(home, {}, {"user": "celine", "group": "celine", "extra_groups": []}, "", "")
    assert seen == [("sh", "celine", "celine")] * 4   # password, celine.md, its own, config
    assert (home / ".config" / "opencode" / "server.password").stat().st_mode & 0o777 == 0o600


def test_a_folder_under_tilde_is_in_her_home_not_roots(tmp_path):
    spec = importlib.util.spec_from_file_location("opencode_t3", ROOT / "shellbox" / "agents" / "opencode.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    agent.HOME = tmp_path
    (tmp_path / "site").mkdir()
    assert agent._dir("~/site") == agent._dir("site") == str(tmp_path / "site")
    assert agent._dir("~") == str(tmp_path)
