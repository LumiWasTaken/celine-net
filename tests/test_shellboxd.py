"""shellboxd in a real shellbox container, and the image's greeting and prompts. Needs
docker and builds the image locally (`-m shellbox`); every container is named
test-shellboxd-* and removed afterwards."""
from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from test_shellboxd_run import until

pytestmark = pytest.mark.shellbox

ROOT = Path(__file__).parent.parent
IMAGE = os.environ.get("SHELLBOX_TEST_IMAGE", "celine-shellbox-test")
KEY = "test-key-" + "k" * 16
KEY_HASH = hashlib.sha256(KEY.encode()).hexdigest()
names = (f"t{n}" for n in itertools.count(1))


def docker(*args: str, check: bool = True, timeout: float = 600) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"docker {' '.join(args)}: {r.stderr}")
    return r.stdout


@pytest.fixture(scope="session")
def image():
    if shutil.which("docker") is None:
        pytest.skip("no docker")
    docker("build", "-q", "-t", IMAGE, "--build-arg", "HER_UID=1000", "-f",
           "shellbox/Dockerfile", str(ROOT))
    return IMAGE


def hash_file(text: str) -> Path:
    """`text` in a file for the box's /etc/shellbox/key.hash, as compose mounts it."""
    p = Path(tempfile.gettempdir()) / f"test-shellboxd-{hashlib.sha256(text.encode()).hexdigest()[:8]}.hash"
    p.write_text(text + "\n")
    return p


class Box:
    """One shellbox container and a small client for its daemon."""

    def __init__(self, name: str, *run_args: str, command: list[str] | None = None,
                 wait: bool = True, key_hash: str = KEY_HASH):
        self.name = f"test-shellboxd-{name}"
        docker("rm", "-f", self.name, check=False)
        docker("run", "-d", "--name", self.name, "--init", "--hostname", "shellbox",
               "--cap-add", "SYS_PTRACE", "-v", f"{hash_file(key_hash)}:/etc/shellbox/key.hash:ro",
               "-p", "127.0.0.1::8770", *run_args, IMAGE,
               *(command or []))
        if wait:
            self.wait_ready()

    def wait_ready(self) -> None:
        for _ in range(100):
            try:
                self.port = int(docker("port", self.name, "8770").split("\n")[0]
                                .rsplit(":", 1)[1])
                self.status()
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError(docker("logs", self.name, check=False))

    def restart(self) -> None:
        docker("restart", "-t", "2", self.name)
        self.wait_ready()

    def remove(self) -> None:
        docker("rm", "-f", self.name, check=False)

    def stopped_with(self) -> str:
        """The logs of a container that was meant to refuse to start."""
        for _ in range(50):
            if docker("inspect", "-f", "{{.State.Running}}", self.name).strip() == "false":
                break
            time.sleep(0.1)
        assert docker("inspect", "-f", "{{.State.ExitCode}}", self.name).strip() != "0"
        r = subprocess.run(["docker", "logs", self.name], capture_output=True, text=True)
        return r.stdout + r.stderr

    def sh(self, command: str, user: str = "celine") -> str:
        return docker("exec", "-u", user, self.name, "bash", "-c", command)

    def request(self, method: str, path: str, body: dict | None = None,
                key: str = KEY) -> tuple[int, list]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, [json.loads(ln) for ln in r.read().splitlines() if ln.strip()]
        except urllib.error.HTTPError as e:
            return e.code, [json.loads(e.read())]

    def status(self) -> dict:
        code, out = self.request("GET", "/status")
        assert code == 200
        return out[0]

    def run(self, terminal: str, command: str, **kw) -> dict:
        code, out = self.request("POST", "/run", {"terminal": terminal, "command": command,
                                                  **kw})
        assert code == 200, out
        return out[-1]

    def type(self, terminal: str, text: str = "", **kw) -> dict:
        code, out = self.request("POST", "/type", {"terminal": terminal, "text": text, **kw})
        assert code == 200, out
        return out[-1]

    def read(self, terminal: str | None = None, **kw) -> tuple[int, dict]:
        body = {"terminal": terminal, **kw} if terminal else kw
        code, out = self.request("POST", "/read", body)
        return code, out[0]

    def control(self, terminal: str, action: str) -> tuple[int, dict]:
        code, out = self.request("POST", "/control", {"terminal": terminal, "action": action})
        return code, out[0]

    def keys(self, terminal: str, data: bytes) -> tuple[int, dict]:
        code, out = self.request("POST", "/keys", {"terminal": terminal,
                                                   "data": base64.b64encode(data).decode()})
        return code, out[0]

    def log(self, tid: str, since: int | str = 0, key: str = KEY) -> tuple[int, bytes, str | None]:
        """A terminal's raw log from `since` on: status, bytes, and X-Log-Size."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/log?id={tid}&from={since}",
                                     headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read(), r.headers["X-Log-Size"]
        except urllib.error.HTTPError as e:
            return e.code, e.read(), None


@pytest.fixture(scope="module")
def box(image):
    b = Box("main")
    b.type("keep")          # keeps the tmux server up while tests close their terminals
    yield b
    b.remove()


@pytest.fixture
def term(box):
    name = next(names)
    yield name
    box.sh(f"tmux -L shellbox kill-session -t ={name}: 2>/dev/null || true")


# ---- the run protocol -------------------------------------------------------------------

def test_runs_end_with_their_exit_code_and_run_as_her(box, term):
    first = box.run(term, "id -u")
    assert first["opened"] and (first["state"], first["output"]) == ("done", "1000")
    for command, code in [("true", 0), ("false", 1), ("sh -c 'exit 7'", 7),
                          ("no-such-command", 127), ("false | cat", 0),
                          ("seq 1 100000 | head -2", 0)]:
        began = time.monotonic()
        r = box.run(term, command)
        assert (r["state"], r["exit"]) == ("done", code), command
        assert time.monotonic() - began < 2, command
    r = box.run(term, "sleep 100", timeout=1)
    assert r["state"] == "running" and r["program"] == "sleep"
    code, stopped = box.request("POST", "/stop", {"terminal": term})
    assert (stopped[0]["state"], stopped[0]["exit"]) == ("done", 130)
    r = box.run(term, 'read -p "name? " x; echo "hi $x"')
    assert r["state"] == "waiting"
    assert "hi bob" in box.type(term, "bob")["screen"]
    r = box.type(term, "python3 -q")
    assert (r["state"], r["program"]) == ("waiting", "python3"), "the REPL waits for her"
    assert "144" in box.type(term, "12 * 12")["screen"]
    box.type(term, "exit()")
    r = box.type(term, "vim /tmp/vim-test.txt")
    assert r["state"] == "fullscreen" and r["program"] == "vim"
    box.type(term, keys=["i"], text="hello there", enter=False)
    assert box.type(term, keys=["Escape"], text=":wq")["state"] == "prompt"
    assert box.sh("cat /tmp/vim-test.txt") == "hello there\n"
    assert box.sh(f"ls /var/lib/shellbox/cmd/{first['cmd']}.exit").strip()


def test_a_job_working_out_of_its_group_is_running(box, term):
    """apt runs dpkg in a session of its own and `| tail` prints nothing until the end:
    the whole job is counted, and it never reads the terminal."""
    spin = "import time; t = time.time() + 5\nwhile time.time() < t: pass\nprint('spun')"
    r = box.run(term, f"setsid -w python3 -c \"{spin}\" | tail -1", timeout=60)
    assert (r["state"], r["exit"], r["output"]) == ("done", 0, "spun")


def test_two_typists_do_not_mix_their_lines(box, term):
    box.type(term)
    lines = {"A": "A" * 3000, "B": "B" * 3000}
    threads = [threading.Thread(target=box.type, args=(term, f"echo {v} >> /tmp/mix.txt"))
               for v in lines.values()]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    until(lambda: sorted(box.sh("cat /tmp/mix.txt").split()) == sorted(lines.values()))


def test_hanging_up_leaves_the_command_running(box, term):
    box.type(term)
    s = socket.create_connection(("127.0.0.1", box.port))
    body = json.dumps({"terminal": term, "command": "sleep 2; touch /tmp/hangup-done"}).encode()
    s.sendall(b"POST /run HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + KEY.encode()
              + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    until(lambda: "sleep 2" in box.sh("ps -u celine -o args="))
    s.close()
    until(lambda: box.sh("ls /tmp/hangup-done 2>/dev/null || true").strip())


# ---- the key ------------------------------------------------------------------------------

def test_the_box_never_holds_the_key_and_refuses_a_wrong_one(box):
    environ = box.sh("sudo cat /proc/1/environ /proc/[0-9]*/environ 2>/dev/null | tr '\\0' '\\n'")
    assert KEY not in environ and KEY_HASH not in environ
    assert box.sh("cat /etc/shellbox/key.hash").strip() == KEY_HASH
    with urllib.request.urlopen(f"http://127.0.0.1:{box.port}/health", timeout=10) as r:
        assert json.loads(r.read()) == {"ok": True}
    for path in ("/status", "/log?id=zzzzzz&from=0", "/view?terminal=keep"):
        assert box.request("GET", path, key="wrong")[0] == 401
    assert box.request("GET", "/status", key="")[0] == 401
    assert box.request("POST", "/read", {}, key="wrong")[0] == 401
    assert box.request("POST", "/type", {"terminal": "keep", "text": "id"}, key="wrong")[0] == 401


def test_a_box_given_the_key_instead_of_its_hash_stops(image):
    b = Box("nohash", key_hash=KEY, wait=False)
    try:
        assert "key.hash is not the key's sha256" in b.stopped_with()
    finally:
        b.remove()


# ---- its logs -----------------------------------------------------------------------------

def test_a_terminals_log_is_roots_and_outlives_it(box, term):
    tid = box.run(term, "echo logged-$((6 * 7))")["id"]
    path = f"/var/lib/shellbox/logs/{tid}.raw"
    assert box.sh(f"stat -c '%U %G %a' {path}", user="root") == "root celine 620\n"
    assert box.sh(f"cat {path} >/dev/null 2>&1 || echo refused") == "refused\n"
    assert box.sh(f"rm -f {path} 2>/dev/null; test -e {path} && echo kept") == "kept\n"
    box.sh(f"tmux -L shellbox pipe-pane -t ={term}:")           # the pipe dies
    assert "pipe_rearmed" in box.run(term, "echo still-recorded")["notes"]
    box.run(term, "exit")
    code, data, size = box.log(tid)
    assert code == 200 and b"logged-42" in data and b"still-recorded" in data
    assert int(size) == len(data)
    code, tail, _ = box.log(tid, len(data) - 5)
    assert code == 200 and tail == data[-5:]
    assert box.log("../../etc/passwd")[0] == 404 and box.log(tid, "x")[0] == 400
    assert box.log(tid, key="wrong")[0] == 401


def test_logs_last_until_the_reset(image):
    b = Box("logs")
    try:
        first = b.run("main", "echo before-the-restart")["id"]
        b.restart()
        assert b.run("main", "true")["id"] != first
        code, data, _ = b.log(first)
        assert code == 200 and b"before-the-restart" in data
    finally:
        b.remove()


# ---- the user's controls ------------------------------------------------------------------

def test_stop_pause_resume_and_close(box, term):
    box.type(term)
    result: dict = {}
    th = threading.Thread(target=lambda: result.update(box.run(term, "sleep 100")))
    th.start()
    until(lambda: box.read(term)[1].get("program") == "sleep")
    code, r = box.control(term, "stop")
    assert code == 200 and r["program"] == "sleep"
    th.join(5)
    assert (result["state"], result["exit"]) == ("done", 130), "its run sees the stop"
    box.run(term, "yes > /dev/null", timeout=1)
    code, r = box.control(term, "pause")
    pid = r["pid"]
    ticks = f"cut -d' ' -f14,15 /proc/{pid}/stat"
    before = box.sh(ticks)
    time.sleep(1.5)
    assert box.sh(ticks) == before, "a paused program gets no CPU"
    assert box.run(term, "echo no")["reason"] == "paused"
    assert box.control(term, "resume")[0] == 200
    until(lambda: box.sh(ticks) != before)
    box.control(term, "pause")
    code, r = box.control(term, "close")
    assert code == 200 and r["program"] == "yes"
    until(lambda: not [s for s in box.sh("ps -u celine -o stat=").split()
                       if s.startswith("T")])
    assert box.read(term)[0] == 404


def test_the_users_keys_go_only_into_the_users_terminal(box):
    mine = next(names)
    box.type(mine, "PS1='sam@shellbox:\\w\\$ '", owner="sam")
    code, r = box.keys(mine, b"echo hi\r")
    assert code == 200 and r == {"ok": True, "line": "echo hi"}
    assert box.read(mine)[1]["screen"].split("\n")[-2] == "hi"
    code, r = box.keys("keep", b"echo no\r")
    assert code == 400, "keep is Celine's terminal"
    assert box.keys(mine, b"x" * 4097)[0] == 400
    assert box.request("POST", "/keys", {"terminal": mine, "data": "not base64!"})[0] == 400
    assert box.control(mine, "close")[0] == 200


# ---- the user's live view -----------------------------------------------------------------

def test_the_view_asks_the_page_nothing(box, term):
    """tmux asks its client what it is when it attaches; the page's xterm would answer,
    and the answers would land in the shell as typed text."""
    box.type(term, "echo quiet view")
    s = socket.create_connection(("127.0.0.1", box.port))
    s.sendall(f"GET /view?terminal={term} HTTP/1.1\r\nHost: x\r\n"
              f"Authorization: Bearer {KEY}\r\n\r\n".encode())
    s.settimeout(1.5)
    data = b""
    try:
        while chunk := s.recv(65536):
            data += chunk
    except TimeoutError:
        pass
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 200") and b"quiet view" in body
    for q in (b"\x1b[c", b"\x1b[>c", b"\x1b[>q", b"\x1b]10;?", b"\x1b]11;?"):
        assert q not in body


# ---- how the box starts -------------------------------------------------------------------

def booted(b: Box) -> dict:
    """The status once the package lists and ~/boot.sh are through (they run in turn)."""
    for _ in range(1800):
        s = b.status()
        if s["boot"]["state"] != "running":
            return s
        time.sleep(0.1)
    raise AssertionError(s)


def test_boot_sh_runs_as_her_after_a_restart(image):
    b = Box("boot")
    try:
        assert booted(b)["fresh"] is True
        b.sh("printf 'id -u\\nexit 4\\n' > ~/boot.sh")
        b.restart()
        s = booted(b)
        assert s["fresh"] is False and (s["boot"]["state"], s["boot"]["exit"]) == ("done", 4)
        assert "1000\n" in b.sh("cat ~/boot.log")
    finally:
        b.remove()


def test_an_empty_home_is_seeded_once(image):
    volume = "test-shellboxd-home"
    docker("volume", "rm", "-f", volume, check=False)
    docker("volume", "create", volume)                 # root's: the box gives it to it
    b = Box("seed", "--mount", f"type=volume,src={volume},dst=/home/celine,volume-nocopy=true")
    try:
        assert b.sh("stat -c %U ~/.bashrc").strip() == "celine"
        b.sh("rm ~/.bashrc")
        b.restart()
        assert ".bashrc" not in b.sh("ls -A ~"), "its home is never seeded over"
    finally:
        b.remove()
        docker("volume", "rm", "-f", volume, check=False)


def test_a_home_not_hers_is_given_to_her_and_said(image):
    b = Box("owner", "--tmpfs", "/home/celine:uid=0,gid=0")
    try:
        assert b.sh("stat -c '%U:%G' ~ && stat -c %U ~/.bashrc") == "celine:celine\nceline\n"
        assert "shellboxd: /home/celine was 0:0's, now celine's (1000:1000)\n" in \
            docker("logs", b.name)
    finally:
        b.remove()


# ---- the image: the greeting and the prompts --------------------------------------------

HEART = "  .:::.   .:::."


@pytest.fixture(scope="module")
def greeter(image):
    b = Box("greet", "--cpus", "1.5")
    yield b
    b.remove()


def wait_for(box: Box, name: str, text: str) -> str:
    for _ in range(100):
        shot = box.sh(f"tmux -L t capture-pane -p -t ={name}:")
        if text in shot:
            return shot
        time.sleep(0.1)
    raise AssertionError(f"{text!r} never appeared:\n{shot}")


def test_the_greeting_and_the_prompts_of_her_and_the_user(greeter):
    """A login shell in a tmux session named like the terminal, opened as the daemon does."""
    def open_(owner: str, name: str) -> str:
        greeter.sh(f"tmux -L t new-session -d -s {name} -x 120 -y 40 "
                   f"-e SHELLBOX_TERMINAL={name} -e SHELLBOX_OWNER={owner} "
                   f"-e SHELLBOX_ID=abc123 'bash -l'")
        return wait_for(greeter, name, f"{owner}@shellbox:~$")
    try:
        shot = open_("celine", "build")
        assert f"{HEART}     celine@shellbox, terminal build" in shot
        assert ", 1.5 CPUs" in shot                     # the box's limits, not the host's
        assert "rules: /etc/shellbox/README.md" in shot
        assert f"{HEART}     sam in celine's shellbox, terminal sam" in open_("sam", "sam")
        greeter.sh("tmux -L t send-keys -t =sam: 'cd /tmp' Enter")
        wait_for(greeter, "sam", "sam@shellbox:/tmp$")
        # a login shell inside greets no second time
        greeter.sh("tmux -L t send-keys -t =build: 'bash -l' Enter")
        greeter.sh("tmux -L t send-keys -t =build: 'echo in$SHELLBOX_GREETED' Enter")
        assert wait_for(greeter, "build", "\nin1\n").count(HEART) == 1
    finally:
        greeter.sh("tmux -L t kill-server 2>/dev/null || true")
