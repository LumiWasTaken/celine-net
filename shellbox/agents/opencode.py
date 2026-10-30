"""The coding agent backend `opencode`: opencode's headless server in its shellbox.

Every backend in this folder is one file with the same six functions and Problem,
standard library only; shellboxd loads the one CODING_AGENT names (default opencode) and
serves it on /code/* (see shellboxd.py). Another agent is another file here.

    boot(home, env, as_her, llm_url, llm_key)   write its config, start and supervise it
    start(dir, task, model, session, websearch) -> {"session", "state": "running"}
    attach(session)                             -> the shell command that shows it live
    status(session)                             -> {"session", "state", "error"?}
    result(session)                             -> status + "text", "tool_output", "files"
    abort(session)                              -> {"session", "aborted": bool}
    list()                                      -> [{"session", "title", "dir", "updated"}]
    Problem(text, status)                       what a refused call raises; shellboxd
                                                answers {"error": text} with the status
A session is opencode's: its memory of the task and the folder, kept across calls, so a
second start with the same session carries on. `model` is one of its refs from the app's
model proxy (GET {llm_url}/v1/models), passed to opencode as provider "celine", model
<ref>; opencode.json lists every ref the proxy offered at boot, each with the
`limit.context` and `limit.output` opencode budgets and compacts with: the proxy's
`context_length`, and its `max_output_tokens`, else a quarter of the context (room for a
whole file in one answer, three quarters left for the prompt and the session). A model
the proxy does not size gets CONTEXT, and LOG says so. States: running, done, failed
(the agent's own error, or no answer at all: a model it does not know).

opencode (the pinned release binary in the image) runs as it, `opencode serve` on
127.0.0.1:PORT, HTTP basic auth with the password in ~/.config/opencode/server.password
(made once, kept across resets with its home; only the box and it know it). Its config,
~/.config/opencode/opencode.json, is written at every boot: provider "celine" on the
proxy with its models, every permission allowed, one instructions file about short
final answers, and REA (`rea mcp`, the reverse-engineering MCP server in the image) as an
MCP server. The agent's own settings go in ~/.config/opencode/opencode.jsonc (made empty
once, never written again): opencode reads it after opencode.json and its keys win
(its config.ts loadGlobal), and opencode's own config changes land there too. Every
file in its home is written as it (its browser shares ~/.config). The model list is
fetched again every REFRESH seconds; when it or a size changed and no session is busy,
the server restarts on the new list. What the server prints goes to LOG.

Every Problem is opencode's own answer (its HTTP status and its words) or what the box
saw (no answer on PORT, the server's exit code), with LOG named, so it can act on it.
"""
from __future__ import annotations

import base64
import http.client
import json
import secrets
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

NAME = "opencode"
PORT = 4096
URL = f"http://127.0.0.1:{PORT}"
CONTEXT = 32768             # limit.context of a model the proxy does not size
REFRESH = 3600              # seconds between two looks at the proxy's model list
RETRY = 30                  # seconds between tries while the proxy has not answered yet
RESTART_WAIT = 5            # seconds before the server is started again after it exited
TAIL = 4000                 # characters of the tool outputs a result carries, from the end
LOG = Path("/var/lib/shellbox/opencode.log")    # the server's output; gone with the reset
INSTRUCTIONS = ("Keep your final answer short: what you changed and what you ran, in a "
                "few lines. No preamble.\n")

PASSWORD = ""               # read at boot; tests set it
HOME: Path | None = None    # its home, from boot
_state: dict = {"models": None, "proc": None, "as_her": {}, "llm_url": "", "llm_key": "",
               "cfg": None, "websearch": False}


class Problem(Exception):
    """A plain sentence for the app, with its HTTP status (shellboxd answers it)."""

    def __init__(self, text: str, status: int = 400):
        super().__init__(text)
        self.status = status


# ---- boot --------------------------------------------------------------------------------

def boot(home: Path, env: dict, as_her: dict, llm_url: str, llm_key: str) -> None:
    """The agent's config and password, then the server in a thread of its own that restarts it
    whenever it exits."""
    global PASSWORD, HOME
    HOME, _state["as_her"], _state["llm_url"], _state["llm_key"] = home, as_her, llm_url, llm_key
    cfg = _state["cfg"] = home / ".config" / "opencode"
    pw = cfg / "server.password"
    if not pw.is_file() or not pw.read_text().strip():
        _put(pw, secrets.token_urlsafe(24) + "\n", "600")
    PASSWORD = pw.read_text().strip()
    _put(cfg / "celine.md", INSTRUCTIONS)
    if not (cfg / "opencode.jsonc").exists():
        _put(cfg / "opencode.jsonc", '{\n  "$schema": "https://opencode.ai/config.json"\n}\n')
    _write_config(cfg, llm_url, llm_key, _models(llm_url, llm_key))
    threading.Thread(target=_serve, args=(cfg, env, as_her, llm_url, llm_key),
                     daemon=True).start()


def _models(llm_url: str, llm_key: str) -> dict[str, dict] | None:
    """{ref: its limit} from the proxy, None when it did not answer."""
    if not llm_url:
        return {}
    req = urllib.request.Request(f"{llm_url}/v1/models",
                                 headers={"Authorization": f"Bearer {llm_key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return _limits(json.load(r)["data"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _limits(data: list[dict], log=None) -> dict[str, dict]:
    """{ref: its limit} from the proxy's model list; `log` takes the line about a model
    it does not size (LOG by default)."""
    out = {}
    for m in data:
        ctx = m.get("context_length")
        if not isinstance(ctx, int) or ctx <= 0:
            (log or _log)(f"the model proxy names no context for {m['id']}: {CONTEXT} it is")
            ctx = CONTEXT
        out[str(m["id"])] = {"context": ctx, "output": m.get("max_output_tokens") or ctx // 4}
    return out


def _log(line: str) -> None:
    with open(LOG, "a") as f:
        f.write(line + "\n")


def _write_config(cfg: Path, llm_url: str, llm_key: str, models: dict | None) -> None:
    conf = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"celine": {
            "npm": "@ai-sdk/openai-compatible", "name": "Celine",
            "options": {"baseURL": f"{llm_url}/v1", "apiKey": llm_key},
            "models": {m: {"name": m, "limit": limit} for m, limit in (models or {}).items()}}},
        # websearch is Exa's hosted search (a third party), only when the app's setting is on
        "permission": {"*": "allow", "websearch": "allow"} if _state["websearch"] else "allow",
        "instructions": [str(cfg / "celine.md")],
        "mcp": {"rea": {"type": "local", "command": ["rea", "mcp"], "enabled": True}},
        "autoupdate": False,
    }
    _put(cfg / "opencode.json", json.dumps(conf, indent=1) + "\n")
    _state["models"] = models


def _put(path: Path, text: str, mode: str = "644") -> None:
    """A file of its own, folders on the way too, written by it (as shellboxd's WRITE_SH):
    its browser shares this home, and a root-owned ~/.config kills Brave."""
    subprocess.run(["sh", "-c", 'mkdir -p -- "$(dirname -- "$1")" && (umask 077 && '
                    'cat > "$1") && chmod "$2" -- "$1"', "sh", str(path), mode],
                   input=text.encode(), check=True, **_state["as_her"])


def _serve(cfg: Path, env: dict, as_her: dict, llm_url: str, llm_key: str) -> None:
    """Runs `opencode serve` as it, again after every exit (said in LOG); refreshes the
    model list while it runs."""
    looked = time.monotonic()
    while True:
        with open(LOG, "ab") as out:
            proc = subprocess.Popen(
                ["opencode", "serve", "--hostname", "127.0.0.1", "--port", str(PORT)],
                cwd=str(HOME), env={**env, "OPENCODE_SERVER_PASSWORD": PASSWORD,
                     **({"OPENCODE_ENABLE_EXA": "1"} if _state["websearch"] else {})},
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **as_her)
        _state["proc"] = proc
        while True:
            due = REFRESH if _state["models"] else RETRY      # none yet: a fresh instance
            try:
                proc.wait(timeout=max(looked + due - time.monotonic(), 0))
                break
            except subprocess.TimeoutExpired:
                pass
            looked = time.monotonic()
            models = _models(llm_url, llm_key)
            if models is not None and models != _state["models"] and not _busy():
                _write_config(cfg, llm_url, llm_key, models)
                proc.terminate()          # the loop starts it again on the new list
        _log(f"opencode serve exited with {proc.returncode}; again in {RESTART_WAIT} s")
        time.sleep(RESTART_WAIT)


def _busy() -> bool:
    try:
        return bool(_call("GET", "/session/status"))
    except Problem:
        return False


# ---- the agent over HTTP ------------------------------------------------------------------
# Every function below talks to the box's own server, or to `server`: an `opencode serve`
# on a device (the app's drones, server/celine/sub_agents/drone.py, load this file: it is
# the one opencode client).

class Server:
    """An `opencode serve` elsewhere: its address, its password, where its log is."""

    def __init__(self, url: str, password: str, log: str):
        self.url, self.password, self.log = url.rstrip("/"), password, log


def _request(method: str, path: str, body: dict | None, query: str,
             server: Server | None) -> urllib.request.Request:
    url, pw = (server.url, server.password) if server else (URL, PASSWORD)
    data = json.dumps(body).encode() if body is not None else None
    auth = base64.b64encode(f"opencode:{pw}".encode()).decode()
    return urllib.request.Request(url + path + (f"?{query}" if query else ""), data=data,
                                  method=method, headers={"Authorization": f"Basic {auth}",
                                                          "Content-Type": "application/json"})


def _log_of(server: Server | None) -> str:
    return server.log if server else str(LOG)


def _call(method: str, path: str, body: dict | None = None, query: str = "",
          timeout: float = 30, server: Server | None = None) -> object:
    try:
        with urllib.request.urlopen(_request(method, path, body, query, server),
                                    timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        said = _said(e.read())
        if e.code == 404:
            raise Problem(f"No such session ({NAME} answered 404: {said}).", 404) from None
        raise Problem(f"{NAME} answered {e.code}: {said}. Its log: {_log_of(server)}.",
                      502) from None
    except OSError as e:
        raise Problem(_down(e, server), 503) from None
    try:
        return json.loads(raw) if raw.strip() else None
    except ValueError:
        raise Problem(f"{NAME} answered {path} with something that is not JSON: "
                      f"{_said(raw)}. Its log: {_log_of(server)}.", 502) from None


def events(server: Server | None = None, timeout: float = 60):
    """opencode's event stream (GET /event, server-sent events) -> (a function that
    ends it from any thread, a generator of its events as dicts). No line for `timeout`
    seconds raises TimeoutError."""
    req = _request("GET", "/event", None, "", server)
    conn = (http.client.HTTPSConnection if req.type == "https" else
            http.client.HTTPConnection)(req.host, timeout=timeout)
    try:
        conn.request("GET", req.selector, headers=dict(req.header_items()))
        sock = conn.sock        # the response keeps it; the connection may let go of it
        r = conn.getresponse()
    except OSError as e:
        raise Problem(_down(e, server), 503) from None
    if r.status != 200:
        raise Problem(f"{NAME} answered {r.status} on /event: {_said(r.read())}. "
                      f"Its log: {_log_of(server)}.", 502)

    def end() -> None:
        try:                  # a shutdown, not a close: a read in another thread holds the
            sock.shutdown(socket.SHUT_RDWR)             # response's lock
        except OSError:
            pass

    def each():
        data: list[str] = []
        for raw in r:
            line = raw.decode(errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and data:
                try:
                    yield json.loads("\n".join(data))
                except ValueError:
                    pass
                data = []
    return end, each()


def _said(raw: bytes) -> str:
    """opencode's own words in an answer: its error's name and message, else the text."""
    text = raw.decode(errors="replace").strip()
    try:
        err = json.loads(text)
    except ValueError:
        return text[:300] or "an empty answer"
    if isinstance(err, dict) and (err.get("name") or err.get("data")):
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        return ": ".join(str(x) for x in (err.get("name"), data.get("message")) if x)[:300]
    return text[:300]


def _down(e: OSError, server: Server | None = None) -> str:
    """No answer on PORT: what the box knows of the server, and what it can do."""
    if server is not None:
        return (f"{NAME} is not answering on {server.url} "
                f"({getattr(e, 'reason', None) or e}). Its log: {server.log}.")
    proc = _state["proc"]
    if proc is not None and proc.poll() is not None:
        now = (f"`opencode serve` exited with {proc.returncode}; the box starts it again "
               f"in {RESTART_WAIT} s")
    else:
        now = "it is starting, restarting or hung"
    return (f"{NAME} is not answering on {URL} ({getattr(e, 'reason', None) or e}): {now}. "
            f"Try again in a minute; if it stays down, its log says why: tail -50 {LOG}.")


def _dir(rel: str) -> str:
    # ~ is its home, not root's: this runs in the daemon
    p = Path(rel) if rel.startswith("/") else HOME / rel.removeprefix("~").lstrip("/")
    if not p.is_dir():
        raise Problem(f"No folder {rel} in the home folder.")
    return str(p)


def _q(folder: str) -> str:
    return "directory=" + urllib.request.quote(folder, safe="")


def start(dir: str, task: str, model: str, session: str | None = None,
          websearch: bool = False) -> dict:
    """A new session in `dir`, or the given one, told `task`; it works in the background.
    `websearch` is the app's setting: when it changed, the server restarts with it."""
    folder = _dir(dir)
    if not task.strip():
        raise Problem("The task is empty.")
    if not model.strip():
        raise Problem("Name the model: one of the refs from the model proxy.")
    if _state["models"] is None:
        raise Problem(f"{NAME} has no models yet: the app's model proxy "
                      f"({_state['llm_url']}/v1/models) has not answered the box since it "
                      f"started. The box asks again every {RETRY} s.", 503)
    if bool(websearch) != _state["websearch"]:
        if _busy():
            raise Problem(f"The web search setting changed and {NAME} restarts on it, but a "
                          f"session is working; start again when none is.", 409)
        _state["websearch"] = bool(websearch)
        _write_config(_state["cfg"], _state["llm_url"], _state["llm_key"], _state["models"])
        _state["proc"].terminate()         # _serve starts it again with the new environment
        raise Problem(f"The web search setting changed: {NAME} restarts on it. "
                      f"Start again in {RESTART_WAIT + 5} s.", 503)
    if model not in _state["models"]:
        models = _models(_state["llm_url"], _state["llm_key"])
        models = models or _state["models"]
        if models and model in models and not _busy():
            _write_config(_state["cfg"], _state["llm_url"], _state["llm_key"], models)
            _state["proc"].terminate()     # _serve starts it again on the new list
            raise Problem(f"{model} is new to {NAME}: it restarts on the new model list. "
                          f"Start again in {RESTART_WAIT + 5} s.", 503)
        if not models:
            raise Problem("The app has no chat model with a key yet: the user adds one in "
                          "Settings, Models.", 503)
        raise Problem(f"{model} is not one of the models. The models: {', '.join(models)}.")
    if session:
        info = _call("GET", f"/session/{session}", query=_q(folder))
        if _busy_one(session):
            raise Problem(f"Session {session} is still working; wait, or abort it.", 409)
        folder = info.get("directory") or folder
    else:
        session = new_session(task, _q(folder))
    prompt(session, model, task, _q(folder))
    return {"session": session, "state": "running", "dir": folder, "model": model}


def attach(session: str) -> str:
    """The command that shows a session live (`opencode attach`); the password goes by
    the environment, read from its file, so it is never in the command line."""
    return (f"OPENCODE_SERVER_PASSWORD=$(cat {shlex.quote(str(_state['cfg'] / 'server.password'))})"
            f" opencode attach {URL} --session {shlex.quote(session)}")


def new_session(task: str, query: str = "", server: Server | None = None) -> str:
    """A new session titled with the task's first line -> its id."""
    return _call("POST", "/session", {"title": task.strip().splitlines()[0][:80]},
                 query=query, server=server)["id"]


def prompt(session: str, model: str, task: str, query: str = "",
           server: Server | None = None, system: str | None = None) -> None:
    """`task` to the session on the proxy's `model`; it works in the background. A busy
    session takes it into the work it is on. `system`: the message's system prompt,
    after opencode's own (its prompt body's `system`)."""
    body = {"model": {"providerID": "celine", "modelID": model},
            "parts": [{"type": "text", "text": task}]}
    if system:
        body["system"] = system
    _call("POST", f"/session/{session}/prompt_async", body, query=query, server=server)


def _busy_one(session: str, server: Server | None = None) -> bool:
    st = _call("GET", "/session/status", server=server) or {}
    return (st.get(session) or {}).get("type") in ("busy", "retry")


def _messages(session: str, server: Server | None = None, limit: int | None = 20) -> list[dict]:
    return _call("GET", f"/session/{session}/message", query=f"limit={limit}" if limit else "",
                 server=server) or []


def status(session: str, server: Server | None = None) -> dict:
    """running while opencode works on it; done when its last answer is complete;
    failed with its error, or when it never answered the last task."""
    info = _call("GET", f"/session/{session}", server=server)
    out = {"session": session, "agent": NAME, "dir": info.get("directory"),
           "title": info.get("title"), "updated": (info.get("time") or {}).get("updated")}
    if _busy_one(session, server):
        return {**out, "state": "running"}
    msgs = _messages(session, server)
    last = msgs[-1]["info"] if msgs else {}
    if last.get("role") != "assistant":
        return {**out, "state": "failed" if msgs else "done",
                "error": f"No answer came: {NAME} took the task, but the model never "
                         f"answered and {NAME} kept no error. Is the model one of the proxy's, and "
                         f"is the app's model proxy up? Its log: {_log_of(server)}." if msgs else None}
    err = last.get("error")
    if err:
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        text = ": ".join(str(x) for x in (err.get("name"), data.get("message")) if x)
        if data.get("statusCode"):
            text += f" (the model's server answered {data['statusCode']})"
        return {**out, "state": "failed", "error": (text or json.dumps(err))[:500]}
    return {**out, "state": "done" if (last.get("time") or {}).get("completed")
            else "running"}


def result(session: str, server: Server | None = None) -> dict:
    """The status, the last assistant text, the tail of its tool outputs and the files
    the session changed (opencode's diff)."""
    out = status(session, server)
    text, tools = "", []
    for m in reversed(_messages(session, server)):
        if m["info"].get("role") != "assistant":
            continue
        for p in m.get("parts") or []:
            if p.get("type") == "text" and p.get("text"):
                text = p["text"]
            elif p.get("type") == "tool":
                tools.append(str((p.get("state") or {}).get("output") or ""))
        if text or tools:
            break
    files, files_error = [], None
    try:
        diff = _call("GET", f"/session/{session}/diff", server=server) or []
        files = [{"path": d.get("path"), "status": d.get("status"),
                  "additions": d.get("additions"), "deletions": d.get("deletions")}
                 for d in diff if isinstance(d, dict)]
    except Problem as e:
        files_error = str(e)
    return {**out, "text": text, "tool_output": "\n".join(tools)[-TAIL:], "files": files,
            **({"files_error": files_error} if files_error else {})}


def abort(session: str, server: Server | None = None) -> dict:
    ok = _call("POST", f"/session/{session}/abort", server=server)
    return {"session": session, "aborted": bool(ok)}


def list() -> list[dict]:          # noqa: A001 - the interface's name
    return [{"session": s["id"], "title": s.get("title"), "dir": s.get("directory"),
             "updated": (s.get("time") or {}).get("updated")}
            for s in _call("GET", "/session") or [] if isinstance(s, dict)]
