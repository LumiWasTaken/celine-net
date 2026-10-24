"""The coding agent backend `opencode`: opencode's headless server in its shellbox.

Every backend in this folder is one file with the same six functions and Problem,
standard library only; shellboxd loads the one CODING_AGENT names (default opencode) and
serves it on /code/* (see shellboxd.py). Another agent is another file here.

    boot(home, env, as_her, llm_url, llm_key)   write its config, start and supervise it
    start(dir, task, model, session)            -> {"session", "state": "running"}
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
import json
import secrets
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
_state: dict = {"models": None, "proc": None, "as_her": {}, "llm_url": ""}


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
    HOME, _state["as_her"], _state["llm_url"] = home, as_her, llm_url
    cfg = home / ".config" / "opencode"
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


def _limits(data: list[dict]) -> dict[str, dict]:
    out = {}
    for m in data:
        ctx = m.get("context_length")
        if not isinstance(ctx, int) or ctx <= 0:
            _log(f"the model proxy names no context for {m['id']}: {CONTEXT} it is")
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
        "permission": "allow",
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
                cwd=str(HOME), env={**env, "OPENCODE_SERVER_PASSWORD": PASSWORD},
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **as_her)
        _state["proc"] = proc
        while True:
            due = RETRY if _state["models"] is None else REFRESH
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

def _call(method: str, path: str, body: dict | None = None, query: str = "",
          timeout: float = 30) -> object:
    data = json.dumps(body).encode() if body is not None else None
    auth = base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
    req = urllib.request.Request(URL + path + (f"?{query}" if query else ""), data=data,
                                 method=method, headers={"Authorization": f"Basic {auth}",
                                                         "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        said = _said(e.read())
        if e.code == 404:
            raise Problem(f"No such session ({NAME} answered 404: {said}).", 404) from None
        raise Problem(f"{NAME} answered {e.code}: {said}. Its log: {LOG}.", 502) from None
    except OSError as e:
        raise Problem(_down(e), 503) from None
    try:
        return json.loads(raw) if raw.strip() else None
    except ValueError:
        raise Problem(f"{NAME} answered {path} with something that is not JSON: "
                      f"{_said(raw)}. Its log: {LOG}.", 502) from None


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


def _down(e: OSError) -> str:
    """No answer on PORT: what the box knows of the server, and what it can do."""
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


def start(dir: str, task: str, model: str, session: str | None = None) -> dict:
    """A new session in `dir`, or the given one, told `task`; it works in the background."""
    folder = _dir(dir)
    if not task.strip():
        raise Problem("The task is empty.")
    if not model.strip():
        raise Problem("Name the model: one of the refs from the model proxy.")
    if _state["models"] is None:
        raise Problem(f"{NAME} has no models yet: the app's model proxy "
                      f"({_state['llm_url']}/v1/models) has not answered the box since it "
                      f"started. The box asks again every {RETRY} s.", 503)
    if model not in _state["models"]:
        raise Problem(f"{model} is not one of the models. The models: "
                      f"{', '.join(_state['models']) or 'none yet'}.")
    if session:
        info = _call("GET", f"/session/{session}", query=_q(folder))
        if _busy_one(session):
            raise Problem(f"Session {session} is still working; wait, or abort it.", 409)
        folder = info.get("directory") or folder
    else:
        info = _call("POST", "/session", {"title": task.strip().splitlines()[0][:80]},
                     query=_q(folder))
        session = info["id"]
    _call("POST", f"/session/{session}/prompt_async",
          {"model": {"providerID": "celine", "modelID": model},
           "parts": [{"type": "text", "text": task}]}, query=_q(folder))
    return {"session": session, "state": "running", "dir": folder, "model": model}


def _busy_one(session: str) -> bool:
    st = _call("GET", "/session/status") or {}
    return (st.get(session) or {}).get("type") in ("busy", "retry")


def _messages(session: str) -> list[dict]:
    return _call("GET", f"/session/{session}/message", query="limit=20") or []


def status(session: str) -> dict:
    """running while opencode works on it; done when its last answer is complete;
    failed with its error, or when it never answered the last task."""
    info = _call("GET", f"/session/{session}")
    out = {"session": session, "agent": NAME, "dir": info.get("directory"),
           "title": info.get("title"), "updated": (info.get("time") or {}).get("updated")}
    if _busy_one(session):
        return {**out, "state": "running"}
    msgs = _messages(session)
    last = msgs[-1]["info"] if msgs else {}
    if last.get("role") != "assistant":
        return {**out, "state": "failed" if msgs else "done",
                "error": f"No answer came: {NAME} took the task, but the model never "
                         f"answered and {NAME} kept no error. Is the model one of the proxy's, and "
                         f"is the app's model proxy up? Its log: {LOG}." if msgs else None}
    err = last.get("error")
    if err:
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        text = ": ".join(str(x) for x in (err.get("name"), data.get("message")) if x)
        if data.get("statusCode"):
            text += f" (the model's server answered {data['statusCode']})"
        return {**out, "state": "failed", "error": (text or json.dumps(err))[:500]}
    return {**out, "state": "done" if (last.get("time") or {}).get("completed")
            else "running"}


def result(session: str) -> dict:
    """The status, the last assistant text, the tail of its tool outputs and the files
    the session changed (opencode's diff)."""
    out = status(session)
    text, tools = "", []
    for m in reversed(_messages(session)):
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
        diff = _call("GET", f"/session/{session}/diff") or []
        files = [{"path": d.get("path"), "status": d.get("status"),
                  "additions": d.get("additions"), "deletions": d.get("deletions")}
                 for d in diff if isinstance(d, dict)]
    except Problem as e:
        files_error = str(e)
    return {**out, "text": text, "tool_output": "\n".join(tools)[-TAIL:], "files": files,
            **({"files_error": files_error} if files_error else {})}


def abort(session: str) -> dict:
    ok = _call("POST", f"/session/{session}/abort")
    return {"session": session, "aborted": bool(ok)}


def list() -> list[dict]:          # noqa: A001 - the interface's name
    return [{"session": s["id"], "title": s.get("title"), "dir": s.get("directory"),
             "updated": (s.get("time") or {}).get("updated")}
            for s in _call("GET", "/session") or [] if isinstance(s, dict)]
