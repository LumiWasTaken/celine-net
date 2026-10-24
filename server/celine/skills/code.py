"""The agent's coding agent: a coding agent in its shellbox that works on a folder of its own in the
background, through shellboxd's /code/* (shellbox/shellboxd.py, one backend module in
shellbox/agents/, the box picks it with CODING_AGENT).

Five tools. code__start hands a task and a model to a new session, or to one it keeps
(the session is the agent's memory of the folder and the task); code__status says
running, done or failed; code__result brings its last answer (cut to result_chars), the
files it changed and a tests line when its output shows a pytest or npm summary;
code__abort stops one; code__list lists its sessions. This skill knows nothing of the
backend but the interface and its name on the status line. Every call goes to the
shellbox at its address with the app's key (settings/services.py, keys.py), and no answer
is worded as the shell skill words it (errors.py).
"""
from __future__ import annotations

import re
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx

from . import ToolContext, ToolResult, guide, tool, described
from .. import shellbox
from ..errors import ToolRefused, not_set_up, unreachable_words

POLL_SECONDS = 20          # config/skills.toml [code] poll_seconds: between two code__status
RESULT_CHARS = 2000        # [code] result_chars: of the agent's last answer
# a test summary in the agent's output: pytest's "3 passed, 1 failed in 0.4s", npm's
# (jest, vitest) "Tests: 3 passed, 1 failed, 4 total", go's "ok" and "FAIL" lines
TESTS = re.compile(r"(?m)^(?:=+ )?((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed)"
                   r"(?:, )?)+(?: in [\d.]+s)?)|^Tests:\s+(.+?)\s*$|^(ok  \t\S+.*|FAIL\t\S+.*)$")


def tests_line(*texts: str) -> str | None:
    """The last test summary in the texts, or None."""
    found = None
    for text in texts:
        for m in TESTS.finditer(text or ""):
            found = m[1] or m[2] or m[3]
    return found.strip() if found else None


class Code:
    name = "code"
    app = None
    guide = guide("code", poll=POLL_SECONDS, chars=RESULT_CHARS)
    description = described()

    def __init__(self, cfg, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.transport = transport                  # tests: a fake shellbox
        knobs = cfg.skill("code")
        self.poll = int(knobs.get("poll_seconds", POLL_SECONDS))
        self.chars = int(knobs.get("result_chars", RESULT_CHARS))
        self.guide = guide("code", poll=self.poll, chars=self.chars)   # quotes the knobs

    @property
    def status(self) -> str:
        return "up" if shellbox.url(self.cfg) else "down"

    @property
    def error(self) -> str | None:
        return None if shellbox.url(self.cfg) else not_set_up("the shellbox")

    # ---- the tools ------------------------------------------------------------------------

    @tool(read_only=False, destructive=True, open_world=True)
    async def start(
        self,
        ctx: ToolContext,
        dir: Annotated[str, "The folder in your home the agent works in, such as "
                            "\"projects/site\" or \"~/tools\". It must exist."],
        task: Annotated[str, "What to do there, as you would brief a colleague: the goal, "
                             "what done looks like, what to run to check."],
        model: Annotated[str, "The model for this task: a ref from models__list, such as "
                              "\"openrouter/provider/model\"."],
        session_id: Annotated[str | None, "A session from an earlier code__start to carry "
                                          "on in: it remembers that folder and what it did "
                                          "there. Leave it out for a new one."] = None,
    ) -> ToolResult:
        """Hand a coding task to your coding agent: it reads, edits and runs code in that
        folder on its own, in the background, with the model you name, and answers when
        it is done. Reuse the session id for follow-up work in the same folder: it keeps
        what it learned. Then poll with code__status at the pace its answer names, and read
        the answer with code__result once it says done.
        Returns: the session id and "running"."""
        try:
            status, res = await self._call("POST", "/code/session",
                                           {"dir": dir, "task": task, "model": model,
                                            "session": session_id})
        except ToolRefused as e:
            return self._refused(str(e), session_id)
        except httpx.HTTPError as e:
            return self._unreachable(e)
        if status == 404:
            return self._unknown(session_id, res)
        sid = str(res.get("session") or "")
        content = (f"Session {sid} is running in {res.get('dir') or dir} with {model}. Look "
                   f"with code__status(session_id=\"{sid}\") in about {self.poll} s; "
                   f"code__result brings its answer once it says done.")
        return ToolResult(content=content, display={"session": sid, "state": "running",
                                                    "dir": res.get("dir") or dir,
                                                    "model": model, "task": task})

    @tool(read_only=True, open_world=True)
    async def status_(
        self,
        ctx: ToolContext,
        session_id: Annotated[str, "The session, from code__start."],
    ) -> ToolResult:
        """Where a session stands: running, done or failed (with why). Cheap: poll this,
        not code__result, while it runs.
        Returns: one line with the state."""
        try:
            status, res = await self._call("GET", f"/code/session/{session_id}")
        except ToolRefused as e:
            return self._refused(str(e), session_id)
        except httpx.HTTPError as e:
            return self._unreachable(e)
        if status == 404:
            return self._unknown(session_id, res)
        return ToolResult(content=self._state_line(res) + (
            f" code__result(session_id=\"{session_id}\") brings its answer."
            if res.get("state") == "done" else
            f" Look again in about {self.poll} s." if res.get("state") == "running" else ""),
            display={"session": session_id, "state": res.get("state"), "agent": res.get("agent"),
                     "error": res.get("error")})

    @tool(read_only=True, open_world=True)
    async def result(
        self,
        ctx: ToolContext,
        session_id: Annotated[str, "The session, from code__start."],
    ) -> ToolResult:
        """The session's last answer (cut to result_chars, see the guide), the files it changed and
        the test summary its output showed, when it did. While it still runs, you get what
        it said so far.
        Returns: the state line, the answer, "Files changed: ..." and "Tests: ..."."""
        try:
            status, res = await self._call("GET", f"/code/session/{session_id}/result")
        except ToolRefused as e:
            return self._refused(str(e), session_id)
        except httpx.HTTPError as e:
            return self._unreachable(e)
        if status == 404:
            return self._unknown(session_id, res)
        text = str(res.get("text") or "")
        cut = len(text) > self.chars
        shown = text[:self.chars].rstrip() + (f"\n[cut at {self.chars} characters]" if cut else "")
        lines = [self._state_line(res), shown or "(no answer text yet)"]
        files = [f for f in res.get("files") or [] if isinstance(f, dict) and f.get("path")]
        if res.get("files_error"):
            lines.append(f"Files changed: unknown, {res['files_error']}")
        elif files:
            lines.append("Files changed: " + ", ".join(
                f"{f['path']} ({f.get('status') or 'modified'}, +{f.get('additions') or 0} "
                f"-{f.get('deletions') or 0})" for f in files))
        tests = tests_line(str(res.get("tool_output") or ""), text)
        if tests:
            lines.append(f"Tests: {tests}")
        return ToolResult(content="\n".join(lines),
                          display={"session": session_id, "state": res.get("state"),
                                   "agent": res.get("agent"), "files": files, "tests": tests,
                                   "cut": cut, "error": res.get("error")})

    @tool(read_only=False, open_world=True)
    async def abort(
        self,
        ctx: ToolContext,
        session_id: Annotated[str, "The session, from code__start."],
    ) -> ToolResult:
        """Stop a running session. What it changed so far stays in the folder; the session
        keeps its memory and can be started again.
        Returns: whether it was stopped."""
        try:
            status, res = await self._call("POST", f"/code/session/{session_id}/abort", {})
        except ToolRefused as e:
            return self._refused(str(e), session_id)
        except httpx.HTTPError as e:
            return self._unreachable(e)
        if status == 404:
            return self._unknown(session_id, res)
        content = (f"Session {session_id} stopped." if res.get("aborted")
                   else f"Session {session_id} was not running.")
        return ToolResult(content=content, display={"session": session_id,
                                                    "aborted": bool(res.get("aborted"))})

    @tool(read_only=True, open_world=True)
    async def list(self, ctx: ToolContext) -> ToolResult:
        """Your coding sessions: id, folder and title, newest first, to find one to carry
        on in.
        Returns: one line per session."""
        try:
            status, res = await self._call("GET", "/code/sessions")
        except ToolRefused as e:
            return self._refused(str(e), None)
        except httpx.HTTPError as e:
            return self._unreachable(e)
        rows = sorted((s for s in res.get("sessions") or [] if isinstance(s, dict)),
                      key=lambda s: s.get("updated") or 0, reverse=True)
        if not rows:
            return ToolResult(content="No coding sessions yet. code__start opens one.",
                              display={"sessions": []})
        return ToolResult(content="\n".join(
            f"{s.get('session')}: {s.get('dir') or '?'}: {s.get('title') or ''}".rstrip(": ")
            for s in rows), display={"sessions": rows})

    # ---- the shellbox over HTTP ------------------------------------------------------------

    async def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        return await shellbox.call(self.cfg, method, path, body, transport=self.transport,
                                   user=self.cfg.user)

    @staticmethod
    def _state_line(res: dict) -> str:
        state, agent = res.get("state") or "unknown", res.get("agent") or "the coding agent"
        line = f"Session {res.get('session')} ({agent}): {state}."
        if res.get("error"):
            line += f" {res['error']}"
        return line

    @staticmethod
    def _refused(text: str, session: str | None) -> ToolResult:
        return ToolResult(content=text, is_error=True,
                          display={"session": session, "refused": True})

    @staticmethod
    def _unknown(session: str | None, res: dict) -> ToolResult:
        said = str(res.get("error") or "No such session.")
        return ToolResult(content=f"{said} code__list shows your sessions.", is_error=True,
                          display={"session": session, "unknown": True})

    def _unreachable(self, e: httpx.HTTPError) -> ToolResult:
        if isinstance(e, (httpx.ConnectError, httpx.TimeoutException,
                          httpx.UnsupportedProtocol)):
            content = unreachable_words("the shellbox", shellbox.url(self.cfg), e)
        else:
            content = (f"The shellbox at {urlsplit(shellbox.url(self.cfg)).netloc} dropped the "
                       f"connection or restarted ({type(e).__name__}: {e}). Look with "
                       f"code__list.")
        return ToolResult(content=content, is_error=True, display={"unreachable": True})
