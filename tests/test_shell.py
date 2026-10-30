"""The shell skill (skills/shell.py) against a fake shellbox: what the app sends, what it
guards and how it words the facts; and the daemon's view filter. The daemon's run
protocol has its own suite on a real tmux (tests/test_shellboxd_run.py)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from celine.boot import load
from celine.skills import ToolContext
from celine.skills import shell as shell_mod
from celine.skills.shell import KEY_ALIASES, Shell

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).parent.parent
BOOT = {"boot_id": "2026-10-02T04:00:31+a1b2", "fresh": True,
        "boot": {"state": "done", "exit": 0, "seconds": 41}}


def ran(**kw) -> dict:
    """A run's answer, as the daemon shapes it."""
    base = {"terminal": "main", "id": "m1m1m1", "owner": "celine", "opened": False,
            "notes": [], **BOOT, "shell": "bash", "cwd": "/home/celine", "front": [],
            "program": None, "pid": None, "busy": False, "paused": False, "waiting": False,
            "running_for": None, "quiet_for": 0, "fullscreen": False, "cursor": [3, 1],
            "dead": False, "state": "done", "cmd": "k3x9ab", "command": "ls",
            "log": "/var/lib/shellbox/cmd/k3x9ab.log", "exit": 0, "seconds": 0.4,
            "output": "notes", "tail": None, "cut": None, "size": 6, "outside": False,
            "outside_from": None}
    return {**base, **kw}


def screen(**kw) -> dict:
    """A screen answer (/type, /read without from)."""
    base = {"terminal": "main", "id": "m1m1m1", "owner": "celine", "opened": False,
            "notes": [], **BOOT, "shell": "bash", "cwd": "/home/celine", "front": [],
            "program": None, "pid": None, "busy": False, "paused": False, "waiting": False,
            "running_for": None, "quiet_for": 0, "fullscreen": False, "cursor": [3, 1],
            "dead": False, "state": "prompt", "screen": "celine@shellbox:~$", "highlighted": None,
            "cmd": None}
    return {**base, **kw}


class FakeBox:
    """Stands in for shellboxd: records what the app sends and answers what the test queued:
    an answer, NDJSON lines for /run, a (status, body) tuple or an exception."""

    def __init__(self):
        self.sent: list[tuple[str, dict, dict]] = []
        self.answers: list = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.sent.append((path, json.loads(request.content or b"{}"), dict(request.headers)))
        answer = self.answers.pop(0) if self.answers else (ran() if path in ("/run", "/stop")
                                                           else screen())
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, tuple):
            return httpx.Response(answer[0], json=answer[1])
        if path == "/run":
            lines = answer if isinstance(answer, list) else [answer]
            return httpx.Response(200, content="".join(json.dumps(x) + "\n" for x in lines))
        return httpx.Response(200, json=answer)

    @property
    def bodies(self) -> list[dict]:
        return [b for _, b, _ in self.sent]


@pytest.fixture
def box():
    return FakeBox()


@pytest.fixture
def shell(box, monkeypatch, stack_key):
    monkeypatch.setenv("CELINE_SHELL_URL", "http://shellbox:8770")
    stack_key("shell", "k" * 24)
    sh = Shell(load(), transport=httpx.MockTransport(box.handle))
    sh.cfg.user = "Joe"
    sh.boots["c"] = BOOT["boot_id"]          # conversation c saw this boot already
    return sh


def ctx(store=None, time_left=None):
    async def emit(note: str, **data) -> None:
        pass
    return ToolContext(conversation_id="c", turn_id="t", tool_call_id="x", store=store,
                       config=None, emit=emit, time_left=time_left)


async def test_a_run_is_sent_once_and_its_result_names_exit_duration_and_log(shell, box):
    out = await shell.run(ctx(), terminal="main", command="ls")
    assert box.sent[0][0] == "/run" and box.sent[0][2]["authorization"] == "Bearer " + "k" * 24
    assert box.bodies[0] == {"terminal": "main", "command": "ls", "timeout": 3000.0,
                             "chars": 8000}
    first, *rest = out.content.split("\n")
    assert first == "terminal main ~: exit 0 after 0.4 s. Log: /var/lib/shellbox/cmd/k3x9ab.log"
    assert rest == ["notes"]
    assert out.display["cmd"] == "k3x9ab" and out.display["exit"] == 0
    assert out.display["text"] == "ls" and out.display["head"] == 1
    await shell.run(ctx(), terminal="main", command="make", timeout=5000)
    assert box.bodies[-1]["timeout"] == shell_mod.RUN_MAX
    await shell.run(ctx(time_left=40.5), terminal="main", command="make")
    assert box.bodies[-1]["timeout"] == 37, "it answers before its turn's time is up"
    await shell.run(ctx(time_left=40.5), terminal="main", command="make", background=True)
    assert box.bodies[-1]["timeout"] == shell_mod.RUN_MAX, "a job waits for the end"


async def test_a_run_the_box_flags_as_outside_is_labelled_with_its_source(shell, box):
    box.answers = [ran(outside=True, outside_from="https://www.evil.example/x.sh")]
    out = await shell.run(ctx(), terminal="main", command="cat x.sh")
    assert out.display["outside"] == {"source": "evil.example/x.sh", "by": None}
    assert "outside" not in (await shell.run(ctx(), terminal="main", command="ls")).display


async def test_the_box_verdict_that_a_run_reached_the_net_is_in_its_display(shell, box):
    box.answers = [ran(outside=True, outside_from="curl", net="curl"), ran()]
    assert (await shell.run(ctx(), terminal="main", command="./sync")).display["net"] == "curl"
    assert "net" not in (await shell.run(ctx(), terminal="main", command="ls")).display


async def test_a_timeout_says_the_command_keeps_running_and_where_to_read_on(shell, box):
    box.answers = [ran(state="running", exit=None, seconds=30.2, program="make", pid=812,
                       front=[{"pid": 812, "name": "make", "args": ["make"]}], size=48211,
                       output="cc -o x x.c")]
    out = await shell.run(ctx(), terminal="main", command="make", timeout=30)
    lines = out.content.split("\n")
    assert lines[0].startswith("terminal main ~: still running after 30 s: make (pid 812).")
    assert lines[1] == ('It keeps running: shell__read(terminal="main", from=48211) shows what '
                        'it prints from here, shell__stop(terminal="main") stops it.')


async def test_a_busy_terminal_refuses_with_what_runs_or_waits_and_how_out(shell, box):
    box.answers = [ran(state="busy", reason="running", cmd="k3x9ab", command="make -j4",
                       running_for=182, program="make"),
                   ran(state="busy", reason="running", cmd="k3x9ab", command="read x",
                       running_for=40, waiting=True),
                   ran(state="busy", reason="running", cmd="k3x9ab", command="node build.mjs",
                       running_for=40, waiting=True, program="MainThread", pid=77,
                       front=[{"pid": 77, "name": "MainThread", "args": ["node", "build.mjs"]}])]
    out = await shell.run(ctx(), terminal="main", command="ls")
    assert out.is_error and out.content.startswith(
        'Not run: main still runs command k3x9ab (make -j4) for 3 min 2 s; '
        'shell__stop(terminal="main") stops it, or run in another terminal.')
    out = await shell.run(ctx(), terminal="main", command="ls")
    assert out.is_error and out.content == (
        'Not run: command k3x9ab (read x) waits for input in main, and the line would go to '
        'it. shell__type answers it, shell__stop(terminal="main") stops it, or run in '
        'another terminal.')
    out = await shell.run(ctx(), terminal="main", command="ls")
    assert out.content.startswith("Not run: node (pid 77) waits for input in main, from "
                                  "command k3x9ab (node build.mjs), and the line")


async def test_a_stop_names_what_outlived_ctrl_c_and_the_next_one_ends_it(shell, box):
    py = {"pid": 9, "name": "python3", "args": ["python3"]}
    box.answers = [ran(state="waiting", exit=None, program="python3", pid=9, front=[py],
                       survived=["python3"], killed=None, output=">>> KeyboardInterrupt"),
                   ran(state="done", exit=143, survived=[],
                       killed={"signal": "SIGTERM", "names": ["python3"]}, output="")]
    lines = (await shell.stop(ctx(), terminal="main")).content.split("\n")
    assert lines[1] == ('Ctrl-C did not stop python3: it reads Ctrl-C as a key. '
                        'shell__stop(terminal="main") again ends it (SIGTERM, then SIGKILL).')
    assert 'text="exit()" leaves' in lines[2]
    lines = (await shell.stop(ctx(), terminal="main")).content.split("\n")
    assert lines[0].startswith("terminal main ~: exit 143 after")
    assert lines[1] == "Ctrl-C had not stopped python3, so this stop ended it with SIGTERM."


async def test_a_long_output_is_cut_once_with_lines_and_a_from(shell, box):
    box.answers = [ran(output="1\n2", tail="9999\n10000",
                       cut={"first": 3, "last": 9998, "total": 10000, "from": 4})]
    out = await shell.run(ctx(), terminal="main", command="seq 10000")
    assert out.content.split("\n")[1:] == [
        "1", "2",
        "[lines 3 to 9998 of 10000 are not shown. shell__read(terminal=\"main\", from=4) "
        "reads on from line 3; sed -n '3,82p' /var/lib/shellbox/cmd/k3x9ab.log works too.]",
        "9999", "10000"]


async def test_type_stop_and_read_send_what_they_say(shell, box):
    await shell.type(ctx(), terminal="main", text="python3")
    assert box.sent[-1][:2] == ("/type", {"terminal": "main", "text": "python3", "keys": [],
                                          "enter": True})
    # a text that reads like a key name is typed as written
    await shell.type(ctx(), terminal="main", text="Escape")
    assert box.sent[-1][:2] == ("/type", {"terminal": "main", "text": "Escape", "keys": [],
                                          "enter": True})
    # Enter follows only a text; keys alone never press it
    for args in ({"text": "q", "enter": "false"}, {"keys": "Up", "enter": True}, {}):
        await shell.type(ctx(), terminal="main", **args)
        assert box.bodies[-1]["enter"] is False, args
    await shell.type(ctx(), terminal="main", keys=" ".join(list(KEY_ALIASES) + ["ctrl-d", "^D", "Alt+x"]))
    assert box.bodies[-1]["keys"] == list(KEY_ALIASES.values()) + ["C-d", "C-d", "M-x"]
    out = await shell.stop(ctx(), terminal="build")
    assert box.sent[-1][:2] == ("/stop", {"terminal": "build", "chars": 8000})
    assert out.display["keys"] == "C-c"
    box.answers = [ran(output="b\nc", next=812, size=4000, **{"from": 400})]
    out = await shell.read(ctx(), terminal="main", from_=400)
    assert box.sent[-1][:2] == ("/read", {"terminal": "main", "from": 400, "chars": 8000})
    assert out.content.endswith('b\nc\nmore: shell__read(terminal="main", from=812)')
    out = await shell.read(ctx(), terminal="main")
    assert box.sent[-1][1] == {"terminal": "main", "chars": 8000}
    assert out.content == "terminal main ~: at the prompt\nceline@shellbox:~$"


async def test_a_screen_tells_once_that_her_last_run_ended(shell, box):
    ended = {"id": "k3x9ab", "command": "apt-get install x", "log": "/l", "exit": 0,
             "seconds": 12.0}
    box.answers = [screen(cmd=ended), screen(cmd=ended)]
    first = await shell.type(ctx(), terminal="main", text="y")
    assert "Command k3x9ab (apt-get install x) ended: exit 0 after 12 s. Log: /l" in \
        first.content
    assert "ended" not in (await shell.type(ctx(), terminal="main", text="y")).content


async def test_bad_input_sends_nothing(shell, box):
    for args in ({"text": "make", "keys": "Down Ctrl+Shift+x"},
                 {"text": "ls", "terminal": "Build"},
                 {"text": "ls", "enter": "maybe"}):
        assert (await shell.type(ctx(), **{"terminal": "main", **args})).is_error, args
    for args in ({"command": ""}, {"command": "ls", "timeout": "soon"},
                 {"command": "ls", "terminal": "A B"}):
        assert (await shell.run(ctx(), **{"terminal": "main", **args})).is_error, args
    assert (await shell.read(ctx(), terminal="main", from_=-1)).is_error
    assert box.sent == []


async def test_failures_are_error_results_and_never_take_the_skill_down(
        shell, box, monkeypatch):
    box.answers = [[{"progress": "x"}, {"state": "error", "error": "tmux: fork failed"}],
                   httpx.ConnectError("refused"), [{"progress": "no result follows"}],
                   (400, {"error": "command is the command line to run."})]
    for _ in range(4):
        assert (await shell.run(ctx(), terminal="main", command="ls")).is_error
    box.answers = [(401, {"error": "unauthorized"})]
    assert (await shell.run(ctx(), terminal="main", command="ls")).is_error
    box.answers = [(404, {"error": "No terminal named x.", "terminals": ["main"]})]
    out = await shell.stop(ctx(), terminal="x")
    assert out.content == "No terminal named x. Your terminals: main."
    assert shell.status == "up", "found out at the call, the tools stay"
    monkeypatch.delenv("CELINE_SHELL_URL")
    down = Shell(load())
    assert (down.status, down.error) == ("down", (
        "The shellbox is not set up."))


async def test_the_users_terminal_is_read_only_until_they_allow_it(store, shell, box):
    from celine.core.ids import now
    from celine.terminals import store as terminals
    await terminals.upsert(store, "u1u1u1", "sam", "sam", now())
    for out in (await shell.run(ctx(store=store), terminal="sam", command="ls"),
                await shell.type(ctx(store=store), terminal="sam", text="ls"),
                await shell.stop(ctx(store=store), terminal="sam")):
        assert out.is_error
    assert box.sent == []
    box.answers = [screen(terminal="sam", id="u1u1u1", owner="sam")]
    out = await shell.read(ctx(store=store), terminal="sam")
    assert not out.is_error and box.sent[-1][0] == "/read", "reading it is fine"
    await terminals.set_may_type(store, "u1u1u1", True)
    box.answers = [ran(terminal="sam", id="u1u1u1", owner="sam")]
    out = await shell.run(ctx(store=store), terminal="sam", command="ls")
    assert not out.is_error and box.bodies[-1]["terminal"] == "sam"


# ---- the daemon's view -----------------------------------------------------------------------

def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_app_and_the_box_keep_the_same_numbers_and_names(monkeypatch, tmp_path):
    """Each side of the box's edge runs on its own, so these are written on both: the
    same, or this fails."""
    from celine.terminals import api as terminals_api
    monkeypatch.setenv("HOME", str(tmp_path))
    d = _load("shellboxd_same", ROOT / "shellbox" / "shellboxd.py")
    assert (shell_mod.RUN_MAX, shell_mod.NAME.pattern) == (d.RUN_MAX, d.NAME.pattern)
    assert terminals_api.KEYS_MAX == d.KEYS_MAX
    assert _load("net_tag_same", ROOT / "shellbox" / "net-tag.py").ATTR == d.ORIGIN
    opencode = _load("opencode_same", ROOT / "shellbox" / "agents" / "opencode.py")
    assert f"127.0.0.1:{opencode.PORT}" in (ROOT / "shellbox" / "etc" / "README.md").read_text()
    from celine.sub_agents import cli
    assert tuple(_load("agent_same", ROOT / "shellbox" / "agent.py").CLIS) == cli.CLIS


def test_the_view_keeps_tmux_questions_from_the_page(monkeypatch, tmp_path):
    """What tmux asks its client at attach never reaches the page: xterm would answer,
    and in the user's own terminal the answers would be typed into the shell."""
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("shellboxd", ROOT / "shellbox" / "shellboxd.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    burst = b"\x1b[1;40r\x1b[c\x1b[>c\x1b[>q\x1b]10;?\x1b\\\x1b]11;?\x1b\\\x1b[30;18H"
    assert d.unasked(burst) == (b"\x1b[1;40r\x1b[30;18H", b"")
    # a question split over two chunks: its head waits for the rest
    out, carry = d.unasked(b"text\x1b]10;?")
    assert (out, carry) == (b"text", b"\x1b]10;?")
    assert d.unasked(b"\x1b\\more", carry) == (b"more", b"")
    out, carry = d.unasked(b"x\x1b]11;?\x1b")
    assert (out, carry) == (b"x", b"\x1b]11;?\x1b")
    assert d.unasked(b"\\y", carry) == (b"y", b"")
    assert d.unasked(b"\x1b[2J\x1b[H") == (b"\x1b[2J\x1b[H", b"")
    assert d.unasked(b"A", b"\x1b[") == (b"\x1b[A", b"")


async def test_at_the_cap_she_hears_which_terminal_made_room_or_why_none_did(shell, box):
    box.answers.append(ran(terminal="new", opened=True, notes=["made_room:build"]))
    out = await shell.run(ctx(), command="ls", terminal="new")
    assert "so build, the one of yours that waited at its prompt quiet the longest, was " \
           "closed to open this one." in out.content and not out.is_error
    said = ("There are already 8 terminals open and none of yours has waited at its prompt "
            "for 10 min: main runs make; idle is at its prompt, quiet 3 min.")
    box.answers.append({"terminal": "new", "state": "error", "full": True,
                        "terminals": ["main", "idle"],
                        "error": said})
    out = await shell.run(ctx(), command="ls", terminal="new")
    assert out.is_error and out.content == said
