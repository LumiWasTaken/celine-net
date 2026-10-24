"""The code skill (skills/code.py) against a fake shellbox: what the app sends to
/code/*, how it words the box's facts, and the error sentences when the box refuses or is
away. The box's side of /code/* has its own suite (tests/test_shellboxd_code.py)."""
from __future__ import annotations

import json

import httpx
import pytest

from celine.boot import load
from celine.skills import ToolContext
from celine.skills.code import Code, tests_line

pytestmark = pytest.mark.anyio


class FakeBox:
    """Records what the app sends and answers what the test queued: a body, a (status,
    body) tuple or an exception."""

    def __init__(self):
        self.sent: list[tuple[str, str, dict]] = []
        self.answers: list = []
        self.headers: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.sent.append((request.method, request.url.path, body))
        self.headers.append(dict(request.headers))
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, tuple):
            return httpx.Response(answer[0], json=answer[1])
        return httpx.Response(200, json=answer)


@pytest.fixture
def box():
    return FakeBox()


@pytest.fixture
def code(box, monkeypatch, stack_key):
    monkeypatch.setenv("CELINE_SHELL_URL", "http://shellbox:8770")
    stack_key("shell", "k" * 24)
    skill = Code(load(), transport=httpx.MockTransport(box.handle))
    skill.cfg.user = "Joe"
    return skill


def ctx() -> ToolContext:
    async def emit(note: str, **data) -> None:
        pass
    return ToolContext(conversation_id="c", turn_id="t", tool_call_id="x", store=None,
                       config=None, emit=emit)


async def test_start_sends_dir_task_model_and_session_with_the_key(code, box):
    box.answers = [{"session": "ses_1", "state": "running", "dir": "/home/celine/p",
                    "model": "m/x"}]
    out = await code.start(ctx(), dir="p", task="fix it", model="m/x")
    assert box.sent == [("POST", "/code/session", {"dir": "p", "task": "fix it",
                                                   "model": "m/x", "session": None,
                                                   "websearch": False})]
    assert box.headers[0]["authorization"] == "Bearer " + "k" * 24
    assert out.content.startswith("Session ses_1 is running in /home/celine/p with m/x. Look "
                                  "with code__status(session_id=\"ses_1\") in about 20 s;")
    assert out.display == {"session": "ses_1", "state": "running", "dir": "/home/celine/p",
                           "model": "m/x", "task": "fix it"}
    box.answers = [{"session": "ses_1", "state": "running", "dir": "/home/celine/p"}]
    await code.start(ctx(), dir="p", task="more", model="m/x", session_id="ses_1")
    assert box.sent[-1][2]["session"] == "ses_1"


async def test_a_refusal_of_the_box_is_her_whole_result(code, box):
    box.answers = [(400, {"error": "No folder nope in the home folder."})]
    out = await code.start(ctx(), dir="nope", task="x", model="m")
    assert out.is_error and out.content == "No folder nope in the home folder."
    box.answers = [(409, {"error": "Session ses_1 is still working; wait, or abort it."})]
    out = await code.start(ctx(), dir="p", task="x", model="m", session_id="ses_1")
    assert out.content == "Session ses_1 is still working; wait, or abort it."
    box.answers = [(503, {"error": "The coding agent did not start: no such file"})]
    out = await code.start(ctx(), dir="p", task="x", model="m")
    assert out.content == "The coding agent did not start: no such file"
    box.answers = [(502, {"error": "opencode answered 500: UnknownError: boom. Its log: x."})]
    out = await code.status_(ctx(), session_id="ses_1")
    assert out.is_error and out.content == "opencode answered 500: UnknownError: boom. Its log: x."
    box.answers = [(404, {"error": "No such session."})]
    out = await code.status_(ctx(), session_id="ses_9")
    assert out.is_error and out.content == "No such session. code__list shows your sessions."
    assert out.display["unknown"]


async def test_status_words_the_three_states(code, box):
    box.answers = [{"session": "ses_1", "agent": "opencode", "state": "running"}]
    out = await code.status_(ctx(), session_id="ses_1")
    assert box.sent[-1][:2] == ("GET", "/code/session/ses_1")
    assert out.content == "Session ses_1 (opencode): running. Look again in about 20 s."
    box.answers = [{"session": "ses_1", "agent": "opencode", "state": "done"}]
    out = await code.status_(ctx(), session_id="ses_1")
    assert out.content == ("Session ses_1 (opencode): done. code__result(session_id=\"ses_1\") "
                           "brings its answer.")
    box.answers = [{"session": "ses_1", "agent": "opencode", "state": "failed",
                    "error": "Aborted"}]
    out = await code.status_(ctx(), session_id="ses_1")
    assert out.content == "Session ses_1 (opencode): failed. Aborted"
    assert out.display["state"] == "failed" and out.display["error"] == "Aborted"


async def test_result_cuts_the_text_and_names_files_and_tests(code, box):
    code.chars = 30
    box.answers = [{"session": "ses_1", "agent": "opencode", "state": "done",
                    "text": "I changed the parser and ran the suite, all green now.",
                    "tool_output": "....\n3 passed, 1 skipped in 0.42s\n",
                    "files": [{"path": "src/p.py", "status": "modified", "additions": 4,
                               "deletions": 1}, {"path": "t.py", "status": "added",
                                                 "additions": 9, "deletions": 0}]}]
    out = await code.result(ctx(), session_id="ses_1")
    assert box.sent[-1][:2] == ("GET", "/code/session/ses_1/result")
    assert out.content.split("\n") == [
        "Session ses_1 (opencode): done.",
        "I changed the parser and ran t",
        "[cut at 30 characters]",
        "Files changed: src/p.py (modified, +4 -1), t.py (added, +9 -0)",
        "Tests: 3 passed, 1 skipped in 0.42s"]
    assert out.display["cut"] and out.display["tests"] == "3 passed, 1 skipped in 0.42s"
    box.answers = [{"session": "ses_1", "agent": "opencode", "state": "running", "text": "",
                    "tool_output": "", "files": []}]
    out = await code.result(ctx(), session_id="ses_1")
    assert out.content == "Session ses_1 (opencode): running.\n(no answer text yet)"
    box.answers = [{"session": "ses_1", "agent": "opencode", "state": "done", "text": "ok",
                    "files": [], "files_error": "opencode answered 500: boom."}]
    out = await code.result(ctx(), session_id="ses_1")
    assert out.content.split("\n")[-1] == "Files changed: unknown, opencode answered 500: boom."


def test_the_tests_line_reads_pytest_npm_and_go_summaries():
    assert tests_line("x\n===== 3 passed, 2 failed in 1.2s =====\n") == "3 passed, 2 failed in 1.2s"
    assert tests_line("Tests:       12 passed, 12 total\nTime: 1s") == "12 passed, 12 total"
    assert tests_line("ok  \tgithub.com/x/y\t0.3s") == "ok  \tgithub.com/x/y\t0.3s"
    assert tests_line("1 failed in 0.1s", "all good:\n4 passed in 0.2s") == "4 passed in 0.2s"
    assert tests_line("nothing here") is None


async def test_abort_and_list(code, box):
    box.answers = [{"session": "ses_1", "aborted": True}]
    out = await code.abort(ctx(), session_id="ses_1")
    assert box.sent[-1] == ("POST", "/code/session/ses_1/abort", {})
    assert out.content == "Session ses_1 stopped."
    box.answers = [{"session": "ses_1", "aborted": False}]
    assert (await code.abort(ctx(), session_id="ses_1")).content == "Session ses_1 was not running."
    box.answers = [{"sessions": [
        {"session": "ses_1", "title": "fix it", "dir": "/home/celine/p", "updated": 5},
        {"session": "ses_2", "title": "docs", "dir": "/home/celine/q", "updated": 9}]}]
    out = await code.list(ctx())
    assert box.sent[-1][:2] == ("GET", "/code/sessions")
    assert out.content == "ses_2: /home/celine/q: docs\nses_1: /home/celine/p: fix it"
    box.answers = [{"sessions": []}]
    assert (await code.list(ctx())).content == "No coding sessions yet. code__start opens one."


async def test_a_box_that_drops_the_call_or_has_no_address_says_so(code, box, monkeypatch):
    box.answers = [httpx.ReadError("cut")]
    out = await code.list(ctx())
    assert out.is_error and out.content == (
        "The shellbox at shellbox:8770 dropped the connection or restarted (ReadError: cut). "
        "Look with code__list.")
    monkeypatch.delenv("CELINE_SHELL_URL", raising=False)
    skill = Code(load())
    assert skill.status == "down" and "has no address yet" in skill.error
    out = await skill.list(ctx())
    assert out.is_error and "has no address yet" in out.content


async def test_the_session_terminal_is_listed_under_the_conversation_and_websearch_is_sent(
        code, box, monkeypatch):
    from celine.terminals import store as terminals
    calls = []

    async def upsert(store, id, name, owner, opened_at, opened_by=None):
        calls.append((id, name, owner, opened_by))
    monkeypatch.setattr(terminals, "upsert", upsert)
    code.cfg.settings.general["code_websearch"] = True
    box.answers = [{"session": "ses_1", "dir": "/h/p", "terminal": "code-abc",
                    "terminal_id": "t1abc2"}]
    c = ctx()
    c.store = object()
    await code.start(c, dir="p", task="x", model="m/x")
    assert box.sent[0][2]["websearch"] is True
    assert calls == [("t1abc2", "code-abc", "celine", "c")]
